"""Structured channel and recovery measurements (diagnostic, not calibrated BER)."""
from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class RecoveryMetrics:
    sample_rate: int
    symbol_rate: int
    signal_rms: float
    signal_peak: float
    clipping_fraction: float
    sync_score: float
    mean_bit_confidence: float
    tone_contrast_db: float
    frequency_offset_hz: float
    timing_offset_samples: int
    sample_clock_ratio: float = 1.0
    decode_attempts: int = 1

    def as_dict(self):
        return asdict(self)

    @property
    def channel_grade(self) -> str:
        # Clipping is irreversible information loss. A visually strong tone
        # contrast must never hide a saturated ADC/input path.
        if self.clipping_fraction >= 0.01:
            return "WEAK"
        if self.clipping_fraction >= 0.002:
            return "FAIR"
        if self.tone_contrast_db >= 12.0 and self.mean_bit_confidence >= 0.82:
            return "GOOD"
        if self.tone_contrast_db >= 5.0 and self.mean_bit_confidence >= 0.55:
            return "FAIR"
        return "WEAK"
