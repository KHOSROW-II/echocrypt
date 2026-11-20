"""Authenticated Direct peer API for ECHO-CRYPT applications."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import queue
import threading
import time

from core.reliability import ReliableTextChannel, ReliableProtocolError
from security.peer_handshake import PeerHandshake, HandshakeError


SDK_BYTES_MAGIC = b"ECSK"
SDK_BYTES_AAD = b"ECHOCRYPT/SDK/BYTES/v1"


class DirectPeerError(RuntimeError):
    """Direct peer protocol or lifecycle error."""


@dataclass(frozen=True)
class DirectSendResult:
    message_id: str
    attempts: int
    acknowledged: bool
    local_transmission: object = None


@dataclass(frozen=True)
class DirectMessage:
    message_id: str
    text: str
    duplicate: bool = False


class DirectPeer:
    """A mutually authenticated Direct peer bound to an AcousticClient.

    One DirectPeer owns one active authenticated session on its AcousticClient.
    Broadcast receive profiles may coexist with the Direct hopping profile.
    """

    def __init__(
        self,
        client,
        handshake: PeerHandshake,
        *,
        ack_timeout: float = 8.0,
        retries: int = 3,
    ):
        if not isinstance(handshake, PeerHandshake):
            raise TypeError("handshake must be a PeerHandshake")
        if not getattr(client, "running", False):
            raise DirectPeerError("Start AcousticClient before creating DirectPeer")
        if isinstance(ack_timeout, bool) or not isinstance(ack_timeout, (int, float)) \
                or ack_timeout <= 0 or ack_timeout == float("inf") or ack_timeout != ack_timeout:
            raise ValueError("ack_timeout must be a finite positive number")
        if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 10:
            raise ValueError("retries must be an integer between 0 and 10")

        self.client = client
        self.handshake = handshake
        self.ack_timeout = float(ack_timeout)
        self.retries = retries
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._closed = False
        self._connected = threading.Event()
        self._messages = queue.Queue(maxsize=256)
        self._packets = queue.Queue(maxsize=256)
        self._packet_handlers = {}
        self._raw_packet_handlers = {}
        self._pending_ack_id = None
        self._pending_ack_event = None
        self.protocol_error_count = 0
        self._text_channel = None
        self._subscription = client.on("packet_received", self._on_packet)
        if handshake.established:
            self._activate_protocol()

    @property
    def connected(self) -> bool:
        return self._connected.is_set() and self.handshake.established and not self._closed

    @property
    def peer_fingerprint(self) -> str:
        return self.handshake.peer_fingerprint

    def _ensure_open(self):
        if self._closed:
            raise DirectPeerError("DirectPeer has been closed")

    def _send_packet(self, packet, *, handshake_control=False, label="DIRECT"):
        self._ensure_open()
        options = {"label": label}
        if handshake_control:
            options["disable_hopping"] = True
        else:
            if not self.handshake.established or self.handshake.session is None:
                raise DirectPeerError("Direct handshake is not established")
            options["hop_key"] = self.handshake.session.hopping_key
        transmission = self.client.send_async(bytes(packet), **options)
        return transmission

    def _activate_protocol(self):
        if not self.handshake.established or self.handshake.session is None:
            return
        self.client.set_hop_key(self.handshake.session.hopping_key)
        with self._lock:
            if self._text_channel is None:
                self._text_channel = ReliableTextChannel(self.handshake)
            self._connected.set()

    def _on_packet(self, event):
        if self._closed:
            return
        packet = bytes(event.data)
        if self.handshake.established:
            # The responder may need to replay FINISH-R if the previous final
            # handshake flight was lost. PeerHandshake recognizes only the
            # exact authenticated duplicate; all application frames fall
            # through to the secure protocol router.
            try:
                response = self.handshake.receive(packet)
            except HandshakeError:
                self._process_secure_packet(packet)
                return
            except Exception:
                self.protocol_error_count += 1
                return
            if response is not None:
                try:
                    self._send_packet(
                        response, handshake_control=True, label="DIRECT HANDSHAKE RETRY"
                    ).wait()
                except Exception:
                    self.protocol_error_count += 1
            return

        try:
            response = self.handshake.receive(packet)
        except (HandshakeError, ValueError, TypeError):
            return
        except Exception:
            self.protocol_error_count += 1
            return
        if response is not None:
            try:
                self._send_packet(
                    response, handshake_control=True, label="DIRECT HANDSHAKE"
                ).wait()
            except Exception:
                self.protocol_error_count += 1
                return
        if self.handshake.established:
            try:
                self._activate_protocol()
            except Exception:
                self.protocol_error_count += 1
        return

    def _process_secure_packet(self, packet):
        with self._lock:
            raw_handlers = list(self._raw_packet_handlers.values())
        for handler in raw_handlers:
            try:
                if handler(packet):
                    return
            except Exception:
                self.protocol_error_count += 1
                return

        channel = self._text_channel
        if channel is None or not self.handshake.established:
            return

        # Reliable text DATA and ACK use different AAD domains. Failed AEAD
        # attempts do not advance the SecureChannel replay window.
        try:
            incoming = channel.receive(packet)
        except Exception:
            incoming = None
        if incoming is not None:
            try:
                self._send_packet(incoming.ack_packet, label="DIRECT TEXT ACK").wait()
            except Exception:
                self.protocol_error_count += 1
                return
            if not incoming.duplicate and incoming.text is not None:
                message = DirectMessage(
                    incoming.message_id.hex(), incoming.text, False
                )
                try:
                    self._messages.put_nowait(message)
                except queue.Full:
                    self.protocol_error_count += 1
            return

        with self._lock:
            pending_id = self._pending_ack_id
            pending_event = self._pending_ack_event
        if pending_id is not None and pending_event is not None:
            try:
                if channel.verify_ack(packet, pending_id):
                    pending_event.set()
                    return
            except Exception:
                pass

        try:
            plaintext = self.handshake.session.rx.decrypt(packet, SDK_BYTES_AAD)
        except Exception:
            plaintext = None
        if plaintext is not None:
            if not plaintext.startswith(SDK_BYTES_MAGIC):
                self.protocol_error_count += 1
                return
            payload = plaintext[len(SDK_BYTES_MAGIC):]
            with self._lock:
                handlers = list(self._packet_handlers.values())
            for handler in handlers:
                try:
                    if handler(payload):
                        return
                except Exception:
                    self.protocol_error_count += 1
                    return
            try:
                self._packets.put_nowait(payload)
            except queue.Full:
                self.protocol_error_count += 1
            return

    def _publish_packet(self, message):
        # Packet handlers are intentionally synchronous and documented as fast;
        # the outer AcousticClient callback itself runs on its dispatcher thread.
        with self._lock:
            handlers = list(self._packet_handlers.values())
        for handler in handlers:
            try:
                handler(message)
            except Exception:
                self.protocol_error_count += 1

    def connect(self, *, timeout=30.0, retry_interval=0.75):
        """Run the initiator side of the mutually authenticated handshake."""
        self._ensure_open()
        if self.handshake.role != "initiator":
            raise DirectPeerError("connect() is only valid for an initiator")
        if self.handshake.established:
            self._activate_protocol()
            return self
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
                or not math.isfinite(float(timeout)) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if not isinstance(retry_interval, (int, float)) or retry_interval <= 0:
            raise ValueError("retry_interval must be positive")
        hello = self.handshake.retry_packet
        if hello is None:
            hello = self.handshake.start()
        deadline = time.monotonic() + float(timeout)
        while not self._connected.is_set():
            if time.monotonic() >= deadline:
                raise TimeoutError("Direct handshake timed out")
            retry_packet = self.handshake.retry_packet or hello
            self._send_packet(
                retry_packet, handshake_control=True, label="DIRECT HANDSHAKE HELLO/RETRY"
            ).wait()
            self._connected.wait(min(float(retry_interval), max(0.0, deadline - time.monotonic())))
        return self

    def accept(self, *, timeout=30.0):
        """Run the responder side; the initiator retransmits HELLO if needed."""
        self._ensure_open()
        if self.handshake.role != "responder":
            raise DirectPeerError("accept() is only valid for a responder")
        if self.handshake.established:
            self._activate_protocol()
            return self
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
                or not math.isfinite(float(timeout)) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if not self._connected.wait(float(timeout)):
            raise TimeoutError("Direct handshake timed out waiting for peer")
        return self

    def send_text(self, text, *, ack_timeout=None, retries=None):
        """Send UTF-8 text with authenticated stop-and-wait acknowledgement."""
        self._ensure_open()
        if not self.connected:
            raise DirectPeerError("Direct peer is not connected")
        if ack_timeout is not None and isinstance(ack_timeout, bool):
            raise ValueError("ack_timeout must be a finite positive number")
        selected_timeout = self.ack_timeout if ack_timeout is None else float(ack_timeout)
        selected_retries = self.retries if retries is None else retries
        if not math.isfinite(selected_timeout) or selected_timeout <= 0:
            raise ValueError("ack_timeout must be finite and positive")
        if isinstance(selected_retries, bool) or not isinstance(selected_retries, int) \
                or not 0 <= selected_retries <= 10:
            raise ValueError("retries must be between 0 and 10")
        with self._send_lock:
            outgoing = self._text_channel.prepare(
                text, max_packet_size=self.client.config.max_packet_bytes
            )
            ack_event = threading.Event()
            with self._lock:
                self._pending_ack_id = outgoing.message_id
                self._pending_ack_event = ack_event
            last_tx = None
            try:
                for attempt in range(1, selected_retries + 2):
                    last_tx = self._send_packet(
                        outgoing.packet, label=f"DIRECT TEXT {attempt}"
                    )
                    last_tx.wait()
                    if ack_event.wait(selected_timeout):
                        return DirectSendResult(
                            outgoing.message_id_hex, attempt, True, last_tx
                        )
                raise TimeoutError(
                    f"No authenticated ACK after {selected_retries + 1} attempts"
                )
            finally:
                with self._lock:
                    if self._pending_ack_event is ack_event:
                        self._pending_ack_id = None
                        self._pending_ack_event = None

    def receive_text(self, timeout=None) -> DirectMessage:
        self._ensure_open()
        try:
            return self._messages.get(timeout=timeout)
        except queue.Empty as exc:
            from .exceptions import ReceiveTimeout
            raise ReceiveTimeout("No Direct text message arrived") from exc

    def send_bytes(self, payload, *, label="DIRECT BYTES"):
        """Send one encrypted application-defined byte payload."""
        self._ensure_open()
        if not self.connected:
            raise DirectPeerError("Direct peer is not connected")
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("payload must be bytes-like")
        raw_payload = bytes(payload)
        if len(raw_payload) + len(SDK_BYTES_MAGIC) + 32 > self.client.config.max_packet_bytes:
            raise ValueError("Application payload exceeds the configured modem packet limit")
        raw = SDK_BYTES_MAGIC + raw_payload
        packet = self.handshake.session.tx.encrypt(raw, SDK_BYTES_AAD)
        return self._send_packet(packet, label=label)

    def receive_bytes(self, timeout=None) -> bytes:
        self._ensure_open()
        try:
            return self._packets.get(timeout=timeout)
        except queue.Empty as exc:
            from .exceptions import ReceiveTimeout
            raise ReceiveTimeout("No Direct application packet arrived") from exc

    def send_json(self, topic, payload):
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("topic must be a non-empty string")
        envelope = json.dumps(
            {"topic": topic, "payload": payload},
            ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        return self.send_bytes(envelope, label="DIRECT JSON")

    def receive_json(self, timeout=None):
        raw = self.receive_bytes(timeout=timeout)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DirectPeerError("Received application payload is not valid JSON") from exc
        if not isinstance(value, dict) or set(value) != {"topic", "payload"}:
            raise DirectPeerError("Received JSON envelope has an invalid shape")
        return value

    def add_raw_packet_handler(self, handler):
        """Register a fast handler for encrypted session packets.

        The handler receives the original modem payload and must authenticate
        and consume only its own protocol frames. It runs before text routing.
        """
        self._ensure_open()
        if not callable(handler):
            raise TypeError("handler must be callable")
        token = object()
        with self._lock:
            self._raw_packet_handlers[token] = handler

        def unsubscribe():
            with self._lock:
                self._raw_packet_handlers.pop(token, None)

        from .client import Subscription
        return Subscription(unsubscribe)

    def send_secure_frame(self, packet, *, label="DIRECT PROTOCOL"):
        """Send a packet already encrypted by an authenticated session channel."""
        if not self.connected:
            raise DirectPeerError("Direct peer is not connected")
        return self._send_packet(bytes(packet), label=label)

    def add_packet_handler(self, handler):
        """Register a fast handler for authenticated application-defined bytes.

        The callback receives decrypted payload bytes and returns True when it
        consumed the packet. Move long-running work to an application worker.
        """
        self._ensure_open()
        if not callable(handler):
            raise TypeError("handler must be callable")
        token = object()
        with self._lock:
            self._packet_handlers[token] = handler

        def unsubscribe():
            with self._lock:
                self._packet_handlers.pop(token, None)

        from .client import Subscription
        return Subscription(unsubscribe)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._connected.clear()
        self._subscription.close()
        with self._lock:
            self._packet_handlers.clear()
            self._raw_packet_handlers.clear()
            self._pending_ack_id = None
            self._pending_ack_event = None
        try:
            self.client.set_hop_key(None)
        except Exception:
            pass
        self.handshake.close()
        self._text_channel = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
