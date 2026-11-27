from __future__ import annotations

import types

import pytest

from core import dependencies


class _Stdin:
    def __init__(self, interactive=True):
        self.interactive = interactive
    def isatty(self):
        return self.interactive


def _set_mode(monkeypatch, tmp_path, mode):
    monkeypatch.setattr(dependencies, "MODE_FILE", tmp_path / ".echocrypt-mode")
    dependencies.MODE_FILE.write_text(mode, encoding="utf-8")


def test_install_mode_known_values_and_invalid_fallback(tmp_path, monkeypatch):
    mode_file = tmp_path / "mode"
    monkeypatch.setattr(dependencies, "MODE_FILE", mode_file)
    for mode in ("full", "standby", "manual", "rootless"):
        mode_file.write_text(mode.upper(), encoding="utf-8")
        assert dependencies.install_mode() == mode
    mode_file.write_text("unexpected", encoding="utf-8")
    assert dependencies.install_mode() == "manual"
    mode_file.unlink()
    assert dependencies.install_mode() == "manual"


def test_require_python_package_returns_already_installed_module(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(dependencies.importlib, "import_module", lambda name: sentinel)
    assert dependencies.require_python_package("fake_pkg", "fake-dist", "Audio") is sentinel


def test_require_python_package_in_manual_mode_reports_install_command(monkeypatch, tmp_path):
    _set_mode(monkeypatch, tmp_path, "manual")
    def missing(_name):
        raise ImportError("not installed")
    monkeypatch.setattr(dependencies.importlib, "import_module", missing)
    with pytest.raises(RuntimeError, match="pip install fake-dist"):
        dependencies.require_python_package("fake_pkg", "fake-dist", "Audio")


def test_require_python_package_does_not_prompt_in_noninteractive_session(monkeypatch, tmp_path):
    _set_mode(monkeypatch, tmp_path, "standby")
    monkeypatch.setattr(dependencies.importlib, "import_module", lambda _name: (_ for _ in ()).throw(ImportError()))
    monkeypatch.setattr(dependencies.sys, "stdin", _Stdin(False))
    with pytest.raises(RuntimeError, match="non-interactive"):
        dependencies.require_python_package("fake_pkg", "fake-dist", "Audio")


def test_require_python_package_respects_declined_prompt(monkeypatch, tmp_path):
    _set_mode(monkeypatch, tmp_path, "rootless")
    monkeypatch.setattr(dependencies.importlib, "import_module", lambda _name: (_ for _ in ()).throw(ImportError()))
    monkeypatch.setattr(dependencies.sys, "stdin", _Stdin(True))
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    with pytest.raises(RuntimeError, match="was not started"):
        dependencies.require_python_package("fake_pkg", "fake-dist", "Audio")


def test_require_python_package_installs_only_after_explicit_acceptance(monkeypatch, tmp_path):
    _set_mode(monkeypatch, tmp_path, "standby")
    monkeypatch.setattr(dependencies.sys, "stdin", _Stdin(True))
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    calls = []
    sentinel = object()
    attempts = {"count": 0}
    def import_module(name):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ImportError("missing")
        return sentinel
    monkeypatch.setattr(dependencies.importlib, "import_module", import_module)
    monkeypatch.setattr(dependencies.subprocess, "check_call", lambda command: calls.append(command))
    monkeypatch.setattr(dependencies.importlib, "invalidate_caches", lambda: None)
    assert dependencies.require_python_package("fake_pkg", "fake-dist", "Audio") is sentinel
    assert calls == [[dependencies.sys.executable, "-m", "pip", "install", "fake-dist"]]


def test_offer_codec2_install_returns_true_when_library_exists(monkeypatch):
    import ctypes.util
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: "libcodec2.so" if name == "codec2" else None)
    assert dependencies.offer_codec2_install() is True


def test_offer_codec2_install_is_disabled_outside_standby_or_non_linux(monkeypatch):
    import ctypes.util, platform
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: None)
    monkeypatch.setattr(dependencies, "install_mode", lambda: "manual")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(dependencies.sys, "stdin", _Stdin(True))
    assert dependencies.offer_codec2_install() is False

    monkeypatch.setattr(dependencies, "install_mode", lambda: "standby")
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    assert dependencies.offer_codec2_install() is False


def test_offer_codec2_install_decline_and_missing_apt_are_safe(monkeypatch, capsys):
    import ctypes.util, platform
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: None)
    monkeypatch.setattr(dependencies, "install_mode", lambda: "standby")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(dependencies.sys, "stdin", _Stdin(True))
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert dependencies.offer_codec2_install() is False

    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    monkeypatch.setattr(dependencies, "shutil_which", lambda _cmd: None)
    assert dependencies.offer_codec2_install() is False
    assert "APT is unavailable" in capsys.readouterr().out


def test_offer_codec2_install_handles_empty_index_and_success(monkeypatch, capsys):
    import ctypes.util, platform
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: None)
    monkeypatch.setattr(dependencies, "install_mode", lambda: "standby")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(dependencies.sys, "stdin", _Stdin(True))
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    monkeypatch.setattr(dependencies, "shutil_which", lambda _cmd: "/usr/bin/apt-cache")
    monkeypatch.setattr(dependencies.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="unrelated\n"))
    assert dependencies.offer_codec2_install() is False
    assert "No libcodec2 package" in capsys.readouterr().out

    calls = []
    monkeypatch.setattr(dependencies.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="libcodec2-1.0.5\n"))
    monkeypatch.setattr(dependencies.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(dependencies, "shutil_which", lambda cmd: "/usr/bin/apt-cache" if cmd == "apt-cache" else "/usr/bin/sudo")
    monkeypatch.setattr(dependencies.subprocess, "check_call", lambda command: calls.append(command))
    library_checks = iter([None, "libcodec2.so"])
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: next(library_checks))
    assert dependencies.offer_codec2_install() is True
    assert calls == [["sudo", "apt-get", "install", "-y", "libcodec2-1.0.5"]]


def test_offer_codec2_install_handles_package_manager_failure(monkeypatch, capsys):
    import ctypes.util, platform, subprocess
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: None)
    monkeypatch.setattr(dependencies, "install_mode", lambda: "standby")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(dependencies.sys, "stdin", _Stdin(True))
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    monkeypatch.setattr(dependencies, "shutil_which", lambda _cmd: "/usr/bin/apt-cache")
    monkeypatch.setattr(dependencies.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="libcodec2-1.0\n"))
    def fail(_cmd): raise subprocess.CalledProcessError(1, "apt-get")
    monkeypatch.setattr(dependencies.subprocess, "check_call", fail)
    assert dependencies.offer_codec2_install() is False
    assert "Codec2 installation failed" in capsys.readouterr().out


def test_offer_portaudio_install_returns_true_when_library_exists(monkeypatch):
    import ctypes.util
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: "libportaudio.so" if name == "portaudio" else None)
    assert dependencies.offer_portaudio_install() is True


def test_offer_portaudio_install_respects_mode_and_tty(monkeypatch):
    import ctypes.util
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: None)
    monkeypatch.setattr(dependencies, "install_mode", lambda: "manual")
    monkeypatch.setattr(dependencies.sys, "stdin", _Stdin(True))
    assert dependencies.offer_portaudio_install() is False
    monkeypatch.setattr(dependencies, "install_mode", lambda: "standby")
    monkeypatch.setattr(dependencies.sys, "stdin", _Stdin(False))
    assert dependencies.offer_portaudio_install() is False


def test_offer_portaudio_install_termux_decline_and_pkg_success(monkeypatch):
    import ctypes.util
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: None)
    monkeypatch.setattr(dependencies, "install_mode", lambda: "standby")
    monkeypatch.setattr(dependencies.sys, "stdin", _Stdin(True))
    monkeypatch.setenv("TERMUX_VERSION", "0.118")
    monkeypatch.setattr(dependencies, "shutil_which", lambda cmd: "/data/data/com.termux/files/usr/bin/pkg" if cmd == "pkg" else None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert dependencies.offer_portaudio_install() is False

    calls = []
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    monkeypatch.setattr(dependencies.subprocess, "check_call", lambda command: calls.append(command))
    library_checks = iter([None, "libportaudio.so"])
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: next(library_checks))
    assert dependencies.offer_portaudio_install() is True
    assert calls == [["/data/data/com.termux/files/usr/bin/pkg", "install", "-y", "portaudio", "pulseaudio", "libffi"]]


def test_offer_portaudio_install_rootless_never_invokes_host_package_manager(monkeypatch, capsys):
    import ctypes.util
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: None)
    monkeypatch.setattr(dependencies, "install_mode", lambda: "rootless")
    monkeypatch.setattr(dependencies.sys, "stdin", _Stdin(True))
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.delenv("PREFIX", raising=False)
    assert dependencies.offer_portaudio_install() is False
    assert "will not invoke a host package manager" in capsys.readouterr().out


def test_offer_portaudio_install_linux_apt_success_uses_sudo_when_unprivileged(monkeypatch):
    import ctypes.util, platform
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: None)
    monkeypatch.setattr(dependencies, "install_mode", lambda: "standby")
    monkeypatch.setattr(dependencies.sys, "stdin", _Stdin(True))
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.delenv("PREFIX", raising=False)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    monkeypatch.setattr(dependencies, "shutil_which", lambda cmd: "/usr/bin/apt-get" if cmd == "apt-get" else "/usr/bin/sudo")
    monkeypatch.setattr(dependencies.os, "geteuid", lambda: 1000, raising=False)
    calls = []
    monkeypatch.setattr(dependencies.subprocess, "check_call", lambda command: calls.append(command))
    library_checks = iter([None, "libportaudio.so"])
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: next(library_checks))
    assert dependencies.offer_portaudio_install() is True
    assert calls == [["sudo", "apt-get", "install", "-y", "libportaudio2"]]


def test_offer_portaudio_install_handles_missing_apt_sudo_and_failure(monkeypatch, capsys):
    import ctypes.util, platform, subprocess
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: None)
    monkeypatch.setattr(dependencies, "install_mode", lambda: "standby")
    monkeypatch.setattr(dependencies.sys, "stdin", _Stdin(True))
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.delenv("PREFIX", raising=False)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    monkeypatch.setattr(dependencies.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(dependencies, "shutil_which", lambda cmd: None)
    assert dependencies.offer_portaudio_install() is False
    assert "APT is unavailable" in capsys.readouterr().out

    monkeypatch.setattr(dependencies, "shutil_which", lambda cmd: "/usr/bin/apt-get" if cmd == "apt-get" else None)
    assert dependencies.offer_portaudio_install() is False
    assert "sudo is unavailable" in capsys.readouterr().out

    monkeypatch.setattr(dependencies, "shutil_which", lambda cmd: "/usr/bin/apt-get" if cmd == "apt-get" else "/usr/bin/sudo")
    def fail(_command): raise subprocess.CalledProcessError(1, "apt-get")
    monkeypatch.setattr(dependencies.subprocess, "check_call", fail)
    assert dependencies.offer_portaudio_install() is False
    assert "PortAudio installation failed" in capsys.readouterr().out


def test_shutil_which_delegates_to_path_lookup(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda command: f"/mock/bin/{command}")
    assert dependencies.shutil_which("apt-get") == "/mock/bin/apt-get"


def test_offer_portaudio_install_termux_missing_pkg_and_install_failure(monkeypatch, capsys):
    import ctypes.util, subprocess
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: None)
    monkeypatch.setattr(dependencies, "install_mode", lambda: "standby")
    monkeypatch.setattr(dependencies.sys, "stdin", _Stdin(True))
    monkeypatch.setenv("TERMUX_VERSION", "0.118")
    monkeypatch.setattr(dependencies, "shutil_which", lambda _cmd: None)
    assert dependencies.offer_portaudio_install() is False

    monkeypatch.setattr(dependencies, "shutil_which", lambda _cmd: "/mock/pkg")
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    def fail(_command):
        raise subprocess.CalledProcessError(1, "pkg")
    monkeypatch.setattr(dependencies.subprocess, "check_call", fail)
    assert dependencies.offer_portaudio_install() is False
    assert "Termux audio runtime installation failed" in capsys.readouterr().out


def test_offer_portaudio_install_non_linux_and_declined_prompt_are_safe(monkeypatch):
    import ctypes.util, platform
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: None)
    monkeypatch.setattr(dependencies, "install_mode", lambda: "full")
    monkeypatch.setattr(dependencies.sys, "stdin", _Stdin(True))
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.delenv("PREFIX", raising=False)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    assert dependencies.offer_portaudio_install() is False

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    assert dependencies.offer_portaudio_install() is False


def test_offer_portaudio_install_uses_apt_without_sudo_when_root(monkeypatch):
    import ctypes.util, platform
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: None)
    monkeypatch.setattr(dependencies, "install_mode", lambda: "full")
    monkeypatch.setattr(dependencies.sys, "stdin", _Stdin(True))
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.delenv("PREFIX", raising=False)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    monkeypatch.setattr(dependencies, "shutil_which", lambda cmd: "/usr/bin/apt-get" if cmd == "apt-get" else None)
    monkeypatch.setattr(dependencies.os, "geteuid", lambda: 0, raising=False)
    commands = []
    monkeypatch.setattr(dependencies.subprocess, "check_call", lambda command: commands.append(command))
    library_checks = iter([None, "libportaudio.so"])
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: next(library_checks))
    assert dependencies.offer_portaudio_install() is True
    assert commands == [["apt-get", "install", "-y", "libportaudio2"]]
