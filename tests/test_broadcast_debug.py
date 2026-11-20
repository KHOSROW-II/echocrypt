import threading
import unittest

from core.broadcast import BroadcastCodec, BroadcastReassembler
from interface.communication import (
    _broadcast_packet_router, _broadcast_send_frames, _supported_symbol_rates,
)


class BroadcastDebugTests(unittest.TestCase):
    def test_broadcast_router_reports_receive_decode_and_delivery_without_plaintext(self):
        sender = BroadcastCodec(sender_id=b"ALICE001", max_packet_size=256)
        receiver = BroadcastCodec(sender_id=b"BOB00001", max_packet_size=256)
        reassembler = BroadcastReassembler(receiver)
        events = []
        messages = []
        engine_ref = {"engine": object()}
        handler = _broadcast_packet_router(
            engine_ref, receiver, reassembler, None, messages,
            threading.RLock(), {},
            on_debug=lambda stage, message, **fields: events.append((stage, message, fields)),
        )
        private_text_marker = "DO_NOT_WRITE_MESSAGE_CONTENT_TO_DEBUG"
        frame = sender.encode_text(private_text_marker)[0]
        handler(frame)

        stages = [stage for stage, _message, _fields in events]
        self.assertIn("BROADCAST_RX", stages)
        self.assertIn("BROADCAST_DECODE", stages)
        self.assertIn("BROADCAST_DELIVER", stages)
        self.assertEqual(len(messages), 1)
        self.assertIn(private_text_marker, messages[0]["body"])
        self.assertNotIn(private_text_marker, repr(events))

    def test_broadcast_send_uses_reliable_rate_and_keeps_fast_legacy_fallback(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        config = SimpleNamespace(sample_rate=48000, symbol_rate=200,
                                 fast_symbol_rate=800)
        self.assertEqual(_supported_symbol_rates(config), (400, 200, 100, 800))

        class RecordingEngine:
            def __init__(self):
                self.config = config
                self.calls = []

            def send_packet(self, packet, **kwargs):
                self.calls.append((bytes(packet), kwargs))

        engine = RecordingEngine()
        with patch("interface.communication.packet_duration", return_value=1.0):
            _broadcast_send_frames(engine, [b"broadcast-test"], "TEST", copies=1)
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(engine.calls[0][1]["symbol_rate"], 400)

    def test_broadcast_router_logs_codec_rejections(self):
        receiver = BroadcastCodec(sender_id=b"BOB00001", max_packet_size=256)
        reassembler = BroadcastReassembler(receiver)
        events = []
        handler = _broadcast_packet_router(
            {"engine": object()}, receiver, reassembler, None, [],
            threading.RLock(), {},
            on_debug=lambda stage, message, **fields: events.append((stage, message, fields)),
        )
        with self.assertRaises(Exception):
            handler(b"not-a-broadcast-frame")
        self.assertTrue(any(stage == "BROADCAST_ERROR" for stage, *_ in events))


class _FrameListener:
    def __init__(self):
        import queue
        self.frames = queue.Queue()
        self.busy = False
        self.started = False

    def start(self):
        self.started = True
        return self

    def stop(self):
        self.started = False

    def pause(self):
        return None

    def resume(self):
        return None

    def get_frame(self, timeout=None):
        return self.frames.get(timeout=timeout)


class BroadcastDebugIntegrationTests(unittest.TestCase):
    def test_debug_traces_real_broadcast_waveform_to_file_without_live_terminal_output(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from core.debug import AcousticDebug
        import time
        from types import SimpleNamespace

        import numpy as np
        from core.broadcast import BROADCAST_HOP_KEY
        from core.modem import modulate
        from core.session_engine import AudioSessionEngine

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "acoustic-debug.log"
            trace = AcousticDebug(True, log_path)
            sender = BroadcastCodec(sender_id=b"ALICE001", max_packet_size=256)
            receiver = BroadcastCodec(sender_id=b"BOB00001", max_packet_size=256)
            reassembler = BroadcastReassembler(receiver)
            frame = sender.encode_text("FILE_ONLY_DEBUG_TEST")[0]
            samples = modulate(frame, sample_rate=48000, symbol_rate=200,
                mark=1800, space=2400, gain=0.45, max_payload=1024,
                fec_mode="conv", hop_key=BROADCAST_HOP_KEY)
            listener = _FrameListener()
            messages = []
            config = SimpleNamespace(sample_rate=48000, symbol_rate=200,
                mark_hz=1800, space_hz=2400, gain=0.45, max_payload=1024,
                input_device=None, output_device=None)
            engine_ref = {"engine": None}
            handler = _broadcast_packet_router(engine_ref, receiver, reassembler,
                None, messages, threading.RLock(), {}, on_debug=trace.emit)
            engine = AudioSessionEngine(config, hop_key=BROADCAST_HOP_KEY,
                symbol_rates=(200,), max_packet_bytes=1024, on_packet=handler,
                listener=listener, channel_access=False, on_debug=trace.emit)
            engine_ref["engine"] = engine
            try:
                with patch("builtins.print") as console_print:
                    engine.start()
                    listener.frames.put(np.concatenate((np.zeros(500, dtype=np.float32),
                        samples, np.zeros(1500, dtype=np.float32))))
                    deadline = time.monotonic() + 3.0
                    while not messages and time.monotonic() < deadline:
                        time.sleep(0.01)
                    engine.stop()
                    console_print.assert_not_called()
                self.assertEqual(len(messages), 1)
                content = log_path.read_text(encoding="utf-8")
                for stage in ("SYNC", "CRC", "PACKET", "BROADCAST_DELIVER"):
                    self.assertIn(f"[ACOUSTIC:{stage}]", content)
                self.assertNotIn("FILE_ONLY_DEBUG_TEST", content)
            finally:
                engine.stop()

    def test_debug_traces_real_broadcast_waveform_from_demodulator_to_chat_delivery(self):
        import time
        from types import SimpleNamespace

        import numpy as np
        from core.broadcast import BROADCAST_HOP_KEY
        from core.modem import modulate
        from core.session_engine import AudioSessionEngine

        sender = BroadcastCodec(sender_id=b"ALICE001", max_packet_size=256)
        receiver = BroadcastCodec(sender_id=b"BOB00001", max_packet_size=256)
        reassembler = BroadcastReassembler(receiver)
        secret_text = "DO_NOT_EXPOSE_BROADCAST_TEXT"
        frame = sender.encode_text(secret_text)[0]
        samples = modulate(
            frame, sample_rate=48000, symbol_rate=200,
            mark=1800, space=2400, gain=0.45, max_payload=1024,
            fec_mode="conv", hop_key=BROADCAST_HOP_KEY,
        )
        listener = _FrameListener()
        events = []
        messages = []
        config = SimpleNamespace(
            sample_rate=48000, symbol_rate=200, mark_hz=1800, space_hz=2400,
            gain=0.45, max_payload=1024, input_device=None, output_device=None,
        )
        engine_ref = {"engine": None}
        handler = _broadcast_packet_router(
            engine_ref, receiver, reassembler, None, messages,
            threading.RLock(), {},
            on_debug=lambda stage, message, **fields: events.append((stage, message, fields)),
        )
        engine = AudioSessionEngine(
            config, hop_key=BROADCAST_HOP_KEY, symbol_rates=(200,),
            max_packet_bytes=1024, on_packet=handler, listener=listener,
            channel_access=False,
            on_debug=lambda stage, message, **fields: events.append((stage, message, fields)),
        )
        engine_ref["engine"] = engine
        try:
            engine.start()
            listener.frames.put(np.concatenate((np.zeros(500, dtype=np.float32), samples,
                                                 np.zeros(1500, dtype=np.float32))))
            deadline = time.monotonic() + 3.0
            while not messages and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(messages), 1)
            stages = [stage for stage, _message, _fields in events]
            for required in ("RX", "SYNC", "CRC", "PACKET", "BROADCAST_RX",
                             "BROADCAST_DECODE", "BROADCAST_DELIVER"):
                self.assertIn(required, stages)
            self.assertNotIn(secret_text, repr(events))
        finally:
            engine.stop()


if __name__ == "__main__":
    unittest.main()
