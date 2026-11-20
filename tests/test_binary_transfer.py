import hashlib
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.binary_transfer import ChunkIntegrityError, ReliableBinaryChannel
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
            self.assertEqual(
                receiver.transfer_progress(outgoing.manifest.transfer_id)[2], (0,)
            )

            # A resend is freshly encrypted with a new nonce/counter, so the
            # receiver can accept it despite consuming the first damaged frame.
            repaired_packet = sender.chunk_packet(outgoing, 0, max_packet_size=1024)
            repaired = receiver.receive_chunk(repaired_packet)
            self.assertIsNotNone(repaired.completed_path)
            self.assertEqual(repaired.completed_path.read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
