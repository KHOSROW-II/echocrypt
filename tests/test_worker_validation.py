import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import worker_validation


class FakeAudioBackend:
    def query_devices(self, device=None, kind=None):
        direction = kind or ("input" if device == 0 else "output")
        return {
            "name": str(device) if device is not None else f"Default {direction}",
            "hostapi": 0,
            "max_input_channels": 1 if direction == "input" else 0,
            "max_output_channels": 1 if direction == "output" else 0,
            "default_samplerate": 48000.0,
        }

    def query_hostapis(self, index=None):
        value = {"name": "FakeAudio", "devices": [0, 1]}
        return value if index is not None else [value]


def make_config():
    return SimpleNamespace(
        input_device=None, output_device=None, sample_rate=48000,
        mark_hz=1800, space_hz=2400, ultrasonic_enabled=False,
        ultrasonic_mark_hz=20700, ultrasonic_space_hz=22200,
        full_duplex_audio=False, multi_worker_enabled=True, multi_worker_count=4,
    )

def passing_result(cfg):
    from core.modem import HOP_OFFSETS
    frequencies = sorted({
        int(base + offset)
        for base in (cfg.mark_hz, cfg.space_hz)
        for offset in HOP_OFFSETS
    })
    return {
        "passed": True,
        "tones": [{"frequency": value, "detected": True} for value in frequencies],
    }


class WorkerHardwareApprovalTests(unittest.TestCase):
    def test_cpu_and_ram_resource_gate_caps_worker_count_separately(self):
        mib = 1024 * 1024
        gib = 1024 * mib
        self.assertEqual(worker_validation.compute_worker_hardware_limit(1, 8 * gib), 1)
        self.assertEqual(worker_validation.compute_worker_hardware_limit(2, 512 * mib), 1)
        self.assertEqual(worker_validation.compute_worker_hardware_limit(2, 1 * gib), 2)
        self.assertEqual(worker_validation.compute_worker_hardware_limit(16, 1 * gib), 8)
        self.assertEqual(worker_validation.compute_worker_hardware_limit(16, None), 8)

    def test_audio_approval_is_still_required_after_cpu_ram_capacity_passes(self):
        cfg = make_config()
        with patch.object(worker_validation, "worker_hardware_profile", return_value={
            "logical_cpus": 8, "memory_bytes": 8 * 1024**3,
            "memory_display": "8.0 GiB", "max_workers": 8,
        }):
            self.assertEqual(worker_validation.effective_worker_count(cfg), 1)

    def test_missing_approval_disables_parallel_workers(self):
        cfg = make_config()
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(worker_validation, "_approval_path", return_value=Path(temp) / "approval.json"), \
             patch("core.audio._sounddevice", return_value=FakeAudioBackend()):
            approved, reason = worker_validation.worker_hardware_approval_status(cfg)
            self.assertFalse(approved)
            self.assertIn("has not been run", reason)
            self.assertEqual(worker_validation.effective_worker_count(cfg), 1)

    def test_approval_is_bound_to_current_devices_and_modem_settings(self):
        cfg = make_config()
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(worker_validation, "_approval_path", return_value=Path(temp) / "approval.json"), \
             patch("core.audio._sounddevice", return_value=FakeAudioBackend()):
            worker_validation.approve_worker_hardware(
                cfg, passing_result(cfg)
            )
            approved, _ = worker_validation.worker_hardware_approval_status(cfg)
            self.assertTrue(approved)
            self.assertEqual(worker_validation.effective_worker_count(cfg), 4)

            changed = make_config()
            changed.input_device = "Different microphone"
            approved, reason = worker_validation.worker_hardware_approval_status(changed)
            self.assertFalse(approved)
            self.assertIn("changed", reason)
            self.assertEqual(worker_validation.effective_worker_count(changed), 1)

    def test_incomplete_tone_results_cannot_approve_workers(self):
        cfg = make_config()
        with self.assertRaisesRegex(ValueError, "every active MARK/SPACE"):
            worker_validation.approve_worker_hardware(
                cfg, {"passed": True, "tones": [{"frequency": 1800, "detected": True}]}
            )

    def test_failed_test_cannot_be_saved_as_approval(self):
        with self.assertRaisesRegex(ValueError, "cannot approve"):
            worker_validation.approve_worker_hardware(make_config(), {"passed": False, "tones": []})

    def test_approval_path_is_user_local_not_inside_repository(self):
        path = worker_validation._approval_path()
        self.assertNotIn("echocrypt/config", str(path))


if __name__ == "__main__":
    unittest.main()
