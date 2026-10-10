"""Waveform measurements computed from sample data.

All functions take a sample array ``y`` (volts, or raw ADC counts when voltage
scaling is unavailable) and a sample interval ``dt`` (seconds, or ``None`` when
time scaling is unavailable). Results are plain dicts with explicit units,
method descriptions and limitations, so callers never have to guess how a
number was obtained.

Edge timing uses a Schmitt-trigger crossing detector (hysteresis) to reject
noise chatter, with linear interpolation between samples at the reference level.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]

#: DSO5102P rise time at the BNC (manual: < 3.5 ns). Edges faster than ~2x this are bandwidth-limited.
INSTRUMENT_RISE_TIME_S = 3.5e-9
#: Timebase accuracy from the DSO5000P manual (±50 ppm).
TIMEBASE_ACCURACY_PPM = 50.0


class MeasurementError(ValueError):
    """The record does not support the requested measurement (e.g. fewer than one cycle)."""


@dataclass(frozen=True)
class Levels:
    top: float
    base: float
    method: str

    @property
    def amplitude(self) -> float:
        return self.top - self.base

    def at(self, fraction: float) -> float:
        return self.base + fraction * (self.top - self.base)


def as_float(y: npt.ArrayLike) -> FloatArray:
    a = np.asarray(y, dtype=np.float64)
    if a.ndim != 1 or a.size < 2:
        raise MeasurementError("need a 1-D record with at least 2 samples")
    if not np.all(np.isfinite(a)):
        raise MeasurementError("record contains non-finite values")
    return a


def levels(y: npt.ArrayLike, bins: int = 256) -> Levels:
    """Top/base levels by the histogram-mode method (as used by DSO "Top"/"Base" readouts).

    For waveforms without flat tops (e.g. sine) the modes are not well defined and the
    method falls back to max/min, mirroring common oscilloscope behaviour.
    """
    a = as_float(y)
    lo, hi = float(np.min(a)), float(np.max(a))
    if hi - lo <= 0:
        return Levels(hi, lo, "flat record: top = base = value")
    nb = max(16, min(bins, a.size // 8))
    counts, edges = np.histogram(a, bins=nb, range=(lo, hi))
    half = nb // 2

    def mode(sl: slice, fallback: float) -> tuple[float, bool]:
        # A flat top/base concentrates most of its half's samples within +-1 bin of the mode;
        # a sine's arcsine distribution puts only ~20% there at 256 bins.
        c = counts[sl]
        total = int(c.sum())
        if total < 16:
            return fallback, False
        k = int(np.argmax(c)) + (sl.start or 0)
        k0, k1 = max(0, k - 1), min(nb - 1, k + 1)
        if counts[k0 : k1 + 1].sum() < 0.3 * total:
            return fallback, False
        sel = a[(a >= edges[k0]) & (a <= edges[k1 + 1])]
        return float(sel.mean()), True

    top, ok_t = mode(slice(half, nb), hi)
    base, ok_b = mode(slice(0, half), lo)
    method = (
        "histogram mode of upper/lower halves" if ok_t and ok_b else "max/min (no flat top/base detected)"
    )
    if not (ok_t and ok_b):
        top, base = hi, lo
    return Levels(top, base, method)


def voltage_stats(y: npt.ArrayLike) -> dict[str, float]:
    a = as_float(y)
    lv = levels(a)
    return {
        "maximum": float(np.max(a)),
        "minimum": float(np.min(a)),
        "peak_to_peak": float(np.max(a) - np.min(a)),
        "mean": float(a.mean()),
        "rms": float(math.sqrt(float(np.mean(a * a)))),
        "ac_rms": float(a.std()),
        "top": lv.top,
        "base": lv.base,
        "amplitude": lv.amplitude,
        "levels_method": lv.method,  # type: ignore[dict-item]
    }


def crossings(y: FloatArray, level: float, hysteresis: float) -> tuple[FloatArray, FloatArray]:
    """Return (rising, falling) crossing positions in fractional sample indices.

    A rising crossing is registered only after the signal was below ``level - h`` and
    then exceeds ``level + h``; its position is interpolated where it passes ``level``.
    """
    h = max(hysteresis, 0.0)
    lo_mask = y < level - h
    hi_mask = y > level + h
    idx = np.nonzero(lo_mask | hi_mask)[0]
    if idx.size < 2:
        return np.empty(0), np.empty(0)
    state = hi_mask[idx]
    change = np.nonzero(state[1:] != state[:-1])[0]
    rising: list[float] = []
    falling: list[float] = []
    for c in change:
        i0, i1 = int(idx[c]), int(idx[c + 1])  # last sample in old state, first in new state
        seg = y[i0 : i1 + 1]
        if state[c + 1]:  # low -> high
            js = np.nonzero((seg[:-1] < level) & (seg[1:] >= level))[0]
            j = int(js[-1]) if js.size else 0
            a, b = seg[j], seg[j + 1]
            rising.append(i0 + j + (level - a) / (b - a) if b != a else i0 + j)
        else:
            js = np.nonzero((seg[:-1] > level) & (seg[1:] <= level))[0]
            j = int(js[-1]) if js.size else 0
            a, b = seg[j], seg[j + 1]
            falling.append(i0 + j + (a - level) / (a - b) if b != a else i0 + j)
    return np.asarray(rising), np.asarray(falling)


def default_hysteresis(lv: Levels, y: FloatArray) -> float:
    noise = float(noise_estimate(y)["noise_rms"])
    return max(0.05 * lv.amplitude, 3.0 * noise)


def _timing_unc(
    dt: float | None, span_s: float, simulated: bool, edge_jitter_s: float = 0.0
) -> dict[str, Any]:
    """Relative uncertainty of a period/frequency derived from two edge times ``span_s`` apart.

    Combines (in quadrature) the interpolation/quantisation error of each edge (~dt/sqrt(12)),
    the noise-induced edge jitter (noise_rms / slope at the crossing) and, for hardware, the
    instrument's +-50 ppm timebase accuracy.
    """
    if dt is None or span_s <= 0:
        return {}
    rel_sampling = math.sqrt(2.0) * (dt / math.sqrt(12.0)) / span_s
    rel_noise = math.sqrt(2.0) * edge_jitter_s / span_s
    out: dict[str, Any] = {
        "relative_uncertainty_sampling": rel_sampling,
        "relative_uncertainty_noise": rel_noise,
        "edge_jitter_s_est": edge_jitter_s,
    }
    rel_tb = 0.0 if simulated else TIMEBASE_ACCURACY_PPM * 1e-6
    if not simulated:
        out["relative_uncertainty_timebase"] = rel_tb
    out["relative_uncertainty_est"] = math.sqrt(rel_sampling**2 + rel_noise**2 + rel_tb**2)
    out["uncertainty_note"] = "1-sigma estimate; excludes probe/signal-source effects"
    return out


def _edge_jitter(a: FloatArray, edges: FloatArray, dt: float | None) -> float:
    """noise_rms / |slope| at the crossings, in seconds (or samples when dt is None)."""
    if edges.size == 0:
        return 0.0
    noise = float(noise_estimate(a)["noise_rms"])
    grad = np.gradient(a)
    idx = np.clip(np.round(edges).astype(int), 0, a.size - 1)
    slope = float(np.median(np.abs(grad[idx])))  # units per sample
    if slope <= 0:
        return 0.0
    return noise / slope * (dt if dt is not None else 1.0)


def frequency(y: npt.ArrayLike, dt: float | None, simulated: bool = False) -> dict[str, Any]:
    """Frequency/period from 50%-level rising crossings over all complete cycles in the record."""
    a = as_float(y)
    lv = levels(a)
    if lv.amplitude <= 0:
        raise MeasurementError("flat record: no edges")
    hyst = default_hysteresis(lv, a)
    rising, falling = crossings(a, lv.at(0.5), hyst)
    edges = rising if rising.size >= falling.size else falling
    if edges.size < 2:
        raise MeasurementError(
            f"fewer than one complete cycle in the record ({edges.size} qualifying edge(s)); use a slower timebase"
        )
    periods = np.diff(edges)
    period_samples = (edges[-1] - edges[0]) / (edges.size - 1)
    unit_dt = dt if dt is not None else 1.0
    period = period_samples * unit_dt
    out: dict[str, Any] = {
        "frequency_hz" if dt is not None else "frequency_per_sample": 1.0 / period,
        "period_s" if dt is not None else "period_samples": period,
        "cycles_measured": int(edges.size - 1),
        "period_std": float(periods.std() * unit_dt) if periods.size > 1 else 0.0,
        "samples_per_cycle": float(period_samples),
        "reference_level": lv.at(0.5),
        "hysteresis": hyst,
        "method": "mean interval between 50%-level crossings (Schmitt hysteresis, linear interpolation)",
    }
    out.update(_timing_unc(dt, (edges[-1] - edges[0]) * unit_dt, simulated, _edge_jitter(a, edges, dt)))
    if period_samples < 10:
        out["warning"] = "fewer than 10 samples per cycle: timing accuracy is limited; use a faster timebase"
    return out


def duty_cycle(y: npt.ArrayLike, dt: float | None) -> dict[str, Any]:
    a = as_float(y)
    lv = levels(a)
    hyst = default_hysteresis(lv, a)
    rising, falling = crossings(a, lv.at(0.5), hyst)
    if rising.size < 2 or falling.size < 1:
        raise MeasurementError("need at least one complete cycle (two rising edges and one falling edge)")
    pos, neg = [], []
    for r0, r1 in itertools.pairwise(rising):
        f = falling[(falling > r0) & (falling < r1)]
        if f.size:
            pos.append(f[0] - r0)
            neg.append(r1 - f[0])
    if not pos:
        raise MeasurementError("could not pair rising and falling edges")
    unit = dt if dt is not None else 1.0
    pw, nw = float(np.mean(pos)), float(np.mean(neg))
    return {
        "duty_cycle_percent": 100.0 * pw / (pw + nw),
        "positive_width": pw * unit,
        "negative_width": nw * unit,
        "width_unit": "s" if dt is not None else "samples",
        "cycles_measured": len(pos),
        "duty_cycle_resolution_percent": 100.0 * math.sqrt(2) / (pw + nw),
        "reference_level": lv.at(0.5),
        "method": "positive width / period at the 50% level, averaged over complete cycles",
    }


def transition_time(
    y: npt.ArrayLike, dt: float | None, rising: bool, lo: float = 0.1, hi: float = 0.9
) -> dict[str, Any]:
    """10%-90% rise (or 90%-10% fall) time averaged over all complete edges."""
    a = as_float(y)
    lv = levels(a)
    if lv.amplitude <= 0:
        raise MeasurementError("flat record: no edges")
    hyst = default_hysteresis(lv, a)
    r, f = crossings(a, lv.at(0.5), hyst)
    mids = r if rising else f
    l_lo, l_hi = lv.at(lo), lv.at(hi)
    times: list[float] = []
    for m in mids:
        c = int(m)
        # walk backwards to the low threshold and forwards to the high threshold
        start_level, end_level = (l_lo, l_hi) if rising else (l_hi, l_lo)
        i = c
        while i > 0 and ((a[i] > start_level) if rising else (a[i] < start_level)):
            i -= 1
        j = c + 1
        while j < a.size - 1 and ((a[j] < end_level) if rising else (a[j] > end_level)):
            j += 1
        if i == 0 or j >= a.size - 1:
            continue  # edge truncated by the record boundary
        t_start = _interp_cross(a, i, start_level)
        t_end = _interp_cross(a, j - 1, end_level)
        times.append(t_end - t_start)
    if not times:
        raise MeasurementError(f"no complete {'rising' if rising else 'falling'} edge in the record")
    unit = dt if dt is not None else 1.0
    val = float(np.median(times)) * unit
    out: dict[str, Any] = {
        "rise_time" if rising else "fall_time": val,
        "unit": "s" if dt is not None else "samples",
        "edges_measured": len(times),
        "thresholds": [lo, hi],
        "method": f"{int(lo * 100)}%-{int(hi * 100)}% of base-to-top, linear interpolation, median over edges",
    }
    warnings = []
    if dt is not None and val < 3 * dt:
        warnings.append(
            f"transition spans fewer than 3 samples (dt={dt:.3g} s): result is limited by the sample "
            "interval and is an upper bound; use a faster timebase"
        )
    if dt is not None and val < 2 * INSTRUMENT_RISE_TIME_S:
        warnings.append("transition is near the instrument's 3.5 ns rise time: result is bandwidth-limited")
    if warnings:
        out["warnings"] = warnings
    return out


def _interp_cross(a: FloatArray, i: int, level: float) -> float:
    y0, y1 = a[i], a[i + 1]
    if y1 == y0:
        return float(i)
    return i + float((level - y0) / (y1 - y0))


def noise_estimate(y: npt.ArrayLike) -> dict[str, Any]:
    """Robust white-noise RMS from first differences.

    Differences remove slow signal content; outliers (edges) are rejected with a 5-sigma
    window derived from the MAD, then sigma = std(kept differences) / sqrt(2). Using the
    trimmed standard deviation rather than the MAD itself avoids the MAD's bias on
    coarsely quantised 8-bit data. Noise well below 1 LSB cannot be resolved.
    """
    a = as_float(y)
    d = np.diff(a)
    med = float(np.median(d))
    dev = np.abs(d - med)
    s0 = 1.4826 * float(np.median(dev))
    if s0 > 0:
        thr = 5.0 * s0
    else:
        nz = dev[dev > 0]
        thr = 3.0 * float(np.min(nz)) if nz.size else 0.0
    kept = d[dev <= thr]
    sigma = float(np.std(kept)) / math.sqrt(2.0) if kept.size > 1 else 0.0
    return {
        "noise_rms": sigma,
        "differences_rejected": int(d.size - kept.size),
        "method": "std of first differences after 5-sigma (MAD) outlier rejection, / sqrt(2)",
    }


def snr_db(signal_rms: float, noise_rms: float) -> float | None:
    if noise_rms <= 0 or signal_rms <= 0:
        return None
    return 20.0 * math.log10(signal_rms / noise_rms)
