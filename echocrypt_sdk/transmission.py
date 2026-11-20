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

    def wait(self, timeout: Optional[float] = None) -> TransmissionResult:
        """Wait for completion and return local frame playback duration.

        A timeout only limits this wait; it does not cancel transmission.
        Call cancel() explicitly to request cancellation.
        """
        duration = self._future.result(timeout=timeout)
        return TransmissionResult(
            payload_bytes=self.payload_bytes,
            duration_seconds=float(duration),
            label=self.label,
            completed_at=time.time(),
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
        self._future.add_done_callback(lambda _future: callback(self))
