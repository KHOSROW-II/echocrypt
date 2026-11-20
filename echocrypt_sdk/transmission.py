"""Transmission handles returned by asynchronous packet sends."""
from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
import threading
import time
from typing import Callable, Optional


@dataclass(frozen=True)
class TransmissionResult:
    """Result returned after one acoustic frame has finished playing."""

    payload_bytes: int
    duration_seconds: float
    label: str
    completed_at: float


class Transmission:
    """Cancelable handle for one queued acoustic packet transmission."""

    def __init__(
        self,
        future: Future,
        cancel_event: threading.Event,
        cancel_callback: Callable[[threading.Event], object],
        payload_bytes: int,
        label: str,
    ):
        self._future = future
        self._cancel_event = cancel_event
        self._cancel_callback = cancel_callback
        self.payload_bytes = int(payload_bytes)
        self.label = str(label)
        self._completed_at = None
        self._future.add_done_callback(self._mark_completed)

    def _mark_completed(self, _future) -> None:
        self._completed_at = time.time()

    def wait(self, timeout: Optional[float] = None) -> TransmissionResult:
        """Wait for completion and return local frame playback duration.

        A timeout only limits this wait; it does not cancel transmission.
        Call cancel() explicitly to request cancellation.
        """
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or timeout < 0 or timeout == float("inf") or timeout != timeout
        ):
            raise ValueError("timeout must be a finite non-negative number or None")
        duration = self._future.result(timeout=timeout)
        return TransmissionResult(
            payload_bytes=self.payload_bytes,
            duration_seconds=float(duration),
            label=self.label,
            completed_at=self._completed_at or time.time(),
        )

    result = wait

    def cancel(self) -> bool:
        """Request cancellation; queued and active jobs are both supported."""
        if self._future.done():
            return False
        self._cancel_event.set()
        self._cancel_callback(self._cancel_event)
        return True

    @property
    def done(self) -> bool:
        return self._future.done()

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def exception(self, timeout: Optional[float] = None):
        return self._future.exception(timeout=timeout)

    def add_done_callback(self, callback: Callable[["Transmission"], object]) -> None:
        if not callable(callback):
            raise TypeError("callback must be callable")
        def dispatch(_future):
            def invoke():
                try:
                    callback(self)
                except Exception:
                    # User callback exceptions must never escape on the TX worker.
                    pass

            threading.Thread(
                target=invoke,
                name="echocrypt-sdk-send-callback",
                daemon=True,
            ).start()

        self._future.add_done_callback(dispatch)
