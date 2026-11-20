import hashlib
import unittest

import numpy as np
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.modem import modulate, demodulate
from core.crypto import ReplayError, SecureChannel
from core.reliability import ReliableTextChannel
from security.peer_handshake import PeerHandshake, HandshakeError, HandshakeState
from security.profiles import SecurityProfile
from security.identity import raw_public


SAMPLE_RATE = 48000
SYMBOL_RATE = 200


def acoustic_loopback(packet):
    """Synthetic acoustic path: sample offset, low-level noise and silence tail."""
    wave = modulate(packet, sample_rate=SAMPLE_RATE, symbol_rate=SYMBOL_RATE)
    recording = np.concatenate((np.zeros(613), wave, np.zeros(900)))
    rng = np.random.default_rng(20261119)
    recording += rng.normal(0.0, 0.006, recording.size)
    return demodulate(recording, sample_rate=SAMPLE_RATE, symbol_rate=SYMBOL_RATE)


class PeerProtocolTests(unittest.TestCase):
    def make_peers(self):
        alice_identity = Ed25519PrivateKey.generate()
        bob_identity = Ed25519PrivateKey.generate()
        alice_pub = raw_public(alice_identity.public_key())
        bob_pub = raw_public(bob_identity.public_key())
        alice_fp = hashlib.sha256(alice_pub).hexdigest()
        bob_fp = hashlib.sha256(bob_pub).hexdigest()
        alice = PeerHandshake("initiator", alice_identity, bob_pub, bob_fp)
        bob = PeerHandshake("responder", bob_identity, alice_pub, alice_fp)
        return alice, bob

    def test_full_handshake_and_text_over_modem(self):
        alice, bob = self.make_peers()

        hello_i = acoustic_loopback(alice.start())
        hello_r = bob.receive(hello_i)
        finish_i = alice.receive(acoustic_loopback(hello_r))
        finish_r = bob.receive(acoustic_loopback(finish_i))
        self.assertEqual(bob.state, HandshakeState.ESTABLISHED)
        self.assertIsNone(alice.receive(acoustic_loopback(finish_r)))
        self.assertEqual(alice.state, HandshakeState.ESTABLISHED)

        message = "ECHO-CRYPT secure text — سلام، پیام آزمایشی"
        encrypted = alice.send_text(message)
        self.assertNotIn(message.encode("utf-8"), encrypted)
        self.assertEqual(bob.receive_text(acoustic_loopback(encrypted)), message)

        reply = bob.send_text("پاسخ از Peer دوم")
        self.assertEqual(alice.receive_text(acoustic_loopback(reply)), "پاسخ از Peer دوم")

    def test_handshake_control_retries_are_idempotent(self):
        alice, bob = self.make_peers()
        hello_i = alice.start()
        hello_r = bob.receive(hello_i)
        # Initiator may retransmit HELLO-I if HELLO-R was not heard.
        self.assertEqual(bob.receive(hello_i), hello_r)
        finish_i = alice.receive(hello_r)
        # Responder may retransmit HELLO-R if FINISH-I was not heard.
        self.assertEqual(alice.receive(hello_r), finish_i)
        finish_r = bob.receive(finish_i)
        self.assertIsNone(alice.receive(finish_r))
        self.assertTrue(alice.established)
        self.assertTrue(bob.established)

    def test_encrypted_profile_exchange_over_hopped_conv_modem(self):
        alice, bob = self.make_peers()
        hello_i = acoustic_loopback(alice.start())
        hello_r = bob.receive(hello_i)
        finish_i = alice.receive(acoustic_loopback(hello_r))
        finish_r = bob.receive(acoustic_loopback(finish_i))
        alice.receive(acoustic_loopback(finish_r))

        sender = ReliableTextChannel(alice)
        receiver = ReliableTextChannel(bob)
        outgoing = sender.prepare("ECHOCRYPT-PROFILE/1:peer_123", max_packet_size=1024)
        waveform = modulate(outgoing.packet, hop_key=alice.session.hopping_key,
                            fec_mode="conv")
        # Include a deliberately hot/clipped software channel while preserving
        # the same in-band FEC and authenticated hopping key.
        waveform = np.clip(waveform * 2.0, -1.0, 1.0)
        packet = demodulate(waveform, hop_key=bob.session.hopping_key)
        incoming = receiver.receive(packet)
        self.assertEqual(incoming.text, "ECHOCRYPT-PROFILE/1:peer_123")
        self.assertTrue(sender.verify_ack(incoming.ack_packet, outgoing.message_id))


    def test_security_profile_negotiates_strongest_signed_settings(self):
        alice_identity = Ed25519PrivateKey.generate()
        bob_identity = Ed25519PrivateKey.generate()
        alice_pub = raw_public(alice_identity.public_key())
        bob_pub = raw_public(bob_identity.public_key())
        alice_fp = hashlib.sha256(alice_pub).hexdigest()
        bob_fp = hashlib.sha256(bob_pub).hexdigest()
        alice = PeerHandshake(
            "initiator", alice_identity, bob_pub, bob_fp,
            security_profile=SecurityProfile.preset("low"),
        )
        bob = PeerHandshake(
            "responder", bob_identity, alice_pub, alice_fp,
            security_profile=SecurityProfile.preset("secure"),
        )

        hello_i = alice.start()
        hello_r = bob.receive(hello_i)
        finish_i = alice.receive(hello_r)
        finish_r = bob.receive(finish_i)
        self.assertIsNone(alice.receive(finish_r))
        self.assertEqual(alice.negotiated_profile, SecurityProfile.preset("secure"))
        self.assertEqual(bob.negotiated_profile, SecurityProfile.preset("secure"))
        self.assertEqual(alice.session.tx.padding_block, 256)
        self.assertEqual(bob.session.rx.padding_block, 256)
        self.assertIsNotNone(alice.session.hopping_key)

        secret = "profile negotiation test"
        packet = alice.session.tx.encrypt(secret.encode(), b"profile-test")
        self.assertEqual(len(packet), alice.session.tx.encrypted_size(len(secret.encode())))
        self.assertEqual(bob.session.rx.decrypt(packet, b"profile-test").decode(), secret)

    def test_custom_security_profile_must_match_on_both_peers(self):
        alice_identity = Ed25519PrivateKey.generate()
        bob_identity = Ed25519PrivateKey.generate()
        alice_pub = raw_public(alice_identity.public_key())
        bob_pub = raw_public(bob_identity.public_key())
        alice = PeerHandshake(
            "initiator", alice_identity, bob_pub, hashlib.sha256(bob_pub).hexdigest(),
            security_profile=SecurityProfile.custom(400, 64, True, 3),
        )
        bob = PeerHandshake(
            "responder", bob_identity, alice_pub, hashlib.sha256(alice_pub).hexdigest(),
            security_profile=SecurityProfile.custom(400, 128, True, 3),
        )
        with self.assertRaises(HandshakeError):
            bob.receive(alice.start())

    def test_secure_channel_padding_is_authenticated_and_length_aligned(self):
        key = b"p" * 32
        sender = SecureChannel(key, padding_block=64)
        receiver = SecureChannel(key, padding_block=64)
        for message in (b"x", b"hello", b"short message", bytes(range(128))):
            with self.subTest(length=len(message)):
                packet = sender.encrypt(message, b"padding-test")
                self.assertEqual(len(packet), sender.encrypted_size(len(message)))
                self.assertEqual((len(packet) - 32) % 64, 0)
                self.assertEqual(receiver.decrypt(packet, b"padding-test"), message)

    def test_wrong_peer_identity_is_rejected(self):
        alice_identity = Ed25519PrivateKey.generate()
        bob_identity = Ed25519PrivateKey.generate()
        stranger = Ed25519PrivateKey.generate()
        bob_pub = raw_public(bob_identity.public_key())
        stranger_pub = raw_public(stranger.public_key())
        wrong_pin = hashlib.sha256(stranger_pub).hexdigest()
        alice = PeerHandshake("initiator", alice_identity, bob_pub, hashlib.sha256(bob_pub).hexdigest())
        bob = PeerHandshake("responder", bob_identity, raw_public(alice_identity.public_key()),
                            hashlib.sha256(raw_public(alice_identity.public_key())).hexdigest())
        hello = alice.start()
        # The packet is signed by Alice; Bob rejects it if configured with a different pin.
        bad_bob = PeerHandshake("responder", bob_identity, stranger_pub, wrong_pin)
        with self.assertRaises(HandshakeError):
            bad_bob.receive(hello)

    def test_handshake_state_machine_rejects_out_of_order(self):
        alice, bob = self.make_peers()
        with self.assertRaises(HandshakeError):
            alice.receive(b"not a hello")
        hello = alice.start()
        response = bob.receive(acoustic_loopback(hello))
        finish = alice.receive(acoustic_loopback(response))
        with self.assertRaises(HandshakeError):
            alice.receive(finish)


    def test_modified_hello_signature_is_rejected(self):
        alice, bob = self.make_peers()
        hello = bytearray(alice.start())
        hello[-1] ^= 0x01
        with self.assertRaises(HandshakeError):
            bob.receive(bytes(hello))

    def test_oversized_text_does_not_consume_send_counter(self):
        alice, bob = self.make_peers()
        hello_i = alice.start()
        hello_r = bob.receive(hello_i)
        finish_i = alice.receive(hello_r)
        finish_r = bob.receive(finish_i)
        alice.receive(finish_r)
        counter_before = alice.session.tx.counter
        with self.assertRaises(ValueError):
            alice.send_text("A" * 100, max_packet_size=64)
        self.assertEqual(alice.session.tx.counter, counter_before)

    def test_replay_window_is_bounded_and_allows_small_reordering(self):
        key = b"k" * 32
        sender = SecureChannel(key)
        receiver = SecureChannel(key, replay_window=4)
        packets = [sender.encrypt(bytes([i])) for i in range(4)]
        self.assertEqual(receiver.decrypt(packets[0]), b"\x00")
        self.assertEqual(receiver.decrypt(packets[2]), b"\x02")
        self.assertEqual(receiver.decrypt(packets[1]), b"\x01")
        with self.assertRaises(ReplayError):
            receiver.decrypt(packets[1])
        self.assertEqual(receiver.decrypt(packets[3]), b"\x03")
        newer = [sender.encrypt(b"x") for _ in range(4)]
        receiver.decrypt(newer[-1])
        with self.assertRaises(ReplayError):
            receiver.decrypt(packets[0])


if __name__ == "__main__":
    unittest.main()
