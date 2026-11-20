import unittest
from unittest.mock import patch

import numpy as np

from core.audio import ContinuousAudioListener, _explain_android_input_error
from core.modem import demodulate, modulate


class _FakeStream:
    def __init__(self, *, callback, blocksize, **_kwargs):
        self.callback = callback
        self.blocksize = blocksize
        self.closed = False

    def start(self):
        rate = 48000
        packet = modulate(
            b"persistent-background-listener",
            sample_rate=rate,
            symbol_rate=200,
            mark=1800,
            space=2400,
            gain=0.45,
            max_payload=1024,
            fec_mode="conv",
            hop_key=b"h" * 32,
        )
        samples = np.concatenate((
            np.zeros(rate // 5, dtype=np.float32),
            packet,
            np.zeros(rate // 5, dtype=np.float32),
        ))
        padded = ((len(samples) + self.blocksize - 1) // self.blocksize) * self.blocksize
        samples = np.pad(samples, (0, padded - len(samples)))
        # Simulate the live callback. The collector must preserve pre-roll,
        # segment the complete burst and stop only after the quiet tail.
        for offset in range(0, len(samples), self.blocksize):
            block = samples[offset:offset + self.blocksize]
            self.callback(block.reshape(-1, 1), self.blocksize, None, None)

    def stop(self):
        return None

    def close(self):
        self.closed = True


class _FakeSoundDevice:
    def InputStream(self, **kwargs):
        return _FakeStream(**kwargs)


class ContinuousAudioListenerTests(unittest.TestCase):
    def test_android_opensles_input_failure_has_actionable_termux_guidance(self):
        import os

        error = RuntimeError(
            "Error opening InputStream: Unanticipated host error "
            "[PaErrorCode -9999]: 'Initializing inputstream failed' "
            "[android OpenSLES error -9999]"
        )
        with patch.dict(os.environ, {
            "ANDROID_ROOT": "/system",
            "TERMUX_VERSION": "0.118",
            "PREFIX": "/data/data/com.termux/files/usr",
        }, clear=True), patch("core.audio.os.path.exists", return_value=False):
            explained = _explain_android_input_error(error)

        self.assertIsInstance(explained, RuntimeError)
        self.assertIn("before modem decoding", str(explained))
        self.assertIn("RECORD_AUDIO", str(explained))
        self.assertIn("Termux:API", str(explained))
        self.assertIn("live PCM stream", str(explained))

    def test_non_android_input_error_is_preserved(self):
        import os

        error = RuntimeError("OpenSL ES generic backend failure")
        with patch.dict(os.environ, {}, clear=True), patch("core.audio.os.path.exists", return_value=False):
            explained = _explain_android_input_error(error)
        self.assertIs(explained, error)

    def test_continuous_listener_segments_and_decodes_a_hopped_modem_burst(self):
        listener = ContinuousAudioListener(
            48000,
            blocksize=1024,
            activity_frequencies=(1200, 1500, 1800, 2100, 2400, 2700, 3000),
            quiet_seconds=0.12,
            pre_roll_seconds=0.20,
            max_frame_seconds=10,
        )
        with patch("core.audio._sounddevice", return_value=_FakeSoundDevice()):
            listener.start()
            try:
                frame = listener.get_frame(timeout=3.0)
                decoded = demodulate(
                    frame, 48000, 200, 1800, 2400, 1024,
                    hop_key=b"h" * 32,
                )
                self.assertEqual(decoded, b"persistent-background-listener")
                snapshot = listener.snapshot()
                self.assertEqual(snapshot["captured_frames"], 1)
                self.assertEqual(snapshot["dropped_blocks"], 0)
            finally:
                listener.stop()

    def test_continuous_listener_emits_debug_for_listening_detection_capture_and_resume(self):
        events = []
        listener = ContinuousAudioListener(
            48000,
            blocksize=1024,
            activity_frequencies=(1200, 1500, 1800, 2100, 2400, 2700, 3000),
            quiet_seconds=0.12,
            pre_roll_seconds=0.20,
            max_frame_seconds=10,
            on_debug=lambda stage, message, **fields: events.append((stage, message, fields)),
        )
        with patch("core.audio._sounddevice", return_value=_FakeSoundDevice()):
            listener.start()
            try:
                listener.get_frame(timeout=3.0)
                stages = [stage for stage, _message, _fields in events]
                self.assertIn("AUDIO", stages)
                self.assertIn("DETECT", stages)
                self.assertIn("CAPTURE", stages)
                level_events = [fields for stage, _message, fields in events if stage == "LEVEL"]
                self.assertTrue(level_events)
                self.assertIn("tone_ratio", level_events[0])
            finally:
                listener.stop()


if __name__ == "__main__":
    unittest.main()
