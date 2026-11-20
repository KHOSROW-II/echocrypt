"""Analyze a known preamble to estimate effective acoustic channel quality."""
from __future__ import annotations

import math
import numpy as np

from .recovery_metrics import RecoveryMetrics


def analyze_channel(samples, start: int, samples_per_symbol: int, sync_bits,
                    mark: int, space: int, sample_rate: int,
                    frequency_offset_hz: float, sync_score: float,
                    timing_offset_samples: int, sample_clock_ratio: float = 1.0,
                    decode_attempts: int = 1) -> RecoveryMetrics:
    x = np.asarray(samples, dtype=np.float64).reshape(-1)
    expected_margins = []
    for index, expected in enumerate(sync_bits):
        offset = start + index * samples_per_symbol
        segment = x[offset:offset + samples_per_symbol]
        if segment.size < samples_per_symbol:
            break
        n = np.arange(segment.size, dtype=np.float64)
        window = np.hanning(segment.size)
        mark_ref = np.exp(-2j * np.pi * (mark + frequency_offset_hz) * n / sample_rate)
        space_ref = np.exp(-2j * np.pi * (space + frequency_offset_hz) * n / sample_rate)
        mark_corr = abs(np.sum(segment * window * mark_ref)) ** 2
        space_corr = abs(np.sum(segment * window * space_ref)) ** 2
        good, bad = (mark_corr, space_corr) if expected else (space_corr, mark_corr)
        expected_margins.append((good - bad) / max(good + bad, 1e-18))
    confidence = float(np.mean(np.abs(expected_margins))) if expected_margins else 0.0
    good_bad = []
    for margin in expected_margins:
        # Convert normalized mark/space margin to a bounded effective contrast.
        ratio = (1.0 + margin) / max(1.0 - margin, 1e-6)
        good_bad.append(10.0 * math.log10(max(ratio, 1e-6)))
    contrast_db = float(np.median(good_bad)) if good_bad else -120.0
    rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    clipping = float(np.mean(np.abs(x) >= 0.995)) if x.size else 0.0
    return RecoveryMetrics(
        sample_rate=int(sample_rate), symbol_rate=int(sample_rate // samples_per_symbol),
        signal_rms=rms, signal_peak=peak, clipping_fraction=clipping,
        sync_score=float(sync_score), mean_bit_confidence=confidence,
        tone_contrast_db=contrast_db, frequency_offset_hz=float(frequency_offset_hz),
        timing_offset_samples=int(timing_offset_samples),
        sample_clock_ratio=float(sample_clock_ratio), decode_attempts=int(decode_attempts),
    )
