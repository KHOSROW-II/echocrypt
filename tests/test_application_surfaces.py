from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import platform_paths, profile
from core.config import Config
from interface import dashboard, menu, settings, theme
from scripts import generate_changelog, register_browser_host


@pytest.mark.parametrize(("value", "width", "expected"), [
    ("short", 8, "short"), ("long text", 4, "lon…"), ("x", 1, "x"),
    ("anything", 1, "…"), ("anything", 0, ""), ("anything", -2, ""),
])
def test_theme_truncation_boundaries(value, width, expected):
    assert theme._truncate(value, width) == expected


def test_theme_dashboard_is_bounded_on_narrow_terminals(monkeypatch):
    monkeypatch.setattr(theme.shutil, "get_terminal_size", lambda fallback: os.terminal_size((36, 24)))
    rendered = theme.dashboard(
        "IDENTITY READY", "a very long input device name " * 4,
        "a very long output device name " * 4, Config(),
        session_status="CONNECTED", username="test-user",
    )
    assert max(map(len, rendered.splitlines())) <= 34
    assert "TRANSPORT" in rendered and "CONNECTED" in rendered


def test_dashboard_compact_header_survives_broken_config_identity_and_profile(monkeypatch):
    monkeypatch.setattr(dashboard, "load", lambda: (_ for _ in ()).throw(ValueError("bad config")))
    monkeypatch.setattr(dashboard, "get_fingerprint", lambda: (_ for _ in ()).throw(OSError("key store")))
    monkeypatch.setattr(dashboard, "get_username", lambda: (_ for _ in ()).throw(OSError("profile")))
    monkeypatch.setattr(dashboard, "_audio_names", lambda _cfg: ("Mic", "Speaker"))
    monkeypatch.setattr(dashboard.shutil, "get_terminal_size", lambda fallback: os.terminal_size((52, 24)))
    fragments = dashboard.header_fragments(session_status="NOT CONNECTED", compact=True)
    assert [item[0] for item in fragments] == ["class:brand", "class:dashboard"]
    assert "anon-000" in fragments[1][1] and "Mic" in fragments[1][1] and "Speaker" in fragments[1][1]
    assert max(map(len, fragments[1][1].splitlines())) <= 50


def test_dashboard_audio_names_fall_back_to_configured_device_identifiers(monkeypatch):
    monkeypatch.setattr(
        dashboard, "configured_device_names",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audio unavailable")),
    )
    cfg = SimpleNamespace(input_device="USB Mic", output_device=4)
    assert dashboard._audio_names(cfg) == ("Device USB Mic", "Device 4")


def test_clear_terminal_is_noop_without_interactive_terminal(monkeypatch):
    class NonTTY:
        def isatty(self):
            return False
    monkeypatch.setattr(theme.sys, "stdout", NonTTY())
    calls = []
    monkeypatch.setattr(theme.os, "system", lambda command: calls.append(command) or 0)
    theme.clear_terminal()
    assert calls == []


def test_clear_terminal_uses_ansi_fallback_when_term_is_missing(monkeypatch):
    class TTY:
        def __init__(self):
            self.value = ""
        def isatty(self):
            return True
        def write(self, value):
            self.value += value
        def flush(self):
            pass
    output = TTY()
    monkeypatch.setattr(theme.sys, "stdout", output)
    monkeypatch.setattr(theme.os, "name", "posix")
    monkeypatch.delenv("TERM", raising=False)
    theme.clear_terminal()
    assert "\x1b[2J\x1b[H" in output.value


def test_settings_numeric_editor_saves_cast_value(monkeypatch):
    saved = []
    monkeypatch.setattr(settings, "load", lambda: SimpleNamespace(record_seconds=30.0))
    monkeypatch.setattr(settings, "render_app_header", lambda **kwargs: None)
    monkeypatch.setattr(settings, "prompt", lambda _text: "12.5")
    monkeypatch.setattr(settings, "save_setting", lambda *args: saved.append(args))
    monkeypatch.setattr(settings, "_pause", lambda: None)
    settings._edit_numeric("audio", "record_seconds", "record_seconds", "Record duration", float)
    assert saved == [("audio", "record_seconds", 12.5)]


def test_settings_numeric_editor_rejects_invalid_input_without_saving(monkeypatch, capsys):
    saved = []
    monkeypatch.setattr(settings, "load", lambda: SimpleNamespace(max_retries=3))
    monkeypatch.setattr(settings, "render_app_header", lambda **kwargs: None)
    monkeypatch.setattr(settings, "prompt", lambda _text: "not-a-number")
    monkeypatch.setattr(settings, "save_setting", lambda *args: saved.append(args))
    monkeypatch.setattr(settings, "_pause", lambda: None)
    settings._edit_numeric("protocol", "max_retries", "max_retries", "Retries", int)
    assert saved == []
    assert "Invalid value" in capsys.readouterr().out


def test_settings_numeric_editor_blank_input_keeps_configuration(monkeypatch):
    saved, paused = [], []
    monkeypatch.setattr(settings, "load", lambda: SimpleNamespace(gain=0.35))
    monkeypatch.setattr(settings, "render_app_header", lambda **kwargs: None)
    monkeypatch.setattr(settings, "prompt", lambda _text: "  ")
    monkeypatch.setattr(settings, "save_setting", lambda *args: saved.append(args))
    monkeypatch.setattr(settings, "_pause", lambda: paused.append(True))
    settings._edit_numeric("audio", "gain", "gain", "Gain", float)
    assert saved == [] and paused == []


def test_settings_device_selection_turns_off_full_duplex_and_saves_input(monkeypatch):
    saved = []
    class FakeMenu:
        def __init__(self, title, items):
            self.title, self.items = title, items
        def run(self):
            return "4"
    monkeypatch.setattr(settings, "render_app_header", lambda **kwargs: None)
    monkeypatch.setattr(settings, "list_devices", lambda: [(4, "Virtual Mic", 1, 0), (5, "Speaker", 0, 2)])
    monkeypatch.setattr(settings, "Menu", FakeMenu)
    monkeypatch.setattr(settings, "save_setting", lambda *args: saved.append(args))
    monkeypatch.setattr(settings, "_pause", lambda: None)
    settings._select_device("input")
    assert saved == [("audio", "full_duplex", False), ("audio", "input_device", 4)]


def test_settings_device_selection_back_does_not_save(monkeypatch):
    class FakeMenu:
        def __init__(self, *args):
            pass
        def run(self):
            return "back"
    saved = []
    monkeypatch.setattr(settings, "render_app_header", lambda **kwargs: None)
    monkeypatch.setattr(settings, "list_devices", lambda: [])
    monkeypatch.setattr(settings, "Menu", FakeMenu)
    monkeypatch.setattr(settings, "save_setting", lambda *args: saved.append(args))
    settings._select_device("output")
    assert saved == []


def test_settings_device_selection_reports_enumeration_failure(monkeypatch, capsys):
    paused = []
    monkeypatch.setattr(settings, "render_app_header", lambda **kwargs: None)
    monkeypatch.setattr(settings, "list_devices", lambda: (_ for _ in ()).throw(RuntimeError("PortAudio unavailable")))
    monkeypatch.setattr(settings, "_pause", lambda: paused.append(True))
    settings._select_device("output")
    assert "PortAudio unavailable" in capsys.readouterr().out
    assert paused == [True]


def test_settings_menu_returns_on_back(monkeypatch):
    monkeypatch.setattr(settings, "load", lambda: Config())
    class FakeMenu:
        def __init__(self, *args):
            pass
        def run(self):
            return "back"
    monkeypatch.setattr(settings, "Menu", FakeMenu)
    assert settings.settings_menu() is None


def test_main_menu_exit_does_not_start_any_subsystem(monkeypatch):
    class FakeMenu:
        def __init__(self, *args):
            pass
        def run(self):
            return "exit"
    monkeypatch.setattr(menu, "Menu", FakeMenu)
    assert menu.main() is None


@pytest.mark.parametrize(("platform", "browser", "host_tail"), [
    ("win32", "chrome", ("Google", "Chrome", "User Data", "NativeMessagingHosts")),
    ("win32", "firefox", ("Mozilla", "NativeMessagingHosts")),
    ("darwin", "edge", ("Microsoft Edge", "NativeMessagingHosts")),
    ("darwin", "firefox", ("Mozilla", "NativeMessagingHosts")),
    ("linux", "chrome", ("google-chrome", "NativeMessagingHosts")),
    ("linux", "edge", ("microsoft-edge", "NativeMessagingHosts")),
    ("linux", "firefox", ("mozilla", "NativeMessagingHosts")),
])
def test_browser_host_registration_paths_are_platform_specific(monkeypatch, tmp_path, platform, browser, host_tail):
    monkeypatch.setattr(register_browser_host.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    host_dir, config_dir = register_browser_host.paths(browser)
    assert host_dir.parts[-len(host_tail):] == host_tail
    assert config_dir.name == "ECHOCRYPT"


def test_browser_host_registration_writes_chromium_manifest(monkeypatch, tmp_path):
    host_dir, config_dir = tmp_path / "native", tmp_path / "settings"
    executable = tmp_path / "echocrypt-native-host"
    executable.write_bytes(b"native host placeholder")
    extension_id = "a" * 32
    monkeypatch.setattr(register_browser_host, "paths", lambda _browser: (host_dir, config_dir))
    monkeypatch.setattr(register_browser_host.sys, "argv", [
        "register_browser_host.py", "--browser", "chrome", "--extension-id", extension_id,
        "--native-host", str(executable),
    ])
    register_browser_host.main()
    manifest = json.loads((host_dir / f"{register_browser_host.HOST}.json").read_text())
    stored = json.loads((config_dir / "browser-native-host.json").read_text())
    assert manifest["allowed_origins"] == [f"chrome-extension://{extension_id}/"]
    assert stored["extensions"]["chrome"] == extension_id
    assert manifest["path"] == str(executable.resolve())


def test_browser_host_registration_rejects_invalid_extension_id(monkeypatch, tmp_path):
    executable = tmp_path / "native-host"
    executable.write_bytes(b"host")
    monkeypatch.setattr(register_browser_host, "paths", lambda _browser: (tmp_path / "hosts", tmp_path / "cfg"))
    monkeypatch.setattr(register_browser_host.sys, "argv", [
        "register_browser_host.py", "--browser", "chrome", "--extension-id", "invalid-id",
        "--native-host", str(executable),
    ])
    with pytest.raises(SystemExit, match="32 characters"):
        register_browser_host.main()


def test_release_changelog_command_writes_full_notes(monkeypatch, tmp_path):
    calls = []
    def fake_git(*args):
        calls.append(args)
        if args[0] == "tag":
            return "v0.1.0"
        if args[0] == "log":
            return "abc123 Add regression tests"
        raise AssertionError(args)
    output = tmp_path / "release" / "notes.md"
    monkeypatch.setattr(generate_changelog, "git", fake_git)
    generate_changelog.main([
        "--ref", "deadbeef", "--tag", "nightly-test", "--output", str(output)
    ])
    content = output.read_text(encoding="utf-8")
    assert "# ECHOCRYPT nightly-test" in content
    assert "v0.1.0..deadbeef" in calls[1][1]
    assert "abc123 Add regression tests" in content
    assert "Complete project changelog" in content
    assert "Draft" in content


def test_username_profile_roundtrip_and_validation(monkeypatch, tmp_path):
    monkeypatch.setattr(profile, "PROFILE_PATH", tmp_path / "profile.json")
    assert profile.set_username("test_user-1") == "test_user-1"
    assert profile.get_username() == "test_user-1"
    with pytest.raises(ValueError, match="Username must be"):
        profile.set_username("x")
    assert json.loads((tmp_path / "profile.json").read_text())["username"] == "test_user-1"


def test_frozen_resource_directory_uses_pyinstaller_meipass(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(platform_paths.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert platform_paths.bundled_resource_dir() == tmp_path
