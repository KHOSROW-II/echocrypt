import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import echocrypt


def completed(command, stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)


class MobileBootstrapTests(unittest.TestCase):
    def test_termux_detection(self):
        with patch.dict(os.environ, {"PREFIX": "/data/data/com.termux/files/usr", "TERMUX_VERSION": "0.118"}, clear=True), \
             patch.object(echocrypt.sys, "executable", "/data/data/com.termux/files/usr/bin/python"), \
             patch.object(echocrypt.sys, "prefix", "/data/data/com.termux/files/usr"):
            self.assertTrue(echocrypt._is_termux())

    def test_proot_python_does_not_masquerade_as_native_termux_from_inherited_env(self):
        with patch.dict(os.environ, {"PREFIX": "/data/data/com.termux/files/usr", "TERMUX_VERSION": "0.118", "ANDROID_ROOT": "/system"}, clear=True), \
             patch.object(echocrypt.sys, "executable", "/usr/bin/python3"), \
             patch.object(echocrypt.sys, "prefix", "/usr"):
            self.assertFalse(echocrypt._is_termux())
            self.assertTrue(echocrypt._is_android_runtime())

    def test_missing_import_probe_parses_only_explicit_missing_markers(self):
        fake = completed(["python"], stdout='ECHOCRYPT_IMPORT_FAILURES={"numpy": "ImportError: broken ABI"}\n')
        with patch.object(echocrypt.subprocess, "run", return_value=fake) as run:
            missing = echocrypt._missing_imports(Path("/fake/python"), ("numpy", "cryptography"))
        self.assertEqual(missing, ["numpy"])
        self.assertIn("ECHOCRYPT_IMPORT_FAILURES=", run.call_args.args[0][2])

    def test_missing_import_probe_runs_in_target_python_and_detects_missing_module(self):
        import sys
        missing_name = "__echocrypt_intentionally_missing_test_module__"
        self.assertEqual(
            echocrypt._missing_imports(Path(sys.executable), ("json", missing_name)),
            [missing_name],
        )

    def test_broken_venv_is_rebuilt_and_python_pip_are_verified(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            venv_dir = Path(temp) / ".venv"
            broken = venv_dir / "bin" / "python"
            broken.parent.mkdir(parents=True)
            broken.write_text("not an executable", encoding="utf-8")
            with patch.object(echocrypt, "VENV_DIR", venv_dir), patch("builtins.print"):
                python_path = echocrypt._ensure_project_venv()
                self.assertEqual(python_path, venv_dir / "bin" / "python")
                self.assertTrue(python_path.is_file())
                probe = subprocess.run([str(python_path), "-c", "import pip"], capture_output=True)
                self.assertEqual(probe.returncode, 0, probe.stderr.decode(errors="replace"))

    def test_android_detection_without_termux(self):
        with patch.dict(os.environ, {"ANDROID_ROOT": "/system"}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                self.assertTrue(echocrypt._is_android_runtime())
                self.assertFalse(echocrypt._is_termux())

    def test_termux_forces_rootless_even_with_saved_desktop_mode(self):
        with patch.object(echocrypt, "_is_termux", return_value=True), \
             patch.object(echocrypt, "_saved_mode", return_value="full"):
            self.assertEqual(echocrypt._choose_mode(), "rootless")

    def test_termux_rejects_desktop_bootstrap_profile(self):
        with patch.object(echocrypt, "_is_termux", return_value=True):
            with self.assertRaises(RuntimeError):
                echocrypt._choose_mode("full")

    def test_android_non_termux_auto_selects_rootless(self):
        with patch.object(echocrypt, "_is_termux", return_value=False), \
             patch.object(echocrypt, "_is_android_runtime", return_value=True), \
             patch.object(echocrypt, "_saved_mode", return_value="standby"):
            self.assertEqual(echocrypt._choose_mode(), "rootless")

    def test_mobile_termux_bootstrap_installs_python_pip_and_native_dependencies(self):
        calls = []
        with patch.object(echocrypt.shutil, "which", side_effect=lambda name: "/termux/bin/" + name), \
             patch.object(echocrypt, "_run", side_effect=lambda cmd, **kw: calls.append(cmd)), \
             patch.object(echocrypt, "_repair_termux_numpy_runtime"), \
             patch.object(echocrypt, "_verify_termux_audio_runtime", return_value=(1, 1)), \
             patch("builtins.print"):
            echocrypt._bootstrap_termux_rootless()

        self.assertEqual(len(calls), 2)
        package_command = " ".join(calls[0])
        self.assertIn("python-pip", package_command)
        self.assertIn("python-numpy", package_command)
        self.assertIn("python-cryptography", package_command)
        self.assertIn("portaudio", package_command)
        self.assertIn("termux-api", package_command)
        self.assertNotIn("pulseaudio", package_command)
        self.assertNotIn("python-ensurepip-wheels", package_command)
        pip_command = " ".join(calls[1])
        self.assertIn("-m pip install", pip_command)
        self.assertIn("sounddevice", pip_command)
        self.assertNotIn("--upgrade", pip_command)
        flattened = " ".join(" ".join(map(str, call)) for call in calls)
        self.assertNotIn("sudo", flattened)
        self.assertNotIn("apt-get", flattened)
        self.assertNotIn("-m venv", flattened)

    def test_termux_numpy_repair_refreshes_upgrade_and_reinstalls_native_abi_packages(self):
        calls = []
        with patch.object(echocrypt, "_probe_import_failures",
                          side_effect=[({"numpy": "ImportError: undefined C++ symbol"}, ""),
                                       ({}, "")]), \
             patch.object(echocrypt.shutil, "which",
                          side_effect=lambda name: {"pkg": "/termux/bin/pkg", "apt-get": "/termux/bin/apt-get"}.get(name)), \
             patch.object(echocrypt, "_run", side_effect=lambda cmd, **kw: calls.append((cmd, kw))), \
             patch("builtins.print"):
            echocrypt._repair_termux_numpy_runtime()

        commands = [call[0] for call in calls]
        self.assertEqual(commands[0], ["/termux/bin/pkg", "update"])
        self.assertEqual(commands[1], ["/termux/bin/pkg", "upgrade", "-y"])
        self.assertEqual(
            commands[2],
            ["/termux/bin/apt-get", "install", "--reinstall", "-y",
             "libc++", "python", "python-numpy"],
        )
        self.assertTrue(all(call[1].get("check") is False for call in calls))

    def test_termux_numpy_repair_reports_original_and_final_import_failure(self):
        failure = "ImportError: dlopen failed: cannot locate symbol hash_memory"
        with patch.object(echocrypt, "_probe_import_failures",
                          side_effect=[({"numpy": failure}, ""),
                                       ({"numpy": failure}, ""),
                                       ({"numpy": failure}, "")]), \
             patch.object(echocrypt.shutil, "which",
                          side_effect=lambda name: {"pkg": "/termux/bin/pkg", "apt-get": "/termux/bin/apt-get"}.get(name)), \
             patch.object(echocrypt, "_run"), \
             patch("builtins.print"):
            with self.assertRaisesRegex(RuntimeError, "cannot locate symbol hash_memory"):
                echocrypt._repair_termux_numpy_runtime()

    def test_termux_launch_checks_audio_without_reinstall_when_api_cli_exists(self):
        with patch.object(echocrypt.sys, "argv", ["echocrypt.py"]), \
             patch.object(echocrypt, "_choose_mode", return_value="rootless"), \
             patch.object(echocrypt, "_is_termux", return_value=True), \
             patch.object(echocrypt, "_missing_imports", return_value=[]), \
             patch.object(echocrypt, "_saved_mode", return_value="rootless"), \
             patch.object(echocrypt.shutil, "which", return_value="/termux/bin/termux-microphone-record"), \
             patch.object(echocrypt, "_verify_termux_audio_runtime") as verify, \
             patch.object(echocrypt, "_bootstrap") as bootstrap, \
             patch("interface.menu.main") as app_main:
            echocrypt.main()
        verify.assert_called_once_with()
        bootstrap.assert_not_called()
        app_main.assert_called_once_with()

    def test_termux_launch_bootstraps_when_termux_api_cli_is_missing(self):
        with patch.object(echocrypt.sys, "argv", ["echocrypt.py"]), \
             patch.object(echocrypt, "_choose_mode", return_value="rootless"), \
             patch.object(echocrypt, "_is_termux", return_value=True), \
             patch.object(echocrypt, "_missing_imports", return_value=[]), \
             patch.object(echocrypt, "_saved_mode", return_value="rootless"), \
             patch.object(echocrypt.shutil, "which", side_effect=lambda name: None if name == "termux-microphone-record" else "/termux/bin/" + name), \
             patch.object(echocrypt, "_verify_termux_audio_runtime") as verify, \
             patch.object(echocrypt, "_bootstrap") as bootstrap, \
             patch("interface.menu.main") as app_main:
            echocrypt.main()
        verify.assert_not_called()
        bootstrap.assert_called_once_with("rootless")
        app_main.assert_called_once_with()

    def test_termux_launch_repairs_bootstrap_when_audio_self_test_raises(self):
        with patch.object(echocrypt.sys, "argv", ["echocrypt.py"]), \
             patch.object(echocrypt, "_choose_mode", return_value="rootless"), \
             patch.object(echocrypt, "_is_termux", return_value=True), \
             patch.object(echocrypt, "_missing_imports", return_value=[]), \
             patch.object(echocrypt, "_saved_mode", return_value="rootless"), \
             patch.object(echocrypt.shutil, "which", return_value="/termux/bin/termux-microphone-record"), \
             patch.object(echocrypt, "_verify_termux_audio_runtime", side_effect=RuntimeError("PortAudio unavailable")), \
             patch.object(echocrypt, "_bootstrap") as bootstrap, \
             patch("interface.menu.main") as app_main, \
             patch("builtins.print"):
            echocrypt.main()
        bootstrap.assert_called_once_with("rootless")
        app_main.assert_called_once_with()

    def test_termux_bootstrap_fails_if_python_portaudio_import_self_test_fails(self):
        calls = []
        with patch.object(echocrypt.shutil, "which", side_effect=lambda name: "/termux/bin/" + name), \
             patch.object(echocrypt, "_run", side_effect=lambda cmd, **kw: calls.append(cmd)), \
             patch.object(echocrypt, "_verify_termux_audio_runtime",
                          side_effect=RuntimeError("PortAudio load error")):
            with self.assertRaisesRegex(RuntimeError, "PortAudio load error"):
                echocrypt._bootstrap_termux_rootless()

    def test_termux_live_input_probe_accepts_a_successful_stream_open(self):
        result = completed(
            ["python", "-c", "probe"],
            stdout="ECHOCRYPT_INPUT_STREAM=OK\n",
        )
        with patch.object(echocrypt.subprocess, "run", return_value=result) as run:
            opened, detail = echocrypt._probe_termux_live_input_stream()
        self.assertTrue(opened)
        self.assertEqual(detail, "")
        self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_termux_live_input_probe_reports_stream_open_failure(self):
        result = completed(
            ["python", "-c", "probe"],
            stdout="ECHOCRYPT_INPUT_STREAM_ERROR=OpenSL ES -9999\n",
            returncode=4,
        )
        with patch.object(echocrypt.subprocess, "run", return_value=result):
            opened, detail = echocrypt._probe_termux_live_input_stream()
        self.assertFalse(opened)
        self.assertIn("OpenSL ES -9999", detail)

    def test_generic_rootless_bootstrap_uses_unprivileged_venv(self):
        calls = []
        fake_python = Path("/tmp/echocrypt-rootless/bin/python")
        with patch.object(echocrypt, "_is_termux", return_value=False), \
             patch.object(echocrypt, "_ensure_project_venv", return_value=fake_python) as ensure, \
             patch.object(echocrypt, "_install_requirements", side_effect=lambda *a, **kw: calls.append((a, kw))), \
             patch.object(echocrypt, "_missing_imports", return_value=[]), \
             patch.object(echocrypt, "_probe_sounddevice", return_value=(True, 1, 1, "")), \
             patch.object(echocrypt, "_save_mode") as save, \
             patch("builtins.print"):
            echocrypt._bootstrap("rootless")
        ensure.assert_called_once_with()
        self.assertEqual(calls[0][1], {"audio": True})
        save.assert_called_once_with("rootless")

    def test_manual_profile_does_not_try_to_launch_uninstalled_application(self):
        fake_python = Path("/tmp/echocrypt-manual/bin/python")
        with patch.object(echocrypt, "_is_termux", return_value=False), \
             patch.object(echocrypt, "_ensure_project_venv", return_value=fake_python), \
             patch.object(echocrypt, "_install_requirements") as install, \
             patch.object(echocrypt, "_save_mode") as save, \
             patch("builtins.print"):
            echocrypt._bootstrap("manual")
        install.assert_not_called()
        save.assert_called_once_with("manual")

    def test_core_and_audio_requirements_are_separate(self):
        core = (echocrypt.ROOT / "requirements.txt").read_text(encoding="utf-8")
        audio = (echocrypt.ROOT / "requirements-audio.txt").read_text(encoding="utf-8")
        self.assertNotIn("sounddevice", core)
        self.assertIn("sounddevice", audio)

    def test_mode_parser_supports_rootless(self):
        mode, rest, requested = echocrypt._parse_bootstrap_args(["--bootstrap", "rootless", "--verbose"])
        self.assertEqual(mode, "rootless")
        self.assertEqual(rest, ["--verbose"])
        self.assertTrue(requested)

    def test_bare_bootstrap_flag_requests_profile_selector(self):
        mode, rest, requested = echocrypt._parse_bootstrap_args(["--bootstrap"])
        self.assertIsNone(mode)
        self.assertEqual(rest, [])
        self.assertTrue(requested)


if __name__ == "__main__":
    unittest.main()
