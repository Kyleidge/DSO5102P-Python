"""Conversion of raw 8-bit sample records into time and voltage coordinates.

Neither conversion has been verified against a DSO5102P by this project. They
are built from documented evidence (see docs/usb-protocol.md §Scaling):

Vertical
    ``V = (raw - position_counts) * volts_per_div * probe / counts_per_div`` with a
    nominal 25 counts/div (elinux: the 8-bit range spans ~10.2 div; dsoc-extended's
    trigger-level formula uses the same /25 factor).

Horizontal
    The record covers the acquisition window at ``memory_depth / 20`` samples per
    division (elinux: "20 DIV horizontal"; DSO5000P manual: "sample interval =
    s/div ÷ 200" for the default 4K memory). When the depth is unknown the samples
    per division are inferred from the record length; dsoc-extended observed shorter
    reads (16 div) while the on-screen menu is open.

The calibration factors are configurable so a hardware validation run can correct
them, and every capture records which method and status applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import numpy as np
import numpy.typing as npt

from hantek_mcp.protocol.settings import NOMINAL_COUNTS_PER_DIV

REALTIME_MAX_SAMPLE_RATE = 1e9
_PLAUSIBLE_SPANS = (16.0, 19.2, 20.0)
_SAMPLES_PER_DIV_CANDIDATES = (200.0, 1000.0, 2000.0)
ADC_MIN = -128
ADC_MAX = 127
#: Samples at or beyond these values are treated as clipped (elinux: data spans -127..127).
CLIP_LOW = -127
CLIP_HIGH = 127


class ScalingStatus(StrEnum):
    VERIFIED = "verified"  # confirmed on real hardware by a recorded validation run
    NOMINAL_UNVERIFIED = "nominal_unverified"  # documented/inferred model, not yet validated
    SIMULATED = "simulated"  # exact by construction, simulator only
    UNAVAILABLE = "unavailable"  # cannot be computed (e.g. fine V/div, unknown index)


@dataclass
class ScalingCalibration:
    counts_per_div: float = NOMINAL_COUNTS_PER_DIV
    samples_per_div_override: float | None = None
    voltage_verified: bool = False
    time_verified: bool = False


@dataclass
class HorizontalScaling:
    sample_interval_s: float | None
    samples_per_div: float | None
    span_divisions: float | None
    trigger_index: float
    method: str
    status: ScalingStatus
    notes: list[str] = field(default_factory=list)

    @property
    def sample_rate_hz(self) -> float | None:
        return None if not self.sample_interval_s else 1.0 / self.sample_interval_s

    def time_axis(self, n: int) -> npt.NDArray[np.float64]:
        if self.sample_interval_s is None:
            return np.arange(n, dtype=np.float64)
        return (np.arange(n, dtype=np.float64) - self.trigger_index) * self.sample_interval_s

    def as_dict(self) -> dict[str, Any]:
        return {
            "sample_interval_s": self.sample_interval_s,
            "sample_rate_hz": self.sample_rate_hz,
            "samples_per_div": self.samples_per_div,
            "span_divisions": self.span_divisions,
            "trigger_index": self.trigger_index,
            "method": self.method,
            "status": str(self.status),
            "notes": list(self.notes),
        }


@dataclass
class VerticalScaling:
    volts_per_count: float | None
    offset_counts: int
    counts_per_div: float
    method: str
    status: ScalingStatus
    notes: list[str] = field(default_factory=list)

    def to_volts(self, raw: npt.NDArray[np.int8]) -> npt.NDArray[np.float64] | None:
        if self.volts_per_count is None:
            return None
        return (raw.astype(np.float64) - self.offset_counts) * self.volts_per_count

    def as_dict(self) -> dict[str, Any]:
        return {
            "volts_per_count": self.volts_per_count,
            "offset_counts": self.offset_counts,
            "counts_per_div": self.counts_per_div,
            "method": self.method,
            "status": str(self.status),
            "notes": list(self.notes),
        }


def horizontal_scaling(
    n_samples: int,
    seconds_per_div: float | None,
    memory_depth: int | None,
    calibration: ScalingCalibration,
    simulated: bool = False,
    trigger_time_raw: int = 0,
) -> HorizontalScaling:
    notes: list[str] = []
    trig_idx = n_samples / 2.0
    status = (
        ScalingStatus.SIMULATED
        if simulated
        else (ScalingStatus.VERIFIED if calibration.time_verified else ScalingStatus.NOMINAL_UNVERIFIED)
    )
    if seconds_per_div is None or n_samples <= 0:
        return HorizontalScaling(
            None, None, None, trig_idx, "timebase unknown", ScalingStatus.UNAVAILABLE, ["timebase index unknown"]
        )
    spd: float | None = None
    method = ""
    if calibration.samples_per_div_override:
        spd = float(calibration.samples_per_div_override)
        method = "configured samples_per_div override"
    elif memory_depth:
        spd = memory_depth / 20.0
        method = "memory_depth/20 samples per division"
        span = n_samples / spd
        if not any(abs(span - s) < 0.05 for s in _PLAUSIBLE_SPANS):
            notes.append(f"record length {n_samples} implies an unusual {span:.2f}-division span")
    else:
        for cand in _SAMPLES_PER_DIV_CANDIDATES:
            span = n_samples / cand
            if any(abs(span - s) < 1e-6 for s in _PLAUSIBLE_SPANS):
                spd = cand
                method = f"inferred {cand:g} samples/div from record length"
                break
        if spd is None:
            spd = n_samples / 20.0
            method = "fallback: record assumed to span 20 divisions"
            notes.append("memory depth unknown and record length matched no known pattern")
    dt = seconds_per_div / spd
    if 1.0 / dt > REALTIME_MAX_SAMPLE_RATE * 1.0001:
        notes.append(
            f"implied sample rate {1.0 / dt:.3g} S/s exceeds the 1 GS/s real-time rate: points are "
            "interpolated (sin x/x) or equivalent-time, not independent ADC samples"
        )
    if trigger_time_raw:
        notes.append(
            f"horizontal trigger offset raw value {trigger_time_raw} is not applied (units unverified); "
            "time axis assumes the trigger at the record centre"
        )
    if not simulated:
        notes.append("trigger assumed at record centre (unverified on DSO5102P)")
    return HorizontalScaling(dt, spd, n_samples / spd, trig_idx, method, status, notes)


def vertical_scaling(
    volts_per_div: float | None,
    probe: int | None,
    position_counts: int,
    fine: bool,
    coupling: str | None,
    inverted: bool,
    calibration: ScalingCalibration,
    simulated: bool = False,
) -> VerticalScaling:
    notes: list[str] = []
    cpd = calibration.counts_per_div
    if fine:
        return VerticalScaling(
            None, position_counts, cpd, "unavailable", ScalingStatus.UNAVAILABLE,
            ["fine V/div is active; the exact V/div is not reported in the settings record"],
        )  # fmt: skip
    if volts_per_div is None or probe is None:
        return VerticalScaling(
            None, position_counts, cpd, "unavailable", ScalingStatus.UNAVAILABLE,
            ["V/div or probe index not recognised"],
        )  # fmt: skip
    if coupling == "GND":
        notes.append("channel coupling is GND: input is grounded")
    if inverted:
        notes.append("channel invert is on; whether the sample stream is inverted is unverified")
    status = (
        ScalingStatus.SIMULATED
        if simulated
        else (ScalingStatus.VERIFIED if calibration.voltage_verified else ScalingStatus.NOMINAL_UNVERIFIED)
    )
    if status == ScalingStatus.NOMINAL_UNVERIFIED:
        notes.append(
            f"nominal {cpd:g} counts/div and offset subtraction are not yet validated on a DSO5102P"
        )
    return VerticalScaling(
        volts_per_count=volts_per_div * probe / cpd,
        offset_counts=position_counts,
        counts_per_div=cpd,
        method="(raw - position) * V/div * probe / counts_per_div",
        status=status,
        notes=notes,
    )


def volts_to_counts(volts: float, volts_per_div: float, probe: int, counts_per_div: float) -> float:
    return volts * counts_per_div / (volts_per_div * probe)
