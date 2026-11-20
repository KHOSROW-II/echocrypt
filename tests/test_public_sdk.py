import queue
import threading
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import patch

from echocrypt_sdk import (
    AcousticClient,
    AcousticConfig,
    ClientClosedError,
    ClientNotRunningError,
    PacketTooLargeError,
    ReceiveTimeout,
)


class FakeWaiter:
    def __init__(self, predicate):
        self.predicate = predicate
        self.event = threading.Event()
        self.packet = None
        self.cancelled = False
        self.error = None

    def wait(self, timeout=None, cancel_event=None):
        if not self.event.wait(timeout):
            raise TimeoutError("test waiter timeout")
        if self.error is not None:
            raise self.error
        if self.cancelled and self.packet is None:
            raise RuntimeError("waiter cancelled")
        return self.packet

    def cancel(self, error=None):
        self.cancelled = True
        self.error = error
        self.event.set()

    def resolve(self, packet):
        self.packet = bytes(packet)
        self.event.set()


class FakeEngine:
    def __init__(self, config, **kwargs):
        self.config = config
        self.on_packet = kwargs["on_packet"]
        self.on_event = kwargs["on_event"]
        self.running = False
        self.future = None
        self.cancel_event = None
        self.waiter = None
        self.sent = []

    def start(self):
        self.running = True
        self.on_event(SimpleNamespace(
            kind="listener_started", message="started", payload=None,
            timestamp=1.0, metadata={},
        ))
        return self

    def stop(self, wait=True):
        self.running = False
        self.on_event(SimpleNamespace(
            kind="listener_stopped", message="stopped", payload=None,
            timestamp=2.0, metadata={},
        ))

    def submit_packet(self, packet, **kwargs):
        if not self.running:
            raise RuntimeError("not running")
        self.future = Future()
        self.sent.append((bytes(packet), kwargs))
        self.cancel_event = kwargs["cancel_event"]
        return self.future

    def cancel_transmission(self, event):
        self.cancel_event = event
        event.set()
        return True

    def expect_packet(self, predicate):
        self.waiter = FakeWaiter(predicate)
        return self.waiter

    def cancel_waiter(self, waiter):
        waiter.cancel()

    def deliver(self, packet):
        self.on_packet(packet)

    def deliver_waiter_packet(self, packet):
        if self.waiter is not None and self.waiter.predicate(packet):
            self.waiter.resolve(packet)
            return True
        self.deliver(packet)
        return False


class PublicSDKTests(unittest.TestCase):
    def setUp(self):
        self.engine_patch = patch("echocrypt_sdk.client.AudioSessionEngine", FakeEngine)
        self.engine_patch.start()
        self.addCleanup(self.engine_patch.stop)

    def make_client(self, **kwargs):
        client = AcousticClient(**kwargs)
        self.addCleanup(client.close)
        return client

    def test_default_configuration_is_valid_and_maps_payload_limit(self):
        config = AcousticConfig()
        core = config.to_core_config()
        self.assertEqual(config.symbol_rates, (config.symbol_rate,))
        self.assertEqual(core.max_payload, config.max_packet_bytes)
        self.assertEqual(core.sample_rate, 48000)

    def test_invalid_configuration_is_rejected_before_audio_start(self):
        with self.assertRaises(ValueError):
            AcousticConfig(sample_rate=44100, symbol_rate=200)
        with self.assertRaises(ValueError):
            AcousticConfig(gain=1.5)
        with self.assertRaises(ValueError):
            AcousticConfig(symbol_rates=(200, 200, 0))
        with self.assertRaises(ValueError):
            AcousticConfig(channel_access="always")

    def test_sending_requires_a_started_client(self):
        client = self.make_client()
        with self.assertRaises(ClientNotRunningError):
            client.send_async(b"hello")

    def test_send_async_returns_result_and_preserves_fec_options(self):
        client = self.make_client()
        client.start()
        transmission = client.send_async(
            b"hello", fec_mode="hamming", symbol_rate=800, label="telemetry"
        )
        packet, options = client._engine.sent[-1]
        self.assertEqual(packet, b"hello")
        self.assertEqual(options["fec_mode"], "hamming")
        self.assertEqual(options["symbol_rate"], 800)
        client._engine.future.set_result(0.375)
        result = transmission.wait(timeout=1)
        self.assertEqual(result.payload_bytes, 5)
        self.assertEqual(result.duration_seconds, 0.375)
        self.assertEqual(result.label, "telemetry")
        self.assertTrue(transmission.done)

    def test_queued_transmission_can_be_cancelled(self):
        client = self.make_client()
        client.start()
        transmission = client.send_async(b"cancel me")
        self.assertTrue(transmission.cancel())
        self.assertTrue(transmission.cancelled)
        self.assertTrue(client._engine.cancel_event.is_set())

    def test_packet_limit_is_enforced_before_queueing(self):
        client = self.make_client(config=AcousticConfig(max_packet_bytes=122))
        client.start()
        with self.assertRaises(PacketTooLargeError):
            client.send_async(b"x" * 123)
        self.assertEqual(client._engine.sent, [])

    def test_receive_queue_and_packet_event_are_both_populated(self):
        client = self.make_client()
        received = []
        callback_event = threading.Event()
        client.on("packet_received", lambda event: (received.append(event.data), callback_event.set()))
        client.start()
        client._engine.deliver(b"opaque-packet")
        self.assertEqual(client.receive(timeout=1), b"opaque-packet")
        self.assertTrue(callback_event.wait(1))
        self.assertEqual(received, [b"opaque-packet"])

    def test_slow_event_callback_does_not_block_packet_delivery(self):
        client = self.make_client()
        entered = threading.Event()
        release = threading.Event()

        def slow_callback(_event):
            entered.set()
            release.wait(1.5)

        client.on("packet_received", slow_callback)
        client.start()
        client._engine.deliver(b"packet")
        self.assertTrue(entered.wait(1))
        self.assertEqual(client.receive(timeout=0.2), b"packet")
        release.set()

    def test_one_shot_waiter_can_match_a_protocol_response(self):
        client = self.make_client()
        client.start()
        waiter = client.expect_packet(lambda packet: packet.startswith(b"ACK"))
        self.assertTrue(client._engine.deliver_waiter_packet(b"ACK-1"))
        self.assertEqual(waiter.wait(timeout=1), b"ACK-1")

    def test_receive_timeout_uses_sdk_exception(self):
        client = self.make_client()
        with self.assertRaises(ReceiveTimeout):
            client.receive(timeout=0.01)

    def test_closed_client_cannot_be_restarted(self):
        client = self.make_client()
        client.close()
        self.assertTrue(client.closed)
        with self.assertRaises(ClientClosedError):
            client.start()


if __name__ == "__main__":
    unittest.main()
