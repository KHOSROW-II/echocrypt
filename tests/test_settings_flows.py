from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.config import Config
from interface import settings


class _MenuSequence:
    choices: list[str | None] = []

    def __init__(self, _title, _items):
        pass

    def run(self):
        if not self.choices:
            raise AssertionError("Unexpected extra menu invocation")
        return self.choices.pop(0)


def _menus(monkeypatch, *choices):
    _MenuSequence.choices = list(choices)
    monkeypatch.setattr(settings, "Menu", _MenuSequence)


def test_settings_menu_dispatches_each_configuration_submenu(monkeypatch):
    _menus(monkeypatch, "profile", "audio", "modem", "transfer", "protocol", "back")
    monkeypatch.setattr(settings, "load", lambda: Config())
    calls = []
    for name in ("_profile_settings", "_audio_settings", "_modem_settings", "_transfer_settings", "_protocol_settings"):
        monkeypatch.setattr(settings, name, lambda name=name: calls.append(name))

    assert settings.settings_menu() is None
    assert calls == ["_profile_settings", "_audio_settings", "_modem_settings", "_transfer_settings", "_protocol_settings"]


def test_settings_menu_toggles_debug_and_resets_audio_devices(monkeypatch, capsys):
    _menus(monkeypatch, "debug", "defaults", "back")
    monkeypatch.setattr(settings, "load", lambda: SimpleNamespace(debug_enabled=False))
    monkeypatch.setattr(settings, "render_app_header", lambda **_kwargs: None)
    monkeypatch.setattr(settings, "_pause", lambda: None)
    saved = []
    monkeypatch.setattr(settings, "save_setting", lambda *args: saved.append(args))

    settings.settings_menu()

    assert saved == [
        ("debug", "enabled", True),
        ("audio", "input_device", "default"),
        ("audio", "output_device", "default"),
    ]
    output = capsys.readouterr().out
    assert "ENABLED" in output
    assert "system's default audio devices" in output


def test_settings_debug_log_shows_only_last_eighty_lines(monkeypatch, tmp_path, capsys):
    _menus(monkeypatch, "debug-log", "back")
    fake_module = tmp_path / "interface" / "settings.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.write_text("# test module path", encoding="utf-8")
    log_path = tmp_path / "logs" / "acoustic-debug.log"
    log_path.parent.mkdir()
    log_path.write_text("\n".join(f"line-{i}" for i in range(100)), encoding="utf-8")
    monkeypatch.setattr(settings, "__file__", str(fake_module))
    monkeypatch.setattr(settings, "render_app_header", lambda **_kwargs: None)
    monkeypatch.setattr(settings, "_pause", lambda: None)

    settings.settings_menu()

    output = capsys.readouterr().out
    assert "line-20" in output
    assert "line-99" in output
    assert "line-19" not in output


def test_audio_settings_dispatches_diagnostics_and_numeric_editors(monkeypatch):
    _menus(monkeypatch, "speaker-test", "mic-test", "ultrasonic-test", "sample", "record", "gain", "back")
    monkeypatch.setattr(settings, "load", lambda: Config())
    calls = []
    monkeypatch.setattr(settings, "run_speaker_test", lambda: calls.append("speaker"))
    monkeypatch.setattr(settings, "run_microphone_level_test", lambda: calls.append("microphone"))
    monkeypatch.setattr(settings, "run_ultrasonic_hardware_test", lambda: calls.append("ultrasonic") or True)
    monkeypatch.setattr(settings, "_pause", lambda: None)
    monkeypatch.setattr(settings, "_edit_numeric", lambda *args: calls.append(args))
    
    settings._audio_settings()

    assert calls == [
        "speaker", "microphone", "ultrasonic",
        ("audio", "sample_rate", "sample_rate", "Sample rate (48000 or 96000 recommended)", int),
        ("audio", "record_seconds", "record_seconds", "Record window in seconds", float),
        ("audio", "gain", "gain", "Audio gain (0.05 to 0.95)", float),
    ]


def test_modem_settings_requires_hardware_pass_before_enabling_ultrasonic(monkeypatch, capsys):
    _menus(monkeypatch, "ultrasonic_enabled", "back")
    state = {"enabled": False}

    def load():
        config = Config()
        config.ultrasonic_enabled = state["enabled"]
        return config

    monkeypatch.setattr(settings, "load", load)
    monkeypatch.setattr(settings, "run_ultrasonic_hardware_test", lambda: False)
    saved = []

    def save(section, key, value):
        saved.append((section, key, value))
        if section == "modem" and key == "ultrasonic_enabled":
            state["enabled"] = value

    monkeypatch.setattr(settings, "save_setting", save)
    monkeypatch.setattr(settings, "_pause", lambda: None)
    monkeypatch.setattr(settings, "_edit_numeric", lambda *args: saved.append(args))
    monkeypatch.setattr(settings, "render_app_header", lambda **_kwargs: None)

    settings._modem_settings()
    assert saved == []
    assert state["enabled"] is False
    assert "NOT enabled" in capsys.readouterr().out


def test_transfer_settings_blocks_workers_without_hardware_approval(monkeypatch, capsys):
    _menus(monkeypatch, "multi_worker_enabled", "back")
    config = Config()
    config.multi_worker_enabled = False
    monkeypatch.setattr(settings, "load", lambda: config)
    monkeypatch.setattr(settings, "worker_hardware_profile", lambda: {
        "max_workers": 4, "logical_cpus": 4, "memory_display": "8 GiB",
    })
    monkeypatch.setattr(settings, "worker_hardware_approval_status", lambda _cfg: (False, "device pair not approved"))
    saved = []
    monkeypatch.setattr(settings, "save_setting", lambda *args: saved.append(args))
    monkeypatch.setattr(settings, "_pause", lambda: None)

    settings._transfer_settings()

    assert saved == []
    assert "Run the dedicated audio-path hardware test first" in capsys.readouterr().out


def test_transfer_settings_blocks_workers_when_resource_cap_is_one(monkeypatch, capsys):
    _menus(monkeypatch, "multi_worker_enabled", "back")
    monkeypatch.setattr(settings, "load", lambda: Config())
    monkeypatch.setattr(settings, "worker_hardware_profile", lambda: {
        "max_workers": 1, "logical_cpus": 1, "memory_display": "256 MiB",
    })
    monkeypatch.setattr(settings, "worker_hardware_approval_status", lambda _cfg: (True, "approved"))
    saved = []
    monkeypatch.setattr(settings, "save_setting", lambda *args: saved.append(args))
    monkeypatch.setattr(settings, "_pause", lambda: None)

    settings._transfer_settings()

    assert saved == []
    assert "only one preparation worker" in capsys.readouterr().out


def test_transfer_settings_allows_workers_after_both_gates_pass(monkeypatch, capsys):
    _menus(monkeypatch, "multi_worker_enabled", "back")
    config = Config()
    config.multi_worker_enabled = False
    monkeypatch.setattr(settings, "load", lambda: config)
    monkeypatch.setattr(settings, "worker_hardware_profile", lambda: {
        "max_workers": 4, "logical_cpus": 4, "memory_display": "8 GiB",
    })
    monkeypatch.setattr(settings, "worker_hardware_approval_status", lambda _cfg: (True, "approved"))
    saved = []
    monkeypatch.setattr(settings, "save_setting", lambda *args: saved.append(args))
    monkeypatch.setattr(settings, "_pause", lambda: None)

    settings._transfer_settings()

    assert saved == [("transfer", "multi_worker_enabled", True)]
    assert "ENABLED" in capsys.readouterr().out


def test_profile_settings_rejects_invalid_name_then_saves_valid_name(monkeypatch, capsys):
    _menus(monkeypatch, "username", "username", "back")
    prompts = iter(["x", "valid_user"])
    monkeypatch.setattr(settings, "get_username", lambda: "guest")
    monkeypatch.setattr(settings, "prompt", lambda _message: next(prompts))
    saved = []
    monkeypatch.setattr(settings, "set_username", lambda value: saved.append(value) or value)
    monkeypatch.setattr(settings, "render_app_header", lambda **_kwargs: None)
    monkeypatch.setattr(settings, "_pause", lambda: None)

    settings._profile_settings()

    assert saved == ["valid_user"]
    assert "Saved username: valid_user" in capsys.readouterr().out


def test_setup_virtual_audio_saves_both_owned_devices(monkeypatch, capsys):
    monkeypatch.setattr(settings, "render_app_header", lambda **_kwargs: None)
    monkeypatch.setattr(settings, "load", lambda: SimpleNamespace(sample_rate=48000))
    endpoints = {
        "input_device": "ECHOCRYPT Virtual Speaker Monitor",
        "output_device": "ECHOCRYPT Virtual Microphone TX",
        "application_microphone": "ECHOCRYPT Virtual Microphone",
        "application_speaker": "ECHOCRYPT Virtual Speaker",
    }
    monkeypatch.setattr(settings, "setup_virtual_audio", lambda **_kwargs: endpoints)
    saved = []
    monkeypatch.setattr(settings, "save_setting", lambda *args: saved.append(args))
    monkeypatch.setattr(settings, "_pause", lambda: None)

    settings._setup_virtual_audio()

    assert saved == [
        ("audio", "input_device", endpoints["input_device"]),
        ("audio", "output_device", endpoints["output_device"]),
        ("audio", "full_duplex", True),
    ]
    assert "Full-duplex mode is enabled" in capsys.readouterr().out


def test_browser_tab_audio_refuses_ultrasonic_configuration(monkeypatch, capsys):
    monkeypatch.setattr(settings, "render_app_header", lambda **_kwargs: None)
    config = Config()
    config.ultrasonic_enabled = True
    monkeypatch.setattr(settings, "load", lambda: config)
    monkeypatch.setattr(settings, "setup_virtual_audio", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not be called")))
    monkeypatch.setattr(settings, "_pause", lambda: None)

    settings._setup_browser_tab_audio()

    assert "Disable Ultrasonic acoustic mode" in capsys.readouterr().out


def test_remove_virtual_audio_resets_config_only_when_using_virtual_devices(monkeypatch, capsys):
    monkeypatch.setattr(settings, "render_app_header", lambda **_kwargs: None)
    monkeypatch.setattr(settings, "load", lambda: SimpleNamespace(input_device="ECHOCRYPT Virtual RX", output_device=2))
    monkeypatch.setattr(settings, "remove_virtual_audio", lambda: {"removed_module_ids": [11], "errors": ["module busy"]})
    monkeypatch.setattr(settings, "is_virtual_rx_device", lambda value: value == "ECHOCRYPT Virtual RX")
    monkeypatch.setattr(settings, "is_virtual_tx_device", lambda value: False)
    monkeypatch.setattr(settings, "_pause", lambda: None)
    saved = []
    monkeypatch.setattr(settings, "save_setting", lambda *args: saved.append(args))

    settings._remove_virtual_audio()

    assert saved == [
        ("audio", "full_duplex", False),
        ("audio", "input_device", "default"),
        ("audio", "output_device", "default"),
    ]
    output = capsys.readouterr().out
    assert "11" in output and "Cleanup warning: module busy" in output
