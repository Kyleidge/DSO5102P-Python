"""Scaling model and signal analysis against synthetic signals with known answers."""

from __future__ import annotations

import math

import numpy as np
import pytest

from hantek_mcp.analysis import fft, measurements, timing
from hantek_mcp.analysis.quality import record_quality
from hantek_mcp.protocol.scaling import (
    ScalingCalibration,
    ScalingStatus,
    horizontal_scaling,
    vertical_scaling,
)

RNG = np.random.default_rng(42)
DT = 1e-6
T = np.arange(20000) * DT


def sine(f: float, a: float = 1.0, phase: float = 0.0, noise: float = 0.0) -> np.ndarray:
    return (
        a * np.sin(2 * np.pi * f * T + phase) + RNG.normal(0, noise, T.size)
        if noise
        else a * np.sin(2 * np.pi * f * T + phase)
    )


def pwm(f: float, duty: float, hi: float = 3.3, lo: float = 0.0) -> np.ndarray:
    return np.where((T * f) % 1.0 < duty, hi, lo).astype(float)


# ----------------------------------------------------------------------------- scaling


def test_vertical_scaling_formula() -> None:
    cal = ScalingCalibration()
    v = vertical_scaling(
        0.5, 10, position_counts=-10, fine=False, coupling="DC", inverted=False, calibration=cal
    )
    assert v.status == ScalingStatus.NOMINAL_UNVERIFIED
    raw = np.array([-10, 15, 40], dtype=np.int8)
    np.testing.assert_allclose(v.to_volts(raw), [0.0, 5.0, 10.0])  # 25 counts = 1 div = 5 V at the tip


def test_vertical_scaling_unavailable_in_fine_mode_and_verified_flag() -> None:
    v = vertical_scaling(
        0.5, 1, 0, fine=True, coupling="DC", inverted=False, calibration=ScalingCalibration()
    )
    assert v.status == ScalingStatus.UNAVAILABLE and v.to_volts(np.zeros(3, np.int8)) is None
    v2 = vertical_scaling(1, 1, 0, False, "DC", False, ScalingCalibration(voltage_verified=True))
    assert v2.status == ScalingStatus.VERIFIED


@pytest.mark.parametrize(
    ("n", "depth", "spd", "method"),
    [
        (4000, 4000, 200.0, "memory_depth"),
        (40000, 40000, 2000.0, "memory_depth"),
        (3200, None, 200.0, "inferred"),
        (3840, None, 200.0, "inferred"),
        (32000, None, 2000.0, "inferred"),
        (1234, None, 1234 / 20, "fallback"),
    ],
)
def test_horizontal_model(n: int, depth: int | None, spd: float, method: str) -> None:
    h = horizontal_scaling(n, 1e-3, depth, ScalingCalibration())
    assert h.samples_per_div == pytest.approx(spd)
    assert method in h.method
    assert h.sample_interval_s == pytest.approx(1e-3 / spd)
    t = h.time_axis(n)
    assert t[n // 2] == pytest.approx(0.0, abs=1e-15)


def test_horizontal_flags_interpolated_rates_and_override() -> None:
    h = horizontal_scaling(4000, 4e-9, 4000, ScalingCalibration())
    assert any("exceeds the 1 GS/s" in n for n in h.notes)
    h2 = horizontal_scaling(4000, 1e-3, 4000, ScalingCalibration(samples_per_div_override=250))
    assert h2.samples_per_div == 250 and "override" in h2.method


# ----------------------------------------------------------------------------- measurements


def test_voltage_stats_sine() -> None:
    s = measurements.voltage_stats(sine(1000, 2.0))
    assert s["peak_to_peak"] == pytest.approx(4.0, rel=1e-3)
    assert s["rms"] == pytest.approx(2.0 / math.sqrt(2), rel=1e-3)
    assert s["mean"] == pytest.approx(0.0, abs=1e-3)


def test_levels_square_uses_histogram() -> None:
    y = pwm(1000, 0.5, 5.0, 0.0) + RNG.normal(0, 0.02, T.size)
    lv = measurements.levels(y)
    assert "histogram" in lv.method
    assert lv.top == pytest.approx(5.0, abs=0.01) and lv.base == pytest.approx(0.0, abs=0.01)


@pytest.mark.parametrize("f", [137.0, 1000.0, 12345.0])
def test_frequency_accuracy(f: float) -> None:
    r = measurements.frequency(sine(f, 1.0, noise=0.01), DT)
    err = abs(r["frequency_hz"] - f) / f
    assert err < 3 * r["relative_uncertainty_est"]  # the reported uncertainty must be honest
    assert r["relative_uncertainty_est"] < 2e-3
    clean = measurements.frequency(sine(f, 1.0), DT)
    assert clean["frequency_hz"] == pytest.approx(f, rel=1e-5)


def test_frequency_requires_a_full_cycle() -> None:
    with pytest.raises(measurements.MeasurementError):
        measurements.frequency(sine(30.0), DT)  # 0.6 cycles in 20 ms


@pytest.mark.parametrize("duty", [0.1, 0.25, 0.5, 0.8])
def test_duty_cycle(duty: float) -> None:
    r = measurements.duty_cycle(pwm(2000, duty), DT)
    assert r["duty_cycle_percent"] == pytest.approx(duty * 100, abs=0.2)


def test_rise_and_fall_time_of_linear_edges() -> None:
    # trapezoid: 50 us linear ramps -> 10-90% = 40 us
    period = 1e-3
    ph = (T % period) / period
    y = np.clip(np.minimum(ph / 0.05, (1 - ph) / 0.05), 0, 1) * 5.0
    rt = measurements.transition_time(y, DT, rising=True)
    ft = measurements.transition_time(y, DT, rising=False)
    assert rt["rise_time"] == pytest.approx(40e-6, rel=0.02)
    assert ft["fall_time"] == pytest.approx(40e-6, rel=0.02)


def test_noise_estimate_ignores_slow_signal() -> None:
    y = sine(500, 1.0) + RNG.normal(0, 0.05, T.size)
    r = measurements.noise_estimate(y)
    assert r["noise_rms"] == pytest.approx(0.05, rel=0.1)


# ----------------------------------------------------------------------------- fft


def test_fft_tone_frequency_and_amplitude_on_bin() -> None:
    f0 = 50 / (T.size * DT)  # exactly on bin 50
    sp = fft.spectrum(sine(f0, 0.8), DT, "hann")
    pk = fft.find_peaks(sp, 1)[0]
    assert pk["frequency_hz"] == pytest.approx(f0, rel=1e-6)
    assert pk["amplitude"] == pytest.approx(0.8, rel=1e-3)
    assert sp["bin_width_hz"] == pytest.approx(50.0)


def test_fft_flattop_amplitude_off_bin_and_rms_scaling() -> None:
    sp = fft.spectrum(sine(1234.5, 1.0), DT, "flattop", scaling="rms")
    pk = fft.find_peaks(sp, 1)[0]
    assert pk["amplitude"] == pytest.approx(1 / math.sqrt(2), rel=0.01)
    assert pk["frequency_hz"] == pytest.approx(1234.5, abs=sp["bin_width_hz"] / 2)


def test_square_wave_thd() -> None:
    y = np.where(np.sin(2 * np.pi * 1000 * T) >= 0, 1.0, -1.0)
    sp = fft.spectrum(y, DT, "blackmanharris")
    h = fft.harmonic_analysis(sp, 1000.0, 7)
    # odd harmonics 1/3, 1/5, 1/7 -> THD(2..7) = sqrt(1/9+1/25+1/49) = 0.4142
    assert h["thd_ratio"] == pytest.approx(math.sqrt(1 / 9 + 1 / 25 + 1 / 49), rel=0.01)


def test_decimate_spectrum_keeps_peak() -> None:
    sp = fft.spectrum(sine(5000, 1.0), DT)
    f, a = fft.decimate_spectrum(sp, 100)
    assert len(f) <= 101
    assert max(a) == pytest.approx(float(np.max(sp["amplitude"])))


def test_invalid_window_rejected() -> None:
    with pytest.raises(measurements.MeasurementError):
        fft.spectrum(sine(1000), DT, "nope")


# ----------------------------------------------------------------------------- timing


@pytest.mark.parametrize("deg", [-120.0, -30.0, 45.0, 170.0])
def test_phase_difference(deg: float) -> None:
    a = sine(1000)
    b = sine(1000, 0.5, math.radians(deg))
    r = timing.phase_difference(a, b, DT)
    assert r["phase_deg"] == pytest.approx(deg, abs=0.2)


def test_edge_delay_and_cross_correlation() -> None:
    a = pwm(1000, 0.3)
    shift = 37  # samples
    b = np.roll(a, shift)
    d = timing.edge_delay(a, b, T, T)
    assert d["delay_s"] == pytest.approx(shift * DT, abs=DT)
    c = timing.cross_correlation(a, b, DT, max_lag_s=200e-6)
    assert c["lag_s"] == pytest.approx(shift * DT, abs=DT)


def test_pulses_and_edges() -> None:
    y = pwm(1000, 0.25)
    p = timing.detect_pulses(y, T)
    assert p["count"] >= 19
    assert p["width_stats_s"]["mean"] == pytest.approx(250e-6, rel=0.01)
    e = timing.detect_edges(y, T, max_edges=5)
    assert len(e["edges"]) == 5 and e["truncated"]
    assert p["pulses"][0]["width_s"] == pytest.approx(250e-6, rel=0.01)


def test_compare_records() -> None:
    a = sine(1000)
    r = timing.compare_records(a, a * 1.1, DT, DT)
    assert r["correlation_coefficient"] == pytest.approx(1.0)
    assert r["vpp_b"] / r["vpp_a"] == pytest.approx(1.1)


# ----------------------------------------------------------------------------- quality


def test_record_quality_flags() -> None:
    clipped = np.clip(np.round(sine(1000, 200)), -128, 127).astype(np.int8)
    q = record_quality(clipped)
    assert q["clipped"] and not q["valid"]
    flat = np.zeros(1000, np.int8)
    assert record_quality(flat)["flat"]
    small = np.round(sine(1000, 5)).astype(np.int8)
    assert any("resolution is poor" in w for w in record_quality(small)["warnings"])
    stale = record_quality(small, identical_to_previous=True)
    assert not stale["valid"] and any("stale" in w for w in stale["warnings"])
