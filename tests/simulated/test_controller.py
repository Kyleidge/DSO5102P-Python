"""Controller behaviour against the protocol-level simulator."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest

from hantek_mcp.errors import (
    AbortedError,
    DeviceDisconnectedError,
    NotConnectedError,
    ParameterError,
    SettingNotConfirmedError,
    TriggerTimeoutError,
    UnsupportedOperationError,
)
from hantek_mcp.instrument.controller import ScopeController
from hantek_mcp.models import Freshness
from hantek_mcp.protocol.keys import Key
from hantek_mcp.transport.sim_signals import SignalSpec
from hantek_mcp.transport.simulator import SimulatedTransport
from tests.conftest import sim_config


def test_connect_reads_identity_from_instrument(controller: ScopeController) -> None:
    ident = controller.identify()
    assert ident["connected"] and ident["simulated"] and ident["backend"] == "simulator"
    assert ident["model"] == "DSO5102P"
    assert ident["settings_layout_source"] == "instrument:/protocol.inf"
    assert ident["key_table_matches_dso5102p"] is True


def test_settings_changes_are_read_back(controller: ScopeController, sim: SimulatedTransport) -> None:
    r = controller.set_volts_per_div(1, 0.2)
    assert r.confirmed == 0.2 and r.verification == "readback_confirmed" and r.key_presses == 2
    assert controller.set_probe(1, 10).confirmed == 10
    assert controller.read_settings().channels[1].effective_volts_per_div == pytest.approx(2.0)
    assert controller.set_volts_per_div(1, 5.0).confirmed == 5.0  # 0.5 V/div at the BNC x10
    assert controller.set_coupling(2, "AC").confirmed == "AC"
    assert controller.set_coupling(2, "GND").confirmed == "GND"
    assert controller.set_timebase(8e-6).confirmed == pytest.approx(8e-6)
    assert controller.set_position(2, -1.5).confirmed == pytest.approx(-1.5, abs=0.04)  # 1/25 div steps
    assert controller.set_trigger_source("CH2").confirmed == "CH2"
    assert controller.set_trigger_mode("normal").confirmed == "normal"
    assert controller.set_trigger_slope("falling").confirmed == "falling"
    assert controller.set_trigger_coupling("hf").confirmed == "HF_REJECT"
    assert controller.set_channel_enabled(2, False).confirmed is False
    assert controller.set_channel_enabled(2, True).confirmed is True
    raw = sim.scope.raw
    assert raw["VERT-CH2-COUP"] == 2 and raw["TRIG-SRC"] == 1 and raw["TRIG-MODE"] == 1


def test_channel_menu_toggle_does_not_leave_channel_disabled(
    controller: ScopeController, sim: SimulatedTransport
) -> None:
    sim.scope.menu = "CH1"  # menu already showing: a naive menu press would switch CH1 off
    controller.set_coupling(1, "AC")
    assert sim.scope.raw["VERT-CH1-DISP"] == 1


@pytest.mark.parametrize(
    ("call", "args"),
    [
        ("set_volts_per_div", (1, 3.0)),
        ("set_volts_per_div", (3, 1.0)),
        ("set_timebase", (1e-3,)),
        ("set_timebase", (80.0,)),
        ("set_coupling", (1, "XX")),
        ("set_probe", (1, 3)),
        ("set_position", (1, 11.0)),
        ("set_trigger_source", ("CH3",)),
        ("set_trigger_mode", ("single",)),
        ("set_trigger_level", (1000.0,)),
    ],
)
def test_invalid_parameters_are_rejected_before_any_command(
    controller: ScopeController, sim: SimulatedTransport, call: str, args: tuple[object, ...]
) -> None:
    before = len(sim.scope.key_log)
    with pytest.raises(ParameterError):
        getattr(controller, call)(*args)
    assert len(sim.scope.key_log) == before


def test_snap_selects_nearest(controller: ScopeController) -> None:
    assert controller.set_timebase(1e-3, snap=True).confirmed == pytest.approx(8e-4)
    assert controller.set_volts_per_div(1, 3.0, snap=True).confirmed == pytest.approx(2.0)


def test_unverifiable_setting_is_reported(controller: ScopeController, sim: SimulatedTransport) -> None:
    orig = sim.scope.press

    def broken_press(code: int) -> int:  # instrument ignores the key
        return 0 if code == Key.CH1_VDIV_UP else orig(code)

    sim.scope.press = broken_press  # type: ignore[method-assign]
    with pytest.raises(SettingNotConfirmedError) as ei:
        controller.set_volts_per_div(1, 2.0)
    assert ei.value.details["observed"] == 1.0


def test_trigger_controls_require_edge_type(controller: ScopeController, sim: SimulatedTransport) -> None:
    sim.scope.raw["TRIG-TYPE"] = 2  # pulse
    with pytest.raises(UnsupportedOperationError):
        controller.set_trigger_source("CH2")


def test_trigger_level_converges(controller: ScopeController) -> None:
    r = controller.set_trigger_level(-1.2)
    assert r.confirmed == -30
    assert controller.get_trigger()["level_v"] == pytest.approx(-1.2)


def test_single_capture_is_fresh_and_scaled(controller: ScopeController) -> None:
    cap = controller.capture(1, "single")
    assert cap.acquisition.freshness == Freshness.NEW_TRIGGER_CONFIRMED and cap.acquisition.trigger_observed
    assert cap.simulated and cap.warnings[0].startswith("SIMULATED DATA")
    assert cap.sample_count == 4000 and cap.raw_samples.dtype == np.int8
    assert cap.sample_rate_hz == pytest.approx(1e6)  # 200 us/div, 4K -> 200 samples/div
    assert cap.voltage_v is not None
    assert float(np.max(cap.voltage_v)) == pytest.approx(5.0, abs=0.1)
    assert cap.voltage_status == "simulated" and cap.time_status == "simulated"
    # trigger at t=0 on the rising edge at 2.5 V (level 62 counts = 2.48 V)
    i0 = int(np.argmin(np.abs(cap.time_s)))
    assert cap.voltage_v[i0 - 5] < 2.5 < cap.voltage_v[i0 + 5]


def test_stale_buffer_is_detected_when_stopped(controller: ScopeController) -> None:
    controller.stop()
    a = controller.capture(1, "running")
    b = controller.capture(1, "running")
    assert b.quality["identical_to_previous"] is True
    assert any("stale" in w for w in b.warnings)
    assert a.acquisition.freshness == Freshness.STOPPED_SNAPSHOT


def test_running_captures_differ(controller: ScopeController) -> None:
    controller.run()
    a = controller.capture(1, "running")
    b = controller.capture(1, "running")
    assert a.raw_sha1 != b.raw_sha1 and b.quality["identical_to_previous"] is False


def test_trigger_timeout_and_force(controller: ScopeController, sim: SimulatedTransport) -> None:
    controller.set_trigger_mode("normal")
    sim.set_signal(1, SignalSpec("sine", 1000, 0.5))  # never reaches the 2.48 V level
    t0 = time.monotonic()
    with pytest.raises(TriggerTimeoutError) as ei:
        controller.capture(1, "single", timeout_s=0.3)
    assert time.monotonic() - t0 < 2
    assert ei.value.details["trigger_state"] == "ready"
    cap = controller.capture(1, "single", timeout_s=0.3, force_on_timeout=True)
    assert cap.acquisition.freshness == Freshness.FORCED_NEW and not cap.acquisition.trigger_observed


def test_two_channel_capture_shares_acquisition(controller: ScopeController) -> None:
    a, b = controller.capture_channels([1, 2], "single")
    assert a.acquisition.group_id == b.acquisition.group_id is not None
    assert a.acquisition.simultaneity and a.acquisition.simultaneity.startswith("same_stopped_acquisition")
    controller.run()
    c, _d = controller.capture_channels([1, 2], "running")
    assert c.acquisition.simultaneity and c.acquisition.simultaneity.startswith("not_guaranteed")


def test_disabled_channel_reports_no_data(controller: ScopeController) -> None:
    from hantek_mcp.errors import NoSampleDataError

    controller.set_channel_enabled(2, False)
    with pytest.raises(NoSampleDataError):
        controller.capture(2, "stop_and_read")


def test_clipping_detected(controller: ScopeController) -> None:
    controller.set_position(1, 2.0)  # 0..5 V square at 1 V/div shifted up by 2 div -> top clips
    cap = controller.capture(1, "single")
    assert cap.quality["clipped"]


def test_disconnect_marks_lost_and_reconnect_recovers(
    controller: ScopeController, sim: SimulatedTransport
) -> None:
    sim.disconnect()
    with pytest.raises(DeviceDisconnectedError):
        controller.capture(1, "single")
    assert str(controller.state) == "lost"
    with pytest.raises(NotConnectedError):
        controller.read_settings()
    assert controller.recent_errors[-1]["code"] == "DEVICE_DISCONNECTED"
    controller.connect()  # a fresh simulated transport (the device was 're-plugged')
    assert controller.is_connected
    assert controller.capture(1, "single").sample_count == 4000


def test_abort_interrupts_trigger_wait(controller: ScopeController, sim: SimulatedTransport) -> None:
    controller.set_trigger_mode("normal")
    sim.set_signal(1, SignalSpec("dc", offset_v=0.0))
    threading.Timer(0.2, controller.request_abort).start()
    t0 = time.monotonic()
    with pytest.raises(AbortedError):
        controller.capture(1, "single", timeout_s=10)
    assert time.monotonic() - t0 < 2
    # the abort does not stick: the next operation works
    sim.set_signal(1, SignalSpec("square", 1000, 2.5, 2.5))
    assert (
        controller.capture(1, "single", timeout_s=2).acquisition.freshness == Freshness.NEW_TRIGGER_CONFIRMED
    )


def test_concurrent_access_is_serialised(controller: ScopeController) -> None:
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            for _ in range(5):
                if i % 3 == 0:
                    controller.capture(1 + i % 2, "running")
                elif i % 3 == 1:
                    controller.read_settings()
                else:
                    controller.set_volts_per_div(2, [0.5, 1.0][i % 2])
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    stats = controller.session.stats  # type: ignore[union-attr]
    assert stats.checksum_errors == stats.framing_errors == stats.stray_frames == stats.timeouts == 0


def test_key_table_mismatch_disables_key_emulation(tmp_path: Path) -> None:
    class OddSim(SimulatedTransport):
        def _file(self, name: str) -> list[bytes]:
            if name == "/keyprotocol.inf":
                return self._bulk(0x10, b"[TOTAL] 1\r\n[START]\r\n[OTHER-KEY] 1\r\n[END]")
            return super()._file(name)

    c = ScopeController(sim_config(tmp_path), lambda cfg: OddSim(seed=1))
    c.connect()
    assert c.key_names_match is False
    with pytest.raises(UnsupportedOperationError):
        c.set_volts_per_div(1, 2.0)
    assert c.capture(1, "stop_and_read").sample_count == 4000  # downloads still work
    c.disconnect()


def test_direct_write_mode_experimental(tmp_path: Path) -> None:
    c = ScopeController(sim_config(tmp_path, control={"settings_write_mode": "direct_write_experimental"}))
    c.connect()
    r = c.set_volts_per_div(1, 0.05)
    assert r.method == "direct_settings_write_experimental" and r.confirmed == 0.05 and r.key_presses == 0
    assert isinstance(c.transport, SimulatedTransport) and c.transport.scope.settings_writes == 1
    c.disconnect()


def test_default_setup_requires_config_and_confirmation(controller: ScopeController, tmp_path: Path) -> None:
    with pytest.raises(UnsupportedOperationError):
        controller.default_setup(confirm=True)
    c = ScopeController(sim_config(tmp_path / "b", control={"allow_disruptive_operations": True}))
    c.connect()
    with pytest.raises(ParameterError):
        c.default_setup(confirm=False)
    assert c.default_setup(confirm=True)["default_setup"]
    c.disconnect()


def test_events_are_emitted(controller: ScopeController) -> None:
    seen: list[str] = []
    controller.add_listener(lambda e: seen.append(e["kind"]))
    controller.set_volts_per_div(1, 2.0)
    controller.capture(1, "single")
    assert "setting_changed" in seen and "capture" in seen and "key_press" in seen


def test_screenshot(controller: ScopeController) -> None:
    rgb, fmt = controller.screenshot()
    assert rgb.shape == (480, 800, 3) and fmt == "rgb565"
    assert (rgb[..., 0] > 200).any()  # yellow CH1 trace pixels


@pytest.mark.parametrize("waveform", ["sine", "square", "pwm", "triangle", "sawtooth", "noise", "dc", "off"])
def test_simulated_waveforms(controller: ScopeController, sim: SimulatedTransport, waveform: str) -> None:
    sim.set_signal(1, SignalSpec(waveform, 1000, 1.0, 0.0, duty_cycle=0.2))  # type: ignore[arg-type]
    cap = controller.capture(1, "stop_and_read")
    v = cap.voltage_v
    assert v is not None
    if waveform == "off":
        assert np.ptp(v) == 0
    elif waveform == "dc":
        assert np.ptp(v) <= 0.08
    elif waveform == "noise":
        assert 0.8 < np.std(v) < 1.2
    else:
        assert np.ptp(v) == pytest.approx(2.0, abs=0.12)
        if waveform == "pwm":
            assert np.mean(v > 0) == pytest.approx(0.2, abs=0.01)
