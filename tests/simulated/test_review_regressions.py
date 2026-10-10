"""Regression tests for issues found by the independent code review."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import numpy as np
import pytest

from hantek_mcp.analysis.quality import record_quality
from hantek_mcp.errors import AcquisitionError, DeviceDisconnectedError, PathNotAllowedError
from hantek_mcp.instrument.controller import ScopeController
from hantek_mcp.models import Freshness
from hantek_mcp.protocol.commands import Command
from hantek_mcp.protocol.keys import Key
from hantek_mcp.protocol.session import ProtocolSession, SessionTimeouts
from hantek_mcp.service import ScopeService
from hantek_mcp.storage.captures import CaptureStore
from hantek_mcp.transport.simulator import SimulatedTransport
from tests.conftest import sim_config
from tests.unit.test_infrastructure import make_capture


def test_analysis_tools_are_not_advertised_read_only(service: ScopeService) -> None:
    from mcp import Client

    from hantek_mcp.mcp.server import build_server

    async def main() -> None:
        async with Client(build_server(service)) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            for name in ("scope_measure_frequency", "scope_fft", "scope_measure_phase", "scope_detect_edges"):
                assert tools[name].annotations is not None
                assert tools[name].annotations.read_only_hint is False, name
            assert tools["scope_get_samples"].annotations.read_only_hint is True  # type: ignore[union-attr]

    anyio.run(main)


def test_stale_stopped_record_is_reported_invalid(service: ScopeService) -> None:
    service.stop()
    service.capture(1, "stop_and_read")
    second = service.capture(1, "stop_and_read")["capture"]
    r = service.measure("voltage", second["capture_id"])
    assert not r["valid"] and any("stale" in w for w in r["warnings"])


def test_single_that_never_arms_is_not_returned_as_new(
    controller: ScopeController, sim: SimulatedTransport
) -> None:
    controller.stop()
    orig = sim.scope.press

    def ignore_single(code: int) -> int:
        return 0 if code == Key.SINGLE_SEQ else orig(code)

    sim.scope.press = ignore_single  # type: ignore[method-assign]
    before = sim.scope.acquisitions
    with pytest.raises(AcquisitionError):
        controller.capture(1, "single", timeout_s=1)
    assert sim.scope.acquisitions == before


def test_single_from_stop_with_fast_completion_is_confirmed_by_content(
    controller: ScopeController, sim: SimulatedTransport
) -> None:
    controller.stop()
    sim.scope.single_polls = 1  # simulate a trigger so fast that the armed state is never seen

    orig = sim.scope.press

    def fast_single(code: int) -> int:
        r = orig(code)
        if code == Key.SINGLE_SEQ:
            sim.scope.single_polls = 1
        return r

    sim.scope.press = fast_single  # type: ignore[method-assign]
    cap = controller.capture(1, "single")
    assert cap.acquisition.freshness == Freshness.NEW_RECORD_CONTENT_CHANGED


def test_forced_capture_reports_true_wait(controller: ScopeController, sim: SimulatedTransport) -> None:
    from hantek_mcp.transport.sim_signals import SignalSpec

    controller.set_trigger_mode("normal")
    sim.set_signal(1, SignalSpec("dc"))
    cap = controller.capture(1, "single", timeout_s=0.4, force_on_timeout=True)
    assert cap.acquisition.freshness == Freshness.FORCED_NEW
    assert cap.acquisition.wait_s is not None and cap.acquisition.wait_s >= 0.4


class LateReplyTransport(SimulatedTransport):
    """Withholds the first settings reply and delivers it just before the next reply."""

    def __init__(self) -> None:
        super().__init__(seed=1)
        self.held: list[bytes] = []
        self.hold_next_settings = True

    def _handle(self, marker: int, cmd: int, payload: bytes) -> list[bytes]:
        out = super()._handle(marker, cmd, payload)
        if cmd == Command.READ_SETTINGS and self.hold_next_settings:
            self.hold_next_settings = False
            self.held = out
            return []
        late, self.held = self.held, []
        return late + out


def test_late_reply_does_not_desynchronise_the_session() -> None:
    t = LateReplyTransport()
    t.open()
    s = ProtocolSession(t, SessionTimeouts(default_s=0.2, flush_s=0.01))
    from hantek_mcp.protocol.settings import decode_settings, default_layout

    lay = default_layout()
    t.scope.raw["VERT-CH1-VB"] = 3
    first = s.read_settings_blob()  # times out once, then resynchronises
    t.scope.raw["VERT-CH1-VB"] = 8
    second = s.read_settings_blob()
    assert decode_settings(first, lay)[0]["VERT-CH1-VB"] == 3
    assert decode_settings(second, lay)[0]["VERT-CH1-VB"] == 8  # not the stale late reply
    assert s.echo(b"after") == b"after"


def test_usb_open_failure_releases_interface(
    monkeypatch: pytest.MonkeyPatch, fake_usb: dict[str, Any]
) -> None:
    import usb.util

    from hantek_mcp.transport.usb import UsbSelector, UsbTransport

    calls: list[str] = []
    monkeypatch.setattr(usb.util, "claim_interface", lambda d, i: calls.append("claim"))
    monkeypatch.setattr(usb.util, "release_interface", lambda d, i: calls.append("release"))
    dev = fake_usb["devices"][0]
    with pytest.raises(Exception):  # noqa: B017 - TransportError for the missing endpoint
        UsbTransport(UsbSelector(endpoint_in=0x83)).open()
    assert calls == ["claim", "release"]
    assert dev.detached is False  # kernel driver re-attached


def test_nested_disconnect_is_handled_once(controller: ScopeController, sim: SimulatedTransport) -> None:
    kinds: list[str] = []
    controller.add_listener(lambda e: kinds.append(e["kind"]))
    sim.disconnect()
    with pytest.raises(DeviceDisconnectedError):
        controller.set_volts_per_div(1, 2.0)  # nested operations inside
    assert kinds.count("connection_lost") == 1


def test_decimation_keeps_the_end_of_the_record(service: ScopeService) -> None:
    cid = service.capture(1)["capture"]["capture_id"]
    cap = service.store.get(cid)
    cap.raw_samples[-1] = 127
    r = service.get_samples(cid, 999, "minmax")
    assert r["returned_points"] <= 999
    assert r["time_s"][-1] == pytest.approx(float(cap.time_s[-1]))
    assert max(r["raw_counts"]) == 127
    m = service.get_samples(cid, 999, "mean")
    assert m["returned_points"] <= 999 and m["time_s"][-1] > float(cap.time_s[-10])


def test_single_rail_sample_counts_as_clipped() -> None:
    raw = np.zeros(4000, dtype=np.int8)
    raw[::1000] = 127
    q = record_quality(raw)
    assert q["clipped"] and not q["valid"]


def test_repeated_import_never_overwrites(tmp_path: Path) -> None:
    st = CaptureStore(tmp_path)
    st.add(make_capture("x"))
    p = st.export_npz("x")
    ids = {st.import_export(p.name).capture_id for _ in range(2)}
    assert len(ids) == 2 and len(st) == 3


@pytest.mark.parametrize("bad", ["CON .csv", "con.", "COM0", "lpt0.csv", "nul .x", "name."])
def test_windows_device_names_rejected(tmp_path: Path, bad: str) -> None:
    st = CaptureStore(tmp_path)
    st.add(make_capture("c"))
    with pytest.raises(PathNotAllowedError):
        st.export_csv("c", bad)


def test_plan_with_bad_arguments_reports_step_error(service: ScopeService) -> None:
    r = service.run_plan(
        [
            {"action": "phase", "args": {"bogus": 1}},
            {"action": "wait", "args": {"seconds": "soon"}},
            {"action": "capture", "args": {"channel": 1}},
        ],
        stop_on_error=False,
    )
    assert [x["ok"] for x in r["results"]] == [False, False, True]
    assert r["results"][0]["error"]["code"] == "INVALID_PARAMETER"


def test_workflows_are_exclusive(tmp_path: Path) -> None:
    from hantek_mcp.errors import UnsupportedOperationError

    c = ScopeController(sim_config(tmp_path))
    c.begin_workflow("a")
    with pytest.raises(UnsupportedOperationError):
        c.begin_workflow("b")
    c.end_workflow()
    c.begin_workflow("b")
    c.end_workflow()
