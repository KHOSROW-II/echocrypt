import threading
import unittest
from unittest.mock import patch

from core.transport import _play_with_cancel


class CancellablePlaybackTests(unittest.TestCase):
    def test_cancel_stops_active_portaudio_playback(self):
        entered = threading.Event()
        release = threading.Event()
        cancelled = threading.Event()
        errors = []

        def fake_play(_wave, _rate, _device):
            entered.set()
            release.wait(2.0)

        def fake_stop():
            release.set()

        def run():
            try:
                _play_with_cancel([0.0, 0.1], 48000, None, cancelled)
            except Exception as exc:
                errors.append(exc)

        with patch("core.audio.play", side_effect=fake_play), \
             patch("core.audio.stop_playback", side_effect=fake_stop):
            worker = threading.Thread(target=run)
            worker.start()
            self.assertTrue(entered.wait(1.0))
            cancelled.set()
            worker.join(1.5)

        self.assertFalse(worker.is_alive())
        self.assertTrue(release.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], InterruptedError)


if __name__ == "__main__":
    unittest.main()
