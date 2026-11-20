"""Adaptive Signal Recovery Engine orchestration."""
from __future__ import annotations

import time
import struct

from .profile_generator import default_profile, profile_for_grade
from .timing_recovery import resample_clock
from .errors import HeaderCRCError, PreambleNotFoundError


class AdaptiveSignalRecoveryEngine:
    """Run bounded timing-recovery attempts around a caller-supplied decoder.

    The decoder must return ``(payload, metrics)``. Metrics are returned only
    after the frame has passed the modem CRC; the protocol layer must still
    perform its own AEAD authentication before exposing application data.
    """

    def __init__(self, profile=None):
        self.profile = profile or default_profile()
        self.last_metrics = None
        self.last_attempt_count = 0

    def recover(self, samples, decoder, on_debug=None, timing_recovery=True):
        active_profile = self.profile
        profiles = [active_profile]
        balanced = default_profile()
        if active_profile.name != balanced.name:
            # Keep a robust fallback for sudden channel changes after an easy frame.
            profiles.append(balanced)
        last_error = None
        started = time.monotonic()
        attempts = 0
        for profile in profiles:
            candidates = profile.clock_ratios if timing_recovery else (1.0,)
            for ratio in candidates:
                attempts += 1
                adjusted = resample_clock(samples, ratio)
                if ratio != 1.0 and on_debug:
                    try:
                        on_debug("TIMING", "ASRE testing bounded sample-clock candidate",
                                 sample_clock_ratio=f"{ratio:.4f}", attempt=attempts,
                                 profile=profile.name)
                    except Exception:
                        pass
                try:
                    payload, metrics = decoder(
                        adjusted, sample_clock_ratio=ratio, decode_attempts=attempts,
                        recovery_profile=profile)
                    self.last_attempt_count = attempts
                    self.last_metrics = metrics
                    self.profile = profile_for_grade(metrics.channel_grade)
                    if on_debug:
                        try:
                            on_debug("ASRE", "Adaptive recovery accepted a CRC-valid modem frame",
                                     profile=profile.name, next_profile=self.profile.name,
                                     attempts=attempts, channel_grade=metrics.channel_grade,
                                     tone_contrast_db=f"{metrics.tone_contrast_db:.2f}",
                                     confidence=f"{metrics.mean_bit_confidence:.3f}",
                                     frequency_offset_hz=metrics.frequency_offset_hz,
                                     sample_clock_ratio=f"{ratio:.4f}",
                                     elapsed_ms=f"{(time.monotonic() - started) * 1000:.1f}")
                        except Exception:
                            pass
                    return payload, metrics
                except (ValueError, struct.error) as exc:
                    last_error = exc
                    if on_debug:
                        try:
                            on_debug("RECOVERY", "Candidate failed frame validation",
                                     sample_clock_ratio=f"{ratio:.4f}", profile=profile.name,
                                     error=type(exc).__name__, detail=str(exc))
                        except Exception:
                            pass
                    if isinstance(exc, PreambleNotFoundError):
                        # Clock resampling cannot rescue a capture that has no
                        # recognizable preamble in the full pre-roll search.
                        break
                    if isinstance(exc, HeaderCRCError) and (
                            exc.sync_score >= 0.50 or attempts >= 5):
                        # A strong, stable preamble with a failed header CRC is
                        # not helped by blindly testing all 17 clock ratios.
                        # Keep recovery bounded so RX can reach queued fragments.
                        break
            if isinstance(last_error, PreambleNotFoundError) or (
                    isinstance(last_error, HeaderCRCError) and
                    (last_error.sync_score >= 0.50 or attempts >= 5)):
                break
        self.last_attempt_count = attempts
        if last_error is None:
            raise ValueError("ASRE has no recovery candidates")
        if isinstance(last_error, HeaderCRCError):
            raise HeaderCRCError(
                f"ASRE stopped after {attempts} bounded candidates: {last_error}",
                sync_score=last_error.sync_score,
            ) from last_error
        raise ValueError(f"ASRE exhausted {attempts} recovery candidates: {last_error}") from last_error

