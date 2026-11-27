import base64
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from browser_extension import native_host


class NativeHostProtocolTests(unittest.TestCase):
    def test_native_message_round_trip_uses_little_endian_length_prefix(self):
        stream = io.BytesIO()
        payload = {"type": "ready", "sample_rate": 48000}
        native_host.write_message(stream, payload)
        raw = stream.getvalue()
        length = struct.unpack("=I", raw[:4])[0]
        self.assertEqual(length, len(raw) - 4)
        self.assertEqual(native_host.read_message(io.BytesIO(raw)), payload)

    def test_native_message_rejects_oversized_frame(self):
        raw = struct.pack("=I", native_host.MAX_MESSAGE_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "length"):
            native_host.read_message(io.BytesIO(raw))

    def test_pcm_decoder_accepts_bounded_48khz_s16le(self):
        payload = bytes([1, 0]) * 960
        message = {
            "type": "pcm",
            "sample_rate": 48000,
            "pcm_s16le": base64.b64encode(payload).decode("ascii"),
        }
        self.assertEqual(native_host._decode_pcm(message), payload)

    def test_pcm_decoder_rejects_invalid_rate_and_unbounded_payload(self):
        payload = bytes([0, 0]) * 960
        message = {
            "type": "pcm",
            "sample_rate": 44100,
            "pcm_s16le": base64.b64encode(payload).decode("ascii"),
        }
        with self.assertRaisesRegex(ValueError, "48000"):
            native_host._decode_pcm(message)

        message["sample_rate"] = 48000
        message["pcm_s16le"] = base64.b64encode(b"x" * (native_host.MAX_PCM_BYTES + 2)).decode("ascii")
        with self.assertRaisesRegex(ValueError, "frame length"):
            native_host._decode_pcm(message)

    def test_native_wrapper_forwards_the_extension_origin_argument(self):
        wrapper = Path(__file__).resolve().parents[1] / "scripts" / "echocrypt_tab_host"
        contents = wrapper.read_text(encoding="utf-8")
        self.assertIn('native_host.py" "$@"', contents)

    def test_native_sink_probe_uses_pactl_sink_names(self):
        result = type("Result", (), {
            "returncode": 0,
            "stdout": "1" + chr(9) + "echocrypt_tab_rx" + chr(9) + "module-null-sink" + chr(9) + "2" + chr(9) + "RUNNING" + chr(10),
            "stderr": "",
        })()
        with patch("browser_extension.native_host.shutil.which", return_value="/usr/bin/pactl"):
            with patch("browser_extension.native_host.subprocess.run", return_value=result):
                self.assertTrue(native_host._sink_exists())

    def test_native_host_rejects_unregistered_extension_origin(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            native_host, "_config_path", return_value=Path(temp) / "host.json"
        ):
            Path(temp, "host.json").write_text(
                json.dumps({"extension_id": "a" * 32}), encoding="utf-8"
            )
            self.assertTrue(native_host.origin_is_allowed("chrome-extension://" + "a" * 32 + "/"))
            self.assertFalse(native_host.origin_is_allowed("chrome-extension://" + "b" * 32 + "/"))


if __name__ == "__main__":
    unittest.main()
