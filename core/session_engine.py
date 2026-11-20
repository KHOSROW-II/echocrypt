"""Central acoustic session engine for continuous RX and serialized TX.

No server, socket, TCP/UDP or network transport is used.
"""
from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
import queue
import random
import threading
import time
from typing import Callable

import numpy as np

from core.audio import ContinuousAudioListener, play, stop_playback
from core.modem import HOP_OFFSETS, SYNC_V2, demodulate, modulate
from core.recovery.errors import FrameCRCError, HeaderCRCError


# Broadcast frames need a quiet gap longer than the listener's 300 ms
# burst-ending threshold, otherwise consecutive packets can be merged.
BROADCAST_INTER_FRAME_GAP_SECONDS = 1.0


class SessionEngineError(RuntimeError):
    pass


class ChannelBusyTimeout(SessionEngineError):
    pass


@dataclass(frozen=True)
class SessionEvent:
    kind: str
    message: str = ""
    payload: object = None
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


class PacketWaiter:
    """One-shot packet predicate used for asynchronous protocol responses."""

    def __init__(self, predicate: Callable[[bytes], bool]):
        if not callable(predicate):
            raise TypeError("predicate must be callable")
        self.predicate = predicate
        self._event = threading.Event()
        self.packet = None
        self.error = None
        self.cancelled = False

    def _resolve(self, packet):
        self.packet = bytes(packet)
        self._event.set()

    def wait(self, timeout=None, cancel_event=None):
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise SessionEngineError("Acoustic response wait cancelled")
            if deadline is None:
                wait_for = 0.05 if cancel_event is not None else None
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Timed out waiting for an acoustic protocol response")
                wait_for = min(0.05, remaining) if cancel_event is not None else remaining
            if self._event.wait(wait_for):
                break
        if self.error is not None:
            raise self.error
        if self.cancelled and self.packet is None:
            raise SessionEngineError("Acoustic response waiter was cancelled")
        return self.packet

    def cancel(self, error=None):
        self.cancelled = True
        if error is not None:
            self.error = error
        self._event.set()


@dataclass
class _TransmitJob:
    packet: bytes
    fec_mode: str
    symbol_rate: int
    hop_key: bytes | None
    label: str
    future: Future
    channel_access: bool = True
    cancel_event: threading.Event | None = None


class AcousticChannelAccess:
    """Listen-before-talk with randomized backoff for a shared acoustic lane.

    It reduces collisions but cannot eliminate hidden-terminal collisions.
    """

    def __init__(self, listener, *, slot_seconds=0.12, min_idle_seconds=0.28,
                 max_backoff_slots=16, max_wait_seconds=20.0, rng=None):
        self.listener = listener
        self.slot_seconds = float(slot_seconds)
        self.min_idle_seconds = float(min_idle_seconds)
        self.max_backoff_slots = int(max_backoff_slots)
        self.max_wait_seconds = float(max_wait_seconds)
        self.rng = rng or random.SystemRandom()

    def acquire(self, running_event=None, cancel_event=None):
        """Wait for an idle acoustic lane, aborting promptly on stop/cancel."""
        deadline = time.monotonic() + self.max_wait_seconds
        idle_since = None

        def check_abort():
            if running_event is not None and not running_event.is_set():
                raise SessionEngineError("Session stopped while waiting for channel access")
            if cancel_event is not None and cancel_event.is_set():
                raise SessionEngineError("Transmission cancelled while waiting for channel access")

        def pause(seconds):
            end = time.monotonic() + max(0.0, seconds)
            while time.monotonic() < end:
                check_abort()
                time.sleep(min(0.025, max(0.0, end - time.monotonic())))
            check_abort()

        while time.monotonic() < deadline:
            check_abort()
            if self.listener.busy:
                idle_since = None
                pause(self.slot_seconds / 2)
                continue
            if idle_since is None:
                idle_since = time.monotonic()
            if time.monotonic() - idle_since < self.min_idle_seconds:
                pause(min(self.slot_seconds / 2, self.min_idle_seconds))
                continue
            slots = self.rng.randint(0, self.max_backoff_slots)
            interrupted = False
            for _ in range(slots):
                check_abort()
                if self.listener.busy:
                    interrupted = True
                    break
                if time.monotonic() >= deadline:
                    break
                pause(self.slot_seconds)
            if interrupted or self.listener.busy:
                idle_since = None
                continue
            check_abort()
            return
        check_abort()
        raise ChannelBusyTimeout("Acoustic channel did not become idle before transmit timeout")



class AudioSessionEngine:
    """Long-lived acoustic session with continuous receive and queued transmit."""

    def __init__(self, config, *, hop_key=None, symbol_rates=None,
                 max_packet_bytes=None, on_packet=None, on_event=None,
                 listener=None, inter_frame_gap=0.20, channel_access=True,
                 on_debug=None):
        self.config = config
        self.on_debug = on_debug
        self.hop_key = bytes(hop_key) if hop_key is not None else None
        self._hop_key_lock = threading.RLock()
        self._additional_receive_hop_keys = []
        self._receive_hop_keys = (self.hop_key,) if self.hop_key is not None else (None,)
        self.symbol_rates = tuple(dict.fromkeys(
            int(rate) for rate in (symbol_rates or (config.symbol_rate,))
        ))
        if not self.symbol_rates or any(rate <= 0 for rate in self.symbol_rates):
            raise ValueError("At least one positive symbol rate is required")
        self.max_packet_bytes = int(max_packet_bytes or config.max_payload)
        if not 1 <= self.max_packet_bytes <= config.max_payload:
            raise ValueError("Packet limit is outside the configured modem maximum")
        self.on_packet = on_packet
        self.on_event = on_event
        self.inter_frame_gap = max(0.0, float(inter_frame_gap))
        frequencies = {
            int(base + offset)
            for base in (config.mark_hz, config.space_hz)
            for offset in HOP_OFFSETS
        }
        maximum_airtime = max(
            (len(SYNC_V2) + 512 + 24 * (self.max_packet_bytes + 4)) / rate
            for rate in self.symbol_rates
        ) + 3.0
        self.listener = listener or ContinuousAudioListener(
            config.sample_rate, config.input_device,
            activity_frequencies=tuple(sorted(frequencies)),
            max_frame_seconds=min(600.0, max(30.0, maximum_airtime)),
            quiet_seconds=0.30,
            on_error=lambda exc: self._emit("audio_error", str(exc), payload=exc),
            on_debug=self._debug if self.on_debug is not None else None,
        )
        if channel_access == "broadcast":
            # Public rooms favor shorter contention windows because the acoustic
            # carrier is low-rate; randomized slots still reduce collisions.
            self.channel_access = AcousticChannelAccess(
                self.listener, slot_seconds=0.04, min_idle_seconds=0.12,
                max_backoff_slots=8, max_wait_seconds=5.0,
            )
        else:
            self.channel_access = AcousticChannelAccess(self.listener) if channel_access else None
        self.events = queue.Queue(maxsize=512)
        self._tx_queue = queue.Queue(maxsize=128)
        self._waiters = []
        self._waiters_lock = threading.RLock()
        self._tx_state_lock = threading.RLock()
        self._active_job = None
        self._playback_active = threading.Event()
        self._running = threading.Event()
        self._decoder_thread = None
        self._tx_thread = None
        self._started = False
        self._start_lock = threading.Lock()
        self._shutdown_thread = None

    def _debug(self, stage, message, **fields):
        """Emit privacy-safe acoustic diagnostics without affecting transport."""
        if self.on_debug is None:
            return
        try:
            self.on_debug(stage, message, **fields)
        except Exception:
            # A broken diagnostics sink must never interrupt real-time audio.
            pass

    @property
    def running(self):
        return self._running.is_set()

    @property
    def receive_hop_keys(self):
        with self._hop_key_lock:
            return tuple(self._receive_hop_keys)

    def _refresh_receive_hop_keys(self):
        # Always retain the fixed-frequency profile alongside protocol-specific
        # hopping keys so Direct and Broadcast can coexist on one listener.
        keys = [None]
        if self.hop_key is not None:
            keys.append(self.hop_key)
        keys.extend(self._additional_receive_hop_keys)
        self._receive_hop_keys = tuple(dict.fromkeys(keys))

    def set_hop_key(self, hop_key):
        """Atomically change the default TX and RX hopping key for this session."""
        if hop_key is not None:
            if not isinstance(hop_key, (bytes, bytearray, memoryview)):
                raise TypeError("hop_key must be bytes-like or None")
            hop_key = bytes(hop_key)
            if len(hop_key) != 32:
                raise ValueError("hop_key must contain exactly 32 bytes")
        with self._hop_key_lock:
            self.hop_key = hop_key
            self._refresh_receive_hop_keys()

    def add_receive_hop_key(self, hop_key):
        """Listen for an additional protocol hopping profile without changing TX."""
        if not isinstance(hop_key, (bytes, bytearray, memoryview)):
            raise TypeError("hop_key must be bytes-like")
        key = bytes(hop_key)
        if len(key) != 32:
            raise ValueError("hop_key must contain exactly 32 bytes")
        with self._hop_key_lock:
            if key not in self._additional_receive_hop_keys:
                self._additional_receive_hop_keys.append(key)
            self._refresh_receive_hop_keys()

    def remove_receive_hop_key(self, hop_key):
        key = bytes(hop_key)
        with self._hop_key_lock:
            self._additional_receive_hop_keys = [
                existing for existing in self._additional_receive_hop_keys
                if existing != key
            ]
            self._refresh_receive_hop_keys()

    def _emit(self, kind, message="", payload=None, **metadata):
        event = SessionEvent(kind, str(message), payload, metadata=metadata)
        try:
            self.events.put_nowait(event)
        except queue.Full:
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            try:
                self.events.put_nowait(event)
            except queue.Full:
                pass
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                pass

    def start(self):
        """Start the session, waiting for any prior async shutdown to finish."""
        while True:
            wait_for_shutdown = None
            with self._start_lock:
                if self._started and self._running.is_set():
                    return self
                pending = self._shutdown_thread
                if pending is not None and pending.is_alive():
                    wait_for_shutdown = pending
                else:
                    self._shutdown_thread = None
                    # A completed shutdown always clears _started. This guard
                    # also repairs state if a previous cleanup exited early.
                    self._started = False
                    self._running.set()
                    try:
                        self.listener.start()
                        self._decoder_thread = threading.Thread(
                            target=self._decoder_loop, name="echocrypt-session-rx", daemon=True
                        )
                        self._tx_thread = threading.Thread(
                            target=self._transmit_loop, name="echocrypt-session-tx", daemon=True
                        )
                        self._decoder_thread.start()
                        self._tx_thread.start()
                        self._started = True
                    except Exception:
                        self._running.clear()
                        try:
                            self.listener.stop()
                        except Exception:
                            pass
                        raise
            if wait_for_shutdown is not None:
                wait_for_shutdown.join()
                continue
            break
        self._emit("listener_started", "Continuous acoustic listener is active")
        self._debug("SESSION", "Continuous acoustic session engine started",
                    sample_rate=self.config.sample_rate,
                    symbol_rates=",".join(str(x) for x in self.symbol_rates),
                    mark_hz=self.config.mark_hz, space_hz=self.config.space_hz,
                    input_device=("SYSTEM DEFAULT" if self.config.input_device is None else self.config.input_device),
                    output_device=("SYSTEM DEFAULT" if self.config.output_device is None else self.config.output_device),
                    max_packet_bytes=self.max_packet_bytes,
                    channel_access="broadcast" if isinstance(self.channel_access, AcousticChannelAccess)
                    and self.channel_access.max_wait_seconds == 5.0 else
                    "listen-before-talk" if self.channel_access else "disabled")
        return self

    def stop(self, *, wait=True):
        """Stop the session; optionally return immediately while cleanup runs.

        The audio listener and PortAudio workers can take noticeable time to
        close. Interactive Disconnect uses wait=False so the UI exits first;
        normal mode transitions and context-manager cleanup remain synchronous.
        """
        if not wait:
            with self._start_lock:
                if not self._started:
                    return
                if self._shutdown_thread is not None and self._shutdown_thread.is_alive():
                    return
                # Signal every worker immediately, before the cleanup thread
                # waits to acquire the lifecycle lock.
                self._running.clear()
                worker = threading.Thread(
                    target=self._stop_sync, name="echocrypt-session-shutdown", daemon=True
                )
                self._shutdown_thread = worker
                worker.start()
            return

        with self._start_lock:
            worker = self._shutdown_thread
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join()
            return
        self._stop_sync()

    def _stop_sync(self):
        with self._start_lock:
            if not self._started:
                return
            self._running.clear()
            stopped_error = SessionEngineError("Acoustic session engine stopped before transmission")
            with self._tx_state_lock:
                playback_active = self._playback_active.is_set()
                if self._active_job is not None and not self._active_job.future.done():
                    self._active_job.future.set_exception(stopped_error)
                while True:
                    try:
                        pending = self._tx_queue.get_nowait()
                    except queue.Empty:
                        break
                    if pending is not None and not pending.future.done():
                        pending.future.set_exception(stopped_error)
                try:
                    self._tx_queue.put_nowait(None)
                except queue.Full:
                    pass
            try:
                self.listener.stop()
            finally:
                if playback_active:
                    try:
                        stop_playback()
                    except Exception:
                        pass
                with self._waiters_lock:
                    for waiter in self._waiters:
                        waiter.cancel(SessionEngineError("Session stopped while waiting for response"))
                    self._waiters.clear()
                # Do not declare shutdown complete while an audio worker is still
                # alive. A subsequent start must not create a second worker while
                # the previous worker still owns the audio device.
                for thread in (self._decoder_thread, self._tx_thread):
                    if thread is not None and thread is not threading.current_thread():
                        thread.join()

                # The TX loop exits when _running is cleared as well as when it
                # consumes the sentinel. If it was busy during stop, the sentinel
                # can remain queued and make the next TX worker exit immediately.
                while True:
                    try:
                        stale = self._tx_queue.get_nowait()
                    except queue.Empty:
                        break
                    if stale is not None and not stale.future.done():
                        stale.future.set_exception(stopped_error)

                self._decoder_thread = self._tx_thread = None
                self._started = False
                self._shutdown_thread = None
        self._emit("listener_stopped", "Acoustic session listener stopped")
        self._debug("SESSION", "Continuous acoustic session engine stopped")

    def expect_packet(self, predicate: Callable[[bytes], bool]) -> PacketWaiter:
        """Register before transmitting to avoid losing a fast peer response."""
        waiter = PacketWaiter(predicate)
        with self._waiters_lock:
            if not self.running:
                raise SessionEngineError("Audio session engine is not running")
            self._waiters.append(waiter)
        return waiter

    def cancel_waiter(self, waiter):
        with self._waiters_lock:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
        waiter.cancel()

    def _route_waiter(self, packet):
        with self._waiters_lock:
            candidates = list(self._waiters)
        for waiter in candidates:
            if waiter.cancelled:
                with self._waiters_lock:
                    if waiter in self._waiters:
                        self._waiters.remove(waiter)
                continue
            try:
                matched = bool(waiter.predicate(packet))
            except Exception:
                matched = False
            if matched:
                with self._waiters_lock:
                    if waiter in self._waiters:
                        self._waiters.remove(waiter)
                waiter._resolve(packet)
                self._emit("response_received", "Acoustic response matched a pending operation")
                return True
        return False

    def submit_packet(self, packet, *, fec_mode="conv", symbol_rate=None,
                      hop_key=None, disable_hopping=False, label="DATA", channel_access=None,
                      cancel_event=None):
        if not self.running:
            raise SessionEngineError("Audio session engine is not running")
        raw = bytes(packet)
        if not raw:
            raise ValueError("Cannot transmit an empty packet")
        rate = self.config.symbol_rate if symbol_rate is None else int(symbol_rate)
        future = Future()
        if not isinstance(disable_hopping, bool):
            raise TypeError("disable_hopping must be bool")
        with self._hop_key_lock:
            selected_hop_key = (
                None if disable_hopping
                else (self.hop_key if hop_key is None else bytes(hop_key))
            )
        job = _TransmitJob(
            raw, str(fec_mode), rate,
            selected_hop_key,
            str(label), future,
            self.channel_access is not None if channel_access is None else bool(channel_access),
            cancel_event,
        )
        with self._tx_state_lock:
            if not self.running:
                raise SessionEngineError("Audio session engine is not running")
            try:
                self._tx_queue.put_nowait(job)
            except queue.Full as exc:
                raise SessionEngineError("Transmit queue is full") from exc
        return future

    def cancel_transmission(self, cancel_event):
        """Cancel jobs associated with an operation and interrupt its active audio.

        Cancellation is scoped to the supplied event, so unrelated queued chat
        traffic is not affected. A currently playing frame is stopped immediately.
        """
        if cancel_event is None:
            return False
        cancel_event.set()
        with self._tx_state_lock:
            active = self._active_job
            should_stop_audio = bool(
                active is not None and active.cancel_event is cancel_event
                and self._playback_active.is_set()
            )
            active_matches = active is not None and active.cancel_event is cancel_event
        if should_stop_audio:
            try:
                stop_playback()
            except Exception:
                pass
        return bool(should_stop_audio or active_matches)

    def send_packet(self, packet, *, fec_mode="conv", symbol_rate=None,
                    hop_key=None, disable_hopping=False, label="DATA", timeout=None,
                    channel_access=None, cancel_event=None):
        future = self.submit_packet(
            packet, fec_mode=fec_mode, symbol_rate=symbol_rate,
            hop_key=hop_key, disable_hopping=disable_hopping, label=label,
            channel_access=channel_access, cancel_event=cancel_event,
        )
        return future.result(timeout=timeout)

    def _transmit_loop(self):
        while self._running.is_set():
            try:
                job = self._tx_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if job is None:
                break
            with self._tx_state_lock:
                self._active_job = job
            self._debug("TX", "Transmit job dequeued", label=job.label,
                        packet_bytes=len(job.packet), fec=job.fec_mode,
                        symbol_rate=job.symbol_rate,
                        channel_access=job.channel_access,
                        queue_depth=self._tx_queue.qsize())
            if (job.future.cancelled() or not self.running or
                    (job.cancel_event is not None and job.cancel_event.is_set())):
                if not job.future.done():
                    reason = "Transmission cancelled" if job.cancel_event is not None and job.cancel_event.is_set() else "Session stopped before transmission"
                    job.future.set_exception(SessionEngineError(reason))
                with self._tx_state_lock:
                    self._active_job = None
                continue
            try:
                if job.channel_access and self.channel_access is not None:
                    self._debug("CHANNEL", "Waiting for acoustic channel access",
                                label=job.label, listener_busy=self.listener.busy,
                                policy="listen-before-talk with randomized backoff")
                    self.channel_access.acquire(self._running, job.cancel_event)
                    self._debug("CHANNEL", "Acoustic channel access granted", label=job.label)
                if not self.running:
                    raise SessionEngineError("Session stopped before audio playback")
                if job.cancel_event is not None and job.cancel_event.is_set():
                    raise SessionEngineError("Transmission cancelled before audio playback")
                self.listener.pause()
                try:
                    self._debug("MODULATE", "Creating acoustic waveform", label=job.label,
                                packet_bytes=len(job.packet), fec=job.fec_mode,
                                symbol_rate=job.symbol_rate,
                                mark_hz=self.config.mark_hz, space_hz=self.config.space_hz,
                                hopping=job.hop_key is not None)
                    wave = modulate(
                        job.packet, self.config.sample_rate, job.symbol_rate,
                        self.config.mark_hz, self.config.space_hz,
                        self.config.gain, self.max_packet_bytes,
                        fec_mode=job.fec_mode, hop_key=job.hop_key,
                    )
                    with self._tx_state_lock:
                        if not self.running:
                            raise SessionEngineError("Session stopped before audio playback")
                        if job.cancel_event is not None and job.cancel_event.is_set():
                            raise SessionEngineError("Transmission cancelled before audio playback")
                        self._playback_active.set()
                    try:
                        play(wave, self.config.sample_rate, self.config.output_device)
                    finally:
                        self._playback_active.clear()
                    if job.cancel_event is not None and job.cancel_event.is_set():
                        raise SessionEngineError("Transmission cancelled during audio playback")
                    if self.inter_frame_gap and self.running:
                        if job.cancel_event is not None:
                            if job.cancel_event.wait(self.inter_frame_gap):
                                raise SessionEngineError("Transmission cancelled between acoustic frames")
                        else:
                            time.sleep(self.inter_frame_gap)
                    duration = len(wave) / self.config.sample_rate
                    self._debug("TX", "Acoustic waveform playback completed", label=job.label,
                                samples=len(wave), duration=f"{duration:.3f}s",
                                peak=f"{float(max(abs(float(wave.min())), abs(float(wave.max())))):.5f}")
                    if not job.future.done():
                        job.future.set_result(duration)
                    self._emit("transmitted", f"{job.label} transmitted", bytes=len(job.packet),
                               duration=duration, fec=job.fec_mode,
                               symbol_rate=job.symbol_rate)
                finally:
                    self.listener.resume()
            except Exception as exc:
                self._debug("TX_ERROR", "Transmit job failed", label=job.label,
                            error=type(exc).__name__, detail=str(exc))
                if not job.future.done():
                    job.future.set_exception(exc)
                self._emit("transmit_error", f"{job.label}: {exc}", payload=exc)
            finally:
                with self._tx_state_lock:
                    if self._active_job is job:
                        self._active_job = None

    def _decoder_loop(self):
        while self._running.is_set():
            try:
                samples = self.listener.get_frame(timeout=0.25)
            except queue.Empty:
                continue
            except Exception as exc:
                if self._running.is_set():
                    self._emit("listener_error", str(exc), payload=exc)
                continue
            if samples is None:
                break
            packet = None
            errors = []
            used_rate = None
            frame_index = self.listener.snapshot().get("captured_frames", 0) if hasattr(self.listener, "snapshot") else None
            self._debug("RX", "Captured audio frame handed to decoder",
                        samples=len(samples), duration=f"{len(samples) / self.config.sample_rate:.3f}s",
                        frame_index=frame_index,
                        peak=f"{float(np.max(np.abs(samples))):.6f}" if len(samples) else "0")
            with self._hop_key_lock:
                receive_hop_keys = tuple(self._receive_hop_keys)
            for rate in self.symbol_rates:
                strong_header_failure = False
                strong_frame_failure = False
                for hop_key in receive_hop_keys:
                    try:
                        packet = demodulate(
                            samples, self.config.sample_rate, rate,
                            self.config.mark_hz, self.config.space_hz,
                            self.max_packet_bytes, hop_key=hop_key,
                            on_debug=self._debug if self.on_debug is not None else None,
                        )
                        used_rate = rate
                        break
                    except Exception as exc:
                        errors.append(
                            f"{rate} baud/{'fixed' if hop_key is None else 'hopping'}: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        self._debug("RECOVERY", "Acoustic frame decode attempt failed",
                                    symbol_rate=rate, hopping=hop_key is not None,
                                    error=type(exc).__name__, detail=str(exc),
                                    samples=len(samples))
                        if isinstance(exc, HeaderCRCError) and exc.sync_score >= 0.50:
                            # The protected header is fixed-frequency; hopping
                            # keys affect only payload symbols. A strong header
                            # CRC failure cannot be repaired by trying another
                            # receive key, so leave this key loop immediately.
                            strong_header_failure = True
                            break
                        if isinstance(exc, FrameCRCError) and exc.sync_score >= 0.50:
                            # Payload hopping differs by key, so continue through
                            # registered receive profiles before giving up.
                            strong_frame_failure = True
                if packet is not None:
                    break
                if strong_header_failure:
                    self._debug(
                        "RECOVERY",
                        "Strong preamble found but protected header CRC failed; "
                        "hopping keys cannot repair a fixed-frequency header",
                        symbol_rate=rate,
                    )
                    break
                if strong_frame_failure:
                    self._debug(
                        "RECOVERY",
                        "Strong preamble and header found but frame CRC failed for "
                        "all registered hopping profiles; skipping unrelated baud rates",
                        symbol_rate=rate,
                    )
                    break
            if packet is None:
                self._debug("FRAME_REJECTED", "Acoustic burst rejected at all configured symbol rates",
                            attempts=len(errors), errors=" ; ".join(errors),
                            samples=len(samples))
                self._emit("frame_rejected", "Acoustic burst failed synchronization/FEC/CRC",
                           errors=tuple(errors), samples=len(samples))
                continue
            self._debug("PACKET", "Modem packet passed synchronization, FEC and CRC",
                        packet_bytes=len(packet), magic=packet[:4].decode("ascii", errors="replace"),
                        symbol_rate=used_rate)
            self._emit("frame_received", "Validated modem frame received", payload=packet,
                       bytes=len(packet), symbol_rate=used_rate)
            if self._route_waiter(packet):
                self._debug("ROUTER", "Validated packet consumed by pending protocol waiter",
                            packet_bytes=len(packet), magic=packet[:4].decode("ascii", errors="replace"))
                continue
            if self.on_packet is not None:
                try:
                    self._debug("ROUTER", "Dispatching validated packet to application protocol handler",
                                packet_bytes=len(packet),
                                magic=packet[:4].decode("ascii", errors="replace"))
                    self.on_packet(packet)
                    self._debug("ROUTER", "Application protocol handler returned",
                                packet_bytes=len(packet),
                                magic=packet[:4].decode("ascii", errors="replace"))
                except Exception as exc:
                    self._debug("PROTOCOL_ERROR", "Application protocol handler rejected packet",
                                error=type(exc).__name__, detail=str(exc),
                                packet_bytes=len(packet),
                                magic=packet[:4].decode("ascii", errors="replace"))
                    self._emit("protocol_error", str(exc), payload=exc,
                               packet_bytes=len(packet))

    def next_event(self, timeout=None):
        return self.events.get(timeout=timeout)

    def drain_events(self, limit=100):
        result = []
        for _ in range(max(0, int(limit))):
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                break
        return result

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False
