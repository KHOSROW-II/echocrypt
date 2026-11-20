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

    def test_tone_activity_gate_covers_frequency_offsets_supported_by_demodulator(self):
        from core.audio import _tone_band_ratio

        rate = 48000
        samples = np.arange(1024, dtype=np.float64)
        shifted_tone = np.sin(2 * np.pi * 1000 * samples / rate).astype(np.float32)
        ratio = _tone_band_ratio(
            shifted_tone, rate,
            (1200, 1500, 1800, 2100, 2400, 2700, 3000),
            bandwidth_hz=240.0,
        )
        self.assertGreater(ratio, 0.90)

    def test_listener_does_not_split_broadcast_burst_on_short_tone_ratio_dropout(self):
        from core.audio import _tone_band_ratio
        from core.broadcast import BROADCAST_HOP_KEY

        rate = 48000
        payload = b"broadcast-continuous-capture-regression"
        waveform = modulate(
            payload, rate, 400, mark=1800, space=2400, gain=0.40,
            max_payload=1024, fec_mode="conv", hop_key=BROADCAST_HOP_KEY,
        )

        class BroadcastStream:
            def __init__(self, *, callback, blocksize, **_kwargs):
                self.callback = callback
                self.blocksize = blocksize

            def start(self):
                samples = np.concatenate((
                    np.zeros(rate // 5, dtype=np.float32),
                    waveform,
                    np.zeros(rate // 4, dtype=np.float32),
                ))
                padded = ((len(samples) + self.blocksize - 1) // self.blocksize) * self.blocksize
                samples = np.pad(samples, (0, padded - len(samples)))
                for offset in range(0, len(samples), self.blocksize):
                    block = samples[offset:offset + self.blocksize]
                    self.callback(block.reshape(-1, 1), self.blocksize, None, None)

            def stop(self):
                return None

            def close(self):
                return None

        class BroadcastBackend:
            def InputStream(self, **kwargs):
                return BroadcastStream(**kwargs)

        ratio_calls = [0]

        def simulate_short_spectral_dropout(samples, sample_rate, frequencies, bandwidth_hz=240.0):
            index = ratio_calls[0]
            ratio_calls[0] += 1
            if 30 <= index < 45:
                return 0.0
            return _tone_band_ratio(samples, sample_rate, frequencies, bandwidth_hz)

        listener = ContinuousAudioListener(
            rate, blocksize=1024,
            activity_frequencies=(1200, 1500, 1800, 2100, 2400, 2700, 3000),
            quiet_seconds=0.18, pre_roll_seconds=0.65, max_frame_seconds=10,
        )
        with patch("core.audio._sounddevice", return_value=BroadcastBackend()):
            with patch("core.audio._tone_band_ratio", side_effect=simulate_short_spectral_dropout):
                listener.start()
                try:
                    frame = listener.get_frame(timeout=3.0)
                    decoded = demodulate(
                        frame, rate, 400, 1800, 2400, 1024,
                        hop_key=BROADCAST_HOP_KEY,
                    )
                    self.assertEqual(decoded, payload)
                    self.assertEqual(listener.snapshot()["captured_frames"], 1)
                    self.assertGreaterEqual(frame.size, len(waveform))
                finally:
                    listener.stop()


    def test_listener_ends_burst_when_mobile_input_noise_rises_after_detection(self):
        rate = 48000
        payload = b"mobile-noise-floor-regression"
        waveform = modulate(
            payload, rate, 400, mark=1800, space=2400, gain=0.40,
            max_payload=1024, fec_mode="conv", hop_key=b"m" * 32,
        )
        # Simulate an Android input-gain ramp: the room's 500 Hz hum becomes
        # much louder after the first modem tone, but it is not modem activity.
        tail_count = rate // 2
        tail_t = np.arange(tail_count, dtype=np.float64) / rate
        ambient = (0.005 * np.sin(2 * np.pi * 500 * tail_t)).astype(np.float32)
        samples = np.concatenate((
            np.zeros(rate // 5, dtype=np.float32), waveform, ambient,
        ))

        class MobileStream:
            def __init__(self, *, callback, blocksize, **_kwargs):
                self.callback = callback
                self.blocksize = blocksize

            def start(self):
                padded = ((len(samples) + self.blocksize - 1) // self.blocksize) * self.blocksize
                source = np.pad(samples, (0, padded - len(samples)))
                for offset in range(0, len(source), self.blocksize):
                    block = source[offset:offset + self.blocksize]
                    self.callback(block.reshape(-1, 1), self.blocksize, None, None)

            def stop(self):
                return None

            def close(self):
                return None

        class MobileBackend:
            def InputStream(self, **kwargs):
                return MobileStream(**kwargs)

        listener = ContinuousAudioListener(
            rate, blocksize=1024,
            activity_frequencies=(1200, 1500, 1800, 2100, 2400, 2700, 3000),
            quiet_seconds=0.12, pre_roll_seconds=0.65, max_frame_seconds=10,
        )
        with patch("core.audio._sounddevice", return_value=MobileBackend()):
            listener.start()
            try:
                frame = listener.get_frame(timeout=3.0)
                decoded = demodulate(
                    frame, rate, 400, 1800, 2400, 1024, hop_key=b"m" * 32,
                )
                self.assertEqual(decoded, payload)
                self.assertEqual(listener.snapshot()["captured_frames"], 1)
                deadline = __import__("time").monotonic() + 1.0
                while listener.busy and __import__("time").monotonic() < deadline:
                    __import__("time").sleep(0.01)
                self.assertFalse(listener.busy, "mobile ambient noise must not hold RX busy")
            finally:
                listener.stop()

    def test_listener_splits_fixed_broadcast_frames_after_guard_gap(self):
        from core.session_engine import BROADCAST_INTER_FRAME_GAP_SECONDS

        rate = 48000
        payloads = (b"broadcast-file-frame-one", b"broadcast-file-frame-two")
        waveforms = [
            modulate(
                payload, rate, 800, mark=1800, space=2400, gain=0.40,
                max_payload=1024, fec_mode="conv", hop_key=None,
            )
            for payload in payloads
        ]

        class TwoFrameStream:
            def __init__(self, *, callback, blocksize, **_kwargs):
                self.callback = callback
                self.blocksize = blocksize

            def start(self):
                samples = np.concatenate((
                    np.zeros(rate // 5, dtype=np.float32),
                    waveforms[0],
                    np.zeros(int(rate * BROADCAST_INTER_FRAME_GAP_SECONDS), dtype=np.float32),
                    waveforms[1],
                    np.zeros(rate // 2, dtype=np.float32),
                ))
                padded = ((len(samples) + self.blocksize - 1) // self.blocksize) * self.blocksize
                samples = np.pad(samples, (0, padded - len(samples)))
                for offset in range(0, len(samples), self.blocksize):
                    block = samples[offset:offset + self.blocksize]
                    self.callback(block.reshape(-1, 1), self.blocksize, None, None)

            def stop(self):
                return None

            def close(self):
                return None

        class TwoFrameBackend:
            def InputStream(self, **kwargs):
                return TwoFrameStream(**kwargs)

        listener = ContinuousAudioListener(
            rate, blocksize=1024,
            activity_frequencies=(1200, 1500, 1800, 2100, 2400, 2700, 3000),
            quiet_seconds=0.30, pre_roll_seconds=0.65, max_frame_seconds=20,
        )
        with patch("core.audio._sounddevice", return_value=TwoFrameBackend()):
            listener.start()
            try:
                first = listener.get_frame(timeout=4.0)
                second = listener.get_frame(timeout=4.0)
                # Clean guard-separated frames must both pass PHY decoding.
                self.assertEqual(
                    demodulate(first, rate, 800, 1800, 2400, 1024),
                    payloads[0],
                )
                self.assertEqual(
                    demodulate(second, rate, 800, 1800, 2400, 1024),
                    payloads[1],
                )
                self.assertEqual(listener.snapshot()["captured_frames"], 2)
            finally:
                listener.stop()

    def test_listener_splits_broadcast_chunks_across_high_rms_non_modem_gap(self):
        from core.audio import _tone_band_ratio
        from core.session_engine import BROADCAST_INTER_FRAME_GAP_SECONDS

        rate = 48000
        activity_frequencies = (1200, 1500, 1800, 2100, 2400, 2700, 3000)
        payloads = (
            bytes((index * 17 + 3) & 0xFF for index in range(350)),
            bytes((index * 29 + 11) & 0xFF for index in range(350)),
        )
        waveforms = [
            modulate(
                payload, rate, 800, mark=1800, space=2400, gain=0.40,
                max_payload=1024, fec_mode="conv", hop_key=None,
            )
            for payload in payloads
        ]

        # Reproduce the real failure: the modem tones disappear in the
        # inter-frame guard interval, but strong out-of-band room noise keeps
        # broadband RMS above the legacy 0.010 hysteresis floor.
        gap_count = int(rate * BROADCAST_INTER_FRAME_GAP_SECONDS)
        gap_time = np.arange(gap_count, dtype=np.float64) / rate
        ambient_gap = (0.032 * np.sin(2 * np.pi * 500 * gap_time)).astype(np.float32)
        self.assertGreater(float(np.sqrt(np.mean(ambient_gap ** 2))), 0.010)
        self.assertLess(
            _tone_band_ratio(ambient_gap, rate, activity_frequencies),
            0.10,
        )

        class NoisyGapStream:
            def __init__(self, *, callback, blocksize, **_kwargs):
                self.callback = callback
                self.blocksize = blocksize

            def start(self):
                samples = np.concatenate((
                    np.zeros(rate // 5, dtype=np.float32),
                    waveforms[0],
                    ambient_gap,
                    waveforms[1],
                    np.zeros(rate // 2, dtype=np.float32),
                ))
                padded = ((len(samples) + self.blocksize - 1) // self.blocksize) * self.blocksize
                samples = np.pad(samples, (0, padded - len(samples)))
                for offset in range(0, len(samples), self.blocksize):
                    block = samples[offset:offset + self.blocksize]
                    self.callback(block.reshape(-1, 1), self.blocksize, None, None)

            def stop(self):
                return None

            def close(self):
                return None

        class NoisyGapBackend:
            def InputStream(self, **kwargs):
                return NoisyGapStream(**kwargs)

        listener = ContinuousAudioListener(
            rate, blocksize=1024,
            activity_frequencies=activity_frequencies,
            quiet_seconds=0.30, pre_roll_seconds=0.65, max_frame_seconds=20,
            # The fake stream feeds samples without real-time pacing; provision
            # enough blocks so this test measures segmentation, not test-harness
            # producer/consumer scheduling.
            queue_blocks=2048,
        )
        with patch("core.audio._sounddevice", return_value=NoisyGapBackend()):
            listener.start()
            try:
                first = listener.get_frame(timeout=4.0)
                second = listener.get_frame(timeout=4.0)
                # Noise may lower PHY confidence; this regression focuses on
                # separating bursts. Bound each capture so a merged recording
                # cannot satisfy the two-frame assertion.
                self.assertLess(len(first), len(waveforms[0]) + int(rate * 0.85))
                self.assertLess(len(second), len(waveforms[1]) + int(rate * 1.20))
                self.assertEqual(listener.snapshot()["captured_frames"], 2)
            finally:
                listener.stop()

    def test_stop_joins_collector_and_restart_never_overlaps_workers(self):
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
            collector = listener._thread
            with patch.object(collector, "join", wraps=collector.join) as join:
                listener.stop()
                join.assert_called_once_with(timeout=3.0)
            self.assertFalse(collector.is_alive())
            self.assertIsNone(listener._thread)

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
