import hashlib
import tempfile
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

from core.binary_transfer import ReliableBinaryChannel
from core.config import Config
from interface.communication import _direct_packet_router, _engine_send_file
from security.identity import fingerprint, raw_public
from security.peer_handshake import PeerHandshake
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class _Waiter:
    def __init__(self, predicate):
        self.predicate = predicate
        self.event = threading.Event()
        self.packet = None

    def resolve(self, packet):
        self.packet = bytes(packet)
        self.event.set()

    def wait(self, timeout=None, cancel_event=None):
        if cancel_event is None:
            ready = self.event.wait(timeout)
        else:
            import time
            deadline = None if timeout is None else time.monotonic() + timeout
            ready = False
            while not cancel_event.is_set():
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                if self.event.wait(0.02 if remaining is None else min(0.02, remaining)):
                    ready = True
                    break
            if cancel_event.is_set():
                raise InterruptedError("test transfer cancelled")
        if not ready:
            raise TimeoutError("test response timed out")
        return self.packet

    def cancel(self, _error=None):
        self.event.set()


class _FakeEngine:
    def __init__(self, config):
        self.config = config
        self.symbol_rates = (400, 200, 100)
        self.peer = None
        self.on_packet = None
        self.waiters = []
        self.drop_completion_count = 0

    def expect_packet(self, predicate):
        waiter = _Waiter(predicate)
        self.waiters.append(waiter)
        return waiter

    def cancel_waiter(self, waiter):
        if waiter in self.waiters:
            self.waiters.remove(waiter)
        waiter.cancel()

    def deliver(self, packet):
        raw = bytes(packet)
        for waiter in list(self.waiters):
            try:
                matched = bool(waiter.predicate(raw))
            except Exception:
                matched = False
            if matched:
                self.waiters.remove(waiter)
                waiter.resolve(raw)
                return True
        if self.on_packet is not None:
            self.on_packet(raw)
        return False

    def submit_packet(self, packet, **options):
        raw = bytes(packet)
        label = str(options.get("label", ""))
        if label.startswith("FILE TRANSFER COMPLETE") and self.drop_completion_count:
            self.drop_completion_count -= 1
        elif self.peer is not None:
            self.peer.deliver(raw)
        future = Future()
        future.set_result(0.01)
        return future


class _NoText:
    def receive(self, _packet):
        return None


def _connected_handshakes():
    identity_a = Ed25519PrivateKey.generate()
    identity_b = Ed25519PrivateKey.generate()
    a = PeerHandshake(
        "initiator", identity_a, raw_public(identity_b.public_key()),
        fingerprint(identity_b.public_key()),
    )
    b = PeerHandshake(
        "responder", identity_b, raw_public(identity_a.public_key()),
        fingerprint(identity_a.public_key()),
    )
    reply = b.receive(a.start())
    finish_i = a.receive(reply)
    finish_r = b.receive(finish_i)
    a.receive(finish_r)
    return a, b


class LegacyFileCompletionTests(unittest.TestCase):
    def _run_transfer(self, data, *, drop_first_completion=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.bin"
            source.write_bytes(data)
            handshake_a, handshake_b = _connected_handshakes()
            sender_channel = ReliableBinaryChannel(
                handshake_a, receive_root=root / "sender-downloads",
                state_root=root / "sender-state",
            )
            receiver_channel = ReliableBinaryChannel(
                handshake_b, receive_root=root / "receiver-downloads",
                state_root=root / "receiver-state",
            )
            config = Config(ack_timeout_seconds=0.05, max_retries=2)
            sender = _FakeEngine(config)
            receiver = _FakeEngine(config)
            sender.peer = receiver
            receiver.peer = sender
            receiver.drop_completion_count = 1 if drop_first_completion else 0
            receiver.on_packet = _direct_packet_router(
                {"engine": receiver}, _NoText(), receiver_channel,
                [], threading.RLock(),
            )
            result = _engine_send_file(
                sender_channel, sender, source, config,
                preferred_symbol_rate=400,
            )
            saved = list((root / "receiver-downloads").glob("*"))
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].read_bytes(), data)
            self.assertEqual(result.sha256, hashlib.sha256(data).hexdigest())
            self.assertFalse(result and sender_channel.outgoing_state.joinpath(
                f"{result.transfer_id_hex}.json"
            ).exists())
            self.assertEqual(sender.waiters, [])
            return result

    def test_legacy_chat_transfer_requires_remote_hash_commit_for_success(self):
        content = bytes((index * 17) % 256 for index in range(777))
        result = self._run_transfer(content)
        self.assertEqual(result.file_size, len(content))

    def test_legacy_chat_recovers_if_first_completion_notice_is_lost(self):
        # Exceed the 128-packet authenticated duplicate cache so the
        # recovery manifest must be freshly encrypted, not replayed verbatim.
        content = b"retry completion notice" * 4000
        result = self._run_transfer(content, drop_first_completion=True)
        self.assertEqual(result.sha256, hashlib.sha256(content).hexdigest())

    def test_legacy_chat_empty_file_is_completed_by_manifest(self):
        result = self._run_transfer(b"")
        self.assertEqual(result.file_size, 0)
        self.assertEqual(result.total_chunks, 0)


if __name__ == "__main__":
    unittest.main()
