import unittest

from core.codec import VOICE_HEADER, VOICE_MAGIC, decode_voice_message


class VoiceCodecBoundaryTests(unittest.TestCase):
    def test_ecv1_decoder_rejects_sample_count_above_five_minute_limit(self):
        sample_rate = 4000
        oversized_count = sample_rate * 300 + 1
        header = VOICE_HEADER.pack(
            VOICE_MAGIC, sample_rate, oversized_count, 0, 0
        )
        # Omit the body deliberately: the format limit must be checked before
        # any large decoded PCM buffer is allocated.
        with self.assertRaisesRegex(ValueError, "five-minute"):
            decode_voice_message(header)


if __name__ == "__main__":
    unittest.main()
