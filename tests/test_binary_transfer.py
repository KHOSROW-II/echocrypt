import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.binary_transfer import (
    ChunkIntegrityError, ReliableBinaryChannel, iter_chunk_packets_parallel,
)
from security.peer_handshake import PeerHandshake


def raw_public(key):
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def established_peers():
    alice_identity = Ed25519PrivateKey.generate()
    bob_identity = Ed25519PrivateKey.generate()
    alice_public = raw_public(alice_identity.public_key())
    bob_public = raw_public(bob_identity.public_key())
    alice = PeerHandshake(
        "initiator", alice_identity, bob_public, hashlib.sha256(bob_public).hexdigest()
    )
    bob = PeerHandshake(
        "responder", bob_identity, alice_public, hashlib.sha256(alice_public).hexdigest()
    )
    hello_r = bob.receive(alice.start())
    finish_i = alice.receive(hello_r)
    finish_r = bob.receive(finish_i)
    alice.receive(finish_r)
    return alice, bob


class BinaryTransferIntegrityTests(unittest.TestCase):
    def test_corrupt_received_chunk_is_marked_missing_and_can_be_retransmitted(self):
        alice, bob = established_peers()
        payload = b"A" * 128 + b"B" * 128 + b"C" * 128

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.bin"
            source.write_bytes(payload)
            sender = ReliableBinaryChannel(alice, state_root=root / "sender-state")
            receiver = ReliableBinaryChannel(
                bob, receive_root=root / "downloads", state_root=root / "receiver-state"
            )
            outgoing = sender.create_outgoing(source, chunk_size=128)
            manifest = sender.manifest_packet(outgoing, max_packet_size=1024)
            receiver.receive_manifest(manifest)

            for index in (0, 1):
                packet = sender.chunk_packet(outgoing, index, max_packet_size=1024)
                receiver.receive_chunk(packet)

            _state_path, partial_path = receiver._state_paths(outgoing.manifest.transfer_id)
            with partial_path.open("r+b") as stream:
                stream.seek(0)
                stream.write(b"X")

            final_packet = sender.chunk_packet(outgoing, 2, max_packet_size=1024)
            with self.assertRaises(ChunkIntegrityError) as caught:
                receiver.receive_chunk(final_packet)
            self.assertEqual(caught.exception.transfer_id, outgoing.manifest.transfer_id)
            self.assertEqual(caught.exception.indexes, (0,))
            # The last authenticated DATA frame is ACKed even though the
            # receiver separately requests a fresh copy of the damaged chunk.
            self.assertTrue(sender.verify_ack(
                caught.exception.ack_packet, outgoing.manifest.transfer_id, 1, 2
            ))
            self.assertEqual(
                receiver.transfer_progress(outgoing.manifest.transfer_id)[2], (0,)
            )

            # A resend is freshly encrypted with a new nonce/counter, so the
            # receiver can accept it despite consuming the first damaged frame.
            repaired_packet = sender.chunk_packet(outgoing, 0, max_packet_size=1024)
            repaired = receiver.receive_chunk(repaired_packet)
            self.assertIsNotNone(repaired.completed_path)
            self.assertEqual(repaired.completed_path.read_bytes(), payload)


class ParallelChunkPreparationTests(unittest.TestCase):
    def test_parallel_workers_prepare_chunks_concurrently_but_yield_in_order(self):
        class FakeChannel:
            def __init__(self):
                self.lock = threading.Lock()
                self.active = 0
                self.maximum_active = 0

            def chunk_packet(self, _outgoing, index, _max_packet_size):
                with self.lock:
                    self.active += 1
                    self.maximum_active = max(self.maximum_active, self.active)
                try:
                    time.sleep(0.025)
                    return f"packet-{index}".encode()
                finally:
                    with self.lock:
                        self.active -= 1

        channel = FakeChannel()
        outgoing = SimpleNamespace(manifest=SimpleNamespace(total_chunks=8))
        result = list(iter_chunk_packets_parallel(
            channel, outgoing, range(8), max_packet_size=1024, worker_count=3,
        ))

        self.assertEqual([index for index, _packet in result], list(range(8)))
        self.assertEqual(
            [packet for _index, packet in result],
            [f"packet-{index}".encode() for index in range(8)],
        )
        self.assertGreater(channel.maximum_active, 1)
        self.assertLessEqual(channel.maximum_active, 3)

    def test_single_worker_path_preserves_sequential_behavior(self):
        class FakeChannel:
            def chunk_packet(self, _outgoing, index, _max_packet_size):
                return bytes([index])

        outgoing = SimpleNamespace(manifest=SimpleNamespace(total_chunks=4))
        result = list(iter_chunk_packets_parallel(
            FakeChannel(), outgoing, range(1, 4), worker_count=1,
        ))
        self.assertEqual(result, [(1, b"\\x01"), (2, b"\\x02"), (3, b"\\x03")])

    def test_parallel_worker_count_is_bounded(self):
        outgoing = SimpleNamespace(manifest=SimpleNamespace(total_chunks=1))
        with self.assertRaisesRegex(ValueError, "worker_count"):
            list(iter_chunk_packets_parallel(
                object(), outgoing, range(1), worker_count=9,
            ))


if __name__ == "__main__":
    unittest.main()
