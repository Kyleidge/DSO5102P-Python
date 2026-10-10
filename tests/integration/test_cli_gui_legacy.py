"""CLI commands, the GUI (offscreen) and the legacy rcr facade."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from hantek_mcp.cli import main as cli_main
from hantek_mcp.service import ScopeService
from hantek_mcp.transport.simulator import SimulatedTransport
from tests.conftest import sim_config

REPO = Path(__file__).resolve().parents[2]


def test_cli_capture_exports_and_measures(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli_main(
        [
            "capture",
            "--simulate",
            "--data-dir",
            str(tmp_path),
            "--channel",
            "2",
            "--csv",
            "ch2",
            "--measure",
            "frequency",
            "voltage",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["capture"]["simulated"] and Path(out["csv"]).is_file()
    fr = out["measurements"]["frequency"]["results"]
    # unseeded simulator: the error must lie within the uncertainty the tool itself reports
    assert abs(fr["frequency_hz"] - 1000) / 1000 <= 3 * fr["relative_uncertainty_est"]


def test_cli_selftest_info_config_diagnose(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["selftest", "--simulate", "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["pass"]
    assert cli_main(["info", "--simulate", "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["identity"]["simulated"]
    assert cli_main(["config", "--simulate", "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["effective"]["backend"] == "simulator"
    assert cli_main(["diagnose", "--data-dir", str(tmp_path)]) == 0
    assert "usb" in json.loads(capsys.readouterr().out)


def test_cli_usb_without_hardware_fails_cleanly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """With the (default) USB backend and no scope the CLI must fail, never fall back to the simulator."""
    rc = cli_main(["capture", "--backend", "usb", "--data-dir", str(tmp_path)])
    assert rc == 1
    err = json.loads(capsys.readouterr().err)["error"]
    assert err["code"] in ("DEVICE_NOT_FOUND", "USB_BACKEND_UNAVAILABLE", "TRANSPORT_ERROR")


def test_console_entry_point_and_stdout_hygiene(tmp_path: Path) -> None:
    exe = Path(sys.executable).with_name("hantek-mcp")
    cmd = [str(exe)] if exe.exists() else [sys.executable, "-m", "hantek_mcp"]
    r = subprocess.run([*cmd, "--version"], capture_output=True, text=True, timeout=60, check=True)
    assert "hantek-mcp" in r.stdout
    # serve on stdio: with stdin closed immediately, stdout must contain nothing but (possibly) JSON-RPC
    r = subprocess.run(
        [*cmd, "serve", "--simulate", "--data-dir", str(tmp_path)],
        input="",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,  # the exit status after EOF on stdin is not what is under test
    )
    for line in r.stdout.splitlines():
        assert line.startswith("{"), f"non-protocol output on stdout: {line!r}"
    assert "hantek-mcp" in r.stderr  # logs go to stderr


# ---------------------------------------------------------------------------------------- legacy


def test_legacy_facade(monkeypatch: pytest.MonkeyPatch) -> None:
    sys.path.insert(0, str(REPO / "legacy"))
    try:
        import rcr.dso5102p.DSO5102P as legacy_mod  # noqa: N811 - legacy module name

        monkeypatch.setattr(legacy_mod, "UsbTransport", lambda selector: SimulatedTransport(seed=3))
        dso = legacy_mod.DSO5102P(0x049F, 0x505A, False)
        assert dso.Echo([1, 2, 3]) == [1, 2, 3]
        data = dso.ReadSampleData(0)
        assert data.typecode == "B" and len(data) == 4000
        assert len(dso.ReadSettings()) == 208
        img = dso.Screenshot()
        assert img.shape == (480, 800) and img.dtype == np.uint8
        assert dso.ScreenshotRGB().shape == (480, 800, 3)
        assert len(dso.ReadSystemTime()) == 19
        assert dso.ReadFile("/sys.inf").startswith("[DST type]")
        dso.LockControlPanel()
        dso.UnLockControlPanel()
        dso.StopAcquisition()
        dso.StartAcquisition()
        dso.KeyTrigger(0x0C, 0x01)
        with pytest.raises(NotImplementedError):
            dso.RemoteShell("ls /")
    finally:
        sys.path.remove(str(REPO / "legacy"))


# ---------------------------------------------------------------------------------------- GUI


@pytest.mark.gui
def test_gui_live_refresh_with_concurrent_mcp_style_calls(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6.QtWidgets import QApplication

    from hantek_mcp.gui.app import MainWindow

    app = QApplication.instance() or QApplication([])
    svc = ScopeService(sim_config(tmp_path))
    win = MainWindow(svc)

    def wait(cond: Callable[[], bool], t: float = 15) -> bool:
        end = time.time() + t
        while time.time() < end and not cond():
            app.processEvents()
            time.sleep(0.01)
        return cond()

    win.toggle_connect()
    assert wait(lambda: svc.controller.is_connected)
    assert wait(win.ch_controls[2].enabled.isChecked)
    win.capture_now()
    assert wait(lambda: 1 in win.latest and 2 in win.latest)
    x, y = win.plot.curves[1].getData()
    assert len(x) == 4000 and float(np.max(y)) == pytest.approx(5.0, abs=0.1)  # numerical samples, in volts
    assert "SIMULATED" in win.plot.banner.toPlainText()
    # live refresh while another thread (standing in for MCP tool calls) changes settings and measures
    import threading

    errors: list[BaseException] = []

    def mcp_client() -> None:
        try:
            for _ in range(5):
                svc.set_timebase(4e-4)
                svc.measure("frequency", channel=1, mode="stop_and_read")
                svc.set_timebase(2e-4)
        except BaseException as e:
            errors.append(e)

    seq0 = svc.controller._capture_seq
    win.act_live.setChecked(True)
    t = threading.Thread(target=mcp_client)
    t.start()
    assert wait(lambda: not t.is_alive(), 60)
    assert wait(lambda: svc.controller._capture_seq > seq0 + 8, 30)
    win.act_live.setChecked(False)
    assert not errors
    stats = svc.controller.session.stats  # type: ignore[union-attr]
    assert stats.checksum_errors == stats.framing_errors == stats.stray_frames == 0
    win.grab_screenshot()
    assert wait(lambda: win.shot_label.pixmap() is not None and not win.shot_label.pixmap().isNull())
    win.compute_fft()
    assert wait(lambda: "peak" in win.fft_info.text())
    win.refresh_history()
    assert win.history.rowCount() > 0
    win.close()
    assert not svc.controller.is_connected
