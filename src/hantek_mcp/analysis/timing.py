"""Edge, pulse and two-record timing analysis."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import numpy.typing as npt
from scipy import signal as sps

from hantek_mcp.analysis.measurements import (
    MeasurementError,
    as_float,
    crossings,
    default_hysteresis,
    frequency,
    levels,
)

FloatArray = npt.NDArray[np.float64]


def detect_edges(
    y: npt.ArrayLike,
    t: npt.ArrayLike,
    level: float | None = None,
    hysteresis: float | None = None,
    max_edges: int = 500,
) -> dict[str, Any]:
    a = as_float(y)
    tt = np.asarray(t, dtype=np.float64)
    lv = levels(a)
    ref = lv.at(0.5) if level is None else level
    h = default_hysteresis(lv, a) if hysteresis is None else hysteresis
    r, f = crossings(a, ref, h)
    idx = np.arange(tt.size)
    events = sorted(
        [(float(x), "rising") for x in r] + [(float(x), "falling") for x in f], key=lambda e: e[0]
    )
    total = len(events)
    out = [
        {"index": round(x, 3), "time_s": float(np.interp(x, idx, tt)), "type": kind}
        for x, kind in events[:max_edges]
    ]
    return {
        "reference_level": ref,
        "hysteresis": h,
        "rising_count": int(r.size),
        "falling_count": int(f.size),
        "edges": out,
        "truncated": total > max_edges,
        "total_edges": total,
    }


def detect_pulses(
    y: npt.ArrayLike,
    t: npt.ArrayLike,
    polarity: str = "positive",
    level: float | None = None,
    min_width_s: float | None = None,
    max_width_s: float | None = None,
    max_pulses: int = 500,
) -> dict[str, Any]:
    if polarity not in ("positive", "negative"):
        raise MeasurementError("polarity must be 'positive' or 'negative'")
    a = as_float(y)
    tt = np.asarray(t, dtype=np.float64)
    lv = levels(a)
    ref = lv.at(0.5) if level is None else level
    r, f = crossings(a, ref, default_hysteresis(lv, a))
    starts, ends = (r, f) if polarity == "positive" else (f, r)
    idx = np.arange(tt.size)
    pulses = []
    for s0 in starts:
        e = ends[ends > s0]
        if not e.size:
            break  # pulse truncated by the end of the record
        ts, te = float(np.interp(s0, idx, tt)), float(np.interp(e[0], idx, tt))
        w = te - ts
        if (min_width_s is not None and w < min_width_s) or (max_width_s is not None and w > max_width_s):
            continue
        pulses.append({"start_s": ts, "end_s": te, "width_s": w})
    widths = np.array([p["width_s"] for p in pulses]) if pulses else np.empty(0)
    return {
        "polarity": polarity,
        "reference_level": ref,
        "count": len(pulses),
        "pulses": pulses[:max_pulses],
        "truncated": len(pulses) > max_pulses,
        "width_stats_s": None
        if not widths.size
        else {
            "min": float(np.min(widths)),
            "max": float(np.max(widths)),
            "mean": float(widths.mean()),
            "std": float(widths.std()),
        },
    }


def cross_correlation(
    a: npt.ArrayLike, b: npt.ArrayLike, dt: float, max_lag_s: float | None = None
) -> dict[str, Any]:
    """Lag of ``b`` relative to ``a`` (positive: ``b`` is delayed) from the normalised cross-correlation
    peak, refined by parabolic interpolation."""
    x, y = as_float(a), as_float(b)
    if x.size != y.size:
        raise MeasurementError("records must have the same length")
    x = x - x.mean()
    y = y - y.mean()
    denom = math.sqrt(float(np.sum(x * x)) * float(np.sum(y * y)))
    if denom == 0:
        raise MeasurementError("cannot correlate a flat record")
    c = sps.correlate(y, x, mode="full", method="fft") / denom
    lags = sps.correlation_lags(y.size, x.size, mode="full")
    if max_lag_s is not None:
        keep = np.abs(lags * dt) <= max_lag_s
        c, lags = c[keep], lags[keep]
    k = int(np.argmax(c))
    frac = 0.0
    if 0 < k < c.size - 1:
        d = c[k - 1] - 2 * c[k] + c[k + 1]
        if d < 0:
            frac = 0.5 * (c[k - 1] - c[k + 1]) / d
    lag = (lags[k] + frac) * dt
    return {
        "lag_s": float(lag),
        "peak_correlation": float(c[k]),
        "lag_resolution_s": dt,
        "method": "normalised cross-correlation peak with parabolic interpolation",
        "note": "periodic signals correlate at every period; the reported lag is the strongest peak "
        "within max_lag_s",
    }


def phase_difference(a: npt.ArrayLike, b: npt.ArrayLike, dt: float) -> dict[str, Any]:
    """Phase of ``b`` relative to ``a`` at ``a``'s fundamental (positive: ``b`` leads)."""
    x, y = as_float(a), as_float(b)
    if x.size != y.size:
        raise MeasurementError("records must have the same length")
    fr = frequency(x, dt)
    f0 = fr["frequency_hz"]
    cycles = math.floor(x.size * dt * f0)
    if cycles < 1:
        raise MeasurementError("need at least one complete cycle")
    n = round(cycles / (f0 * dt))
    n = min(n, x.size)
    tt = np.arange(n) * dt
    ref = np.exp(-2j * np.pi * f0 * tt)
    xa = np.sum((x[:n] - x[:n].mean()) * ref)
    yb = np.sum((y[:n] - y[:n].mean()) * ref)
    if abs(xa) == 0 or abs(yb) == 0:
        raise MeasurementError("no energy at the fundamental in one of the records")
    phase = math.degrees(float(np.angle(yb / xa)))
    phase = (phase + 180.0) % 360.0 - 180.0
    return {
        "phase_deg": phase,
        "fundamental_hz": f0,
        "cycles_used": cycles,
        "equivalent_delay_s": -phase / 360.0 / f0,
        "phase_resolution_deg_est": 360.0 * f0 * dt / math.sqrt(max(cycles, 1)),
        "method": "single-bin DFT at the fundamental over an integer number of cycles of record A",
        "sign_convention": "positive phase: B leads A; equivalent_delay_s positive: B lags A",
    }


def edge_delay(
    a: npt.ArrayLike, b: npt.ArrayLike, ta: npt.ArrayLike, tb: npt.ArrayLike, slope: str = "rising"
) -> dict[str, Any]:
    """Delay from each edge in A to the next same-slope edge in B, at each record's 50% level."""
    xa, xb = as_float(a), as_float(b)
    taa, tbb = np.asarray(ta, dtype=np.float64), np.asarray(tb, dtype=np.float64)
    la, lb = levels(xa), levels(xb)
    ra, fa = crossings(xa, la.at(0.5), default_hysteresis(la, xa))
    rb, fb = crossings(xb, lb.at(0.5), default_hysteresis(lb, xb))
    ea, eb = (ra, rb) if slope == "rising" else (fa, fb)
    if not ea.size or not eb.size:
        raise MeasurementError(f"no {slope} edges in one of the records")
    t_a = np.interp(ea, np.arange(taa.size), taa)
    t_b = np.interp(eb, np.arange(tbb.size), tbb)
    delays = []
    for t0 in t_a:
        nxt = t_b[t_b >= t0]
        if nxt.size:
            delays.append(float(nxt[0] - t0))
    if not delays:
        raise MeasurementError("no edge in B follows an edge in A")
    d = np.array(delays)
    return {
        "delay_s": float(np.median(d)),
        "delay_mean_s": float(d.mean()),
        "delay_std_s": float(d.std()),
        "edges_paired": int(d.size),
        "slope": slope,
        "method": "time from each A edge to the next B edge at the 50% levels (median)",
    }


def compare_records(
    a: npt.ArrayLike, b: npt.ArrayLike, dt_a: float | None, dt_b: float | None
) -> dict[str, Any]:
    x, y = as_float(a), as_float(b)
    out: dict[str, Any] = {
        "length_a": int(x.size),
        "length_b": int(y.size),
        "same_length": x.size == y.size,
        "same_sample_interval": dt_a is not None
        and dt_b is not None
        and abs(dt_a - dt_b) <= 1e-12 * max(dt_a, dt_b),
        "vpp_a": float(np.ptp(x)),
        "vpp_b": float(np.ptp(y)),
        "mean_a": float(x.mean()),
        "mean_b": float(y.mean()),
        "rms_a": float(np.sqrt(np.mean(x * x))),
        "rms_b": float(np.sqrt(np.mean(y * y))),
    }
    if out["same_length"]:
        diff = y - x
        out["rms_difference"] = float(np.sqrt(np.mean(diff * diff)))
        out["max_abs_difference"] = float(np.abs(diff).max())
        sx, sy = x.std(), y.std()
        out["correlation_coefficient"] = None if sx == 0 or sy == 0 else float(np.corrcoef(x, y)[0, 1])
    for key, arr, dt in (("a", x, dt_a), ("b", y, dt_b)):
        try:
            fr = frequency(arr, dt)
            out[f"frequency_{key}"] = fr.get("frequency_hz", fr.get("frequency_per_sample"))
        except MeasurementError:
            out[f"frequency_{key}"] = None
    return out
