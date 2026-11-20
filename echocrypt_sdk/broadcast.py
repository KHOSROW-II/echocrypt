"""Public Broadcast API for ECHO-CRYPT applications."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
import time

from core.broadcast import (
    BROADCAST_HOP_KEY,
    BroadcastCodec,
    BroadcastDelivery,
    BroadcastError,
    BroadcastReassembler,
    BroadcastRetransmitCache,
    BroadcastType,
)


@dataclass(frozen=True)
class BroadcastFrames:
    message_id: bytes
    frames: tuple[bytes, ...]
    kind: BroadcastType
    retransmission_cached: bool = True

    @property
    def message_id_hex(self):
        return self.message_id.hex()


@dataclass(frozen=True)
class BroadcastResult:
    event: str
    delivery: BroadcastDelivery | None = None
    retry_frames: tuple[bytes, ...] = ()


class BroadcastAPI:
    """Open, unauthenticated Broadcast messaging and file distribution.

    Broadcast is public by design. Its compatibility transform and hopping
    schedule are not secrets and provide no confidentiality or sender identity.
    """

    def __init__(
        self,
        *,
        sender_id=None,
        max_packet_size=1024,
        download_dir="downloads/broadcast",
        max_file_size=64 * 1024 * 1024,
        max_partial_bytes=16 * 1024 * 1024,
        max_partials=64,
        max_cached_bytes=16 * 1024 * 1024,
    ):
        self.codec = BroadcastCodec(
            sender_id=sender_id, max_packet_size=max_packet_size
        )
        self.reassembler = BroadcastReassembler(
            self.codec,
            download_dir=download_dir,
            max_file_size=max_file_size,
            max_partial_bytes=max_partial_bytes,
            max_partials=max_partials,
        )
        self.retransmit_cache = BroadcastRetransmitCache(
            self.codec, max_cached_bytes=max_cached_bytes
        )
        self._lock = threading.RLock()
        self._registered_clients = {}
        self._subscriptions = {}

    @property
    def sender_id(self):
        return self.codec.sender_id

    def _check_client(self, client):
        if self.codec.max_packet_size > client.config.max_packet_bytes:
            raise ValueError(
                "Broadcast max_packet_size exceeds AcousticClient.max_packet_bytes"
            )
        if not client.running:
            raise RuntimeError("Start AcousticClient before transmitting Broadcast frames")

    def register_receiver(self, client):
        """Add the public Broadcast hopping profile to an existing RX session."""
        token = id(client)
        with self._lock:
            if token not in self._registered_clients:
                client.add_receive_hop_key(BROADCAST_HOP_KEY)
                self._registered_clients[token] = client
        return self

    def attach(self, client, *, on_result=None):
        """Attach Broadcast reception to AcousticClient packet events.

        Retry responses are transmitted automatically. Application callbacks
        should remain short; move expensive processing to a worker.
        """
        self.register_receiver(client)
        token = id(client)
        with self._lock:
            if token in self._subscriptions:
                return self._subscriptions[token]

        def handle(event):
            result = self.receive_packet(event.data)
            if result.retry_frames:
                threading.Thread(
                    target=self.transmit,
                    args=(client, result.retry_frames),
                    name="echocrypt-sdk-broadcast-retry",
                    daemon=True,
                ).start()
            if on_result is not None:
                try:
                    on_result(result)
                except Exception:
                    pass

        subscription = client.on("packet_received", handle)
        with self._lock:
            self._subscriptions[token] = subscription
        return subscription

    def request_missing(self, client, *, older_than=None, symbol_rate=None):
        """Transmit selective retry requests for incomplete received messages."""
        frames = self.build_retry_requests(older_than=older_than)
        if not frames:
            return ()
        return self.transmit(client, frames, symbol_rate=symbol_rate)

    def unregister_receiver(self, client):
        token = id(client)
        with self._lock:
            registered = self._registered_clients.pop(token, None)
            subscription = self._subscriptions.pop(token, None)
        if subscription is not None:
            subscription.close()
        if registered is not None:
            registered.remove_receive_hop_key(BROADCAST_HOP_KEY)

    def encode_text(self, text, *, message_id=None):
        frames = self.codec.encode_text(text, message_id=message_id)
        mid = self.codec.decode(frames[0]).message_id
        cached = True
        try:
            self.retransmit_cache.remember(mid, BroadcastType.TEXT, frames)
        except BroadcastError:
            self.retransmit_cache.forget(mid)
            cached = False
        return BroadcastFrames(mid, tuple(frames), BroadcastType.TEXT, cached)

    def encode_file(
        self,
        path,
        *,
        chunk_size=None,
        message_id=None,
        symbol_rate=800,
        cancel_event=None,
    ):
        mid, frames = self.codec.encode_file(
            path, chunk_size=chunk_size, message_id=message_id,
            symbol_rate=symbol_rate, fec_mode="conv", hopping=True,
            cancel_event=cancel_event,
        )
        cached = True
        try:
            self.retransmit_cache.remember(mid, BroadcastType.FILE_META, frames[:1])
            if len(frames) > 1:
                self.retransmit_cache.remember(mid, BroadcastType.FILE_CHUNK, frames[1:])
        except BroadcastError:
            self.retransmit_cache.forget(mid)
            cached = False
        return BroadcastFrames(mid, tuple(frames), BroadcastType.FILE_CHUNK, cached)

    def transmit(
        self,
        client,
        frames,
        *,
        symbol_rate=None,
        fec_mode="conv",
        channel_access=None,
        cancel_event=None,
        on_progress=None,
    ):
        """Transmit frames serially; this is not a remote-delivery guarantee."""
        self._check_client(client)
        symbol_rate = client.config.symbol_rate if symbol_rate is None else symbol_rate
        packets = tuple(bytes(frame) for frame in frames)
        transmissions = []
        total = len(packets)
        if not packets:
            raise ValueError("At least one Broadcast frame is required")
        for index, packet in enumerate(packets, 1):
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Broadcast transmission cancelled")
            tx = client.send_async(
                packet,
                fec_mode=fec_mode,
                symbol_rate=symbol_rate,
                hop_key=BROADCAST_HOP_KEY,
                channel_access=channel_access,
                label=f"BROADCAST {index}/{total}",
            )
            transmissions.append(tx)
            while not tx.done:
                if cancel_event is not None and cancel_event.is_set():
                    tx.cancel()
                    raise InterruptedError("Broadcast transmission cancelled")
                time.sleep(0.02)
            tx.wait()
            if on_progress is not None:
                on_progress(index, total)
        return tuple(transmissions)

    def send_text(self, client, text, **options):
        encoded = self.encode_text(text)
        transmissions = self.transmit(client, encoded.frames, **options)
        return encoded, transmissions

    def send_file(self, client, path, **options):
        symbol_rate = options.pop("symbol_rate", client.config.symbol_rate)
        encoded = self.encode_file(
            path, symbol_rate=symbol_rate,
            cancel_event=options.get("cancel_event"),
        )
        transmissions = self.transmit(
            client, encoded.frames, symbol_rate=symbol_rate, **options
        )
        return encoded, transmissions

    def receive_packet(self, packet):
        """Feed one CRC/FEC-validated modem packet into Broadcast reassembly."""
        result = self.reassembler.feed(packet)
        if result is None:
            return BroadcastResult("progress")
        if isinstance(result, tuple) and result[0] == "retry_request":
            retry_frames = self.retransmit_cache.handle_request(result[1])
            return BroadcastResult(
                "retry_requested", retry_frames=tuple(retry_frames)
            )
        if isinstance(result, BroadcastDelivery):
            if result.duplicate:
                return BroadcastResult("duplicate", result)
            if result.kind == BroadcastType.FILE_META:
                return BroadcastResult("manifest_received", result)
            if result.complete:
                if result.kind == BroadcastType.TEXT:
                    return BroadcastResult("text_received", result)
                if result.path is not None:
                    return BroadcastResult("file_received", result)
            return BroadcastResult("progress", result)
        return BroadcastResult("ignored")

    def build_retry_requests(self, *, older_than=None):
        """Build public selective retry frames for incomplete received messages."""
        requests = []
        for sender_id, message_id, kind, missing in self.reassembler.retry_requests(
            older_than=older_than
        ):
            requests.append(self.codec.encode_retry_request(
                sender_id, message_id, kind, missing
            ))
        return tuple(requests)

    def forget(self, message_id, kind=None):
        return self.retransmit_cache.forget(message_id, kind)

    def close(self):
        with self._lock:
            clients = list(self._registered_clients.values())
            subscriptions = list(self._subscriptions.values())
            self._registered_clients.clear()
            self._subscriptions.clear()
        for subscription in subscriptions:
            try:
                subscription.close()
            except Exception:
                pass
        for client in clients:
            try:
                client.remove_receive_hop_key(BROADCAST_HOP_KEY)
            except Exception:
                pass
