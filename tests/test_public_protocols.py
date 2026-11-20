import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from echocrypt_sdk import (
    AcousticConfig,
    BroadcastAPI,
    DirectPeer,
    SecureFileTransfer,
)
from security.identity import fingerprint, raw_public
from security.peer_handshake import PeerHandshake


class FakeTransmission:
    def __init__(self, label="test"):
        self.label = label
        self.done = True
        self.cancelled = False

    def wait(self, timeout=None):
        return SimpleNamespace(duration_seconds=0.01)

    def cancel(self):
        self.cancelled = True
        return True


class FakeAcousticClient:
    def __init__(self):
        self.running = True
        self.closed = False
        self.config = AcousticConfig(
            sample_rate=48000,
            symbol_rate=200,
            symbol_rates=(200, 800),
            max_packet_bytes=1024,
            channel_access=False,
        )
        self.remote = None
        self.callbacks = {}
        self.sent = []
        self.hop_key = None
        self.receive_hop_keys = set()
        self.drop_handshake_send_number = None
        self.handshake_send_count = 0

    def on(self, event, callback):
        token = object()
        self.callbacks[token] = (event, callback)
        return SimpleNamespace(close=lambda: self.callbacks.pop(token, None))

    def deliver(self, packet):
        event = SimpleNamespace(data=bytes(packet), name="packet_received")
        for event_name, callback in list(self.callbacks.values()):
            if event_name in ("*", "packet_received"):
                callback(event)

    def send_async(self, packet, **kwargs):
        raw = bytes(packet)
        self.sent.append((raw, dict(kwargs)))
        should_drop = False
        if "HANDSHAKE" in kwargs.get("label", ""):
            self.handshake_send_count += 1
            should_drop = self.handshake_send_count == self.drop_handshake_send_number
        if self.remote is not None and not should_drop:
            self.remote.deliver(raw)
        return FakeTransmission(kwargs.get("label", "test"))

    def set_hop_key(self, key):
        self.hop_key = None if key is None else bytes(key)

    def add_receive_hop_key(self, key):
        self.receive_hop_keys.add(bytes(key))

    def remove_receive_hop_key(self, key):
        self.receive_hop_keys.discard(bytes(key))


def make_connected_peers():
    identity_a = Ed25519PrivateKey.generate()
    identity_b = Ed25519PrivateKey.generate()
    client_a = FakeAcousticClient()
    client_b = FakeAcousticClient()
    client_a.remote = client_b
    client_b.remote = client_a
    handshake_a = PeerHandshake(
        "initiator", identity_a, raw_public(identity_b.public_key()),
        fingerprint(identity_b.public_key()),
    )
    handshake_b = PeerHandshake(
        "responder", identity_b, raw_public(identity_a.public_key()),
        fingerprint(identity_a.public_key()),
    )
    peer_a = DirectPeer(client_a, handshake_a, ack_timeout=0.5, retries=1)
    peer_b = DirectPeer(client_b, handshake_b, ack_timeout=0.5, retries=1)
    peer_a.connect(timeout=2, retry_interval=0.1)
    peer_b.accept(timeout=0.5)
    return peer_a, peer_b, client_a, client_b


class DirectSDKTests(unittest.TestCase):
    def test_mutual_handshake_activates_hopping_for_both_directions(self):
        peer_a, peer_b, client_a, client_b = make_connected_peers()
        try:
            self.assertTrue(peer_a.connected)
            self.assertTrue(peer_b.connected)
            key = peer_a.handshake.session.hopping_key
            self.assertEqual(key, peer_b.handshake.session.hopping_key)
            self.assertEqual(client_a.hop_key, key)
            self.assertEqual(client_b.hop_key, key)
            handshake_jobs = [
                options for _packet, options in client_a.sent + client_b.sent
                if "HANDSHAKE" in options.get("label", "")
            ]
            self.assertTrue(handshake_jobs)
            self.assertTrue(all(item.get("disable_hopping") for item in handshake_jobs))
        finally:
            peer_a.close()
            peer_b.close()

    def test_handshake_retries_final_flight_if_responder_finish_was_lost(self):
        identity_a = Ed25519PrivateKey.generate()
        identity_b = Ed25519PrivateKey.generate()
        client_a = FakeAcousticClient()
        client_b = FakeAcousticClient()
        client_a.remote = client_b
        client_b.remote = client_a
        client_b.drop_handshake_send_number = 2
        peer_a = DirectPeer(
            client_a,
            PeerHandshake(
                "initiator", identity_a, raw_public(identity_b.public_key()),
                fingerprint(identity_b.public_key()),
            ),
            ack_timeout=0.5,
            retries=1,
        )
        peer_b = DirectPeer(
            client_b,
            PeerHandshake(
                "responder", identity_b, raw_public(identity_a.public_key()),
                fingerprint(identity_a.public_key()),
            ),
            ack_timeout=0.5,
            retries=1,
        )
        try:
            peer_a.connect(timeout=2, retry_interval=0.05)
            peer_b.accept(timeout=0.5)
            self.assertTrue(peer_a.connected)
            self.assertTrue(peer_b.connected)
            self.assertGreaterEqual(client_b.handshake_send_count, 3)
        finally:
            peer_a.close()
            peer_b.close()

    def test_close_unblocks_a_direct_send_waiting_for_ack(self):
        identity_a = Ed25519PrivateKey.generate()
        identity_b = Ed25519PrivateKey.generate()
        handshake_a = PeerHandshake(
            "initiator", identity_a, raw_public(identity_b.public_key()),
            fingerprint(identity_b.public_key()),
        )
        handshake_b = PeerHandshake(
            "responder", identity_b, raw_public(identity_a.public_key()),
            fingerprint(identity_a.public_key()),
        )
        hello_r = handshake_b.receive(handshake_a.start())
        finish_i = handshake_a.receive(hello_r)
        finish_r = handshake_b.receive(finish_i)
        handshake_a.receive(finish_r)

        client = FakeAcousticClient()  # no remote peer: the ACK will never arrive
        send_started = threading.Event()
        original_send = client.send_async

        def tracked_send(packet, **options):
            send_started.set()
            return original_send(packet, **options)

        client.send_async = tracked_send
        peer = DirectPeer(client, handshake_a)
        errors = []

        def send():
            try:
                peer.send_text("wait for ACK", ack_timeout=10.0, retries=0)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=send)
        worker.start()
        self.assertTrue(send_started.wait(1.0))
        peer.close()
        worker.join(1.0)

        self.assertFalse(worker.is_alive(), "close() left DirectPeer.send_text blocked")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], Exception)
        self.assertIn("closed", str(errors[0]).lower())


    def test_close_unblocks_direct_receive_text_and_bytes(self):
        for method_name in ("receive_text", "receive_bytes"):
            peer_a, peer_b, _client_a, _client_b = make_connected_peers()
            entered = threading.Event()
            errors = []

            def wait_for_data():
                entered.set()
                try:
                    getattr(peer_b, method_name)(timeout=None)
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=wait_for_data)
            worker.start()
            self.assertTrue(entered.wait(0.5))
            peer_b.close()
            worker.join(0.5)
            peer_a.close()

            self.assertFalse(
                worker.is_alive(), f"close() left {method_name}(timeout=None) blocked"
            )
            self.assertEqual(len(errors), 1)
            self.assertIn("closed", str(errors[0]).lower())

    def test_reliable_text_is_acknowledged_and_delivered_once(self):
        peer_a, peer_b, client_a, _client_b = make_connected_peers()
        try:
            result = peer_a.send_text("authenticated hello")
            self.assertTrue(result.acknowledged)
            self.assertEqual(result.attempts, 1)
            message = peer_b.receive_text(timeout=0.1)
            self.assertEqual(message.text, "authenticated hello")
            self.assertEqual(message.message_id, result.message_id)
            direct_data = [
                options for _packet, options in client_a.sent
                if options.get("label", "").startswith("DIRECT TEXT")
            ]
            self.assertTrue(direct_data)
            self.assertEqual(direct_data[-1]["hop_key"], peer_a.handshake.session.hopping_key)
        finally:
            peer_a.close()
            peer_b.close()

    def test_custom_bytes_and_json_are_authenticated_and_routed(self):
        peer_a, peer_b, _client_a, _client_b = make_connected_peers()
        try:
            peer_a.send_bytes(b"\x00\x01custom")
            self.assertEqual(peer_b.receive_bytes(timeout=0.1), b"\x00\x01custom")
            peer_a.send_json("iot.sensor", {"id": 7, "temperature": 21.5}).wait()
            message = peer_b.receive_json(timeout=0.1)
            self.assertEqual(message["topic"], "iot.sensor")
            self.assertEqual(message["payload"]["id"], 7)
        finally:
            peer_a.close()
            peer_b.close()


class SecureFileSDKTests(unittest.TestCase):
    def test_close_unblocks_a_file_event_waiting_without_timeout(self):
        class FakeSubscription:
            def close(self):
                pass

        class FakePeer:
            connected = True
            handshake = SimpleNamespace()
            client = SimpleNamespace(config=AcousticConfig(max_packet_bytes=1024))

            def add_raw_packet_handler(self, _handler):
                return FakeSubscription()

        with tempfile.TemporaryDirectory() as tmp:
            transfer = SecureFileTransfer(
                FakePeer(), receive_root=Path(tmp) / "recv",
                state_root=Path(tmp) / "state",
            )
            entered = threading.Event()
            errors = []

            def wait_for_event():
                entered.set()
                try:
                    transfer.receive_event(timeout=None)
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=wait_for_event)
            worker.start()
            self.assertTrue(entered.wait(0.5))
            transfer.close()
            worker.join(0.5)

            self.assertFalse(worker.is_alive(), "close() left receive_event() blocked")
            self.assertEqual(len(errors), 1)
            self.assertIn("closed", str(errors[0]).lower())

    def test_close_unblocks_a_file_transfer_waiting_for_ack(self):
        import time

        send_started = threading.Event()

        class FakeSubscription:
            def close(self):
                pass

        class FakePeer:
            connected = True
            handshake = SimpleNamespace()
            client = SimpleNamespace(config=AcousticConfig(max_packet_bytes=1024))

            def add_raw_packet_handler(self, _handler):
                return FakeSubscription()

            def send_secure_frame(self, _packet, label=""):
                send_started.set()
                return FakeTransmission(label)

        with tempfile.TemporaryDirectory() as tmp:
            transfer = SecureFileTransfer(
                FakePeer(), receive_root=Path(tmp) / "recv",
                state_root=Path(tmp) / "state",
            )
            errors = []

            def wait_for_ack():
                try:
                    transfer._send_and_wait(
                        b"test-packet", b"T" * 16, 1, 0,
                        ack_timeout=10.0, retries=0,
                    )
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=wait_for_ack)
            worker.start()
            self.assertTrue(send_started.wait(1.0))
            time.sleep(0.05)  # allow the completed local TX to enter its ACK wait
            transfer.close()
            worker.join(1.0)

            self.assertFalse(worker.is_alive(), "close() left the ACK waiter blocked")
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], RuntimeError)
            self.assertIn("closed", str(errors[0]).lower())


    def test_file_transfer_waits_for_authenticated_remote_hash_completion(self):
        peer_a, peer_b, _client_a, _client_b = make_connected_peers()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "payload.bin"
                content = bytes((index * 19) % 256 for index in range(777))
                source.write_bytes(content)
                sender = SecureFileTransfer(
                    peer_a, receive_root=root / "a-downloads",
                    state_root=root / "a-state",
                )
                receiver = SecureFileTransfer(
                    peer_b, receive_root=root / "b-downloads",
                    state_root=root / "b-state",
                )
                result = sender.send_file(
                    source, chunk_size=128, ack_timeout=0.5, retries=1
                )
                self.assertTrue(result.completed)
                self.assertEqual(result.bytes_sent, len(content))
                self.assertEqual(result.sha256, hashlib.sha256(content).hexdigest())
                self.assertEqual(result.acknowledged_chunks, result.total_chunks)
                events = []
                while True:
                    event = receiver.receive_event(timeout=0.1)
                    events.append(event["kind"])
                    if event["kind"] == "file_completed":
                        path = event["result"]["path"]
                        self.assertEqual(path.read_bytes(), content)
                        break
                sender_event = sender.receive_event(timeout=0.1)
                self.assertEqual(sender_event["kind"], "remote_file_completed")
                sender.finish_transfer(result.transfer_id)
                sender.close()
                receiver.close()
        finally:
            peer_a.close()
            peer_b.close()

    def test_zero_length_file_is_completed_by_manifest(self):
        peer_a, peer_b, _client_a, _client_b = make_connected_peers()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "empty.dat"
                source.write_bytes(b"")
                sender = SecureFileTransfer(
                    peer_a, receive_root=root / "recv",
                    state_root=root / "send-state",
                )
                receiver = SecureFileTransfer(
                    peer_b, receive_root=root / "recv2",
                    state_root=root / "recv-state",
                )
                result = sender.send_file(source, ack_timeout=0.5, retries=1)
                self.assertTrue(result.completed)
                self.assertEqual(result.bytes_sent, 0)
                completed = receiver.receive_event(timeout=0.1)
                self.assertEqual(completed["kind"], "manifest_received")
                next_event = receiver.receive_event(timeout=0.1)
                self.assertEqual(next_event["kind"], "file_completed")
                sender.finish_transfer(result.transfer_id)
                sender.close()
                receiver.close()
        finally:
            peer_a.close()
            peer_b.close()


class BroadcastSDKTests(unittest.TestCase):
    def test_broadcast_text_reassembles_and_retry_cache_uses_correct_indexes(self):
        sender = BroadcastAPI(sender_id=b"SENDER01", max_packet_size=256)
        receiver = BroadcastAPI(sender_id=b"RECVR001", max_packet_size=256)
        encoded = sender.encode_text("x" * 1800)
        self.assertGreater(len(encoded.frames), 1)
        receiver.receive_packet(encoded.frames[0])
        retry = receiver.build_retry_requests(older_than=0)
        self.assertTrue(retry)
        response = sender.receive_packet(retry[0])
        self.assertEqual(response.event, "retry_requested")
        self.assertTrue(response.retry_frames)
        delivery = None
        for frame in response.retry_frames:
            result = receiver.receive_packet(frame)
            if result.event == "text_received":
                delivery = result.delivery
        self.assertIsNotNone(delivery)
        self.assertEqual(delivery.preview, "x" * 1800)

    def test_broadcast_attach_registers_receive_profile_and_dispatches_events(self):
        from core.broadcast import BROADCAST_HOP_KEY

        receiver = BroadcastAPI(sender_id=b"RECVR001", max_packet_size=256)
        sender = BroadcastAPI(sender_id=b"SENDER01", max_packet_size=256)
        client = FakeAcousticClient()
        results = []
        receiver.attach(client, on_result=results.append)
        try:
            self.assertIn(BROADCAST_HOP_KEY, client.receive_hop_keys)
            encoded = sender.encode_text("attached receiver")
            client.deliver(encoded.frames[0])
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].event, "text_received")
            self.assertEqual(results[0].delivery.preview, "attached receiver")
        finally:
            receiver.close()
            self.assertNotIn(BROADCAST_HOP_KEY, client.receive_hop_keys)

    def test_broadcast_rejects_size_limit_larger_than_partial_memory_budget(self):
        from core.broadcast import BroadcastCodec, BroadcastReassembler

        codec = BroadcastCodec(sender_id=b"BOB00001", max_packet_size=256)
        with self.assertRaisesRegex(ValueError, "cannot exceed max_partial_bytes"):
            BroadcastReassembler(
                codec, max_file_size=513, max_partial_bytes=512
            )

    def test_broadcast_api_rejects_file_before_encoding_when_over_memory_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "too-large.bin"
            source.write_bytes(b"x" * 513)
            api = BroadcastAPI(
                sender_id=b"SENDER01", max_packet_size=256,
                max_file_size=512, max_partial_bytes=512,
            )
            with self.assertRaisesRegex(ValueError, "reassembly limit"):
                api.encode_file(source)

    def test_broadcast_file_manifest_and_chunks_are_cached_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "broadcast.bin"
            data = b"broadcast-file-data" * 100
            source.write_bytes(data)
            sender = BroadcastAPI(
                sender_id=b"SENDER01", max_packet_size=512,
                download_dir=root / "sender-downloads",
            )
            receiver = BroadcastAPI(
                sender_id=b"RECVR001", max_packet_size=512,
                download_dir=root / "receiver-downloads",
            )
            encoded = sender.encode_file(source, chunk_size=128, symbol_rate=200)
            self.assertGreater(len(encoded.frames), 2)
            delivery = None
            for frame in encoded.frames:
                result = receiver.receive_packet(frame)
                if result.event == "file_received":
                    delivery = result.delivery
            self.assertIsNotNone(delivery)
            self.assertEqual(delivery.path.read_bytes(), data)

            # A missing chunk index is zero-based within FILE_CHUNK frames,
            # not offset by the separately cached manifest.
            retry_packet = receiver.codec.encode_retry_request(
                sender.sender_id, encoded.message_id,
                __import__("core.broadcast", fromlist=["BroadcastType"]).BroadcastType.FILE_CHUNK,
                (0,),
            )
            retry_result = sender.receive_packet(retry_packet)
            self.assertEqual(retry_result.event, "retry_requested")
            self.assertEqual(retry_result.retry_frames[0], encoded.frames[1])


if __name__ == "__main__":
    unittest.main()
