"""Record-level data-quality indicators computed from raw ADC samples."""

from __future__ import annotations

from typing import Any

import numpy as np
import numpy.typing as npt

from hantek_mcp.protocol.scaling import CLIP_HIGH, CLIP_LOW

ADC_SPAN = CLIP_HIGH - CLIP_LOW


def record_quality(raw: npt.NDArray[np.int8], identical_to_previous: bool | None = None) -> dict[str, Any]:
    n = int(raw.size)
    if n == 0:
        return {"sample_count": 0, "valid": False, "warnings": ["empty record"]}
    r = raw.astype(np.int16)
    hi = int(np.count_nonzero(r >= CLIP_HIGH))
    lo = int(np.count_nonzero(r <= CLIP_LOW))
    ptp = int(np.max(r) - np.min(r))
    # The rails are ~5 divisions from centre, beyond the visible screen: any sample there means the
    # true signal may have been larger, so maxima/minima/peak-to-peak cannot be trusted.
    clipped = (hi + lo) >= 1
    utilisation = ptp / ADC_SPAN
    warnings: list[str] = []
    if clipped:
        warnings.append(
            f"signal clipped at the ADC limits ({hi} samples high, {lo} low): amplitude/RMS results are "
            "invalid; increase V/div or adjust the offset"
        )
    if ptp <= 2:
        warnings.append(
            "record is flat (<= 2 ADC counts peak-to-peak): no signal, GND coupling or V/div too large"
        )
    elif utilisation < 0.1:
        warnings.append(
            f"signal uses only {utilisation:.0%} of the ADC range: resolution is poor; decrease V/div"
        )
    if identical_to_previous:
        warnings.append(
            "raw samples are byte-identical to the previous capture of this channel: the acquisition "
            "buffer was probably not refreshed (stale data)"
        )
    return {
        "sample_count": n,
        "raw_min": int(np.min(r)),
        "raw_max": int(np.max(r)),
        "raw_peak_to_peak_counts": ptp,
        "adc_utilisation": round(utilisation, 4),
        "clipped": bool(clipped),
        "clipped_high_samples": hi,
        "clipped_low_samples": lo,
        "flat": ptp <= 2,
        "identical_to_previous": identical_to_previous,
        "valid": not clipped and ptp > 2 and not identical_to_previous,
        "warnings": warnings,
    }
