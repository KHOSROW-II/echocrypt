"""Retry-oriented FEC selection for the current stop-and-wait transport."""
from __future__ import annotations


class AdaptiveFECController:
    """Choose a bounded retry progression using only feedback actually available.

    The initial mode is selected by the caller. Retries first try a distinct,
    generally more robust coding family before falling back to alternatives.
    """

    _PLANS = {
        "raw": ("raw", "hamming", "conv", "repetition3"),
        "hamming": ("hamming", "conv", "repetition3"),
        "conv": ("conv", "repetition3", "conv"),
        "repetition3": ("repetition3", "conv", "hamming"),
    }

    def plan(self, first_mode="hamming", attempts=4):
        if first_mode not in self._PLANS:
            raise ValueError("Unsupported initial FEC mode")
        if not isinstance(attempts, int) or attempts < 1:
            raise ValueError("attempts must be a positive integer")
        source = self._PLANS[first_mode]
        plan = list(source)
        while len(plan) < attempts:
            # Retrying the strongest available profile remains bounded and
            # preserves stop-and-wait semantics for the same ciphertext.
            plan.append(source[-1])
        return tuple(plan[:attempts])

    def mode_for_attempt(self, attempt: int, first_mode="hamming", attempts=4):
        if not isinstance(attempt, int) or attempt < 1:
            raise ValueError("attempt must be >= 1")
        return self.plan(first_mode, max(attempts, attempt))[attempt - 1]
