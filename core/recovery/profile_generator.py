"""Bounded, validated ASRE search profile generation."""
from __future__ import annotations

from dataclasses import dataclass

from .timing_recovery import CLOCK_RATIO_CANDIDATES


@dataclass(frozen=True)
class RecoveryProfile:
    name: str
    clock_ratios: tuple[float, ...] = CLOCK_RATIO_CANDIDATES
    frequency_search_limit_hz: int = 240
    frequency_search_step_hz: int = 10


def default_profile() -> RecoveryProfile:
    return RecoveryProfile("ASRE-BALANCED")


def profile_for_grade(grade: str) -> RecoveryProfile:
    """Return a small validated profile; never generate unbounded knob sets."""
    normalized = str(grade).upper()
    if normalized == "GOOD":
        return RecoveryProfile("ASRE-LOW-LATENCY",
                               (1.0, 1.00005, 0.99995, 1.0001, 0.9999), 180, 10)
    if normalized == "FAIR":
        return default_profile()
    if normalized == "WEAK":
        return RecoveryProfile("ASRE-RESILIENT", CLOCK_RATIO_CANDIDATES, 360, 10)
    raise ValueError("Unknown channel grade")
