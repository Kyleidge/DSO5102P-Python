"""Signal generators for the simulated instrument."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

import numpy as np
import numpy.typing as npt

Waveform = Literal["sine", "square", "pwm", "triangle", "sawtooth", "noise", "dc", "off"]
WAVEFORMS: tuple[str, ...] = ("sine", "square", "pwm", "triangle", "sawtooth", "noise", "dc", "off")


@dataclass
class SignalSpec:
    """Signal applied to a simulated input, expressed at the probe tip.

    ``amplitude_v`` is the peak amplitude (half of Vpp) for periodic waveforms and the
    RMS value for ``noise``. ``phase_deg`` shifts the waveform relative to t=0, which is
    used to model fixed phase offsets between CH1 and CH2.
    """

    waveform: Waveform = "sine"
    frequency_hz: float = 1000.0
    amplitude_v: float = 1.0
    offset_v: float = 0.0
    duty_cycle: float = 0.5
    phase_deg: float = 0.0
    noise_rms_v: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> None:
        if self.waveform not in WAVEFORMS:
            raise ValueError(f"unknown waveform {self.waveform!r}; choose from {WAVEFORMS}")
        if self.frequency_hz <= 0 and self.waveform not in ("dc", "noise", "off"):
            raise ValueError("frequency_hz must be positive")
        if not 0.0 < self.duty_cycle < 1.0:
            raise ValueError("duty_cycle must be in (0, 1)")
        if self.amplitude_v < 0 or self.noise_rms_v < 0:
            raise ValueError("amplitude_v and noise_rms_v must be non-negative")

    def periodic(self) -> bool:
        return self.waveform in ("sine", "square", "pwm", "triangle", "sawtooth")

    def evaluate(self, t: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        """Noise-free waveform value at times ``t`` (seconds)."""
        a, off = self.amplitude_v, self.offset_v
        if self.waveform == "off":
            return np.zeros_like(t)
        if self.waveform in ("dc", "noise"):
            return np.full_like(t, off)
        ph = (t * self.frequency_hz + self.phase_deg / 360.0) % 1.0
        if self.waveform == "sine":
            return off + a * np.sin(2 * np.pi * ph)
        if self.waveform == "square":
            return off + np.where(ph < 0.5, a, -a)
        if self.waveform == "pwm":
            return off + np.where(ph < self.duty_cycle, a, -a)
        if self.waveform == "triangle":
            return off + a * (4 * np.abs(ph - 0.5) - 1) * -1
        if self.waveform == "sawtooth":
            return off + a * (2 * ph - 1)
        raise ValueError(self.waveform)

    def extremes(self) -> tuple[float, float]:
        if self.waveform == "off":
            return 0.0, 0.0
        if self.waveform == "dc":
            return self.offset_v, self.offset_v
        if self.waveform == "noise":
            return self.offset_v - 3 * self.amplitude_v, self.offset_v + 3 * self.amplitude_v
        return self.offset_v - self.amplitude_v, self.offset_v + self.amplitude_v

    def trigger_shift(self, level_v: float, rising: bool) -> float | None:
        """Time shift ``s`` such that ``evaluate(t + s)`` crosses ``level_v`` at t=0 with the
        requested slope, or ``None`` if the noise-free signal never crosses the level."""
        if not self.periodic():
            return None
        lo, hi = self.extremes()
        if not lo < level_v < hi:
            return None
        period = 1.0 / self.frequency_hz
        n = 4096
        t = np.arange(n + 1) * (period / n)
        v = self.evaluate(t) - level_v
        if rising:
            idx = np.nonzero((v[:-1] < 0) & (v[1:] >= 0))[0]
        else:
            idx = np.nonzero((v[:-1] > 0) & (v[1:] <= 0))[0]
        if idx.size == 0:
            return None
        i = int(idx[0])
        frac = v[i] / (v[i] - v[i + 1]) if v[i] != v[i + 1] else 0.0
        return float(t[i] + frac * (t[i + 1] - t[i]))
