"""Physical DSO5102P acceptance tests (Milestone E). NEVER run in CI.

Run on a machine with the scope attached (see docs/acquisition-validation.md):

    HANTEK_MCP_HARDWARE=1 pytest -m hardware tests/hardware -v -s

Set-up before running:
* CH1 probe on the PROBE COMP terminal (~5 V, 1 kHz square), probe switch and the
  scope's CH1 probe setting both at 10x (or both at 1x), CH2 probe on the same
  terminal if available.
* Trigger: edge, source CH1, mode auto. Front-panel menus closed.

Every test appends its observations to a JSON report (``HANTEK_MCP_REPORT``,
default ``hardware-validation-report.json``) and raw protocol artefacts to
``hardware-artifacts/``; please return both to the developer.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hantek_mcp import __version__
from hantek_mcp.config import load_config
from hantek_mcp.service import ScopeService

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif(os.environ.get("HANTEK_MCP_HARDWARE") != "1", reason="set HANTEK_MCP_HARDWARE=1"),
]

REPORT = Path(os.environ.get("HANTEK_MCP_REPORT", "hardware-validation-report.json"))
ARTIFACTS = Path(os.environ.get("HANTEK_MCP_ARTIFACTS", "hardware-artifacts"))
EXPECTED_HZ = float(os.environ.get("HANTEK_MCP_PROBE_COMP_HZ", "1000"))
EXPECTED_VPP = float(os.environ.get("HANTEK_MCP_PROBE_COMP_VPP", "5.0"))


def record(test: str, **data: Any) -> None:
    report: dict[str, Any] = json.loads(REPORT.read_text()) if REPORT.exists() else {}
    report.setdefault(
        "environment",
        {
            "hantek_mcp_version": __version__,
            "python": sys.version,
            "platform": platform.platform(),
        },
    )
    report.setdefault("results", {})[test] = data
    REPORT.write_text(json.dumps(report, indent=2, default=str))


@pytest.fixture(scope="module")
def hw(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ScopeService]:
    cfg = load_config(
        overrides={"backend": "usb", "storage": {"data_dir": str(tmp_path_factory.mktemp("hw"))}}
    )
    svc = ScopeService(cfg)
    svc.connect()
    ARTIFACTS.mkdir(exist_ok=True)
    yield svc
    svc.disconnect()


def test_01_device_detected() -> None:
    from hantek_mcp.transport.usb import usb_diagnostics

    d = usb_diagnostics()
    record("device_detected", diagnostics=d)
    assert d.get("device_count", 0) >= 1, d


def test_02_identity(hw: ScopeService) -> None:
    ident = hw.identify()
    record("identity", identity=ident)
    assert not ident["simulated"]
    assert ident["key_table_matches_dso5102p"] is True
    assert ident["settings_layout_source"].startswith("instrument:")


def test_03_settings_readback(hw: ScopeService) -> None:
    s = hw.settings(include_raw=True)["settings"]
    session = hw.controller.session
    assert session is not None
    blob = session.read_settings_blob()
    (ARTIFACTS / "settings_blob.bin").write_bytes(blob)
    (ARTIFACTS / "protocol.inf").write_bytes(session.read_file("/protocol.inf"))
    record("settings_readback", settings=s, blob_len=len(blob))
    for ch in ("1", "2"):
        assert s["channels"][ch]["volts_per_div"] is not None, "V/div index not decoded"
    assert s["horizontal"]["seconds_per_div"] is not None


@pytest.mark.parametrize("channel", [1, 2])
def test_04_waveform_download(hw: ScopeService, channel: int) -> None:
    r = hw.capture(channel, "stop_and_read")["capture"]
    cap = hw.store.get(r["capture_id"])
    (ARTIFACTS / f"ch{channel}_raw_int8.bin").write_bytes(cap.raw_samples.tobytes())
    record(f"download_ch{channel}", capture=r, metadata=cap.metadata())
    assert cap.sample_count > 0
    assert not cap.simulated


def test_05_probe_comp_scaling(hw: ScopeService) -> None:
    r = hw.self_test(1, EXPECTED_HZ, EXPECTED_VPP, mode="single")
    record("probe_comp_self_test", result=r)
    assert r["pass"], json.dumps(r["checks"], indent=1)


def test_06_trigger_and_freshness(hw: ScopeService) -> None:
    caps = [hw.capture(1, "single")["capture"] for _ in range(3)]
    record("trigger_freshness", captures=caps)
    assert all(c["freshness"] == "new_trigger_confirmed" for c in caps)


def test_07_repeated_captures_not_stale(hw: ScopeService) -> None:
    hw.start()
    caps = [hw.controller.capture(1, "running") for _ in range(5)]
    hashes = [c.raw_sha1 for c in caps]
    record(
        "repeated_running_captures",
        hashes=hashes,
        identical=[c.quality.get("identical_to_previous") for c in caps],
    )
    assert len(set(hashes)) > 1, "every running capture was byte-identical: buffer not refreshing"


def test_08_settings_change_roundtrip(hw: ScopeService) -> None:
    ch = hw.get_channel(1)["channel"]
    original = ch["volts_per_div_at_probe_tip"]
    probe = ch["probe_attenuation"]
    from hantek_mcp.instrument.channels import allowed_volts_per_div

    allowed = allowed_volts_per_div(probe)
    other = (
        allowed[allowed.index(original) + 1] if allowed.index(original) + 1 < len(allowed) else allowed[-2]
    )
    a = hw.set_vertical_scale(1, other)["result"]
    b = hw.set_vertical_scale(1, original)["result"]
    t0 = hw.get_trigger()["trigger"]
    record("settings_roundtrip", change=a, restore=b, trigger=t0)
    assert a["confirmed_value"] == other and b["confirmed_value"] == original


def test_09_screenshot(hw: ScopeService) -> None:
    png, meta = hw.screenshot()
    (ARTIFACTS / "screenshot.png").write_bytes(png)
    record("screenshot", meta=meta)
    assert meta["format"] in ("rgb565", "palette8_greyscale")


def test_10_mcp_stdio_end_to_end_on_hardware(hw: ScopeService, tmp_path: Path) -> None:
    import anyio
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    hw.disconnect()  # release the USB device for the MCP server process
    out: dict[str, Any] = {}

    async def main() -> None:
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "hantek_mcp", "serve", "--data-dir", str(tmp_path)]
        )
        async with Client(params) as c:
            out["status"] = (await c.call_tool("scope_status", {})).structured_content
            cap = (await c.call_tool("scope_capture", {"channel": 1})).structured_content
            out["capture"] = cap
            m = await c.call_tool("scope_measure_frequency", {"capture_id": cap["capture"]["capture_id"]})
            out["frequency"] = m.structured_content

    anyio.run(main)
    hw.connect()
    record("mcp_end_to_end", **out)
    assert out["status"]["simulated"] is False
    assert out["frequency"]["results"]["frequency_hz"] == pytest.approx(EXPECTED_HZ, rel=0.02)
