"""Spectral analysis of sample records.

Amplitude spectra are single-sided and corrected for the window's coherent gain,
so a pure tone that falls on a bin centre reads its true peak amplitude
(``scaling="peak"``) or RMS amplitude (``scaling="rms"``). Peak frequencies are
refined by parabolic interpolation of the log-magnitude around local maxima.
"""

from __future__ import annotations

import math
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
from scipy import signal as sps

from hantek_mcp.analysis.measurements import MeasurementError, as_float

Window = Literal["hann", "hamming", "blackman", "blackmanharris", "flattop", "rectangular"]
WINDOWS: tuple[str, ...] = ("hann", "hamming", "blackman", "blackmanharris", "flattop", "rectangular")
Detrend = Literal["none", "mean", "linear"]


def _window(name: str, n: int) -> npt.NDArray[np.float64]:
    if name not in WINDOWS:
        raise MeasurementError(f"window must be one of {list(WINDOWS)}")
    if name == "rectangular":
        return np.ones(n)
    return np.asarray(sps.get_window(name, n, fftbins=True), dtype=np.float64)


def spectrum(
    y: npt.ArrayLike,
    dt: float,
    window: str = "hann",
    detrend: str = "mean",
    scaling: Literal["peak", "rms"] = "peak",
) -> dict[str, Any]:
    a = as_float(y)
    if dt is None or dt <= 0:
        raise MeasurementError("a valid sample interval is required for spectral analysis")
    n = a.size
    if detrend == "mean":
        a = a - a.mean()
    elif detrend == "linear":
        a = np.asarray(sps.detrend(a, type="linear"), dtype=np.float64)
    elif detrend != "none":
        raise MeasurementError("detrend must be one of none, mean, linear")
    w = _window(window, n)
    cg = float(w.sum()) / n  # coherent gain
    enbw_bins = n * float(np.sum(w * w)) / float(w.sum()) ** 2
    spec = np.fft.rfft(a * w)
    amp = np.abs(spec) / (n * cg)
    amp[1:] *= 2.0  # single-sided
    if n % 2 == 0:
        amp[-1] /= 2.0
    if scaling == "rms":
        amp[1:] /= math.sqrt(2.0)
    freqs = np.fft.rfftfreq(n, dt)
    df = 1.0 / (n * dt)
    return {
        "freqs_hz": freqs,
        "amplitude": amp,
        "bin_width_hz": df,
        "enbw_hz": enbw_bins * df,
        "nyquist_hz": 0.5 / dt,
        "window": window,
        "detrend": detrend,
        "scaling": scaling,
        "n": n,
    }


def find_peaks(
    spec: dict[str, Any], max_peaks: int = 10, min_relative_db: float = -60.0
) -> list[dict[str, Any]]:
    freqs: npt.NDArray[np.float64] = spec["freqs_hz"]
    amp: npt.NDArray[np.float64] = spec["amplitude"]
    if amp.size < 3:
        return []
    ref = float(np.max(amp[1:])) if amp.size > 1 else 0.0
    if ref <= 0:
        return []
    height = ref * 10 ** (min_relative_db / 20)
    # Skip the DC bin; require separation of a few bins so window main lobes count once.
    idx, _ = sps.find_peaks(amp[1:], height=height, distance=3)
    idx = idx + 1
    order = idx[np.argsort(amp[idx])[::-1]][:max_peaks]
    df = spec["bin_width_hz"]
    peaks = []
    for k in order:
        f, m = float(freqs[k]), float(amp[k])
        if 0 < k < amp.size - 1 and amp[k - 1] > 0 and amp[k + 1] > 0:
            la, lb, lc = np.log(amp[k - 1]), np.log(amp[k]), np.log(amp[k + 1])
            denom = la - 2 * lb + lc
            if denom < 0:
                p = 0.5 * (la - lc) / denom
                f = float(freqs[k] + p * df)
                if spec.get("window") != "flattop":  # flat-top bins already read the true amplitude
                    m = float(np.exp(lb - 0.25 * (la - lc) * p))
        peaks.append(
            {
                "frequency_hz": f,
                "amplitude": m,
                "relative_db": 20 * math.log10(m / ref) if m > 0 else None,
                "bin": int(k),
            }
        )
    return peaks


def harmonic_analysis(spec: dict[str, Any], fundamental_hz: float, n_harmonics: int = 5) -> dict[str, Any]:
    """Amplitudes of harmonics 1..n (nearest-bin max within ±2 bins) and THD."""
    freqs: npt.NDArray[np.float64] = spec["freqs_hz"]
    amp: npt.NDArray[np.float64] = spec["amplitude"]
    df = spec["bin_width_hz"]
    out = []
    for h in range(1, n_harmonics + 1):
        f = h * fundamental_hz
        if f > spec["nyquist_hz"]:
            break
        k = round(f / df)
        lo, hi = max(1, k - 2), min(amp.size - 1, k + 2)
        j = lo + int(np.argmax(amp[lo : hi + 1]))
        out.append({"harmonic": h, "frequency_hz": float(freqs[j]), "amplitude": float(amp[j])})
    thd = None
    if len(out) >= 2 and out[0]["amplitude"] > 0:
        thd = math.sqrt(sum(x["amplitude"] ** 2 for x in out[1:])) / out[0]["amplitude"]
    return {"harmonics": out, "thd_ratio": thd, "thd_db": None if not thd else 20 * math.log10(thd)}


def decimate_spectrum(spec: dict[str, Any], max_points: int) -> tuple[list[float], list[float]]:
    """Reduce a spectrum for display/transport keeping per-block maxima (peaks stay visible)."""
    f: npt.NDArray[np.float64] = spec["freqs_hz"]
    a: npt.NDArray[np.float64] = spec["amplitude"]
    if a.size <= max_points:
        return f.tolist(), a.tolist()
    block = math.ceil(a.size / max_points)
    m = a.size // block * block
    ab = a[:m].reshape(-1, block)
    k = ab.argmax(axis=1) + np.arange(ab.shape[0]) * block
    return f[k].tolist(), a[k].tolist()
