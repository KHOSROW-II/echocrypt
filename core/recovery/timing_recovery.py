"""Bounded sample-clock correction candidates for independent audio devices."""
from __future__ import annotations

import numpy as np


# Separate audio clocks commonly differ by tens to hundreds of ppm; jumps
# of 1000 ppm skipped the useful region in real captures. Try fine corrections
# first, then widen the bounded search only when CRC validation still fails.
_CLOCK_PPM_CANDIDATES = (0, 50, -50, 100, -100, 200, -200, 300, -300,
                         500, -500, 1000, -1000, 2000, -2000, 5000, -5000)
CLOCK_RATIO_CANDIDATES = tuple(1.0 + ppm / 1_000_000.0
                               for ppm in _CLOCK_PPM_CANDIDATES)


def resample_clock(samples, source_step: float):
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    if not np.isfinite(source_step) or not 0.9 <= source_step <= 1.1:
        raise ValueError("Sample-clock correction is outside the safe bound")
    if abs(source_step - 1.0) < 1e-12:
        return x
    output_count = int(round(x.size / source_step))
    if output_count < 1:
        raise ValueError("Sample-clock correction produced an empty capture")
    source_axis = np.arange(x.size, dtype=np.float64)
    positions = np.arange(output_count, dtype=np.float64) * source_step
    return np.interp(positions, source_axis, x).astype(np.float32, copy=False)
