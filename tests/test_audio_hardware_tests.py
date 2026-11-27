import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from core import audio


class FakeOutputBackend:
    def __init__(self, channels=2):
        self.channels = channels
        self.played = []

    def query_devices(self, device=None, kind=None):
        return {"name": "Fake stereo output", "max_output_channels": self.channels}

    def play(self, samples, samplerate, device=None, blocking=False):
        self.played.append((np.asarray(samples).copy(), samplerate, device, blocking))


class FakeInputStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


class FakeInputBackend:
    def __init__(self):
        self.stream = None

    def InputStream(self, **kwargs):
        self.stream = FakeInputStream(**kwargs)
        return self.stream


class FakeCallbackStop(Exception):
    pass


class FakeDuplexStream:
    def __init__(self, backend, **kwargs):
        self.backend = backend
        self.kwargs = kwargs
        self.started = False
        self.closed = False

    def start(self):
        self.started = True
        callback = self.kwargs["callback"]
        blocksize = self.kwargs["blocksize"]
        prior_output = np.zeros((blocksize, 1), dtype=np.float32)
        try:
            while True:
                indata = prior_output.copy()
                outdata = np.zeros((blocksize, 1), dtype=np.float32)
                callback(indata, outdata, blocksize, None, None)
                prior_output = outdata
        except FakeCallbackStop:
            pass

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


class FakeDuplexBackend:
    CallbackStop = FakeCallbackStop

    def __init__(self):
        self.stream = None

    def Stream(self, **kwargs):
        self.stream = FakeDuplexStream(self, **kwargs)
        return self.stream


class SpeakerTestTests(unittest.TestCase):
    def test_speaker_test_plays_left_then_right_for_two_seconds(self):
        backend = FakeOutputBackend()
        with patch("core.audio._sounddevice", return_value=backend), \
             patch("core.audio.time.sleep"):
            result = audio.test_speaker_channels(8000, device=None, duration=2.0)

        self.assertEqual(result["channels"], 2)
        self.assertEqual(result["duration_per_channel"], 2.0)
        self.assertEqual(len(backend.played), 2)
        left, right = backend.played[0][0], backend.played[1][0]
        self.assertEqual(left.shape, (16000, 2))
        self.assertEqual(right.shape, (16000, 2))
        self.assertTrue(np.any(left[:, 0]))
        self.assertTrue(np.all(left[:, 1] == 0))
        self.assertTrue(np.all(right[:, 0] == 0))
        self.assertTrue(np.any(right[:, 1]))
        self.assertTrue(all(item[3] for item in backend.played))

    def test_speaker_test_refuses_mono_output(self):
        backend = FakeOutputBackend(channels=1)
        with patch("core.audio._sounddevice", return_value=backend):
            with self.assertRaisesRegex(RuntimeError, "mono"):
                audio.test_speaker_channels(48000)
        self.assertEqual(backend.played, [])

    def test_speaker_test_rejects_invalid_duration(self):
        backend = FakeOutputBackend()
        with patch("core.audio._sounddevice", return_value=backend):
            with self.assertRaises(ValueError):
                audio.test_speaker_channels(48000, duration=10)


class UltrasonicPathTests(unittest.TestCase):
    def test_ultrasonic_full_duplex_test_detects_both_loopback_tones(self):
        backend = FakeDuplexBackend()
        with patch("core.audio._sounddevice", return_value=backend):
            result = audio.test_ultrasonic_path(
                48000, frequencies=(20700, 22200), duration=0.25,
            )
        self.assertTrue(result["passed"])
        self.assertEqual(
            [tone["frequency"] for tone in result["tones"]],
            [20700.0, 22200.0],
        )
        self.assertTrue(all(tone["detected"] for tone in result["tones"]))
        self.assertTrue(backend.stream.closed)

    def test_ultrasonic_test_rejects_frequencies_outside_safe_band(self):
        backend = FakeDuplexBackend()
        with patch("core.audio._sounddevice", return_value=backend):
            with self.assertRaisesRegex(ValueError, "above 20000"):
                audio.test_ultrasonic_path(
                    48000, frequencies=(19000, 22200),
                )
        self.assertIsNone(backend.stream)

    def test_ultrasonic_test_requires_sufficient_sample_rate(self):
        backend = FakeDuplexBackend()
        with patch("core.audio._sounddevice", return_value=backend):
            with self.assertRaisesRegex(ValueError, "at least 44100"):
                audio.test_ultrasonic_path(24000)
        self.assertIsNone(backend.stream)


class MicrophoneMeterTests(unittest.TestCase):
    def test_microphone_monitor_updates_live_rms_and_peak_without_recording(self):
        backend = FakeInputBackend()
        with patch("core.audio._sounddevice", return_value=backend):
            monitor = audio.MicrophoneLevelMonitor(48000, device=None)
            monitor.start()
            self.assertTrue(backend.stream.started)
            samples = np.full((1024, 1), 0.25, dtype=np.float32)
            backend.stream.kwargs["callback"](samples, 1024, None, None)
            state = monitor.snapshot()
            self.assertAlmostEqual(state["rms"], 0.25, places=5)
            self.assertAlmostEqual(state["peak"], 0.25, places=5)
            self.assertGreater(state["percent"], 0)
            self.assertFalse(state["clipping"])
            monitor.stop()
            self.assertFalse(backend.stream.started)
            self.assertTrue(backend.stream.closed)

    def test_microphone_monitor_flags_clipping(self):
        backend = FakeInputBackend()
        with patch("core.audio._sounddevice", return_value=backend):
            monitor = audio.MicrophoneLevelMonitor(48000)
            monitor.start()
            samples = np.full((128, 1), 1.0, dtype=np.float32)
            backend.stream.kwargs["callback"](samples, 128, None, None)
            self.assertTrue(monitor.snapshot()["clipping"])
            monitor.stop()

    def test_microphone_monitor_callback_status_is_visible(self):
        backend = FakeInputBackend()
        with patch("core.audio._sounddevice", return_value=backend):
            monitor = audio.MicrophoneLevelMonitor(48000)
            monitor.start()
            samples = np.zeros((128, 1), dtype=np.float32)
            backend.stream.kwargs["callback"](samples, 128, None, "input overflow")
            self.assertIn("overflow", monitor.snapshot()["status"])
            monitor.stop()


class AudioSettingsMenuTests(unittest.TestCase):
    def test_audio_settings_exposes_both_hardware_tests(self):
        from interface import settings
        captured = {}

        class FakeMenu:
            def __init__(self, title, items):
                captured["title"] = title
                captured["items"] = list(items)

            def run(self):
                return "back"

        with patch.object(settings, "Menu", FakeMenu):
            settings._audio_settings()

        values = [value for value, _label in captured["items"]]
        self.assertIn("speaker-test", values)
        self.assertIn("mic-test", values)
        self.assertIn("ultrasonic-test", values)


if __name__ == "__main__":
    unittest.main()
