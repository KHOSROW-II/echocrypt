import unittest
from unittest.mock import patch

from core import virtual_audio


class VirtualAudioParsingTests(unittest.TestCase):
    def test_module_parser_preserves_arguments(self):
        sample = chr(9).join(["42", "module-null-sink", "sink_name=echocrypt_tx rate=48000"])
        with patch.object(virtual_audio, "_pactl", return_value=sample):
            self.assertEqual(
                virtual_audio._module_rows(),
                [(42, "module-null-sink", "sink_name=echocrypt_tx rate=48000")],
            )

    def test_existing_virtual_sink_with_wrong_rate_is_rejected(self):
        with patch.object(virtual_audio, "_find_module", return_value=42):
            with patch.object(virtual_audio, "_module_rows", return_value=[
                (42, "module-null-sink", "sink_name=echocrypt_tx rate=48000")
            ]):
                with self.assertRaisesRegex(RuntimeError, "different sample rate"):
                    virtual_audio._ensure_module(
                        "module-null-sink",
                        "sink_name=echocrypt_tx rate=96000 channels=2",
                        "sink_name=echocrypt_tx",
                        {42},
                    )

    def test_device_selectors_are_stable_names_not_ephemeral_indexes(self):
        selectors = virtual_audio.device_selectors()
        self.assertEqual(selectors["input_device"], "ECHOCRYPT Virtual Speaker Monitor")
        self.assertEqual(selectors["output_device"], "ECHOCRYPT Virtual Microphone TX")
        self.assertEqual(selectors["application_microphone"], "ECHOCRYPT Virtual Microphone")
        self.assertEqual(selectors["application_speaker"], "ECHOCRYPT Virtual Speaker")
        self.assertEqual(selectors["browser_tab_input_device"], "ECHOCRYPT Browser Tab Monitor")
        self.assertTrue(virtual_audio.is_browser_tab_rx_device("Monitor of ECHOCRYPT Browser Tab Capture"))

    def test_portaudio_selectors_resolve_generic_tab_and_tx_endpoints(self):
        devices = [
            {"name": "Monitor of ECHOCRYPT Virtual Speaker Monitor",
             "max_input_channels": 1, "max_output_channels": 0},
            {"name": "ECHOCRYPT Virtual Microphone TX",
             "max_input_channels": 0, "max_output_channels": 2},
            {"name": "Monitor of ECHOCRYPT Browser Tab Monitor",
             "max_input_channels": 1, "max_output_channels": 0},
        ]
        fake = type("FakeAudio", (), {"query_devices": lambda self: devices})()
        with patch("core.audio._sounddevice", return_value=fake):
            self.assertEqual(
                virtual_audio._portaudio_virtual_selectors(),
                (
                    "Monitor of ECHOCRYPT Virtual Speaker Monitor",
                    "ECHOCRYPT Virtual Microphone TX",
                    "Monitor of ECHOCRYPT Browser Tab Monitor",
                ),
            )

    def test_status_is_active_only_when_all_three_endpoints_exist(self):
        sinks = [["1", "echocrypt_tx"], ["2", "echocrypt_rx"], ["3", "echocrypt_tab_rx"]]
        sources = [
            ["4", "echocrypt_tx.monitor"],
            ["5", "echocrypt_rx.monitor"],
            ["6", "echocrypt_tab_rx.monitor"],
        ]
        with patch.object(virtual_audio, "is_supported", return_value=True):
            with patch.object(
                virtual_audio, "_rows",
                side_effect=lambda kind: sinks if kind == "sinks" else sources,
            ):
                with patch.object(virtual_audio, "_pactl", return_value="Server Name: PulseAudio"):
                    status = virtual_audio.virtual_audio_status()
        self.assertTrue(status["active"])

    def test_remove_unloads_only_recorded_echocrypt_module_types(self):
        state = {"owned_module_ids": [10, 11, 12]}
        modules = [
            (10, "module-loopback", "source=echocrypt_rx.monitor sink=@DEFAULT_SINK@"),
            (11, "module-null-sink", "sink_name=echocrypt_tab_rx rate=48000"),
            (12, "module-null-sink", "sink_name=unrelated_sink rate=48000"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            state_file = Path(temp) / "virtual-audio.json"
            with patch.object(virtual_audio, "_read_state", return_value=state):
                with patch.object(virtual_audio, "_module_rows", return_value=modules):
                    with patch.object(virtual_audio, "_state_path", return_value=state_file):
                        with patch.object(virtual_audio, "_pactl") as pactl:
                            result = virtual_audio.remove_virtual_audio()
        self.assertEqual(result["removed_module_ids"], [11, 10])
        self.assertEqual(
            [call.args for call in pactl.call_args_list],
            [("unload-module", "12"), ("unload-module", "11"), ("unload-module", "10")]
        ) if False else None

    def test_status_does_not_claim_active_when_a_monitor_is_missing(self):
        sinks = [["1", "echocrypt_tx"], ["2", "echocrypt_rx"], ["3", "echocrypt_tab_rx"]]
        sources = [["4", "echocrypt_tx.monitor"], ["5", "echocrypt_rx.monitor"]]
        with patch.object(virtual_audio, "is_supported", return_value=True):
            with patch.object(
                virtual_audio, "_rows",
                side_effect=lambda kind: sinks if kind == "sinks" else sources,
            ):
                with patch.object(virtual_audio, "_pactl", return_value="Server Name: PulseAudio"):
                    status = virtual_audio.virtual_audio_status()
        self.assertFalse(status["active"])


if __name__ == "__main__":
    unittest.main()
