import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core import termux_api


class TermuxMicrophonePermissionTests(unittest.TestCase):
    def test_missing_record_audio_permission_returns_permission_required(self):
        result = Mock(returncode=0, stdout=json.dumps({
            "error": "Please grant the following permission to use this command: android.permission.RECORD_AUDIO"
        }), stderr="")
        with patch.object(termux_api.shutil, "which", return_value="/termux/bin/termux-microphone-record"), \
             patch.object(termux_api, "_run", return_value=result):
            status, detail = termux_api.request_microphone_permission()
        self.assertEqual(status, "permission_required")
        self.assertIn("Choose Allow", detail)

    def test_successful_bounded_recording_is_verified_then_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = []

            def fake_run(command, *, timeout=8.0):
                command = list(command)
                commands.append(command)
                if "-i" in command:
                    return Mock(returncode=0, stdout='{"isRecording": false}', stderr="")
                output_path = Path(command[command.index("-f") + 1])
                output_path.write_bytes(b"fake-m4a-data")
                return Mock(
                    returncode=0,
                    stdout=f"Recording started: {output_path}",
                    stderr="",
                )

            with patch.object(termux_api.shutil, "which", return_value="/termux/bin/termux-microphone-record"), \
                 patch.object(termux_api.tempfile, "gettempdir", return_value=directory), \
                 patch.object(termux_api, "_run", side_effect=fake_run):
                status, detail = termux_api.request_microphone_permission()

            self.assertEqual(status, "granted")
            self.assertIn("removed", detail)
            self.assertEqual(len(commands), 2)
            start = commands[0]
            self.assertEqual(start[start.index("-l") + 1], "1")
            self.assertEqual(start[start.index("-e") + 1], "aac")
            output_path = Path(start[start.index("-f") + 1])
            self.assertFalse(output_path.exists())

    def test_missing_cli_is_reported(self):
        with patch.object(termux_api.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "pkg install termux-api"):
                termux_api.request_microphone_permission()


if __name__ == "__main__":
    unittest.main()
