import hashlib
import unittest
from unittest.mock import patch

import numpy as np
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.modem import modulate, demodulate
from core.reliability import ReliableTextChannel, ReliableProtocolError
from core.crypto import ReplayError
from security.identity import raw_public
from security.peer_handshake import PeerHandshake
from core.audio import _first_sustained_activity, record_until_packet


SAMPLE_RATE = 48000
SYMBOL_RATE = 200


def acoustic_loopback(packet):
    wave = modulate(packet, sample_rate=SAMPLE_RATE, symbol_rate=SYMBOL_RATE)
    recording = np.concatenate((np.zeros(613), wave, np.zeros(1200)))
    recording += np.random.default_rng(20261119).normal(0.0, 0.004, recording.size)
    return demodulate(recording, sample_rate=SAMPLE_RATE, symbol_rate=SYMBOL_RATE)


def make_established_peers():
    alice_identity = Ed25519PrivateKey.generate()
    bob_identity = Ed25519PrivateKey.generate()
    alice_pub = raw_public(alice_identity.public_key())
    bob_pub = raw_public(bob_identity.public_key())
    alice_fp = hashlib.sha256(alice_pub).hexdigest()
    bob_fp = hashlib.sha256(bob_pub).hexdigest()
    alice = PeerHandshake("initiator", alice_identity, bob_pub, bob_fp)
    bob = PeerHandshake("responder", bob_identity, alice_pub, alice_fp)
    hello_r = bob.receive(acoustic_loopback(alice.start()))
    finish_i = alice.receive(acoustic_loopback(hello_r))
    finish_r = bob.receive(acoustic_loopback(finish_i))
    alice.receive(acoustic_loopback(finish_r))
    return alice, bob


class ReliableTextTests(unittest.TestCase):
    def test_acknowledged_message_over_modem(self):
        alice, bob = make_established_peers()
        alice_channel = ReliableTextChannel(alice)
        bob_channel = ReliableTextChannel(bob)

        outgoing = alice_channel.prepare("سلام؛ پیام قابل‌اعتماد")
        incoming = bob_channel.receive(acoustic_loopback(outgoing.packet))
        self.assertFalse(incoming.duplicate)
        self.assertEqual(incoming.text, "سلام؛ پیام قابل‌اعتماد")
        self.assertTrue(alice_channel.verify_ack(
            acoustic_loopback(incoming.ack_packet), outgoing.message_id
        ))

    def test_lost_ack_causes_duplicate_ack_without_duplicate_delivery(self):
        alice, bob = make_established_peers()
        alice_channel = ReliableTextChannel(alice)
        bob_channel = ReliableTextChannel(bob)
        outgoing = alice_channel.prepare("exactly once at the application layer")

        first = bob_channel.receive(acoustic_loopback(outgoing.packet))
        self.assertEqual(first.text, "exactly once at the application layer")
        # Simulate a lost ACK: resend byte-for-byte the original DATA ciphertext.
        second = bob_channel.receive(acoustic_loopback(outgoing.packet))
        self.assertTrue(second.duplicate)
        self.assertIsNone(second.text)
        # The fresh ACK is accepted even though the earlier ACK was lost.
        self.assertTrue(alice_channel.verify_ack(
            acoustic_loopback(second.ack_packet), outgoing.message_id
        ))

    def test_ack_for_another_message_is_rejected(self):
        alice, bob = make_established_peers()
        a = ReliableTextChannel(alice)
        b = ReliableTextChannel(bob)
        first = a.prepare("first")
        second = a.prepare("second")
        incoming = b.receive(acoustic_loopback(first.packet))
        with self.assertRaises(ReliableProtocolError):
            a.verify_ack(acoustic_loopback(incoming.ack_packet), second.message_id)

    def test_replay_outside_duplicate_cache_is_rejected(self):
        alice, bob = make_established_peers()
        a = ReliableTextChannel(alice)
        b = ReliableTextChannel(bob, duplicate_cache_size=1)
        first = a.prepare("first")
        second = a.prepare("second")
        b.receive(acoustic_loopback(first.packet))
        b.receive(acoustic_loopback(second.packet))
        with self.assertRaises(ReplayError):
            b.receive(acoustic_loopback(first.packet))


    def test_automatic_capture_keeps_full_preamble_and_stops_at_silence(self):
        packet = b"capture-test-packet"
        wave = modulate(packet, sample_rate=SAMPLE_RATE, symbol_rate=SYMBOL_RATE)
        rng = np.random.default_rng(55)
        lead = rng.normal(0.0, 0.0003, int(SAMPLE_RATE * 0.55)).astype(np.float32)
        tail = rng.normal(0.0, 0.0003, int(SAMPLE_RATE * 0.70)).astype(np.float32)
        recording = np.concatenate((lead, wave, tail))
        blocksize = 1024

        class FakeInputStream:
            def __init__(self, **kwargs):
                self.offset = 0
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, frames):
                end = self.offset + frames
                part = recording[self.offset:end]
                if len(part) < frames:
                    part = np.pad(part, (0, frames - len(part)))
                self.offset = end
                return part.reshape(-1, 1), False

        class FakeSoundDevice:
            InputStream = FakeInputStream

        with patch("core.audio._sounddevice", return_value=FakeSoundDevice()):
            captured = record_until_packet(
                SAMPLE_RATE, start_timeout=2.0, max_record_seconds=5.0,
                blocksize=blocksize, calibration_seconds=0.35,
                pre_roll_seconds=0.50, silence_seconds=0.30,
            )
        decoded = demodulate(captured, sample_rate=SAMPLE_RATE, symbol_rate=SYMBOL_RATE)
        self.assertEqual(decoded, packet)
        self.assertLess(len(captured), len(recording))


    def test_automatic_capture_when_signal_starts_immediately(self):
        packet = b"early-frame"
        wave = modulate(packet, sample_rate=SAMPLE_RATE, symbol_rate=SYMBOL_RATE)
        tail = np.zeros(int(SAMPLE_RATE * 0.65), dtype=np.float32)
        recording = np.concatenate((wave, tail))
        blocksize = 1024

        class FakeInputStream:
            def __init__(self, **kwargs):
                self.offset = 0
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, frames):
                end = self.offset + frames
                part = recording[self.offset:end]
                if len(part) < frames:
                    part = np.pad(part, (0, frames - len(part)))
                self.offset = end
                return part.reshape(-1, 1), False

        class FakeSoundDevice:
            InputStream = FakeInputStream

        with patch("core.audio._sounddevice", return_value=FakeSoundDevice()):
            captured = record_until_packet(
                SAMPLE_RATE, start_timeout=2.0, max_record_seconds=5.0,
                blocksize=blocksize, calibration_seconds=0.40,
                pre_roll_seconds=0.50, silence_seconds=0.30,
            )
        self.assertEqual(demodulate(captured, sample_rate=SAMPLE_RATE, symbol_rate=SYMBOL_RATE), packet)

    def test_payload_limit_accounts_for_reliability_envelope(self):
        alice, _bob = make_established_peers()
        channel = ReliableTextChannel(alice)
        counter = alice.session.tx.counter
        with self.assertRaises(ValueError):
            channel.prepare("x" * 40, max_packet_size=64)
        self.assertEqual(alice.session.tx.counter, counter)

    def test_activity_detector_requires_sustained_signal(self):
        self.assertIsNone(_first_sustained_activity([0.001, 0.01, 0.001, 0.01], 0.005))
        self.assertEqual(_first_sustained_activity([0.001, 0.01, 0.02, 0.03, 0.001], 0.005), 1)


if __name__ == "__main__":
    unittest.main()
