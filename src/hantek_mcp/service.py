"""ScopeService: the one instrument service shared by the MCP server, GUI and CLI.

It combines the :class:`ScopeController` (hardware owner), the
:class:`CaptureStore` and the analysis functions, and returns JSON-safe dicts
that always state whether data is simulated and how it was obtained.
All methods are blocking and thread-safe; async callers run them in a worker
thread.
"""

from __future__ import annotations

import logging
import math
import operator
import shutil
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from hantek_mcp import __version__
from hantek_mcp.analysis import fft as fftmod
from hantek_mcp.analysis import measurements as meas
from hantek_mcp.analysis import timing
from hantek_mcp.capabilities import capabilities
from hantek_mcp.config import AppConfig
from hantek_mcp.errors import (
    AbortedError,
    HantekError,
    LimitExceededError,
    ParameterError,
    ScalingUnavailableError,
    UnsupportedOperationError,
)
from hantek_mcp.instrument.controller import ScopeController
from hantek_mcp.instrument.core import TransportFactory
from hantek_mcp.models import Freshness, WaveformCapture, utcnow
from hantek_mcp.protocol.decoder import encode_png
from hantek_mcp.protocol.settings import DSO5102P_TB_RANGE, DSO5102P_VB_RANGE, TIMEBASE_S_PER_DIV, VOLTS_PER_DIV
from hantek_mcp.storage.captures import CaptureStore

log = logging.getLogger(__name__)

MEASUREMENTS = (
    "voltage", "frequency", "period", "duty_cycle", "rise_time", "fall_time", "rms", "peak_to_peak", "noise",
)  # fmt: skip
CONDITION_METRICS = ("vpp", "vmax", "vmin", "mean", "rms", "frequency", "duty_cycle", "edge_count",
                     "pulse_width_max", "pulse_width_min")  # fmt: skip
_OPS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le,
}  # fmt: skip


def _f(x: Any) -> Any:
    """Convert numpy scalars/arrays recursively into JSON-safe Python values."""
    if isinstance(x, dict):
        return {k: _f(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_f(v) for v in x]
    if isinstance(x, np.ndarray):
        return [_f(v) for v in x.tolist()]
    if isinstance(x, np.floating):
        v = float(x)
        return v if math.isfinite(v) else None
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x


class ScopeService:
    def __init__(self, config: AppConfig, transport_factory: TransportFactory | None = None) -> None:
        self.config = config
        self.controller = ScopeController(config, transport_factory)
        st = config.storage
        self.store = CaptureStore(st.data_dir, st.max_captures, st.max_disk_mb, st.persist_captures)
        self.started_at = utcnow().isoformat()
        self.tool_activity: deque[dict[str, Any]] = deque(maxlen=500)

    # -- helpers ---------------------------------------------------------------------------------

    @property
    def simulated(self) -> bool:
        return self.controller.simulated

    def _env(self, payload: dict[str, Any]) -> dict[str, Any]:
        out = {"simulated": self.simulated, "backend": self.config.backend}
        out.update(payload)
        return _f(out)  # type: ignore[no-any-return]

    def record_tool_call(self, tool: str, ok: bool, duration_ms: float, error_code: str | None = None) -> None:
        entry = {"time_utc": utcnow().isoformat(), "tool": tool, "ok": ok, "duration_ms": round(duration_ms, 1),
                 "error_code": error_code}  # fmt: skip
        self.tool_activity.append(entry)
        self.controller.emit("mcp_call", **entry)

    def _store(self, caps: list[WaveformCapture]) -> list[dict[str, Any]]:
        for c in caps:
            self.store.add(c)
        return [self.capture_brief(c) for c in caps]

    def capture_brief(self, c: WaveformCapture) -> dict[str, Any]:
        m = c.metadata()
        return _f({  # type: ignore[no-any-return]
            "capture_id": c.capture_id,
            "channel": c.channel,
            "simulated": c.simulated,
            "source": str(c.source),
            "timestamp_utc": m["timestamp_utc"],
            "sample_count": c.sample_count,
            "sample_rate_hz": c.sample_rate_hz,
            "sample_interval_s": c.sample_interval_s,
            "record_duration_s": m["record_duration_s"],
            "voltage_scaling_status": c.voltage_status,
            "time_scaling_status": c.time_status,
            "volts_per_div": c.channel_settings.get("volts_per_div_at_probe_tip"),
            "seconds_per_div": c.timebase_settings.get("seconds_per_div"),
            "freshness": str(c.acquisition.freshness),
            "trigger_observed": c.acquisition.trigger_observed,
            "group_id": c.acquisition.group_id,
            "simultaneity": c.acquisition.simultaneity,
            "quality": {k: v for k, v in c.quality.items() if k != "warnings"},
            "warnings": c.warnings,
        })

    # -- connection / identity -------------------------------------------------------------------

    def connect(self) -> dict[str, Any]:
        return self._env({"identity": self.controller.connect()})

    def disconnect(self) -> dict[str, Any]:
        self.controller.disconnect()
        return self._env({"connected": False})

    def status(self) -> dict[str, Any]:
        st = self.controller.status()
        st.update({
            "service_version": __version__,
            "started_at_utc": self.started_at,
            "captures_stored": len(self.store),
            "workflow_active": self.controller.active_workflow,
        })  # fmt: skip
        if self.controller.is_connected:
            try:
                st["acquisition"] = self.controller.acquisition_state()
            except HantekError as e:
                st["acquisition_error"] = e.to_dict()
        return self._env(st)

    def identify(self) -> dict[str, Any]:
        return self._env(self.controller.identify())

    def capabilities(self) -> dict[str, Any]:
        caps = capabilities(self.simulated)
        caps["limits"] = self.config.limits.model_dump()
        caps["settings_write_method"] = self.controller.write_method
        return self._env(caps)

    def settings(self, include_raw: bool = False) -> dict[str, Any]:
        st = self.controller.read_settings()
        d = st.as_dict(include_raw=include_raw)
        d["trigger"]["level_v"] = self.controller._level_volts(st)
        return self._env({"settings": d, "read_back_from_instrument": True})

    def health(self) -> dict[str, Any]:
        c = self.controller
        out: dict[str, Any] = {"connected": c.is_connected, "connection_state": str(c.state)}
        if c.is_connected:
            t0 = time.monotonic()
            try:
                with c.operation("health_echo") as s:
                    s.echo(b"health")
                out["echo_round_trip_ms"] = round((time.monotonic() - t0) * 1000, 2)
                out["echo_ok"] = True
            except HantekError as e:
                out["echo_ok"] = False
                out["echo_error"] = e.to_dict()
        if c.session:
            stats = c.session.stats
            out["protocol_errors"] = {"timeouts": stats.timeouts, "checksum_errors": stats.checksum_errors,
                                      "framing_errors": stats.framing_errors, "retries": stats.retries}  # fmt: skip
        try:
            du = shutil.disk_usage(self.store.root)
            out["data_dir_free_mb"] = round(du.free / 2**20, 1)
        except OSError:
            out["data_dir_free_mb"] = None
        out["capture_store"] = {"count": len(self.store), "disk_mb": round(self.store.disk_bytes / 2**20, 3),
                                "max_captures": self.store.max_captures,
                                "max_disk_mb": self.config.storage.max_disk_mb}  # fmt: skip
        out["recent_errors"] = len(c.recent_errors)
        return self._env(out)

    # -- channel / timebase / trigger ------------------------------------------------------------

    def get_channel(self, channel: int) -> dict[str, Any]:
        return self._env({"channel": self.controller.get_channel(channel), "read_back_from_instrument": True})

    def _setting(self, res: Any) -> dict[str, Any]:
        return self._env({"result": res.as_dict()})

    def enable_channel(self, channel: int, enabled: bool) -> dict[str, Any]:
        return self._setting(self.controller.set_channel_enabled(channel, enabled))

    def set_vertical_scale(self, channel: int, volts_per_div: float, snap: bool = False) -> dict[str, Any]:
        return self._setting(self.controller.set_volts_per_div(channel, volts_per_div, snap))

    def set_vertical_offset(self, channel: int, divisions: float) -> dict[str, Any]:
        return self._setting(self.controller.set_position(channel, divisions))

    def set_coupling(self, channel: int, coupling: str) -> dict[str, Any]:
        return self._setting(self.controller.set_coupling(channel, coupling))

    def set_probe(self, channel: int, attenuation: int) -> dict[str, Any]:
        return self._setting(self.controller.set_probe(channel, attenuation))

    def set_timebase(self, seconds_per_div: float, snap: bool = False) -> dict[str, Any]:
        return self._setting(self.controller.set_timebase(seconds_per_div, snap))

    def get_trigger(self) -> dict[str, Any]:
        return self._env({"trigger": self.controller.get_trigger(), "read_back_from_instrument": True})

    def set_trigger_source(self, source: str) -> dict[str, Any]:
        return self._setting(self.controller.set_trigger_source(source))

    def set_trigger_mode(self, mode: str) -> dict[str, Any]:
        return self._setting(self.controller.set_trigger_mode(mode))

    def set_trigger_slope(self, slope: str) -> dict[str, Any]:
        return self._setting(self.controller.set_trigger_slope(slope))

    def set_trigger_coupling(self, coupling: str) -> dict[str, Any]:
        return self._setting(self.controller.set_trigger_coupling(coupling))

    def set_trigger_level(self, volts: float | None = None, fifty_percent: bool = False) -> dict[str, Any]:
        if fifty_percent:
            return self._setting(self.controller.set_trigger_level_50())
        if volts is None:
            raise ParameterError("give volts or fifty_percent=true")
        return self._setting(self.controller.set_trigger_level(volts))

    def force_trigger(self) -> dict[str, Any]:
        return self._env(self.controller.force_trigger())

    # -- acquisition -----------------------------------------------------------------------------

    def start(self) -> dict[str, Any]:
        return self._env(self.controller.run())

    def stop(self) -> dict[str, Any]:
        return self._env(self.controller.stop())

    def single(self) -> dict[str, Any]:
        return self._env(self.controller.arm_single())

    def wait_for_trigger(self, timeout_s: float) -> dict[str, Any]:
        return self._env(self.controller.wait_for_trigger(timeout_s))

    def acquisition_state(self) -> dict[str, Any]:
        return self._env(self.controller.acquisition_state())

    def capture(
        self, channel: int, mode: str = "single", timeout_s: float = 5.0, force_on_timeout: bool = False,
        label: str | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        caps = self.controller.capture_channels([channel], mode, timeout_s, force_on_timeout, True, label)
        return self._env({"capture": self._store(caps)[0]})

    def capture_both(
        self, mode: str = "single", timeout_s: float = 5.0, force_on_timeout: bool = False, label: str | None = None
    ) -> dict[str, Any]:
        caps = self.controller.capture_channels([1, 2], mode, timeout_s, force_on_timeout, True, label)
        briefs = self._store(caps)
        return self._env({"captures": briefs, "group_id": caps[0].acquisition.group_id,
                          "simultaneity": caps[0].acquisition.simultaneity})  # fmt: skip

    def _resolve(self, capture_id: str | None, channel: int | None, mode: str = "single") -> WaveformCapture:
        if capture_id:
            return self.store.get(capture_id)
        if channel is None:
            raise ParameterError("give either capture_id or channel (to acquire a new record)")
        caps = self.controller.capture_channels([channel], mode, 5.0, False, True, None)
        self._store(caps)
        return caps[0]

    @staticmethod
    def _data(c: WaveformCapture, need_volts: bool, require_verified: bool) -> tuple[npt.NDArray[np.float64], str, list[str]]:
        notes: list[str] = []
        if c.voltage_v is None:
            if require_verified:
                raise ScalingUnavailableError("voltage scaling is unavailable for this capture")
            notes.append("voltage scaling unavailable: values are raw ADC counts")
            return c.raw_samples.astype(np.float64), "counts", notes
        if require_verified and c.voltage_status not in ("verified", "simulated"):
            raise ScalingUnavailableError(
                f"voltage scaling status is {c.voltage_status!r}; require_verified_scaling was requested",
                details={"voltage_scaling": c.vertical},
            )
        if c.voltage_status == "nominal_unverified" and need_volts:
            notes.append("volts use the nominal, not yet hardware-verified, scaling model")
        return c.voltage_v, "V", notes

    def _envelope(self, c: WaveformCapture, name: str, extra_warnings: list[str], details: dict[str, Any],
                  amplitude_based: bool, timing_based: bool) -> dict[str, Any]:  # fmt: skip
        warnings = list(extra_warnings)
        valid = True
        q = c.quality
        if amplitude_based and q.get("clipped"):
            valid = False
            warnings.append("record is clipped: amplitude results are invalid")
        if q.get("flat"):
            valid = False
            warnings.append("record is flat: no measurable signal")
        if c.acquisition.settings_changed_during_capture:
            valid = False
            warnings.append("settings changed during capture")
        if q.get("identical_to_previous") and c.acquisition.freshness == Freshness.RUNNING_SNAPSHOT:
            valid = False
            warnings.append("record identical to previous capture while running: stale buffer suspected")
        if timing_based and c.time_status == "unavailable":
            valid = False
            warnings.append("time scaling unavailable: timing results are in samples")
        if c.simulated:
            warnings.insert(0, "SIMULATED DATA")
        return self._env({
            "measurement": name,
            "valid": valid,
            "capture_id": c.capture_id,
            "channel": c.channel,
            "sample_count": c.sample_count,
            "sample_interval_s": c.sample_interval_s,
            "voltage_scaling_status": c.voltage_status,
            "time_scaling_status": c.time_status,
            "freshness": str(c.acquisition.freshness),
            "results": details,
            "warnings": warnings,
        })

    def measure(
        self,
        kind: str,
        capture_id: str | None = None,
        channel: int | None = None,
        mode: str = "single",
        require_verified_scaling: bool = False,
    ) -> dict[str, Any]:
        if kind not in MEASUREMENTS:
            raise ParameterError(f"measurement must be one of {list(MEASUREMENTS)}")
        c = self._resolve(capture_id, channel, mode)
        y, unit, notes = self._data(c, True, require_verified_scaling)
        dt = c.sample_interval_s
        tunit = "s" if dt is not None else "samples"
        amp, tim = False, False
        try:
            if kind in ("voltage", "rms", "peak_to_peak"):
                amp = True
                s = meas.voltage_stats(y)
                if kind == "rms":
                    d: dict[str, Any] = {"rms": s["rms"], "ac_rms": s["ac_rms"], "mean": s["mean"], "unit": unit,
                                         "method": "sqrt(mean(v^2)) over the whole record; ac_rms = std"}  # fmt: skip
                elif kind == "peak_to_peak":
                    d = {"peak_to_peak": s["peak_to_peak"], "maximum": s["maximum"], "minimum": s["minimum"],
                         "unit": unit, "method": "max - min over the whole record (includes noise peaks)"}  # fmt: skip
                else:
                    d = {**s, "unit": unit, "method": "statistics over the whole record"}
                lsb = c.vertical.get("volts_per_count")
                if lsb and unit == "V":
                    d["resolution_v"] = lsb
            elif kind in ("frequency", "period"):
                tim = True
                d = meas.frequency(y, dt, c.simulated)
                d["unit"] = "Hz" if kind == "frequency" else tunit
                tc = c.trigger_settings.get("counter_frequency_hz")
                if tc:
                    d["instrument_trigger_counter_hz"] = tc
                    d["instrument_counter_note"] = "the scope's own counter on the trigger source (scale unverified)"
            elif kind == "duty_cycle":
                tim = True
                d = meas.duty_cycle(y, dt)
                d["unit"] = "%"
            elif kind in ("rise_time", "fall_time"):
                tim = True
                d = meas.transition_time(y, dt, rising=kind == "rise_time")
            else:  # noise
                amp = True
                d = meas.noise_estimate(y)
                d["unit"] = unit
                lsb = c.vertical.get("volts_per_count") if unit == "V" else 1.0
                if lsb:
                    d["lsb"] = lsb
                    if d["noise_rms"] < 0.5 * lsb:
                        notes.append("noise is below half an ADC step: quantisation-limited; decrease V/div")
                s = meas.voltage_stats(y)
                d["signal_ac_rms"] = s["ac_rms"]
                d["snr_db_est"] = meas.snr_db(s["ac_rms"], d["noise_rms"])
        except meas.MeasurementError as e:
            return self._envelope(c, kind, [*notes, str(e)], {"error": str(e)}, amp, tim) | {"valid": False}
        warnings = notes + ([d["warning"]] if "warning" in d else []) + list(d.get("warnings", []))
        return self._envelope(c, kind, warnings, d, amp, tim)

    def _pair(self, capture_a: str | None, capture_b: str | None, mode: str) -> tuple[WaveformCapture, WaveformCapture, list[str]]:
        notes: list[str] = []
        if capture_a and capture_b:
            a, b = self.store.get(capture_a), self.store.get(capture_b)
        elif not capture_a and not capture_b:
            caps = self.controller.capture_channels([1, 2], mode, 5.0, False, True, None)
            self._store(caps)
            a, b = caps
        else:
            raise ParameterError("give both capture ids or neither (to capture CH1 and CH2 together)")
        ga, gb = a.acquisition.group_id, b.acquisition.group_id
        if not ga or ga != gb:
            notes.append(
                "captures are not from the same two-channel acquisition: inter-channel timing is meaningless "
                "unless both were triggered identically"
            )
        elif a.acquisition.simultaneity and a.acquisition.simultaneity.startswith("not_guaranteed"):
            notes.append("captures were downloaded while running: simultaneity is not guaranteed")
        if a.sample_interval_s != b.sample_interval_s or a.sample_count != b.sample_count:
            notes.append("records differ in length or sample interval")
        return a, b, notes

    def phase(self, capture_a: str | None = None, capture_b: str | None = None, mode: str = "single") -> dict[str, Any]:
        a, b, notes = self._pair(capture_a, capture_b, mode)
        if a.sample_interval_s is None:
            raise ScalingUnavailableError("time scaling unavailable")
        ya, _, _ = self._data(a, False, False)
        yb, _, _ = self._data(b, False, False)
        try:
            d = timing.phase_difference(ya, yb, a.sample_interval_s)
        except meas.MeasurementError as e:
            d, notes = {"error": str(e)}, [*notes, str(e)]
        out = self._envelope(a, "phase", notes, d, False, True)
        out["capture_id_b"] = b.capture_id
        out["valid"] = out["valid"] and "error" not in d and not any("not from the same" in n for n in notes)
        return out

    def channel_delay(
        self, capture_a: str | None = None, capture_b: str | None = None, slope: str = "rising", mode: str = "single"
    ) -> dict[str, Any]:
        a, b, notes = self._pair(capture_a, capture_b, mode)
        ya, _, _ = self._data(a, False, False)
        yb, _, _ = self._data(b, False, False)
        try:
            d = timing.edge_delay(ya, yb, a.time_s, b.time_s, slope)
            if a.sample_interval_s:
                d["resolution_s"] = a.sample_interval_s
        except meas.MeasurementError as e:
            d, notes = {"error": str(e)}, [*notes, str(e)]
        out = self._envelope(a, "channel_delay", notes, d, False, True)
        out["capture_id_b"] = b.capture_id
        out["valid"] = out["valid"] and "error" not in d and not any("not from the same" in n for n in notes)
        return out

    # -- signal processing -----------------------------------------------------------------------

    def fft(
        self, capture_id: str | None = None, channel: int | None = None, window: str = "hann", detrend: str = "mean",
        scaling: str = "peak", max_points: int = 1000, n_peaks: int = 8,
    ) -> dict[str, Any]:  # fmt: skip
        c = self._resolve(capture_id, channel)
        if c.sample_interval_s is None:
            raise ScalingUnavailableError("time scaling unavailable: cannot compute a frequency axis")
        y, unit, notes = self._data(c, True, False)
        max_points = min(max_points, self.config.limits.max_response_samples)
        if scaling not in ("peak", "rms"):
            raise ParameterError("scaling must be 'peak' or 'rms'")
        try:
            sp = fftmod.spectrum(y, c.sample_interval_s, window, detrend, scaling)  # type: ignore[arg-type]
        except meas.MeasurementError as e:
            raise ParameterError(str(e)) from e
        peaks = fftmod.find_peaks(sp, n_peaks)
        f, a = fftmod.decimate_spectrum(sp, max_points)
        harm = fftmod.harmonic_analysis(sp, peaks[0]["frequency_hz"]) if peaks else None
        details = {
            "unit": f"{unit} ({scaling})", "window": window, "detrend": detrend, "bin_width_hz": sp["bin_width_hz"],
            "enbw_hz": sp["enbw_hz"], "nyquist_hz": sp["nyquist_hz"], "fft_length": sp["n"], "peaks": peaks,
            "harmonics": harm, "spectrum_points": len(f), "spectrum_decimated": len(f) < sp["amplitude"].size,
            "freqs_hz": f, "amplitude": a,
            "limitations": [
                "frequency resolution is limited to the bin width (peak interpolation refines tone frequencies)",
                "the instrument's analog bandwidth is 100 MHz; components near/above Nyquist may be aliased",
            ],
        }  # fmt: skip
        return self._envelope(c, "fft", notes, details, True, True)

    def spectrum_peaks(self, capture_id: str | None = None, channel: int | None = None, n_peaks: int = 10,
                       window: str = "hann", min_relative_db: float = -60.0) -> dict[str, Any]:  # fmt: skip
        c = self._resolve(capture_id, channel)
        if c.sample_interval_s is None:
            raise ScalingUnavailableError("time scaling unavailable")
        y, unit, notes = self._data(c, True, False)
        sp = fftmod.spectrum(y, c.sample_interval_s, window, "mean", "peak")
        peaks = fftmod.find_peaks(sp, min(n_peaks, 50), min_relative_db)
        return self._envelope(c, "spectrum_peaks", notes,
                              {"unit": f"{unit} (peak)", "bin_width_hz": sp["bin_width_hz"], "peaks": peaks}, True, True)  # fmt: skip

    def cross_correlation(self, capture_a: str | None = None, capture_b: str | None = None,
                          max_lag_s: float | None = None) -> dict[str, Any]:  # fmt: skip
        a, b, notes = self._pair(capture_a, capture_b, "single")
        if a.sample_interval_s is None:
            raise ScalingUnavailableError("time scaling unavailable")
        ya, _, _ = self._data(a, False, False)
        yb, _, _ = self._data(b, False, False)
        d = timing.cross_correlation(ya, yb, a.sample_interval_s, max_lag_s)
        out = self._envelope(a, "cross_correlation", notes, d, False, True)
        out["capture_id_b"] = b.capture_id
        return out

    def compare(self, capture_a: str, capture_b: str) -> dict[str, Any]:
        a, b = self.store.get(capture_a), self.store.get(capture_b)
        ya, ua, na = self._data(a, True, False)
        yb, ub, nb = self._data(b, True, False)
        if ua != ub:
            raise ScalingUnavailableError("one capture is in volts and the other in counts")
        d = timing.compare_records(ya, yb, a.sample_interval_s, b.sample_interval_s)
        d["unit"] = ua
        d["settings_differences"] = {
            k: [a.channel_settings.get(k), b.channel_settings.get(k)]
            for k in ("volts_per_div_at_probe_tip", "coupling", "probe_attenuation", "position_counts")
            if a.channel_settings.get(k) != b.channel_settings.get(k)
        }
        out = self._envelope(a, "compare", na + nb, d, True, False)
        out["capture_id_b"] = b.capture_id
        return out

    def edges(self, capture_id: str | None = None, channel: int | None = None, level: float | None = None,
              max_edges: int = 200) -> dict[str, Any]:  # fmt: skip
        c = self._resolve(capture_id, channel)
        y, unit, notes = self._data(c, True, False)
        d = timing.detect_edges(y, c.time_s, level, None, min(max_edges, self.config.limits.max_list_items))
        d["unit"] = unit
        return self._envelope(c, "edges", notes, d, False, True)

    def pulses(self, capture_id: str | None = None, channel: int | None = None, polarity: str = "positive",
               level: float | None = None, min_width_s: float | None = None, max_width_s: float | None = None,
               max_pulses: int = 200) -> dict[str, Any]:  # fmt: skip
        c = self._resolve(capture_id, channel)
        y, unit, notes = self._data(c, True, False)
        try:
            d = timing.detect_pulses(y, c.time_s, polarity, level, min_width_s, max_width_s,
                                     min(max_pulses, self.config.limits.max_list_items))  # fmt: skip
        except meas.MeasurementError as e:
            raise ParameterError(str(e)) from e
        d["unit"] = unit
        return self._envelope(c, "pulses", notes, d, False, True)

    # -- data access -----------------------------------------------------------------------------

    def list_captures(self, limit: int = 50, channel: int | None = None, label: str | None = None) -> dict[str, Any]:
        limit = max(1, min(limit, self.config.limits.max_list_items))
        return self._env({"captures": self.store.list(limit, channel, label), "total": len(self.store)})

    def capture_metadata(self, capture_id: str) -> dict[str, Any]:
        return _f(self.store.get(capture_id).metadata())  # type: ignore[no-any-return]

    def get_samples(self, capture_id: str, max_points: int = 1000, method: str = "minmax") -> dict[str, Any]:
        c = self.store.get(capture_id)
        limit = self.config.limits.max_response_samples
        if not 2 <= max_points <= limit:
            raise LimitExceededError(f"max_points must be within 2..{limit}", details={"limit": limit})
        if method not in ("minmax", "stride", "mean"):
            raise ParameterError("method must be minmax, stride or mean")
        n = c.sample_count
        idx_t, raw, volts = _decimate(c, max_points, method)
        return self._env({
            "capture_id": c.capture_id, "channel": c.channel, "total_samples": n, "returned_points": len(idx_t),
            "decimated": len(idx_t) < n, "decimation_method": method if len(idx_t) < n else "none",
            "time_s": idx_t, "raw_counts": raw, "voltage_v": volts,
            "voltage_scaling_status": c.voltage_status, "time_scaling_status": c.time_status,
            "note": "minmax keeps each block's min and max (envelope preserving); decimated data must not be "
                    "used for timing measurements - use the measurement tools on the full record",
        })  # fmt: skip

    def get_sample_window(self, capture_id: str, start_index: int | None = None, count: int = 500,
                          start_time_s: float | None = None) -> dict[str, Any]:  # fmt: skip
        c = self.store.get(capture_id)
        limit = self.config.limits.max_response_samples
        if not 1 <= count <= limit:
            raise LimitExceededError(f"count must be within 1..{limit}", details={"limit": limit})
        if start_time_s is not None:
            start = int(np.searchsorted(c.time_s, start_time_s))
        elif start_index is not None:
            start = start_index
        else:
            start = 0
        if not 0 <= start < c.sample_count:
            raise ParameterError(f"start is outside the record (0..{c.sample_count - 1})")
        end = min(c.sample_count, start + count)
        return self._env({
            "capture_id": c.capture_id, "channel": c.channel, "start_index": start, "end_index": end,
            "total_samples": c.sample_count, "time_s": c.time_s[start:end], "raw_counts": c.raw_samples[start:end],
            "voltage_v": None if c.voltage_v is None else c.voltage_v[start:end],
            "voltage_scaling_status": c.voltage_status, "time_scaling_status": c.time_status,
        })  # fmt: skip

    def export(self, capture_id: str, fmt: str, name: str | None = None, overwrite: bool = False) -> dict[str, Any]:
        if fmt == "csv":
            p = self.store.export_csv(capture_id, name, overwrite)
        elif fmt == "npz":
            p = self.store.export_npz(capture_id, name, overwrite)
        else:
            raise ParameterError("format must be csv or npz")
        return self._env({"capture_id": capture_id, "format": fmt, "path": str(p), "bytes": p.stat().st_size,
                          "export_directory": str(self.store.export_dir)})  # fmt: skip

    def delete_capture(self, capture_id: str) -> dict[str, Any]:
        self.store.delete(capture_id)
        return self._env({"deleted": capture_id})

    def screenshot(self) -> tuple[bytes, dict[str, Any]]:
        rgb, fmt = self.controller.screenshot()
        png = encode_png(rgb)
        shots = self.store.root / "screenshots"
        shots.mkdir(parents=True, exist_ok=True)
        name = f"{'sim' if self.simulated else 'hw'}-screenshot-{utcnow():%Y%m%d-%H%M%S-%f}.png"
        path = shots / name
        path.write_bytes(png)
        self._prune_dir(shots, keep=50)
        return png, self._env({"path": str(path), "format": fmt, "width": int(rgb.shape[1]),
                               "height": int(rgb.shape[0]), "bytes": len(png),
                               "note": "screen image only: not waveform data; use scope_capture for samples"})  # fmt: skip

    @staticmethod
    def _prune_dir(d: Path, keep: int) -> None:
        files = sorted(d.glob("*.png"), key=lambda p: p.stat().st_mtime)
        for p in files[:-keep]:
            p.unlink(missing_ok=True)

    # -- automation ------------------------------------------------------------------------------

    def abort(self) -> dict[str, Any]:
        busy, wf = self.controller.busy_operation, self.controller.active_workflow
        self.controller.request_abort()
        return self._env({"abort_requested": True, "workflow_active": wf, "busy_operation": busy,
                          "note": "software activity stops at its next check point; an armed single "
                                  "acquisition on the instrument itself stays armed"})  # fmt: skip

    class _Workflow:
        def __init__(self, svc: ScopeService, name: str) -> None:
            self.svc, self.name = svc, name
            self.t0 = time.monotonic()

        def __enter__(self) -> ScopeService._Workflow:
            self.svc.controller.begin_workflow(self.name)
            return self

        def __exit__(self, *exc: object) -> None:
            self.svc.controller.end_workflow()

        def check(self, limit_s: float | None = None) -> None:
            self.svc.controller.check_abort()
            lim = min(limit_s or math.inf, self.svc.config.limits.max_workflow_duration_s)
            if time.monotonic() - self.t0 > lim:
                raise LimitExceededError(f"workflow exceeded its {lim:.0f} s duration limit")

    def find_signal(self, mode: str = "stop_and_read") -> dict[str, Any]:
        with self._Workflow(self, "find_signal"):
            st = self.controller.read_settings()
            out = []
            for ch in (1, 2):
                if not st.channels[ch].enabled:
                    out.append({"channel": ch, "enabled": False, "activity": "unknown (channel disabled)"})
                    continue
                cap = self.controller.capture_channels([ch], mode, 5.0, True, True, "find_signal")[0]
                self.store.add(cap)
                q = cap.quality
                entry: dict[str, Any] = {"channel": ch, "enabled": True, "capture_id": cap.capture_id,
                                         "raw_peak_to_peak_counts": q["raw_peak_to_peak_counts"],
                                         "clipped": q["clipped"], "flat": q["flat"]}  # fmt: skip
                y, unit, _ = self._data(cap, True, False)
                entry["peak_to_peak"] = float(np.ptp(y))
                entry["mean"] = float(np.mean(y))
                entry["unit"] = unit
                try:
                    entry["frequency_hz_est"] = meas.frequency(y, cap.sample_interval_s).get("frequency_hz")
                except meas.MeasurementError:
                    entry["frequency_hz_est"] = None
                entry["activity"] = "none/flat" if q["flat"] else ("clipped" if q["clipped"] else "signal present")
                out.append(entry)
            return self._env({"channels": out, "note": "settings were not changed"})

    def autoset(self, channel: int = 1, method: str = "software", max_iterations: int = 6,
                target_cycles: float = 4.0) -> dict[str, Any]:  # fmt: skip
        if method == "instrument":
            return self._env(self.controller.front_panel_autoset())
        if method != "software":
            raise ParameterError("method must be 'software' or 'instrument'")
        if channel not in (1, 2):
            raise ParameterError("channel must be 1 or 2")
        max_iterations = max(1, min(max_iterations, 12))
        changes: list[dict[str, Any]] = []
        history: list[dict[str, Any]] = []
        with self._Workflow(self, "autoset") as wf:
            ctl = self.controller
            if not ctl.read_settings().channels[channel].enabled:
                changes.append(ctl.set_channel_enabled(channel, True).as_dict())
            for it in range(max_iterations):
                wf.check()
                cap = ctl.capture_channels([channel], "stop_and_read", 5.0, True, True, "autoset")[0]
                self.store.add(cap)
                st = ctl.cached_settings or ctl.read_settings()
                ch = st.channels[channel]
                probe = ch.probe or 1
                q = cap.quality
                step: dict[str, Any] = {"iteration": it, "capture_id": cap.capture_id,
                                        "raw_ptp": q["raw_peak_to_peak_counts"], "clipped": q["clipped"]}  # fmt: skip
                history.append(step)
                vb = ch.vb_index
                lo_vb, hi_vb = DSO5102P_VB_RANGE
                changed = False
                # vertical
                if q["clipped"] and vb < hi_vb:
                    new_vb = min(hi_vb, vb + 2)
                    changes.append(ctl.set_volts_per_div(channel, VOLTS_PER_DIV[new_vb] * probe).as_dict())
                    changed = True
                elif cap.voltage_v is not None and not q["flat"]:
                    vpp = float(np.ptp(cap.voltage_v))
                    want = vpp / 6.0 / probe  # ~6 divisions peak-to-peak
                    cands = [i for i in range(lo_vb, hi_vb + 1) if VOLTS_PER_DIV[i] >= want]
                    new_vb = cands[0] if cands else hi_vb
                    if new_vb != vb:
                        changes.append(ctl.set_volts_per_div(channel, VOLTS_PER_DIV[new_vb] * probe).as_dict())
                        changed = True
                    mid = (float(cap.voltage_v.max()) + float(cap.voltage_v.min())) / 2
                    mid_div = mid / (VOLTS_PER_DIV[new_vb] * probe)
                    pos_div = ch.position_counts / ctl.calibration.counts_per_div
                    if abs(mid_div + pos_div) > 2.0 and abs(mid_div) <= 8:
                        changes.append(ctl.set_position(channel, round(-mid_div, 1)).as_dict())
                        changed = True
                elif q["flat"] and vb > lo_vb:
                    changes.append(ctl.set_volts_per_div(channel, VOLTS_PER_DIV[max(lo_vb, vb - 3)] * probe).as_dict())
                    changed = True
                # horizontal (only once vertical is sane)
                if not changed and not q["flat"] and cap.sample_interval_s:
                    y = cap.voltage_v if cap.voltage_v is not None else cap.raw_samples.astype(float)
                    tb = st.horizontal.tb_index
                    span = cap.horizontal.get("span_divisions") or 20.0
                    try:
                        fr = meas.frequency(y, cap.sample_interval_s)
                        f0 = fr["frequency_hz"]
                        step["frequency_hz"] = f0
                        want_tb = target_cycles / f0 / span
                        lo_tb, hi_tb = DSO5102P_TB_RANGE
                        new_tb = min(range(lo_tb, hi_tb + 1), key=lambda i: abs(math.log(TIMEBASE_S_PER_DIV[i] / want_tb)))
                        if new_tb != tb:
                            changes.append(ctl.set_timebase(TIMEBASE_S_PER_DIV[new_tb]).as_dict())
                            changed = True
                    except meas.MeasurementError:
                        lo_tb, hi_tb = DSO5102P_TB_RANGE
                        if tb < hi_tb:  # fewer than one cycle: slow down by ~10x
                            changes.append(ctl.set_timebase(TIMEBASE_S_PER_DIV[min(hi_tb, tb + 3)]).as_dict())
                            changed = True
                if not changed:
                    break
            # trigger on the channel at mid-level
            wf.check()
            trig_notes: list[str] = []
            try:
                tr = ctl.get_trigger()
                if tr["type"] == "edge":
                    if tr["source"] != f"CH{channel}":
                        changes.append(ctl.set_trigger_source(f"CH{channel}").as_dict())
                    final = ctl.capture_channels([channel], "stop_and_read", 5.0, True, True, "autoset")[0]
                    self.store.add(final)
                    if final.voltage_v is not None and not final.quality["flat"]:
                        mid = (float(final.voltage_v.max()) + float(final.voltage_v.min())) / 2
                        changes.append(ctl.set_trigger_level(mid).as_dict())
                else:
                    trig_notes.append("trigger type is not edge: trigger left unchanged")
            except HantekError as e:
                trig_notes.append(f"trigger adjustment failed: {e}")
            final = ctl.capture_channels([channel], "stop_and_read", 5.0, True, True, "autoset_final")[0]
            self.store.add(final)
        return self._env({
            "method": "software_assisted_autoset",
            "note": "settings chosen from measured data; every change below was confirmed by instrument read-back",
            "changes": changes, "iterations": history, "trigger_notes": trig_notes,
            "final_capture": self.capture_brief(final),
            "final_settings": self.controller.get_channel(channel),
        })  # fmt: skip

    def _metric(self, cap: WaveformCapture, metric: str) -> float | None:
        y, _, _ = self._data(cap, True, False)
        dt = cap.sample_interval_s
        try:
            if metric == "vpp":
                return float(np.ptp(y))
            if metric == "vmax":
                return float(y.max())
            if metric == "vmin":
                return float(y.min())
            if metric == "mean":
                return float(y.mean())
            if metric == "rms":
                return float(np.sqrt(np.mean(y * y)))
            if metric == "frequency":
                return float(meas.frequency(y, dt).get("frequency_hz", math.nan))
            if metric == "duty_cycle":
                return float(meas.duty_cycle(y, dt)["duty_cycle_percent"])
            if metric == "edge_count":
                return float(timing.detect_edges(y, cap.time_s, max_edges=1)["total_edges"])
            if metric in ("pulse_width_max", "pulse_width_min"):
                ws = timing.detect_pulses(y, cap.time_s)["width_stats_s"]
                return None if ws is None else float(ws["max" if metric.endswith("max") else "min"])
        except meas.MeasurementError:
            return None
        raise ParameterError(f"unknown metric {metric!r}; choose from {list(CONDITION_METRICS)}")

    def measure_repeatedly(self, channel: int, metrics: list[str], count: int = 10, interval_s: float = 0.0,
                           mode: str = "single", timeout_s: float = 5.0, keep_captures: bool = False) -> dict[str, Any]:  # fmt: skip
        lim = self.config.limits.max_repeat_count
        if not 1 <= count <= lim:
            raise LimitExceededError(f"count must be within 1..{lim}", details={"limit": lim})
        if not 0 <= interval_s <= 60:
            raise ParameterError("interval_s must be within 0..60")
        for m in metrics:
            if m not in CONDITION_METRICS:
                raise ParameterError(f"unknown metric {m!r}; choose from {list(CONDITION_METRICS)}")
        values: dict[str, list[float | None]] = {m: [] for m in metrics}
        ids: list[str] = []
        stale = 0
        aborted = None
        with self._Workflow(self, "measure_repeatedly") as wf:
            for i in range(count):
                try:
                    wf.check()
                    cap = self.controller.capture_channels([channel], mode, timeout_s, False, True, "repeat")[0]
                except (AbortedError, LimitExceededError) as e:
                    aborted = e.to_dict()
                    break
                if cap.quality.get("identical_to_previous"):
                    stale += 1
                if keep_captures or i == count - 1:
                    self.store.add(cap)
                    ids.append(cap.capture_id)
                for m in metrics:
                    values[m].append(self._metric(cap, m))
                if interval_s and i < count - 1:
                    time.sleep(interval_s)
        stats = {}
        for m, vs in values.items():
            arr = np.array([v for v in vs if v is not None], dtype=float)
            stats[m] = None if not arr.size else {"mean": arr.mean(), "std": arr.std(), "min": arr.min(),
                                                  "max": arr.max(), "n": int(arr.size),
                                                  "failed": len(vs) - int(arr.size)}  # fmt: skip
        return self._env({"channel": channel, "iterations": len(next(iter(values.values()), [])),
                          "statistics": stats, "values": values, "stored_capture_ids": ids,
                          "stale_records_detected": stale, "aborted": aborted,
                          "units": "V for amplitude metrics when scaling is available (else counts); Hz; %; s"})  # fmt: skip

    def acquire_until(self, channel: int, metric: str, op: str, threshold: float, max_attempts: int = 20,
                      timeout_s: float = 60.0, mode: str = "single", capture_timeout_s: float = 5.0) -> dict[str, Any]:  # fmt: skip
        if metric not in CONDITION_METRICS:
            raise ParameterError(f"metric must be one of {list(CONDITION_METRICS)}")
        if op not in _OPS:
            raise ParameterError(f"operator must be one of {list(_OPS)}")
        lim = self.config.limits.max_repeat_count
        if not 1 <= max_attempts <= lim:
            raise LimitExceededError(f"max_attempts must be within 1..{lim}")
        history: list[float | None] = []
        with self._Workflow(self, "acquire_until") as wf:
            for attempt in range(1, max_attempts + 1):
                try:
                    wf.check(timeout_s)
                except LimitExceededError:
                    break
                try:
                    cap = self.controller.capture_channels([channel], mode, capture_timeout_s, False, True, "acquire_until")[0]
                except HantekError as e:
                    if e.code == "TRIGGER_TIMEOUT":
                        history.append(None)
                        continue
                    raise
                v = self._metric(cap, metric)
                history.append(v)
                if v is not None and _OPS[op](v, threshold):
                    self.store.add(cap)
                    return self._env({"condition_met": True, "attempts": attempt, "value": v,
                                      "capture": self.capture_brief(cap), "history": history[-50:]})  # fmt: skip
        return self._env({"condition_met": False, "attempts": len(history), "history": history[-50:],
                          "note": "condition not met within the attempt/time limits"})  # fmt: skip

    def run_plan(self, steps: list[dict[str, Any]], stop_on_error: bool = True) -> dict[str, Any]:
        lim = self.config.limits.max_plan_steps
        if not 1 <= len(steps) <= lim:
            raise LimitExceededError(f"a plan must have 1..{lim} steps", details={"limit": lim})
        results: list[dict[str, Any]] = []
        last_capture: str | None = None
        last_pair: tuple[str, str] | None = None
        with self._Workflow(self, "measurement_plan") as wf:
            for i, step in enumerate(steps):
                action = step.get("action")
                args = dict(step.get("args") or {})
                entry: dict[str, Any] = {"step": i, "action": action}
                try:
                    wf.check()
                    if "capture_id" in args and args["capture_id"] == "$last":
                        args["capture_id"] = last_capture
                    r = self._plan_step(str(action), args, last_pair)
                    if isinstance(r, dict):
                        cap = r.get("capture")
                        if isinstance(cap, dict):
                            last_capture = cap.get("capture_id")
                        caps = r.get("captures")
                        if isinstance(caps, list) and len(caps) == 2:
                            last_pair = (caps[0]["capture_id"], caps[1]["capture_id"])
                    entry.update({"ok": True, "result": r})
                except (AbortedError, LimitExceededError) as e:
                    entry.update({"ok": False, "error": e.to_dict()})
                    results.append(entry)
                    break
                except HantekError as e:
                    entry.update({"ok": False, "error": e.to_dict()})
                    results.append(entry)
                    if stop_on_error:
                        break
                    continue
                results.append(entry)
        return self._env({"steps_requested": len(steps), "steps_executed": len(results),
                          "all_ok": all(r["ok"] for r in results) and len(results) == len(steps),
                          "results": results})  # fmt: skip

    def _plan_step(self, action: str, a: dict[str, Any], last_pair: tuple[str, str] | None) -> Any:
        table: dict[str, Callable[..., Any]] = {
            "enable_channel": self.enable_channel, "set_vertical_scale": self.set_vertical_scale,
            "set_vertical_offset": self.set_vertical_offset, "set_coupling": self.set_coupling,
            "set_probe_attenuation": self.set_probe, "set_timebase": self.set_timebase,
            "set_trigger_source": self.set_trigger_source, "set_trigger_mode": self.set_trigger_mode,
            "set_trigger_slope": self.set_trigger_slope, "set_trigger_level": self.set_trigger_level,
            "capture": self.capture, "capture_both_channels": self.capture_both, "measure": self.measure,
            "fft": self.fft, "start_acquisition": self.start, "stop_acquisition": self.stop,
        }  # fmt: skip
        if action == "wait":
            secs = float(a.get("seconds", 0))
            if not 0 <= secs <= 10:
                raise ParameterError("wait seconds must be within 0..10")
            end = time.monotonic() + secs
            while time.monotonic() < end:
                self.controller.check_abort()
                time.sleep(min(0.05, end - time.monotonic()) if end > time.monotonic() else 0)
            return {"waited_s": secs}
        if action in ("phase", "channel_delay"):
            if last_pair and "capture_a" not in a:
                a = {"capture_a": last_pair[0], "capture_b": last_pair[1], **a}
            return self.phase(**a) if action == "phase" else self.channel_delay(**a)
        fn = table.get(action)
        if fn is None:
            raise ParameterError(f"unknown plan action {action!r}", details={"allowed": [*table, "wait", "phase", "channel_delay"]})
        try:
            return fn(**a)
        except TypeError as e:
            raise ParameterError(f"invalid arguments for {action}: {e}") from e

    # -- diagnostics -----------------------------------------------------------------------------

    def usb_diagnostics(self) -> dict[str, Any]:
        from hantek_mcp.transport.usb import usb_diagnostics

        d: dict[str, Any] = {"backend_in_use": self.config.backend}
        if self.config.backend == "simulator":
            d["note"] = "simulator backend: no USB traffic; USB enumeration below is informational"
        d["usb"] = usb_diagnostics(self.config.usb.vendor_id, self.config.usb.product_id)
        if self.controller.transport is not None:
            d["active_transport"] = self.controller.transport.info().as_dict()
        return self._env(d)

    def protocol_diagnostics(self) -> dict[str, Any]:
        return self._env(self.controller.protocol_diagnostics())

    def recent_errors(self, limit: int = 20) -> dict[str, Any]:
        errs = list(self.controller.recent_errors)[-max(1, min(limit, 100)):]
        return self._env({"errors": errs, "total_recorded": len(self.controller.recent_errors)})

    def self_test(self, channel: int = 1, expected_frequency_hz: float = 1000.0, expected_vpp: float = 5.0,
                  frequency_tolerance_pct: float = 2.0, vpp_tolerance_pct: float = 10.0,
                  mode: str = "single") -> dict[str, Any]:  # fmt: skip
        """Probe-compensation self-test: connect the probe to PROBE COMP (~5 V, 1 kHz square)."""
        checks: list[dict[str, Any]] = []
        st = self.controller.read_settings()
        ch = st.channels[channel]
        checks.append({"check": "channel_enabled", "pass": ch.enabled})
        try:
            cap = self.controller.capture_channels([channel], mode, 5.0, True, True, "self_test")[0]
        except HantekError as e:
            return self._env({"pass": False, "checks": checks, "error": e.to_dict()})
        self.store.add(cap)
        q = cap.quality
        checks.append({"check": "not_clipped", "pass": not q["clipped"]})
        checks.append({"check": "signal_present", "pass": not q["flat"]})
        checks.append({"check": "fresh_record", "pass": cap.acquisition.freshness == Freshness.NEW_TRIGGER_CONFIRMED,
                       "value": str(cap.acquisition.freshness)})  # fmt: skip
        suggestions: dict[str, Any] = {}
        y, unit, _ = self._data(cap, True, False)
        try:
            f = meas.frequency(y, cap.sample_interval_s)["frequency_hz"]
            err = 100 * (f - expected_frequency_hz) / expected_frequency_hz
            checks.append({"check": "frequency", "pass": abs(err) <= frequency_tolerance_pct, "measured_hz": f,
                           "expected_hz": expected_frequency_hz, "error_pct": err,
                           "tolerance_pct": frequency_tolerance_pct})  # fmt: skip
            spd = cap.horizontal.get("samples_per_div")
            if spd:
                suggestions["samples_per_div_if_frequency_error_is_scaling"] = spd * f / expected_frequency_hz
        except meas.MeasurementError as e:
            checks.append({"check": "frequency", "pass": False, "error": str(e)})
        if unit == "V":
            lv = meas.levels(y)
            vpp = lv.amplitude
            err = 100 * (vpp - expected_vpp) / expected_vpp
            checks.append({"check": "amplitude_top_minus_base", "pass": abs(err) <= vpp_tolerance_pct,
                           "measured_v": vpp, "expected_v": expected_vpp, "error_pct": err,
                           "tolerance_pct": vpp_tolerance_pct})  # fmt: skip
            if vpp > 0:
                suggestions["counts_per_div_if_amplitude_error_is_scaling"] = (
                    self.controller.calibration.counts_per_div * vpp / expected_vpp
                )
        else:
            checks.append({"check": "amplitude", "pass": False, "error": "voltage scaling unavailable"})
        ok = all(c["pass"] for c in checks)
        return self._env({
            "pass": ok, "checks": checks, "capture": self.capture_brief(cap),
            "calibration_suggestions": suggestions,
            "note": "suggestions are NOT applied automatically. The probe-comp output's own accuracy is not "
                    "specified by Hantek; confirm against a calibrated reference before changing scaling.",
        })  # fmt: skip


def _decimate(c: WaveformCapture, max_points: int, method: str) -> tuple[list[float], list[float], list[float] | None]:
    n = c.sample_count
    t, raw, v = c.time_s, c.raw_samples.astype(np.float64), c.voltage_v
    if n <= max_points:
        return t.tolist(), raw.tolist(), None if v is None else v.tolist()
    if method == "stride":
        idx = np.linspace(0, n - 1, max_points).astype(int)
        return t[idx].tolist(), raw[idx].tolist(), None if v is None else v[idx].tolist()
    if method == "mean":
        b = math.ceil(n / max_points)
        m = n // b * b
        return (t[:m].reshape(-1, b).mean(1).tolist(), raw[:m].reshape(-1, b).mean(1).tolist(),
                None if v is None else v[:m].reshape(-1, b).mean(1).tolist())  # fmt: skip
    b = math.ceil(n / (max_points // 2))
    m = n // b * b
    rb = raw[:m].reshape(-1, b)
    lo_i = rb.argmin(1) + np.arange(rb.shape[0]) * b
    hi_i = rb.argmax(1) + np.arange(rb.shape[0]) * b
    idx = np.sort(np.stack([lo_i, hi_i], 1), axis=1).ravel()
    return t[idx].tolist(), raw[idx].tolist(), None if v is None else v[idx].tolist()
