import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from core.audio import record_until_packet
from core.debug import AcousticDebug
from core.modem import modulate, demodulate, _demodulate_once
import core.config as config_module


RATE = 48000
SYMBOL_RATE = 200


class _ArrayInputStream:
    recording = np.zeros(1, dtype=np.float32)

    def __init__(self, **kwargs):
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, frames):
        end = self.offset + frames
        part = self.recording[self.offset:end]
        self.offset = end
        if len(part) < frames:
            part = np.pad(part, (0, frames - len(part)))
        return part.reshape(-1, 1), False


class _FakeSoundDevice:
    InputStream = _ArrayInputStream


class AcousticDebugTests(unittest.TestCase):
    def test_noisy_room_audio_is_ignored_until_modem_tones_arrive(self):
        packet = b"debug-pipeline"
        wave = modulate(packet, sample_rate=RATE, symbol_rate=SYMBOL_RATE)
        rng = np.random.default_rng(732)
        noise_before = rng.normal(0.0, 0.03, int(RATE * 0.60)).astype(np.float32)
        noise_after = rng.normal(0.0, 0.03, int(RATE * 0.60)).astype(np.float32)
        recording = np.concatenate((noise_before, wave, noise_after))
        _ArrayInputStream.recording = recording
        events = []
        callback = lambda stage, message, **fields: events.append((stage, message, fields))

        with patch("core.audio._sounddevice", return_value=_FakeSoundDevice):
            captured = record_until_packet(
                RATE, start_timeout=2.0, max_record_seconds=8.0,
                blocksize=1024, calibration_seconds=0.15,
                pre_roll_seconds=0.65, silence_seconds=0.30,
                activity_frequencies=(1800, 2400), on_debug=callback,
            )
        decoded = demodulate(captured, RATE, SYMBOL_RATE, on_debug=callback)
        self.assertEqual(decoded, packet)
        self.assertTrue(any(stage == "LEVEL" for stage, *_ in events))
        self.assertTrue(any(stage == "DETECT" for stage, *_ in events))
        self.assertTrue(any(stage == "CRC" and "verified" in message.lower()
                            for stage, message, _ in events))
        self.assertTrue(any(stage == "PACKET" for stage, *_ in events))

    def test_debug_pipeline_reports_each_decode_stage(self):
        packet = b"ECH1" + b"sample-handshake-payload"
        wave = modulate(packet, sample_rate=RATE, symbol_rate=SYMBOL_RATE)
        events = []
        decoded = demodulate(
            np.concatenate((np.zeros(400, dtype=np.float32), wave,
                            np.zeros(1000, dtype=np.float32))),
            RATE, SYMBOL_RATE,
            on_debug=lambda stage, message, **fields: events.append(stage),
        )
        self.assertEqual(decoded, packet)
        for required in ("DEMOD", "SYNC", "BITS", "FEC", "FRAME", "CRC", "PACKET"):
            self.assertIn(required, events)

    def test_sample_clock_recovery_handles_small_independent_audio_clock_drift(self):
        packet = bytes((i * 7) % 256 for i in range(120))
        wave = modulate(packet, sample_rate=RATE, symbol_rate=SYMBOL_RATE)
        # Simulate a receiver/DAC clock mismatch of +0.2% in captured sample count.
        ratio = 1.002
        captured_count = int(round(len(wave) * ratio))
        captured = np.interp(np.arange(captured_count, dtype=np.float64) / ratio,
                             np.arange(len(wave), dtype=np.float64), wave).astype(np.float32)
        with self.assertRaises(ValueError):
            _demodulate_once(captured, RATE, SYMBOL_RATE)
        recovered = demodulate(captured, RATE, SYMBOL_RATE)
        self.assertEqual(recovered, packet)

    def test_debug_config_defaults_off_and_can_be_saved_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "default.toml"
            config_path.write_text(
                "[audio]\nsample_rate = 48000\ninput_device = \"default\"\noutput_device = \"default\"\n"
                "[modem]\nsymbol_rate = 200\nmark_hz = 1800\nspace_hz = 2400\n"
                "[protocol]\nmax_payload = 1024\n",
                encoding="utf-8",
            )
            with patch.object(config_module, "CONFIG_PATH", config_path):
                self.assertFalse(config_module.load().debug_enabled)
                config_module.save_setting("debug", "enabled", True)
                self.assertTrue(config_module.load().debug_enabled)
                config_module.save_setting("debug", "enabled", False)
                self.assertFalse(config_module.load().debug_enabled)

    def test_debug_trace_writes_log_without_payload_or_key_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "acoustic-debug.log"
            trace = AcousticDebug(True, path)
            trace.emit("LEVEL", "Microphone is listening", rms="0.010000", tone_ratio="0.009")
            trace.emit("PACKET", "Modem frame delivered", payload_bytes=128, payload_type="ECH1")
            content = path.read_text(encoding="utf-8")
        self.assertIn("MIC [", content)
        self.assertIn("NO MODEM TONE", content)
        self.assertIn("payload_bytes=128", content)
        self.assertNotIn("PRIVATE KEY", content)

    def test_disabled_debug_trace_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "must-not-exist.log"
            trace = AcousticDebug(False, path)
            with patch("builtins.print") as output:
                trace.emit("LEVEL", "test", rms="0.1")
            output.assert_not_called()
            self.assertFalse(path.exists())

    def test_enabled_debug_trace_is_file_only_and_never_prints_to_chat_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "acoustic-debug.log"
            trace = AcousticDebug(True, path)
            with patch("builtins.print") as output:
                trace.emit("LEVEL", "Microphone is listening", rms="0.012", tone_ratio="0.003")
                trace.emit("BROADCAST_DELIVER", "Broadcast message routed", message_id="abc123")
            output.assert_not_called()
            content = path.read_text(encoding="utf-8")
            self.assertIn("[ACOUSTIC:LEVEL]", content)
            self.assertIn("[ACOUSTIC:BROADCAST_DELIVER]", content)
            self.assertIn("MIC [", content)


    def test_modem_reports_clipping_in_preflight(self):
        import numpy as np
        from core.modem import demodulate, modulate
        packet = b"clipping-preflight"
        wave = modulate(packet)
        clipped = np.clip(wave * 4.0, -1.0, 1.0)
        events = []
        def capture(stage, message, **fields):
            events.append((stage, message, fields))
        self.assertEqual(demodulate(clipped, on_debug=capture), packet)
        checks = [fields for stage, message, fields in events
                  if stage == "CHANNEL" and message == "Input level and clipping preflight"]
        self.assertEqual(len(checks), 1)
        self.assertGreater(float(checks[0]["clipping_fraction"]), 0.01)
        self.assertEqual(checks[0]["status"], "SEVERE_CLIPPING")


if __name__ == "__main__":
    unittest.main()

class AcousticDebugSettingsMenuTests(unittest.TestCase):
    def test_configuration_menu_exposes_and_toggles_debug_setting(self):
        from types import SimpleNamespace
        from interface import settings as settings_module

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "default.toml"
            config_path.write_text(
                "[audio]\nsample_rate = 48000\ninput_device = \"default\"\noutput_device = \"default\"\n"
                "[modem]\nsymbol_rate = 200\nmark_hz = 1800\nspace_hz = 2400\n"
                "[protocol]\nmax_payload = 1024\n[debug]\nenabled = false\n",
                encoding="utf-8",
            )
            choices = iter(("debug", "back"))
            seen_items = []

            class FakeMenu:
                def __init__(self, title, items):
                    self.title = title
                    self.items = items
                    seen_items.append(items)

                def run(self):
                    return next(choices)

            with patch.object(config_module, "CONFIG_PATH", config_path), \
                 patch.object(settings_module, "Menu", FakeMenu), \
                 patch.object(settings_module, "_pause"), \
                 patch.object(settings_module, "render_app_header"), \
                 patch("builtins.print"):
                settings_module.settings_menu()
                self.assertTrue(config_module.load().debug_enabled)
            labels = [label for _key, label in seen_items[0]]
            self.assertTrue(any("Acoustic debug logging: OFF" in label for label in labels))


class HandshakeDebugTraceTests(unittest.TestCase):
    def test_control_packet_parser_stage_is_reported(self):
        import hashlib
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from security.identity import raw_public
        from security.peer_handshake import PeerHandshake
        from interface.communication import _handshake_receive

        initiator_key = Ed25519PrivateKey.generate()
        responder_key = Ed25519PrivateKey.generate()
        initiator_public = raw_public(initiator_key.public_key())
        responder_public = raw_public(responder_key.public_key())
        initiator_fp = hashlib.sha256(initiator_public).hexdigest()
        responder_fp = hashlib.sha256(responder_public).hexdigest()
        responder = PeerHandshake("responder", responder_key, initiator_public, initiator_fp)
        trace_events = []

        class Trace:
            def emit(self, stage, message, **fields):
                trace_events.append((stage, message, fields))

        hello_i = PeerHandshake("initiator", initiator_key, responder_public, responder_fp).start()
        hello_r = _handshake_receive(responder, hello_i, "HELLO-I", Trace())
        self.assertEqual(hello_r[:4], b"ECH1")
        self.assertTrue(any(stage == "PARSER" and "accepted" in message.lower()
                            for stage, message, _fields in trace_events))
