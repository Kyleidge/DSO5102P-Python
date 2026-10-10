"""Desktop GUI (PySide6 + pyqtgraph).

The GUI owns one :class:`ScopeService`. All instrument and analysis work runs on
a thread pool; the controller's operation lock serialises it with MCP tool calls
when the embedded MCP endpoint is enabled, so both interfaces share a single USB
owner and observe the same connection/acquisition state.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections.abc import Callable
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from hantek_mcp.analysis import measurements as meas
from hantek_mcp.config import AppConfig, load_config
from hantek_mcp.errors import HantekError
from hantek_mcp.gui.widgets import (
    Badge,
    ChannelControls,
    MeasurementTable,
    SpectrumPlot,
    Task,
    WaveformPlot,
    fmt_si,
)
from hantek_mcp.instrument.acquisition import allowed_timebases
from hantek_mcp.models import WaveformCapture
from hantek_mcp.service import ScopeService
from hantek_mcp.storage.captures import load_capture_file

log = logging.getLogger(__name__)


class _EventBridge(QObject):
    """Moves controller events (emitted on worker threads) onto the GUI thread."""

    event = Signal(dict)


class McpHttpHost:
    """Runs the MCP Streamable HTTP endpoint in a background thread, sharing the GUI's service."""

    def __init__(self, service: ScopeService) -> None:
        self.service = service
        self._server: Any = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def url(self) -> str:
        sc = self.service.config.server
        return f"http://{sc.http_host}:{sc.http_port}{sc.http_path}"

    def start(self) -> None:
        if self.running:
            return
        import uvicorn

        from hantek_mcp.mcp.server import build_server, http_app

        app = build_server(self.service, auto_connect=False, owns_service=False)
        asgi = http_app(app, self.service.config)
        sc = self.service.config.server
        cfg = uvicorn.Config(asgi, host=sc.http_host, port=sc.http_port, log_config=None, log_level="warning")
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, name="mcp-http", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = self._thread = None


class MainWindow(QMainWindow):
    def __init__(self, service: ScopeService) -> None:
        super().__init__()
        self.svc = service
        self.pool = QThreadPool.globalInstance()
        self.mcp = McpHttpHost(service)
        self.latest: dict[int, WaveformCapture] = {}
        self._live_busy = False
        self._tasks: set[Task] = set()
        self.setWindowTitle("Hantek DSO5102P — hantek-mcp")
        self.resize(1400, 860)

        self.bridge = _EventBridge()
        self.bridge.event.connect(self._on_event)
        self.svc.controller.add_listener(self.bridge.event.emit)

        self._build_toolbar()
        self._build_central()
        self._build_controls()
        self._build_bottom()

        self.live_timer = QTimer(self)
        self.live_timer.timeout.connect(self._live_tick)
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._refresh_badge)
        self.status_timer.start(1000)
        self._refresh_badge()

    # ------------------------------------------------------------------ layout
    def _build_toolbar(self) -> None:
        tb = QToolBar("main")
        tb.setMovable(False)
        self.addToolBar(tb)
        self.badge = Badge()
        tb.addWidget(self.badge)
        self.act_connect = QAction("Connect", self)
        self.act_connect.triggered.connect(self.toggle_connect)
        tb.addAction(self.act_connect)
        tb.addSeparator()
        self.mode = QComboBox()
        self.mode.addItems(["single", "stop_and_read", "running"])
        self.mode.setToolTip(
            "single: arm & wait for a confirmed new trigger; running: fastest, freshness not guaranteed"
        )
        tb.addWidget(QLabel(" capture mode "))
        tb.addWidget(self.mode)
        self.act_capture = QAction("Capture CH1+CH2", self)
        self.act_capture.triggered.connect(self.capture_now)
        tb.addAction(self.act_capture)
        self.live_interval = QSpinBox()
        self.live_interval.setRange(100, 10000)
        self.live_interval.setValue(500)
        self.live_interval.setSuffix(" ms")
        tb.addWidget(QLabel(" live every "))
        tb.addWidget(self.live_interval)
        self.act_live = QAction("Start live", self)
        self.act_live.setCheckable(True)
        self.act_live.toggled.connect(self.toggle_live)
        tb.addAction(self.act_live)
        tb.addSeparator()
        for text, fn in (
            ("Run", self.svc.start),
            ("Stop", self.svc.stop),
            ("Force trig", self.svc.force_trigger),
        ):
            a = QAction(text, self)
            a.triggered.connect(
                lambda _=False, f=fn, t=text: self.run_task(f, lambda r, t=t: self.log(f"{t}: {r}"))
            )
            tb.addAction(a)
        a = QAction("Abort", self)
        a.triggered.connect(lambda: self.log(f"abort: {self.svc.abort()}"))
        tb.addAction(a)

    def _build_central(self) -> None:
        self.tabs = QTabWidget()
        self.plot = WaveformPlot()
        self.spectrum = SpectrumPlot()
        spec_box = QWidget()
        sl = QVBoxLayout(spec_box)
        row = QHBoxLayout()
        self.fft_ch = QComboBox()
        self.fft_ch.addItems(["CH1", "CH2"])
        self.fft_window = QComboBox()
        self.fft_window.addItems(["hann", "flattop", "blackmanharris", "hamming", "rectangular"])
        self.fft_btn = QPushButton("Compute FFT")
        self.fft_btn.clicked.connect(self.compute_fft)
        self.fft_info = QLabel("")
        for w in (self.fft_ch, self.fft_window, self.fft_btn, self.fft_info):
            row.addWidget(w)
        row.addStretch(1)
        sl.addLayout(row)
        sl.addWidget(self.spectrum)
        shot_box = QWidget()
        shl = QVBoxLayout(shot_box)
        warn = QLabel(
            "Screenshot = picture of the instrument display. It is NOT waveform data and is never used "
            "for measurements."
        )
        warn.setStyleSheet("color: #e0a000;")
        self.shot_btn = QPushButton("Grab screenshot")
        self.shot_btn.clicked.connect(self.grab_screenshot)
        self.shot_label = QLabel("no screenshot")
        self.shot_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        shl.addWidget(warn)
        shl.addWidget(self.shot_btn)
        shl.addWidget(self.shot_label, 1)
        hist_box = QWidget()
        hl = QVBoxLayout(hist_box)
        self.history = QTableWidget(0, 7)
        self.history.setHorizontalHeaderLabels(
            ["capture id", "ch", "time (UTC)", "samples", "freshness", "volts", "simulated"]
        )
        self.history.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.history.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history.doubleClicked.connect(self._load_history_row)
        hrow = QHBoxLayout()
        for text, fn in (
            ("Refresh", self.refresh_history),
            ("Export CSV", lambda: self.export_selected("csv")),
            ("Export NPZ", lambda: self.export_selected("npz")),
            ("Load file…", self.load_file),
            ("Delete", self.delete_selected),
        ):
            b = QPushButton(text)
            b.clicked.connect(fn)
            hrow.addWidget(b)
        hrow.addStretch(1)
        hl.addLayout(hrow)
        hl.addWidget(self.history)
        self.tabs.addTab(self.plot, "Waveform")
        self.tabs.addTab(spec_box, "Spectrum")
        self.tabs.addTab(shot_box, "Screenshot")
        self.tabs.addTab(hist_box, "History")
        self.setCentralWidget(self.tabs)

    def _build_controls(self) -> None:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        self.ch_controls = {ch: ChannelControls(ch) for ch in (1, 2)}
        for ch, w in self.ch_controls.items():
            w.visible.toggled.connect(lambda v, c=ch: self.plot.set_visible(c, v))
            w.apply.clicked.connect(lambda _=False, c=ch: self.apply_channel(c))
            lay.addWidget(w)
        hb = QGroupBox("Horizontal")
        hf = QFormLayout(hb)
        self.timebase = QComboBox()
        for v in allowed_timebases():
            self.timebase.addItem(fmt_si(v, "s") + "/div", v)
        b = QPushButton("Apply")
        b.clicked.connect(self.apply_timebase)
        hf.addRow("timebase", self.timebase)
        hf.addRow(b)
        lay.addWidget(hb)
        tg = QGroupBox("Trigger (edge)")
        tf = QFormLayout(tg)
        self.trig_src = QComboBox()
        self.trig_src.addItems(["CH1", "CH2", "EXT", "EXT/5", "AC50"])
        self.trig_mode = QComboBox()
        self.trig_mode.addItems(["auto", "normal"])
        self.trig_slope = QComboBox()
        self.trig_slope.addItems(["rising", "falling"])
        self.trig_level = QDoubleSpinBox()
        self.trig_level.setRange(-500, 500)
        self.trig_level.setDecimals(3)
        self.trig_level.setSuffix(" V")
        b_apply = QPushButton("Apply")
        b_apply.clicked.connect(self.apply_trigger)
        b_50 = QPushButton("Level 50%")
        b_50.clicked.connect(
            lambda: self.run_task(
                lambda: self.svc.set_trigger_level(fifty_percent=True),
                lambda r: self.log(f"trigger 50%: {r['result']}"),
            )
        )
        tf.addRow("source", self.trig_src)
        tf.addRow("mode", self.trig_mode)
        tf.addRow("slope", self.trig_slope)
        tf.addRow("level", self.trig_level)
        r = QHBoxLayout()
        r.addWidget(b_apply)
        r.addWidget(b_50)
        tf.addRow(r)
        lay.addWidget(tg)
        ab = QGroupBox("Automation")
        al = QVBoxLayout(ab)
        b_auto = QPushButton("Software autoset CH1")
        b_auto.clicked.connect(
            lambda: self.run_task(
                lambda: self.svc.autoset(1),
                lambda r: (self.log(f"autoset: {len(r['changes'])} change(s)"), self.read_settings()),
            )
        )
        b_read = QPushButton("Read settings from scope")
        b_read.clicked.connect(self.read_settings)
        al.addWidget(b_auto)
        al.addWidget(b_read)
        lay.addWidget(ab)
        mb = QGroupBox("MCP server (HTTP, localhost)")
        ml = QVBoxLayout(mb)
        self.mcp_label = QLabel("stopped")
        self.mcp_label.setWordWrap(True)
        self.mcp_btn = QPushButton("Start MCP endpoint")
        self.mcp_btn.clicked.connect(self.toggle_mcp)
        ml.addWidget(self.mcp_label)
        ml.addWidget(self.mcp_btn)
        lay.addWidget(mb)
        lay.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        dock = QDockWidget("Controls", self)
        dock.setWidget(scroll)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

    def _build_bottom(self) -> None:
        self.meas = MeasurementTable()
        self.warn_label = QLabel("")
        self.warn_label.setWordWrap(True)
        self.warn_label.setStyleSheet("color: #e0a000;")
        mw = QWidget()
        ml = QVBoxLayout(mw)
        ml.addWidget(self.meas)
        ml.addWidget(self.warn_label)
        d1 = QDockWidget("Measurements (computed from samples)", self)
        d1.setWidget(mw)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, d1)
        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setMaximumBlockCount(2000)
        d2 = QDockWidget("Activity log (GUI + MCP)", self)
        d2.setWidget(self.activity)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, d2)
        self.tabifyDockWidget(d1, d2)
        d1.raise_()

    # ------------------------------------------------------------------ plumbing
    def log(self, text: str) -> None:
        self.activity.appendPlainText(text)

    def run_task(
        self,
        fn: Callable[[], Any],
        on_done: Callable[[Any], Any] | None = None,
        on_fail: Callable[[dict[str, Any]], Any] | None = None,
    ) -> Task:
        task = Task(fn)
        # Keep the Python wrapper (and its signal QObject) alive until a result is delivered;
        # otherwise the queued signal can be dropped when the wrapper is garbage-collected.
        self._tasks.add(task)
        task.signals.done.connect(lambda _r, t=task: self._tasks.discard(t))
        task.signals.failed.connect(lambda _e, t=task: self._tasks.discard(t))
        if on_done:
            task.signals.done.connect(on_done)
        task.signals.failed.connect(on_fail or self._on_error)
        self.pool.start(task)
        return task

    def _on_error(self, err: dict[str, Any]) -> None:
        self.log(f"ERROR {err.get('code')}: {err.get('message')}")
        self.statusBar().showMessage(f"{err.get('code')}: {err.get('message')}", 8000)

    def _on_event(self, ev: dict[str, Any]) -> None:
        kind = ev.get("kind")
        if kind == "mcp_call":
            self.log(
                f"[MCP] {ev['tool']} {'ok' if ev['ok'] else 'FAILED ' + str(ev.get('error_code'))} "
                f"({ev['duration_ms']} ms)"
            )
        elif kind in ("key_press",):
            return
        else:
            details = {k: v for k, v in ev.items() if k not in ("kind", "time_utc")}
            self.log(f"[{kind}] {json.dumps(details, default=str)[:300]}")
        if kind in ("connected", "disconnected", "connection_lost"):
            self._refresh_badge()

    def _refresh_badge(self) -> None:
        c = self.svc.controller
        if c.is_connected:
            if c.simulated:
                self.badge.set("SIMULATOR", "#c06000")
            else:
                self.badge.set(f"HARDWARE {c.model_name or ''}".strip(), "#207020")
            self.act_connect.setText("Disconnect")
        else:
            self.badge.set(str(c.state).upper(), "#802020" if str(c.state) == "lost" else "#555")
            self.act_connect.setText("Connect")
        busy = c.busy_operation
        self.statusBar().showMessage(
            f"busy: {busy}" if busy else ("workflow: " + c.active_workflow if c.active_workflow else "idle")
        )
        self.mcp_label.setText(f"running at {self.mcp.url}" if self.mcp.running else "stopped")
        self.mcp_btn.setText("Stop MCP endpoint" if self.mcp.running else "Start MCP endpoint")

    # ------------------------------------------------------------------ actions
    def toggle_connect(self) -> None:
        if self.svc.controller.is_connected:
            self.act_live.setChecked(False)
            self.run_task(self.svc.disconnect, lambda _r: self._refresh_badge())
        else:
            self.run_task(self.svc.connect, lambda _r: (self._refresh_badge(), self.read_settings()))

    def read_settings(self) -> None:
        def done(r: dict[str, Any]) -> None:
            s = r["settings"]
            for ch in (1, 2):
                self.ch_controls[ch].load(s["channels"][str(ch)])
            tb = s["horizontal"]["seconds_per_div"]
            for i in range(self.timebase.count()):
                if tb and abs(self.timebase.itemData(i) / tb - 1) < 1e-6:
                    self.timebase.setCurrentIndex(i)
            t = s["trigger"]
            if t.get("source"):
                self.trig_src.setCurrentText(t["source"])
            if t.get("mode"):
                self.trig_mode.setCurrentText(t["mode"])
            if t.get("slope"):
                self.trig_slope.setCurrentText(t["slope"])
            if t.get("level_v") is not None:
                self.trig_level.setValue(t["level_v"])

        self.run_task(self.svc.settings, done)

    def apply_channel(self, ch: int) -> None:
        w = self.ch_controls[ch]
        enabled, coupling = w.enabled.isChecked(), w.coupling.currentText()
        probe, vdiv = int(w.probe.currentText()), float(w.vdiv.currentData())

        def work() -> list[dict[str, Any]]:
            out = [self.svc.enable_channel(ch, enabled)]
            if enabled:
                out += [
                    self.svc.set_probe(ch, probe),
                    self.svc.set_coupling(ch, coupling),
                    self.svc.set_vertical_scale(ch, vdiv),
                ]
            return out

        self.run_task(
            work,
            lambda rs: [
                self.log(f"CH{ch}: {r['result']['setting']} -> {r['result']['confirmed_value']} (read back)")
                for r in rs
            ],
        )

    def apply_timebase(self) -> None:
        v = float(self.timebase.currentData())
        self.run_task(
            lambda: self.svc.set_timebase(v),
            lambda r: self.log(f"timebase -> {r['result']['confirmed_value']} s/div (read back)"),
        )

    def apply_trigger(self) -> None:
        src, mode, slope, lvl = (
            self.trig_src.currentText(),
            self.trig_mode.currentText(),
            self.trig_slope.currentText(),
            self.trig_level.value(),
        )

        def work() -> list[dict[str, Any]]:
            out = [
                self.svc.set_trigger_source(src),
                self.svc.set_trigger_mode(mode),
                self.svc.set_trigger_slope(slope),
            ]
            if src in ("CH1", "CH2"):
                out.append(self.svc.set_trigger_level(lvl))
            return out

        self.run_task(
            work,
            lambda rs: [
                self.log(f"trigger: {r['result']['setting']} -> {r['result']['confirmed_value']}") for r in rs
            ],
        )

    def capture_now(self, on_done: Callable[[], Any] | None = None) -> Task | None:
        channels = [ch for ch in (1, 2) if self.ch_controls[ch].enabled.isChecked()] or [1]
        mode = self.mode.currentText()

        def work() -> list[WaveformCapture]:
            caps = self.svc.controller.capture_channels(channels, mode, 5.0, mode == "single", True, "gui")
            for c in caps:
                self.svc.store.add(c)
            return caps

        def done(caps: list[WaveformCapture]) -> None:
            for c in caps:
                self.show_capture(c)
            self._live_busy = False
            if on_done:
                on_done()

        def fail(err: dict[str, Any]) -> None:
            self._live_busy = False
            self._on_error(err)

        if self._live_busy:
            return None
        self._live_busy = True
        return self.run_task(work, done, fail)

    def show_capture(self, c: WaveformCapture) -> None:
        self.latest[c.channel] = c
        y = c.voltage_v if c.voltage_v is not None else c.raw_samples.astype(float)
        unit = "V" if c.voltage_v is not None else "counts"
        self.plot.set_trace(c.channel, c.time_s, y, unit)
        flags = []
        if c.simulated:
            flags.append("SIMULATED DATA")
        if c.voltage_status not in {"verified", "simulated"}:
            flags.append(f"voltage scaling: {c.voltage_status}")
        self.plot.set_banner(" | ".join(flags))
        self.meas.set_values(c.channel, self._quick_measurements(c, y, unit))
        self.warn_label.setText(
            " • ".join(dict.fromkeys(w for cap in self.latest.values() for w in cap.warnings))
        )

    @staticmethod
    def _quick_measurements(c: WaveformCapture, y: np.ndarray, unit: str) -> dict[str, str]:
        out: dict[str, str] = {}
        if c.quality.get("flat"):
            return {"Status": "flat / no signal"}
        s = meas.voltage_stats(y)
        u = "V" if unit == "V" else " cnt"
        out.update(
            {
                "Vpp": fmt_si(s["peak_to_peak"], u),
                "Vmax": fmt_si(s["maximum"], u),
                "Vmin": fmt_si(s["minimum"], u),
                "Mean": fmt_si(s["mean"], u),
                "RMS": fmt_si(s["rms"], u),
            }
        )
        try:
            out["Frequency"] = fmt_si(meas.frequency(y, c.sample_interval_s).get("frequency_hz"), "Hz")
            out["Duty"] = f"{meas.duty_cycle(y, c.sample_interval_s)['duty_cycle_percent']:.2f} %"
            out["Rise time"] = fmt_si(meas.transition_time(y, c.sample_interval_s, True)["rise_time"], "s")
        except meas.MeasurementError:
            pass
        out["Status"] = ("CLIPPED" if c.quality.get("clipped") else "ok") + f" · {c.acquisition.freshness}"
        return out

    def toggle_live(self, on: bool) -> None:
        self.act_live.setText("Stop live" if on else "Start live")
        if on:
            self.live_timer.start(self.live_interval.value())
        else:
            self.live_timer.stop()

    def _live_tick(self) -> None:
        if not self.svc.controller.is_connected:
            self.act_live.setChecked(False)
            return
        self.capture_now()

    def compute_fft(self) -> None:
        ch = self.fft_ch.currentIndex() + 1
        cap = self.latest.get(ch)
        if cap is None:
            self.log("capture first")
            return

        def done(r: dict[str, Any]) -> None:
            res = r["results"]
            self.spectrum.set_spectrum(res["freqs_hz"], res["amplitude"])
            pk = res["peaks"][0] if res["peaks"] else None
            self.fft_info.setText(
                f"bin {fmt_si(res['bin_width_hz'], 'Hz')}; peak "
                + (f"{fmt_si(pk['frequency_hz'], 'Hz')}" if pk else "—")
            )

        win = self.fft_window.currentText()
        self.run_task(lambda: self.svc.fft(cap.capture_id, window=win, max_points=4000), done)

    def grab_screenshot(self) -> None:
        def done(r: tuple[bytes, dict[str, Any]]) -> None:
            img = QImage.fromData(r[0], "PNG")
            self.shot_label.setPixmap(QPixmap.fromImage(img))
            self.log(f"screenshot saved: {r[1]['path']}")

        self.run_task(self.svc.screenshot, done)

    def refresh_history(self) -> None:
        rows = self.svc.store.list(500)
        self.history.setRowCount(len(rows))
        for i, r in enumerate(rows):
            vals = [
                r["capture_id"],
                str(r["channel"]),
                r["timestamp_utc"],
                str(r["sample_count"]),
                str(r["freshness"]),
                str(r["voltage_status"]),
                "yes" if r["simulated"] else "no",
            ]
            for j, v in enumerate(vals):
                self.history.setItem(i, j, QTableWidgetItem(v))
        self.history.resizeColumnsToContents()

    def _selected_id(self) -> str | None:
        rows = self.history.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.history.item(rows[0].row(), 0)
        return item.text() if item else None

    def _load_history_row(self) -> None:
        cid = self._selected_id()
        if cid:
            self.show_capture(self.svc.store.get(cid))
            self.tabs.setCurrentIndex(0)

    def export_selected(self, fmt: str) -> None:
        cid = self._selected_id() or (self.latest[max(self.latest)].capture_id if self.latest else None)
        if not cid:
            return
        self.run_task(
            lambda: self.svc.export(cid, fmt, overwrite=True), lambda r: self.log(f"exported {r['path']}")
        )

    def delete_selected(self) -> None:
        cid = self._selected_id()
        if cid:
            self.svc.delete_capture(cid)
            self.refresh_history()

    def load_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load capture", str(self.svc.store.export_dir), "Captures (*.csv *.npz)"
        )
        if not path:
            return
        from pathlib import Path

        try:
            cap = load_capture_file(Path(path))
        except HantekError as e:
            QMessageBox.warning(self, "Load failed", str(e))
            return
        self.show_capture(cap)
        self.tabs.setCurrentIndex(0)

    def toggle_mcp(self) -> None:
        try:
            if self.mcp.running:
                self.mcp.stop()
                self.log("MCP endpoint stopped")
            else:
                self.mcp.start()
                self.log(f"MCP endpoint at {self.mcp.url} (shares this window's instrument connection)")
        except HantekError as e:
            QMessageBox.warning(self, "MCP", str(e))
        self._refresh_badge()

    def closeEvent(self, event: Any) -> None:
        self.live_timer.stop()
        self.mcp.stop()
        self.pool.waitForDone(5000)
        if self.svc.controller.is_connected:
            self.svc.disconnect()
        super().closeEvent(event)


def run_gui(config: AppConfig | None = None, mcp_http: bool = False, connect: bool = True) -> int:
    from hantek_mcp.mcp.server import configure_logging

    cfg = config or load_config()
    configure_logging(cfg)
    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow(ScopeService(cfg))
    win.show()
    if mcp_http:
        win.toggle_mcp()
    if connect:
        win.toggle_connect()
    return int(app.exec())


def main() -> int:
    """Entry point for the ``hantek-mcp-gui`` launcher (accepts the same options as ``hantek-mcp gui``)."""
    from hantek_mcp.cli import main as cli_main

    return cli_main(["gui", *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
