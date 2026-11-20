import queue
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from core.crypto import SecureChannel
from core.modem import HOP_OFFSETS
from core.recovery.errors import HeaderCRCError
from core.recovery.errors import HeaderCRCError
from core.session_engine import AudioSessionEngine, PacketWaiter, SessionEngineError


class FakeListener:
    def __init__(self):
        self.frames = queue.Queue()
        self.started = False
        self.busy = False
        self.paused = False
        self.activity_frequencies = ()

    def start(self):
        self.started = True
        return self

    def stop(self):
        self.started = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def get_frame(self, timeout=None):
        return self.frames.get(timeout=timeout)


class SessionEngineTests(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(
            sample_rate=48000, symbol_rate=200, mark_hz=1800, space_hz=2400,
            gain=0.55, max_payload=1024, input_device=None, output_device=None,
        )

    def test_cancelled_packet_waiter_raises_instead_of_returning_none(self):
        waiter = PacketWaiter(lambda _packet: True)
        waiter.cancel()
        with self.assertRaises(SessionEngineError):
            waiter.wait(timeout=0.1)

    def test_listener_tone_bank_contains_all_hopping_mark_and_space_frequencies(self):
        engine = AudioSessionEngine(self.cfg, channel_access=False)
        expected = {base + offset for base in (self.cfg.mark_hz, self.cfg.space_hz)
                    for offset in HOP_OFFSETS}
        self.assertEqual(set(engine.listener.activity_frequencies), expected)

    def test_decoder_tries_registered_hopping_profiles_until_one_matches(self):
        listener = FakeListener()
        received = []
        engine = AudioSessionEngine(
            self.cfg, listener=listener, channel_access=False,
            on_packet=received.append,
        )
        engine.set_hop_key(b"D" * 32)
        engine.add_receive_hop_key(b"B" * 32)
        attempted = []

        def fake_demodulate(*_args, hop_key=None, **_kwargs):
            attempted.append(hop_key)
            if hop_key == b"B" * 32:
                return b"ECBR-multi-profile"
            raise ValueError("wrong hopping profile")

        try:
            with patch("core.session_engine.demodulate", side_effect=fake_demodulate):
                engine.start()
                listener.frames.put(np.zeros(4096, dtype=np.float32))
                deadline = time.monotonic() + 1.0
                while not received and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertEqual(received, [b"ECBR-multi-profile"])
            self.assertEqual(attempted, [None, b"D" * 32, b"B" * 32])
        finally:
            engine.stop()

    def test_hopping_profiles_can_be_registered_and_removed_without_losing_tx_default(self):
        engine = AudioSessionEngine(self.cfg, listener=FakeListener(), channel_access=False)
        direct_key = b"D" * 32
        broadcast_key = b"B" * 32
        engine.set_hop_key(direct_key)
        engine.add_receive_hop_key(broadcast_key)
        self.assertEqual(engine.hop_key, direct_key)
        self.assertIn(direct_key, engine.receive_hop_keys)
        self.assertIn(broadcast_key, engine.receive_hop_keys)
        engine.set_hop_key(None)
        self.assertIsNone(engine.hop_key)
        self.assertIn(broadcast_key, engine.receive_hop_keys)
        engine.remove_receive_hop_key(broadcast_key)
        self.assertEqual(engine.receive_hop_keys, (None,))

    def test_broadcast_uses_short_randomized_channel_access_profile(self):
        listener = FakeListener()
        engine = AudioSessionEngine(
            self.cfg, listener=listener, channel_access="broadcast"
        )
        self.assertIsNotNone(engine.channel_access)
        self.assertEqual(engine.channel_access.slot_seconds, 0.04)
        self.assertEqual(engine.channel_access.min_idle_seconds, 0.12)
        self.assertEqual(engine.channel_access.max_backoff_slots, 8)
        self.assertEqual(engine.channel_access.max_wait_seconds, 5.0)

    def test_strong_header_crc_failure_skips_other_hop_keys_and_symbol_rates(self):
        listener = FakeListener()
        engine = AudioSessionEngine(
            self.cfg, listener=listener, channel_access=False,
            symbol_rates=(400, 200),
        )
        engine.set_hop_key(b"D" * 32)
        engine.add_receive_hop_key(b"B" * 32)
        attempted = []
        decoded = threading.Event()

        def fail_strong_header(*args, hop_key=None, **_kwargs):
            attempted.append((args[2], hop_key))
            decoded.set()
            raise HeaderCRCError("strong preamble/header CRC failure", sync_score=0.90)

        try:
            with patch("core.session_engine.demodulate", side_effect=fail_strong_header):
                engine.start()
                listener.frames.put(np.zeros(4096, dtype=np.float32))
                self.assertTrue(decoded.wait(1.0))
                time.sleep(0.05)
            self.assertEqual(attempted, [(400, None)])
        finally:
            engine.stop()

    def test_strong_header_crc_failure_skips_other_hop_keys_and_symbol_rates(self):
        listener = FakeListener()
        engine = AudioSessionEngine(
            self.cfg, listener=listener, channel_access=False,
            symbol_rates=(400, 200),
        )
        engine.set_hop_key(b"D" * 32)
        engine.add_receive_hop_key(b"B" * 32)
        attempted = []
        decoded = threading.Event()

        def fail_strong_header(*args, hop_key=None, **_kwargs):
            attempted.append((args[2], hop_key))
            decoded.set()
            raise HeaderCRCError("strong preamble/header CRC failure", sync_score=0.90)

        try:
            with patch("core.session_engine.demodulate", side_effect=fail_strong_header):
                engine.start()
                listener.frames.put(np.zeros(4096, dtype=np.float32))
                self.assertTrue(decoded.wait(1.0))
                time.sleep(0.05)
            self.assertEqual(attempted, [(400, None)])
        finally:
            engine.stop()

    def test_decoder_routes_registered_response_before_application_handler(self):
        listener = FakeListener()
        delivered = []
        engine = AudioSessionEngine(
            self.cfg, listener=listener, channel_access=False,
            on_packet=delivered.append,
        )
        try:
            with patch("core.session_engine.demodulate", return_value=b"ACK"):
                engine.start()
                waiter = engine.expect_packet(lambda packet: packet == b"ACK")
                listener.frames.put(np.zeros(4096, dtype=np.float32))
                self.assertEqual(waiter.wait(timeout=1.0), b"ACK")
                self.assertEqual(delivered, [])
        finally:
            engine.stop()

    def test_continuous_decoder_forwards_modem_diagnostics_callback(self):
        listener = FakeListener()
        delivered = []
        events = []
        trace = lambda stage, message, **fields: events.append((stage, message, fields))
        engine = AudioSessionEngine(
            self.cfg, listener=listener, channel_access=False,
            on_packet=delivered.append, on_debug=trace,
        )

        def fake_demodulate(*_args, on_debug=None, **_kwargs):
            self.assertIsNotNone(on_debug)
            on_debug("SYNC", "Preamble correlation completed", normalized_score="0.99")
            on_debug("CRC", "Frame integrity verified", crc="12345678")
            return b"ECBR" + b"safe-test-packet"

        try:
            with patch("core.session_engine.demodulate", side_effect=fake_demodulate):
                engine.start()
                listener.frames.put(np.zeros(4096, dtype=np.float32))
                deadline = time.monotonic() + 1.5
                while not delivered and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertEqual(delivered, [b"ECBR" + b"safe-test-packet"])
            stages = [stage for stage, _message, _fields in events]
            self.assertIn("RX", stages)
            self.assertIn("SYNC", stages)
            self.assertIn("CRC", stages)
            self.assertIn("PACKET", stages)
            self.assertIn("ROUTER", stages)
        finally:
            engine.stop()

    def test_transmit_worker_serializes_jobs_and_resumes_listener(self):
        listener = FakeListener()
        active = 0
        maximum_active = 0
        lock = threading.Lock()
        calls = []

        def fake_play(_wave, _rate, _device):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.03)
            with lock:
                active -= 1

        engine = AudioSessionEngine(
            self.cfg, listener=listener, channel_access=False, inter_frame_gap=0,
        )
        try:
            with patch("core.session_engine.modulate", side_effect=lambda *a, **k: np.ones(100)), \
                 patch("core.session_engine.play", side_effect=fake_play):
                engine.start()
                first = engine.submit_packet(b"one", fec_mode="conv", label="one")
                second = engine.submit_packet(b"two", fec_mode="conv", label="two")
                self.assertGreater(first.result(timeout=2), 0)
                self.assertGreater(second.result(timeout=2), 0)
            self.assertEqual(maximum_active, 1)
            self.assertFalse(listener.paused)
        finally:
            engine.stop()

    def test_stop_interrupts_active_playback_and_fails_the_send_future(self):
        listener = FakeListener()
        entered = threading.Event()
        release = threading.Event()
        engine = AudioSessionEngine(
            self.cfg, listener=listener, channel_access=False,
            inter_frame_gap=0,
        )
        def fake_play(_wave, _rate, _device):
            entered.set()
            release.wait(2.0)
        def fake_stop_playback():
            release.set()
        with patch("core.session_engine.modulate", return_value=np.ones(100)), \
             patch("core.session_engine.play", side_effect=fake_play), \
             patch("core.session_engine.stop_playback", side_effect=fake_stop_playback):
            engine.start()
            future = engine.submit_packet(b"active", fec_mode="conv")
            self.assertTrue(entered.wait(1.0))
            engine.stop()
            with self.assertRaises(Exception):
                future.result(timeout=0.1)
            self.assertTrue(release.is_set())


    def test_restart_after_interrupting_active_playback_keeps_tx_worker_alive(self):
        listener = FakeListener()
        entered = threading.Event()
        release = threading.Event()
        play_count = []
        engine = AudioSessionEngine(
            self.cfg, listener=listener, channel_access=False,
            inter_frame_gap=0,
        )

        def fake_play(_wave, _rate, _device):
            play_count.append(1)
            if len(play_count) == 1:
                entered.set()
                release.wait(2.0)

        def fake_stop_playback():
            release.set()

        with patch("core.session_engine.modulate", return_value=np.ones(100)), \
             patch("core.session_engine.play", side_effect=fake_play), \
             patch("core.session_engine.stop_playback", side_effect=fake_stop_playback):
            try:
                engine.start()
                first = engine.submit_packet(b"first", fec_mode="conv")
                self.assertTrue(entered.wait(1.0))
                engine.stop()
                with self.assertRaises(Exception):
                    first.result(timeout=0.1)

                engine.start()
                second = engine.submit_packet(b"second", fec_mode="conv")
                self.assertGreater(second.result(timeout=1.0), 0)
                self.assertEqual(len(play_count), 2)
            finally:
                engine.stop()

    def test_user_cancel_interrupts_active_audio_and_resumes_listener(self):
        listener = FakeListener()
        entered = threading.Event()
        release = threading.Event()
        cancel_event = threading.Event()
        engine = AudioSessionEngine(self.cfg, listener=listener, channel_access=False,
                                    inter_frame_gap=0)

        def fake_play(_wave, _rate, _device):
            entered.set()
            release.wait(2.0)

        def fake_stop_playback():
            release.set()

        with patch("core.session_engine.modulate", return_value=np.ones(100)), \
             patch("core.session_engine.play", side_effect=fake_play), \
             patch("core.session_engine.stop_playback", side_effect=fake_stop_playback):
            engine.start()
            future = engine.submit_packet(b"voice-frame", fec_mode="conv",
                                          cancel_event=cancel_event, label="VOICE CHUNK")
            self.assertTrue(entered.wait(1.0))
            self.assertTrue(engine.cancel_transmission(cancel_event))
            with self.assertRaises(SessionEngineError):
                future.result(timeout=1.0)
            self.assertTrue(release.is_set())
            self.assertFalse(listener.paused)
        engine.stop()

    def test_cancelled_queued_transmission_is_never_played(self):
        listener = FakeListener()
        cancel_event = threading.Event()
        played = []
        engine = AudioSessionEngine(self.cfg, listener=listener, channel_access=False,
                                    inter_frame_gap=0)
        with patch("core.session_engine.modulate", side_effect=lambda *a, **k: np.ones(100)), \
             patch("core.session_engine.play", side_effect=lambda *a, **k: played.append(True)):
            engine.start()
            cancel_event.set()
            future = engine.submit_packet(b"cancel-before-dequeue", cancel_event=cancel_event)
            with self.assertRaises(SessionEngineError):
                future.result(timeout=1.0)
            self.assertEqual(played, [])
        engine.stop()

    def test_async_stop_returns_immediately_and_wait_can_join_cleanup(self):
        listener = FakeListener()
        engine = AudioSessionEngine(self.cfg, listener=listener, channel_access=False)
        engine.start()
        cleanup_entered = threading.Event()
        allow_cleanup = threading.Event()
        original_stop = listener.stop

        def slow_stop():
            cleanup_entered.set()
            allow_cleanup.wait(1.5)
            original_stop()

        with patch.object(listener, "stop", side_effect=slow_stop):
            started = time.monotonic()
            engine.stop(wait=False)
            self.assertLess(time.monotonic() - started, 0.15)
            self.assertFalse(engine.running)
            self.assertTrue(cleanup_entered.wait(0.5))
            allow_cleanup.set()
            engine.stop(wait=True)
        self.assertFalse(listener.started)

    def test_start_waits_for_async_shutdown_before_reopening_listener(self):
        listener = FakeListener()
        engine = AudioSessionEngine(self.cfg, listener=listener, channel_access=False)
        engine.start()
        cleanup_entered = threading.Event()
        allow_cleanup = threading.Event()
        original_stop = listener.stop

        def slow_stop():
            cleanup_entered.set()
            allow_cleanup.wait(1.5)
            original_stop()

        with patch.object(listener, "stop", side_effect=slow_stop):
            engine.stop(wait=False)
            self.assertTrue(cleanup_entered.wait(0.5))
            started = threading.Event()
            runner = threading.Thread(target=lambda: (engine.start(), started.set()))
            runner.start()
            self.assertFalse(started.wait(0.08))
            allow_cleanup.set()
            self.assertTrue(started.wait(1.0))
            runner.join(1.0)
        self.assertTrue(engine.running)
        self.assertTrue(listener.started)
        engine.stop()

    def test_stop_cancels_transmit_waiting_for_a_busy_channel(self):
        listener = FakeListener()
        listener.busy = True
        engine = AudioSessionEngine(
            self.cfg, listener=listener, channel_access=True,
            inter_frame_gap=0,
        )
        engine.start()
        future = engine.submit_packet(b"waiting", fec_mode="conv")
        try:
            time.sleep(0.05)
            started = time.monotonic()
            engine.stop()
            self.assertLess(time.monotonic() - started, 2.0)
            with self.assertRaises(Exception):
                future.result(timeout=0.1)
        finally:
            engine.stop()

    def test_concurrent_encryptions_allocate_unique_monotonic_counters(self):
        channel = SecureChannel(b"K" * 32)
        def encrypt(index):
            return channel.encrypt(f"message-{index}".encode())
        with ThreadPoolExecutor(max_workers=12) as pool:
            packets = list(pool.map(encrypt, range(500)))
        counters = [int.from_bytes(packet[8:16], "big") for packet in packets]
        self.assertEqual(len(set(counters)), 500)
        self.assertEqual(set(counters), set(range(500)))


if __name__ == "__main__":
    unittest.main()
