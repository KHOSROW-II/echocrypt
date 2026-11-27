import unittest
from unittest.mock import patch

from core import virtual_audio


class VirtualAudioParsingTests(unittest.TestCase):
    def test_module_parser_preserves_arguments(self):
        sample = "42\\tmodule-null-sink\\tsink_name=echocrypt_tx rate=48000"
        with patch.object(virtual_audio, "_pactl", return_value=sample):
            self.assertEqual(
                virtual_audio._module_rows(),
                [(42, "module-null-sink", "sink_name=echocrypt_tx rate=48000")],
            )

    def test_device_selectors_are_stable_names_not_ephemeral_indexes(self):
        selectors = virtual_audio.device_selectors()
        self.assertEqual(selectors["input_device"], "ECHOCRYPT Virtual Speaker Monitor")
        self.assertEqual(selectors["output_device"], "ECHOCRYPT Virtual Microphone TX")
        self.assertEqual(selectors["application_microphone"], "ECHOCRYPT Virtual Microphone")
        self.assertEqual(selectors["application_speaker"], "ECHOCRYPT Virtual Speaker")

    def test_status_does_not_claim_active_when_a_monitor_is_missing(self):
        sinks = [["1", "echocrypt_tx"], ["2", "echocrypt_rx"]]
        sources = [["3", "echocrypt_tx.monitor"]]
        with patch.object(virtual_audio, "is_supported", return_value=True), \
             patch.object(virtual_audio, "_rows", side_effect=lambda kind: sinks if kind == "sinks" else sources), \
             patch.object(virtual_audio, "_pactl", return_value="Server Name: PulseAudio"):
            status = virtual_audio.virtual_audio_status()
        self.assertFalse(status["active"])


if __name__ == "__main__":
    unittest.main()
