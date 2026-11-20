import secrets
import unittest

from core.modem import fec_encode, fec_decode, modulate, demodulate
from core.crypto import SecureChannel, ReplayError
from core.session import Session
from security.handshake import create_hello, verify_hello, derive_keys
from security.identity import raw_public, fingerprint
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class Tests(unittest.TestCase):
    def test_fec_roundtrip(self):
        data = bytes(range(256))
        self.assertEqual(fec_decode(fec_encode(data)), data)

    def test_fec_corrects_every_single_bit(self):
        for value in range(256):
            original = bytes([value])
            encoded = fec_encode(original)
            for bit_index in range(len(encoded)):
                damaged = encoded.copy()
                damaged[bit_index] ^= 1
                with self.subTest(value=value, bit=bit_index):
                    self.assertEqual(fec_decode(damaged), original)

    def test_modem_roundtrip(self):
        payload = b"echocrypt acoustic test"
        self.assertEqual(demodulate(modulate(payload)), payload)

    def test_modem_roundtrip_with_offset_and_noise(self):
        payload = b"offset and noise"
        waveform = modulate(payload)
        import numpy as np
        recording = np.concatenate((np.zeros(613), waveform, np.zeros(800)))
        noise = np.random.default_rng(1234).normal(0.0, 0.01, recording.size)
        recording = recording + noise
        self.assertEqual(demodulate(recording), payload)

    def test_crypto_and_replay(self):
        key = secrets.token_bytes(32)
        sender, receiver = SecureChannel(key), SecureChannel(key)
        packet = sender.encrypt(b"hello", b"context")
        self.assertEqual(receiver.decrypt(packet, b"context"), b"hello")
        with self.assertRaises(ReplayError):
            receiver.decrypt(packet, b"context")

    def test_crypto_rejects_tampering(self):
        key = secrets.token_bytes(32)
        sender, receiver = SecureChannel(key), SecureChannel(key)
        packet = bytearray(sender.encrypt(b"authenticated data"))
        packet[-1] ^= 0x01
        with self.assertRaises(InvalidTag):
            receiver.decrypt(bytes(packet))

    def test_session_directional_keys(self):
        a_to_b, b_to_a = secrets.token_bytes(32), secrets.token_bytes(32)
        a = Session(a_to_b, b_to_a, "B")
        b = Session(b_to_a, a_to_b, "A")
        self.assertEqual(b.decrypt_text(a.encrypt_text("سلام")), "سلام")

    def test_signed_handshake_and_directional_key_agreement(self):
        initiator_identity = Ed25519PrivateKey.generate()
        responder_identity = Ed25519PrivateKey.generate()

        initiator_hello, initiator_ephemeral, initiator_nonce = create_hello(initiator_identity)
        responder_hello, responder_ephemeral, responder_nonce = create_hello(responder_identity)

        initiator_public = raw_public(initiator_identity.public_key())
        responder_public = raw_public(responder_identity.public_key())

        responder_ephemeral_public, verified_responder_nonce = verify_hello(
            responder_hello, responder_public, fingerprint(responder_identity.public_key())
        )
        initiator_ephemeral_public, verified_initiator_nonce = verify_hello(
            initiator_hello, initiator_public, fingerprint(initiator_identity.public_key())
        )

        a_tx, a_rx = derive_keys(
            initiator_ephemeral, responder_ephemeral_public,
            initiator_nonce, verified_responder_nonce, True
        )
        b_tx, b_rx = derive_keys(
            responder_ephemeral, initiator_ephemeral_public,
            verified_initiator_nonce, responder_nonce, False
        )
        self.assertEqual(a_tx, b_rx)
        self.assertEqual(a_rx, b_tx)
        self.assertNotEqual(a_tx, a_rx)

    def test_handshake_rejects_wrong_fingerprint(self):
        identity = Ed25519PrivateKey.generate()
        hello, _, _ = create_hello(identity)
        public = raw_public(identity.public_key())
        with self.assertRaises(ValueError):
            verify_hello(hello, public, "00" * 32)

    def test_handshake_rejects_modified_signature(self):
        identity = Ed25519PrivateKey.generate()
        hello, _, _ = create_hello(identity)
        hello["signature"] = ("00" * 64)
        public = raw_public(identity.public_key())
        with self.assertRaises(InvalidSignature):
            verify_hello(hello, public, fingerprint(identity.public_key()))


if __name__ == "__main__":
    unittest.main()
