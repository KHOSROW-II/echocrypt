import unittest
from unittest.mock import patch

import numpy as np

from core.modem import (
    HEADER_V2_COPY_BITS, HEADER_V2_BITS,
    SYNC_V2, SYNC_V3,
    demodulate,
    fec_decode,
    fec_encode,
    modulate,
)
from core.recovery.fec_controller import AdaptiveFECController
from core.recovery.interleaver import interleave, deinterleave
from core.recovery.soft_decoder import fec_decode_soft
from core.recovery.profile_generator import profile_for_grade, default_profile
from core.recovery.engine import AdaptiveSignalRecoveryEngine
from core.recovery.errors import HeaderCRCError
from core.recovery.timing_recovery import CLOCK_RATIO_CANDIDATES


RATE = 48000


class ASRECodecTests(unittest.TestCase):
    def test_soft_hamming_decode_uses_confidence_not_only_hard_bits(self):
        data = b"soft"
        bits = fec_encode(data, "hamming")
        llrs = [4.0 if bit else -4.0 for bit in bits]
        # Force one wrong but low-confidence decision in each byte's codeword.
        llrs[2] = -0.08 if bits[2] else 0.08
        self.assertEqual(fec_decode_soft(llrs, "hamming", len(data)), data)

    def test_convolutional_viterbi_roundtrip(self):
        data = bytes((index * 29) & 0xFF for index in range(48))
        encoded = fec_encode(data, "conv")
        self.assertEqual(fec_decode(encoded, "conv", len(data)), data)

    def test_viterbi_soft_decode_handles_low_confidence_burst(self):
        data = bytes((index * 11 + 7) & 0xFF for index in range(80))
        encoded = fec_encode(data, "conv")
        llrs = [3.0 if bit else -3.0 for bit in encoded]
        # A burst of ambiguous symbols after interleaving should be recoverable.
        for index in range(71, 79):
            llrs[index] = 0.03 if encoded[index] else -0.03
        self.assertEqual(fec_decode_soft(llrs, "conv", len(data)), data)

    def test_interleaver_is_an_exact_inverse_for_ragged_blocks(self):
        values = list(range(103))
        self.assertEqual(deinterleave(interleave(values, 8), 8), values)

    def test_demodulator_finds_preamble_after_broadband_room_noise_preroll(self):
        rng = np.random.default_rng(20261120)
        payload = b"noise-preroll-broadcast-receiver-test"
        hop_key = b"B" * 32
        waveform = modulate(payload, RATE, 400, gain=0.28,
                            fec_mode="conv", hop_key=hop_key)
        # Room noise begins before the modem burst. Its energy is above the old
        # 4%-of-peak gate, which previously pinned the sync search to sample 0
        # and searched only the first ~30 ms instead of the retained pre-roll.
        noise_before = rng.normal(0.0, 0.035, int(RATE * 0.45)).astype(np.float32)
        noisy_burst = (waveform + rng.normal(0.0, 0.035, len(waveform))).astype(np.float32)
        noise_after = rng.normal(0.0, 0.035, int(RATE * 0.22)).astype(np.float32)
        recording = np.concatenate((noise_before, noisy_burst, noise_after))
        self.assertEqual(demodulate(recording, RATE, 400, hop_key=hop_key), payload)

    def test_receiver_falls_back_after_a_stronger_false_preamble_candidate(self):
        # Force sync search to return a stronger false ECF2 interpretation at
        # the real ECF3 boundary, followed by the weaker correct ECF3 candidate.
        # The ECF2 protected header is structurally decodable but carries ECF3
        # magic, so the receiver must reject it and continue to candidate two.
        import core.modem as modem

        rate = 800
        hop_key = b"K" * 32
        payload = bytes((index * 17 + 3) & 0xFF for index in range(180))
        waveform = modulate(payload, RATE, rate, gain=0.35,
                            fec_mode="conv", hop_key=hop_key)
        events = []
        candidates = [
            (0.354, 0, "ECF2", SYNC_V2),
            (0.289, 0, "ECF3", SYNC_V3),
        ]
        with patch.object(modem, "_find_preamble_candidates", return_value=candidates):
            recovered = demodulate(
                waveform, RATE, rate, hop_key=hop_key,
                on_debug=lambda stage, message, **fields: events.append((stage, message, fields)),
            )

        self.assertEqual(recovered, payload)
        self.assertTrue(any(stage == "SYNC_CANDIDATE" for stage, *_ in events))
        self.assertTrue(any(
            stage == "SYNC" and "selected acoustic preamble candidate" in message
            for stage, message, _ in events
        ))

    def test_strong_header_crc_failure_is_not_hidden_by_false_candidates(self):
        import core.modem as modem

        payload = b"strong-sync-header-error"
        waveform = modulate(
            payload, RATE, 400, gain=0.4, fec_mode="conv", wire_version=3
        )
        calls = [0]

        def fake_header_decode(_llrs):
            calls[0] += 1
            if calls[0] <= 5:
                raise HeaderCRCError("strong sync header CRC mismatch", sync_score=0.90)
            raise ValueError("false candidate has an incomplete/wrong header")

        candidates = [
            (0.90, 0, "ECF3", SYNC_V3),
            (0.30, RATE // 400, "ECF3", SYNC_V3),
        ]
        with patch.object(modem, "_find_preamble_candidates", return_value=candidates):
            with patch.object(modem, "estimate_frequency_offset", return_value=(0, 1.0)):
                with patch.object(modem, "_estimate_preamble_tone_balance", return_value=None):
                    with patch.object(modem, "_header_v2_decode", side_effect=fake_header_decode):
                        with self.assertRaises(HeaderCRCError) as raised:
                            modem._demodulate_once(
                                waveform, RATE, 400, 1800, 2400, 1024,
                                hop_key=b"K" * 32,
                            )
        self.assertAlmostEqual(raised.exception.sync_score, 0.90)
        self.assertEqual(calls[0], 6)

    def test_random_noise_is_rejected_as_preamble_without_clock_retry_storm(self):
        rng = np.random.default_rng(91)
        recording = rng.normal(0.0, 0.035, int(RATE * 1.2)).astype(np.float32)
        engine = AdaptiveSignalRecoveryEngine(default_profile())
        events = []
        with self.assertRaisesRegex(ValueError, "Preamble confidence too low"):
            demodulate(recording, RATE, 400, recovery_engine=engine,
                       on_debug=lambda stage, message, **fields: events.append((stage, message, fields)))
        self.assertEqual(engine.last_attempt_count, 1)
        self.assertEqual(sum(stage == "TIMING" for stage, *_ in events), 0)

    def test_ecf2_header_survives_loss_of_one_redundant_copy(self):
        payload = b"header-protection" * 5
        waveform = modulate(payload, sample_rate=RATE, symbol_rate=200, wire_version=2)
        damaged = waveform.copy()
        sps = RATE // 200
        second_copy_start = (len(SYNC_V2) + HEADER_V2_COPY_BITS) * sps
        second_copy_end = (len(SYNC_V2) + 2 * HEADER_V2_COPY_BITS) * sps
        # Erase one complete header copy; the second copy remains intact.
        damaged[second_copy_start:second_copy_end] = 0.0
        self.assertEqual(demodulate(damaged, RATE, 200), payload)


    def test_ecf3_interleaving_recovers_contiguous_erasure_burst(self):
        payload = bytes((index * 19 + 7) & 0xFF for index in range(128))
        waveform = modulate(payload, sample_rate=RATE, symbol_rate=200,
                            gain=0.35, fec_mode="conv", wire_version=3)
        damaged = waveform.copy()
        sps = RATE // 200
        # Erase a contiguous 32-symbol burst inside the FEC payload. ECF3's
        # deterministic interleaver spreads these erasures across the codeword.
        burst_start = (len(SYNC_V3) + HEADER_V2_COPY_BITS * 2 + 120) * sps
        damaged[burst_start:burst_start + 32 * sps] = 0.0
        self.assertEqual(demodulate(damaged, RATE, 200), payload)

    def test_ecf2_legacy_payload_is_still_decoded_without_interleaver(self):
        payload = b"legacy-ecf2-payload" * 8
        waveform = modulate(payload, RATE, 200, wire_version=2, fec_mode="conv")
        self.assertEqual(demodulate(waveform, RATE, 200), payload)

    def test_ecf1_legacy_frames_remain_decodable(self):
        payload = b"old-stage-seven-frame"
        waveform = modulate(payload, RATE, 200, wire_version=1, fec_mode="hamming")
        self.assertEqual(demodulate(waveform, RATE, 200), payload)

    def test_carrier_frequency_offset_is_estimated_and_compensated(self):
        payload = bytes((index * 7) & 0xFF for index in range(90))
        # Both transmitter tones are shifted by +25 Hz relative to receiver config.
        waveform = modulate(payload, RATE, 200, mark=1825, space=2425)
        got, metrics = demodulate(waveform, RATE, 200, mark=1800, space=2400,
                                  return_metrics=True)
        self.assertEqual(got, payload)
        self.assertIn(metrics.frequency_offset_hz, (20.0, 30.0))
        self.assertGreaterEqual(metrics.mean_bit_confidence, 0.5)

    def test_large_acoustic_carrier_offsets_are_recovered(self):
        payload = bytes((index * 13 + 9) & 0xFF for index in range(64))
        # Exercise the wider bounded search needed when two audio paths have
        # significant end-to-end frequency error.
        for offset in (-220, 220):
            with self.subTest(offset_hz=offset):
                waveform = modulate(
                    payload, RATE, 400,
                    mark=1800 + offset, space=2400 + offset,
                    gain=0.45, fec_mode="conv",
                )
                self.assertEqual(
                    demodulate(waveform, RATE, 400, mark=1800, space=2400),
                    payload,
                )

    def test_asre_expands_frequency_search_when_the_best_offset_hits_boundary(self):
        payload = bytes((index * 31 + 5) & 0xFF for index in range(72))
        for offset in (-420, 420, -500, 500):
            with self.subTest(offset_hz=offset):
                waveform = modulate(
                    payload, RATE, 800,
                    mark=1800 + offset, space=2400 + offset,
                    gain=0.45, fec_mode="conv",
                )
                engine = AdaptiveSignalRecoveryEngine(profile_for_grade("WEAK"))
                events = []
                recovered = demodulate(
                    waveform, RATE, 800, mark=1800, space=2400,
                    recovery_engine=engine,
                    on_debug=lambda stage, message, **fields:
                        events.append((stage, message, fields)),
                )
                self.assertEqual(recovered, payload)
                self.assertTrue(any(
                    stage == "CHANNEL"
                    and "search expanded after reaching its boundary" in message
                    for stage, message, _fields in events
                ))

    def test_header_decode_recovers_from_asymmetric_tone_channel_gain(self):
        payload = bytes((index * 23 + 11) & 0xFF for index in range(96))
        waveform = modulate(
            payload, RATE, 400, gain=0.45, fec_mode="conv",
            wire_version=3,
        )
        # Model a speaker/microphone path that attenuates the MARK band
        # substantially more than the SPACE band.
        spectrum = np.fft.rfft(waveform)
        frequencies = np.fft.rfftfreq(len(waveform), d=1.0 / RATE)
        mark_band = (frequencies >= 1500) & (frequencies <= 2100)
        spectrum[mark_band] *= 0.25
        imbalanced = np.fft.irfft(spectrum, n=len(waveform)).astype(np.float32)
        self.assertEqual(demodulate(imbalanced, RATE, 400), payload)

    def test_strong_sync_header_failure_tries_bounded_clock_recovery(self):
        from types import SimpleNamespace

        engine = AdaptiveSignalRecoveryEngine(default_profile())
        attempted = []

        def recover_after_clock_correction(_samples, *, sample_clock_ratio=1.0, **_kwargs):
            attempted.append(sample_clock_ratio)
            if abs(sample_clock_ratio - 1.0002) > 1e-9:
                raise HeaderCRCError(
                    "synthetic protected-header CRC failure",
                    sync_score=0.72,
                )
            return b"recovered", SimpleNamespace(channel_grade="FAIR")

        payload, _metrics = engine.recover(
            np.zeros(48000, dtype=np.float32),
            recover_after_clock_correction,
        )
        self.assertEqual(payload, b"recovered")
        self.assertEqual(engine.last_attempt_count, 6)
        self.assertIn(1.0002, attempted)

    def test_persistent_strong_header_failure_is_bounded_to_eleven_clock_candidates(self):
        engine = AdaptiveSignalRecoveryEngine(default_profile())

        def reject_header(_samples, **_kwargs):
            raise HeaderCRCError("synthetic protected-header CRC failure", sync_score=0.72)

        with self.assertRaises(HeaderCRCError):
            engine.recover(np.zeros(48000, dtype=np.float32), reject_header)
        self.assertEqual(engine.last_attempt_count, 11)

    def test_hopping_key_required_does_not_trigger_clock_recovery_retries(self):
        from core.recovery.errors import HoppingKeyRequiredError

        engine = AdaptiveSignalRecoveryEngine(default_profile())
        waveform = modulate(
            b"broadcast-hop-key-required",
            RATE,
            400,
            fec_mode="conv",
            hop_key=b"H" * 32,
        )
        with self.assertRaises(HoppingKeyRequiredError):
            demodulate(
                waveform,
                RATE,
                400,
                hop_key=None,
                recovery_engine=engine,
            )
        self.assertEqual(engine.last_attempt_count, 1)

    def test_strong_hopping_header_error_propagates_before_false_candidates(self):
        import core.modem as modem
        from core.recovery.errors import HoppingKeyRequiredError

        waveform = modulate(
            b"valid-broadcast-header",
            RATE,
            400,
            fec_mode="conv",
            hop_key=b"H" * 32,
        )
        with self.assertRaises(HoppingKeyRequiredError):
            modem._demodulate_once(
                waveform, RATE, 400, 1800, 2400, 1024,
                hop_key=None,
            )

    def test_strong_frame_crc_failure_recovers_a_two_thousand_ppm_clock_offset(self):
        from types import SimpleNamespace
        from core.recovery.errors import FrameCRCError

        engine = AdaptiveSignalRecoveryEngine(default_profile())
        attempted = []

        def recover_after_clock_correction(_samples, *, sample_clock_ratio=1.0, **_kwargs):
            attempted.append(sample_clock_ratio)
            if abs(sample_clock_ratio - 1.002) > 1e-9:
                raise FrameCRCError("synthetic frame CRC failure", sync_score=0.91)
            return b"recovered-after-drift", SimpleNamespace(channel_grade="FAIR")

        payload, _metrics = engine.recover(
            np.zeros(48000, dtype=np.float32),
            recover_after_clock_correction,
        )
        self.assertEqual(payload, b"recovered-after-drift")
        self.assertEqual(engine.last_attempt_count, 8)
        self.assertIn(1.002, attempted)

    def test_asre_reports_channel_metrics(self):
        payload = b"channel telemetry"
        waveform = modulate(payload)
        got, metrics = demodulate(waveform, return_metrics=True)
        self.assertEqual(got, payload)
        self.assertIn(metrics.channel_grade, {"GOOD", "FAIR", "WEAK"})
        self.assertGreater(metrics.signal_peak, 0)
        self.assertEqual(metrics.sample_clock_ratio, 1.0)


    def test_final_fec_symbol_is_not_faded_inside_the_frame(self):
        payload = bytes((index * 13 + 5) & 0xFF for index in range(118))
        waveform = modulate(payload, sample_rate=RATE, symbol_rate=200, gain=0.55)
        sps = RATE // 200
        guard = min(96, max(8, sps // 2))
        self.assertEqual(len(waveform) % sps, guard)
        final_data_symbol = waveform[-guard - sps:-guard]
        trailing_guard = waveform[-guard:]
        self.assertGreater(float(np.sqrt(np.mean(final_data_symbol ** 2))), 0.30)
        self.assertAlmostEqual(float(trailing_guard[-1]), 0.0, places=6)

    def test_soft_viterbi_modem_frame_roundtrip(self):
        rng = np.random.default_rng(20261119)
        payload = bytes(rng.integers(0, 256, 118, dtype=np.uint8))
        waveform = modulate(payload, RATE, 200, fec_mode="conv")
        self.assertEqual(demodulate(waveform, RATE, 200), payload)

    def test_convolutional_handshake_frame_survives_moderate_synthetic_noise(self):
        rng = np.random.default_rng(7718)
        payload = bytes(rng.integers(0, 256, 118, dtype=np.uint8))
        waveform = modulate(payload, RATE, 200, gain=0.55, fec_mode="conv") * 0.27
        recording = np.clip(
            waveform + rng.normal(0.0, 0.051, len(waveform)).astype(np.float32),
            -1.0, 1.0,
        )
        engine = AdaptiveSignalRecoveryEngine(default_profile())
        self.assertEqual(demodulate(recording, RATE, 200,
                                    recovery_engine=engine), payload)

    def test_fine_clock_recovery_handles_small_independent_clock_offsets(self):
        rng = np.random.default_rng(9137)
        payload = bytes(rng.integers(0, 256, 118, dtype=np.uint8))
        waveform = modulate(payload, RATE, 200, gain=0.4)
        self.assertIn(1.00005, CLOCK_RATIO_CANDIDATES)
        self.assertIn(0.99995, CLOCK_RATIO_CANDIDATES)
        for ppm in (-100, 100):
            ratio = 1.0 + ppm / 1_000_000.0
            count = int(round(len(waveform) * ratio))
            captured = np.interp(
                np.linspace(0, len(waveform) - 1, count),
                np.arange(len(waveform)), waveform,
            ).astype(np.float32)
            engine = AdaptiveSignalRecoveryEngine(default_profile())
            self.assertEqual(
                demodulate(captured, RATE, 200, recovery_engine=engine), payload,
                msg=f"failed to recover a {ppm:+d} ppm sample-clock offset",
            )

    def test_fec_retry_controller_has_bounded_progression(self):
        controller = AdaptiveFECController()
        self.assertEqual(controller.plan("hamming", 4),
                         ("hamming", "conv", "repetition3", "repetition3"))
        self.assertEqual(controller.plan("conv", 3),
                         ("conv", "repetition3", "conv"))
        self.assertEqual(controller.plan("raw", 4),
                         ("raw", "hamming", "conv", "repetition3"))

    def test_severe_clipping_forces_weak_channel_grade(self):
        from core.recovery.recovery_metrics import RecoveryMetrics
        metrics = RecoveryMetrics(
            sample_rate=48000, symbol_rate=200, signal_rms=0.75,
            signal_peak=1.0, clipping_fraction=0.30768, sync_score=0.9999,
            mean_bit_confidence=1.0, tone_contrast_db=46.4,
            frequency_offset_hz=0.0, timing_offset_samples=318,
        )
        self.assertEqual(metrics.channel_grade, "WEAK")

    def test_channel_grade_generates_bounded_profiles(self):
        self.assertEqual(profile_for_grade("GOOD").name, "ASRE-LOW-LATENCY")
        self.assertEqual(profile_for_grade("FAIR").name, "ASRE-BALANCED")
        self.assertEqual(profile_for_grade("WEAK").name, "ASRE-RESILIENT")
        with self.assertRaises(ValueError):
            profile_for_grade("UNKNOWN")


if __name__ == "__main__":
    unittest.main()
