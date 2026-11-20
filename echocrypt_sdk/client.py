"""Application-independent client facade over the ECHO-CRYPT acoustic engine.

This module contains no terminal/UI dependencies. Event callbacks run on a
dedicated dispatcher thread, never on the modem's decoder thread.
"""
from __future__ import annotations

import math
import queue
import threading
import time
from typing import Callable, Optional

from core.session_engine import AudioSessionEngine

from .config import AcousticConfig, _SUPPORTED_FEC
from .events import AcousticEvent
from .exceptions import (
    ClientClosedError,
    ClientNotRunningError,
    PacketTooLargeError,
    ReceiveTimeout,
)
from .transmission import Transmission


_SENTINEL = object()


class Subscription:
    """A removable event subscription."""

    def __init__(self, unsubscribe: Callable[[], None]):
        self._unsubscribe = unsubscribe
        self._closed = False
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._unsubscribe()

    unsubscribe = close

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class PacketWaiter:
    """One-shot waiter for a response matching a fast predicate.

    The predicate is evaluated on the modem decoder thread and must therefore
    be short, deterministic, and non-blocking.
    """

    def __init__(self, engine, waiter):
        self._engine = engine
        self._waiter = waiter
        self._closed = False
        self._lock = threading.Lock()

    def wait(self, timeout: Optional[float] = None) -> bytes:
        try:
            packet = self._waiter.wait(timeout=timeout)
        except TimeoutError:
            self.cancel()
            raise
        with self._lock:
            self._closed = True
        return bytes(packet)

    result = wait

    def cancel(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            if getattr(self._waiter, "packet", None) is not None:
                self._closed = True
                return False
            self._closed = True
        self._engine.cancel_waiter(self._waiter)
        return True


class AcousticClient:
    """Public developer client for sending and receiving acoustic packets.

    The client transports opaque byte packets. Modem framing, FEC and CRC are
    applied by ECHO-CRYPT, but this low-level API does not encrypt, authenticate,
    identify senders, or guarantee remote delivery. Applications needing those
    properties must use an authenticated protocol layer.
    """

    def __init__(
        self,
        config: Optional[AcousticConfig] = None,
        *,
        on_debug: Optional[Callable[..., object]] = None,
        receive_queue_size: int = 128,
        event_queue_size: int = 512,
    ):
        if config is None:
            config = AcousticConfig()
        if not isinstance(config, AcousticConfig):
            raise TypeError("config must be an AcousticConfig instance")
        if isinstance(receive_queue_size, bool) or not isinstance(receive_queue_size, int) \
                or receive_queue_size < 1:
            raise ValueError("receive_queue_size must be a positive integer")
        if isinstance(event_queue_size, bool) or not isinstance(event_queue_size, int) \
                or event_queue_size < 1:
            raise ValueError("event_queue_size must be a positive integer")

        self.config = config
        self._packets = queue.Queue(maxsize=receive_queue_size)
        self._events = queue.Queue(maxsize=event_queue_size)
        self._callbacks = {}
        self._callback_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._dispatcher_thread = None
        self._closed = False
        self._dropped_packets = 0
        self._dropped_events = 0
        self._callback_errors = 0
        self._debug_callback = on_debug

        self._engine = AudioSessionEngine(
            config.to_core_config(),
            symbol_rates=config.symbol_rates,
            max_packet_bytes=config.max_packet_bytes,
            on_packet=self._handle_packet,
            on_event=self._handle_engine_event,
            inter_frame_gap=config.inter_frame_gap,
            channel_access=config.channel_access,
            on_debug=self._handle_debug if on_debug is not None else None,
        )

    @property
    def running(self) -> bool:
        return bool(self._engine.running)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def dropped_packet_count(self) -> int:
        return self._dropped_packets

    @property
    def dropped_event_count(self) -> int:
        return self._dropped_events

    @property
    def callback_error_count(self) -> int:
        return self._callback_errors

    def _ensure_dispatcher(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise ClientClosedError("This AcousticClient has been closed")
            if self._dispatcher_thread is not None and self._dispatcher_thread.is_alive():
                return
            self._dispatcher_thread = threading.Thread(
                target=self._dispatch_loop,
                name="echocrypt-sdk-events",
                daemon=True,
            )
            self._dispatcher_thread.start()

    def start(self) -> "AcousticClient":
        """Start continuous microphone reception and the transmit queue."""
        with self._lifecycle_lock:
            if self._closed:
                raise ClientClosedError("This AcousticClient has been closed")
            self._ensure_dispatcher()
            self._engine.start()
        return self

    def stop(self, *, wait: bool = True) -> None:
        """Stop audio capture and transmission; the client may be restarted."""
        if self._closed:
            return
        self._engine.stop(wait=wait)

    def close(self, *, wait: bool = True) -> None:
        """Permanently stop the client and release its event dispatcher."""
        with self._lifecycle_lock:
            if self._closed:
                return
            try:
                self._engine.stop(wait=wait)
            finally:
                self._closed = True
                dispatcher = self._dispatcher_thread
                try:
                    self._events.put_nowait(_SENTINEL)
                except queue.Full:
                    try:
                        self._events.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self._events.put_nowait(_SENTINEL)
                    except queue.Full:
                        pass
        if wait and dispatcher is not None and dispatcher is not threading.current_thread():
            dispatcher.join(timeout=2.0)

    def subscribe(
        self,
        callback: Callable[[AcousticEvent], object],
        *,
        event: str = "*",
    ) -> Subscription:
        """Subscribe to one event name, or '*' for every event."""
        if self._closed:
            raise ClientClosedError("This AcousticClient has been closed")
        if not callable(callback):
            raise TypeError("callback must be callable")
        if not isinstance(event, str) or not event.strip():
            raise ValueError("event must be a non-empty string")
        event = event.strip()
        token = object()
        with self._callback_lock:
            self._callbacks.setdefault(event, {})[token] = callback

        def unsubscribe():
            with self._callback_lock:
                handlers = self._callbacks.get(event)
                if handlers is None:
                    return
                handlers.pop(token, None)
                if not handlers:
                    self._callbacks.pop(event, None)

        return Subscription(unsubscribe)

    def on(self, event: str, callback: Callable[[AcousticEvent], object]) -> Subscription:
        """Alias for subscribe(callback, event=...)."""
        return self.subscribe(callback, event=event)

    def _publish(self, event: AcousticEvent) -> None:
        try:
            self._events.put_nowait(event)
        except queue.Full:
            try:
                self._events.get_nowait()
                self._dropped_events += 1
            except queue.Empty:
                pass
            try:
                self._events.put_nowait(event)
            except queue.Full:
                self._dropped_events += 1

    def _handle_engine_event(self, raw_event) -> None:
        self._publish(
            AcousticEvent(
                name=str(raw_event.kind),
                message=str(getattr(raw_event, "message", "")),
                data=getattr(raw_event, "payload", None),
                timestamp=float(getattr(raw_event, "timestamp", time.time())),
                metadata=dict(getattr(raw_event, "metadata", {}) or {}),
            )
        )

    def _handle_debug(self, stage: str, message: str, **fields) -> None:
        self._publish(
            AcousticEvent(
                name="debug",
                message=str(message),
                data={"stage": str(stage)},
                metadata=dict(fields),
            )
        )

    def _handle_packet(self, packet: bytes) -> None:
        raw = bytes(packet)
        try:
            self._packets.put_nowait(raw)
        except queue.Full:
            try:
                self._packets.get_nowait()
                self._dropped_packets += 1
            except queue.Empty:
                pass
            try:
                self._packets.put_nowait(raw)
            except queue.Full:
                self._dropped_packets += 1
        self._publish(
            AcousticEvent(
                name="packet_received",
                message="Validated acoustic packet received",
                data=raw,
                metadata={"bytes": len(raw)},
            )
        )

    def _dispatch_loop(self) -> None:
        while True:
            event = self._events.get()
            if event is _SENTINEL:
                return
            if event.name == "debug" and self._debug_callback is not None:
                try:
                    self._debug_callback(
                        event.data.get("stage", ""),
                        event.message,
                        **dict(event.metadata),
                    )
                except Exception:
                    self._callback_errors += 1
            with self._callback_lock:
                callbacks = list(self._callbacks.get(event.name, {}).values())
                callbacks.extend(self._callbacks.get("*", {}).values())
            for callback in callbacks:
                try:
                    callback(event)
                except Exception:
                    self._callback_errors += 1

    def receive(self, timeout: Optional[float] = None) -> bytes:
        """Return the next validated packet, raising ReceiveTimeout on timeout."""
        if self._closed and self._packets.empty():
            raise ClientClosedError("This AcousticClient has been closed")
        if timeout is not None:
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) \
                    or not math.isfinite(timeout) or timeout < 0:
                raise ValueError("timeout must be a finite non-negative number or None")
        try:
            return self._packets.get(timeout=timeout)
        except queue.Empty as exc:
            raise ReceiveTimeout("No acoustic packet arrived before the timeout") from exc

    def expect_packet(self, predicate: Callable[[bytes], bool]) -> PacketWaiter:
        """Register a one-shot packet waiter before sending a request."""
        if not callable(predicate):
            raise TypeError("predicate must be callable")
        if not self.running:
            raise ClientNotRunningError("Start the AcousticClient before waiting for packets")
        return PacketWaiter(self._engine, self._engine.expect_packet(predicate))

    def wait_for_packet(
        self,
        predicate: Callable[[bytes], bool],
        timeout: Optional[float] = None,
    ) -> bytes:
        """Wait for one packet matching predicate."""
        waiter = self.expect_packet(predicate)
        return waiter.wait(timeout=timeout)

    def send_async(
        self,
        packet: bytes,
        *,
        fec_mode: Optional[str] = None,
        symbol_rate: Optional[int] = None,
        hop_key: Optional[bytes] = None,
        label: str = "DATA",
        channel_access: Optional[bool] = None,
    ) -> Transmission:
        """Queue one opaque packet and return a handle that can cancel it."""
        if self._closed:
            raise ClientClosedError("This AcousticClient has been closed")
        if not self.running:
            raise ClientNotRunningError("Start the AcousticClient before transmitting")
        if not isinstance(packet, (bytes, bytearray, memoryview)):
            raise TypeError("packet must be bytes, bytearray, or memoryview")
        raw = bytes(packet)
        if not raw:
            raise ValueError("packet cannot be empty")
        if len(raw) > self.config.max_packet_bytes:
            raise PacketTooLargeError(
                f"Packet has {len(raw)} bytes; configured limit is {self.config.max_packet_bytes}"
            )

        selected_fec = self.config.default_fec_mode if fec_mode is None else fec_mode
        if selected_fec not in _SUPPORTED_FEC:
            raise ValueError("Unsupported FEC mode: " + str(selected_fec))
        if symbol_rate is not None:
            if isinstance(symbol_rate, bool) or not isinstance(symbol_rate, int):
                raise TypeError("symbol_rate must be an integer")
            if symbol_rate <= 0 or self.config.sample_rate % symbol_rate \
                    or self.config.sample_rate // symbol_rate < 32:
                raise ValueError("symbol_rate is invalid for the configured sample rate")
        if hop_key is not None:
            if not isinstance(hop_key, (bytes, bytearray, memoryview)):
                raise TypeError("hop_key must be bytes-like or None")
            hop_key = bytes(hop_key)
        if channel_access is not None and not isinstance(channel_access, bool):
            raise TypeError("channel_access must be bool or None")
        if channel_access is True and self.config.channel_access is False:
            raise ValueError(
                "channel_access is disabled for this client; enable it in AcousticConfig first"
            )

        clean_label = str(label).strip() or "DATA"
        cancel_event = threading.Event()
        future = self._engine.submit_packet(
            raw,
            fec_mode=selected_fec,
            symbol_rate=symbol_rate,
            hop_key=hop_key,
            label=clean_label,
            channel_access=channel_access,
            cancel_event=cancel_event,
        )
        return Transmission(
            future=future,
            cancel_event=cancel_event,
            cancel_callback=self._engine.cancel_transmission,
            payload_bytes=len(raw),
            label=clean_label,
        )

    def send(
        self,
        packet: bytes,
        *,
        timeout: Optional[float] = None,
        fec_mode: Optional[str] = None,
        symbol_rate: Optional[int] = None,
        hop_key: Optional[bytes] = None,
        label: str = "DATA",
        channel_access: Optional[bool] = None,
    ):
        """Queue a packet and block until its local playback completes.

        This reports local playback completion, not remote receipt or delivery.
        A timeout limits the wait and does not cancel the queued transmission.
        """
        transmission = self.send_async(
            packet,
            fec_mode=fec_mode,
            symbol_rate=symbol_rate,
            hop_key=hop_key,
            label=label,
            channel_access=channel_access,
        )
        return transmission.wait(timeout=timeout)

    send_packet_async = send_async
    send_packet = send

    def __enter__(self) -> "AcousticClient":
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
