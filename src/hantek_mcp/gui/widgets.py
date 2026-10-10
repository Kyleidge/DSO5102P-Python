"""Reusable GUI widgets and the background-task plumbing."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QObject, QRunnable, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from hantek_mcp.errors import HantekError
from hantek_mcp.instrument.channels import allowed_volts_per_div

CH_COLOURS = {1: (240, 220, 0), 2: (0, 210, 230)}


class TaskSignals(QObject):
    done = Signal(object)
    failed = Signal(object)


class Task(QRunnable):
    """Run ``fn`` on the thread pool; results come back on the GUI thread via queued signals."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__()
        self.fn = fn
        self.signals = TaskSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            result = self.fn()
        except HantekError as e:
            self.signals.failed.emit(e.to_dict())
        except Exception as e:
            self.signals.failed.emit(
                {
                    "code": "INTERNAL_ERROR",
                    "message": repr(e),
                    "details": {"traceback": traceback.format_exc()},
                }
            )
        else:
            self.signals.done.emit(result)


class WaveformPlot(pg.PlotWidget):
    """Oscilloscope-style plot of numerical samples (never screenshots)."""

    def __init__(self) -> None:
        super().__init__()
        self.setBackground((12, 12, 12))
        self.showGrid(x=True, y=True, alpha=0.25)
        self.setLabel("bottom", "time", units="s")
        self.setLabel("left", "voltage", units="V")
        self.addLegend(offset=(10, 10))
        self.curves = {
            ch: self.plot([], [], pen=pg.mkPen(CH_COLOURS[ch], width=1.2), name=f"CH{ch}") for ch in (1, 2)
        }
        self.banner = pg.TextItem("", color=(255, 140, 0), anchor=(0, 0))
        self.addItem(self.banner, ignoreBounds=True)
        self._units: dict[int, str] = {}

    def set_trace(self, ch: int, t: np.ndarray, y: np.ndarray, unit: str) -> None:
        self._units[ch] = unit
        self.curves[ch].setData(t, y)
        units = set(self._units.values())
        self.setLabel(
            "left", "voltage" if units == {"V"} else "value", units="V" if units == {"V"} else "counts / V"
        )

    def set_visible(self, ch: int, visible: bool) -> None:
        self.curves[ch].setVisible(visible)

    def set_banner(self, text: str) -> None:
        self.banner.setText(text)
        vr = self.viewRange()
        self.banner.setPos(vr[0][0], vr[1][1])


class SpectrumPlot(pg.PlotWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setBackground((12, 12, 12))
        self.showGrid(x=True, y=True, alpha=0.25)
        self.setLabel("bottom", "frequency", units="Hz")
        self.setLabel("left", "amplitude (dBV peak)")
        self.curve = self.plot([], [], pen=pg.mkPen((120, 255, 120), width=1))

    def set_spectrum(self, f: list[float], a: list[float]) -> None:
        amp = np.maximum(np.asarray(a, dtype=float), 1e-9)
        self.curve.setData(np.asarray(f, dtype=float), 20 * np.log10(amp))


class ChannelControls(QGroupBox):
    """Per-channel controls. Display visibility is local; the rest change the instrument."""

    def __init__(self, ch: int) -> None:
        super().__init__(f"CH{ch}")
        self.ch = ch
        r, g, b = CH_COLOURS[ch]
        self.setStyleSheet(f"QGroupBox {{ color: rgb({r},{g},{b}); font-weight: bold; }}")
        form = QFormLayout(self)
        self.visible = QCheckBox("show trace")
        self.visible.setChecked(True)
        self.enabled = QCheckBox("channel on")
        self.vdiv = QComboBox()
        self.coupling = QComboBox()
        self.coupling.addItems(["DC", "AC", "GND"])
        self.probe = QComboBox()
        self.probe.addItems(["1", "10", "100", "1000"])
        self.probe.currentTextChanged.connect(self._refill_vdiv)
        self.apply = QPushButton("Apply")
        row = QHBoxLayout()
        row.addWidget(self.visible)
        row.addWidget(self.enabled)
        form.addRow(row)
        form.addRow("V/div", self.vdiv)
        form.addRow("coupling", self.coupling)
        form.addRow("probe", self.probe)
        form.addRow(self.apply)
        self._refill_vdiv()

    def _refill_vdiv(self) -> None:
        cur = self.vdiv.currentData()
        self.vdiv.clear()
        for v in allowed_volts_per_div(int(self.probe.currentText() or 1)):
            self.vdiv.addItem(_fmt_si(v, "V"), v)
        if cur is not None:
            self.select_vdiv(cur)

    def select_vdiv(self, v: float) -> None:
        for i in range(self.vdiv.count()):
            if abs(self.vdiv.itemData(i) - v) <= 1e-9 * max(v, 1e-12):
                self.vdiv.setCurrentIndex(i)
                return

    def load(self, d: dict[str, Any]) -> None:
        for w in (self.enabled, self.probe, self.coupling, self.vdiv):
            w.blockSignals(True)
        self.enabled.setChecked(bool(d.get("enabled")))
        if d.get("probe_attenuation"):
            self.probe.setCurrentText(str(d["probe_attenuation"]))
            self._refill_vdiv()
        if d.get("coupling"):
            self.coupling.setCurrentText(d["coupling"])
        if d.get("volts_per_div_at_probe_tip"):
            self.select_vdiv(d["volts_per_div_at_probe_tip"])
        for w in (self.enabled, self.probe, self.coupling, self.vdiv):
            w.blockSignals(False)


class MeasurementTable(QTableWidget):
    ROWS = ("Vpp", "Vmax", "Vmin", "Mean", "RMS", "Frequency", "Duty", "Rise time", "Status")

    def __init__(self) -> None:
        super().__init__(len(self.ROWS), 2)
        self.setHorizontalHeaderLabels(["CH1", "CH2"])
        self.setVerticalHeaderLabels(list(self.ROWS))
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

    def set_values(self, ch: int, values: dict[str, str]) -> None:
        for r, name in enumerate(self.ROWS):
            item = QTableWidgetItem(values.get(name, ""))
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.setItem(r, ch - 1, item)


class Badge(QLabel):
    def __init__(self) -> None:
        super().__init__("DISCONNECTED")
        self.setMargin(4)
        self.set("DISCONNECTED", "#555")

    def set(self, text: str, colour: str) -> None:
        self.setText(text)
        self.setStyleSheet(
            f"QLabel {{ background: {colour}; color: white; font-weight: bold; border-radius: 3px; }}"
        )


def labelled(text: str, w: QWidget) -> QWidget:
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(QLabel(text))
    lay.addWidget(w, 1)
    return box


def _fmt_si(v: float | None, unit: str) -> str:
    if v is None:
        return "—"
    av = abs(v)
    for scale, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        if av >= scale or scale == 1e-9:
            return f"{v / scale:.4g} {prefix}{unit}"
    return f"{v:g} {unit}"


fmt_si = _fmt_si
