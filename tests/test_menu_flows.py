from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from interface import menu


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
    monkeypatch.setattr(menu, "Menu", _MenuSequence)


def test_identity_menu_create_and_show_fingerprint(monkeypatch, capsys):
    _menus(monkeypatch, "create", "fp", "back")
    monkeypatch.setattr(menu, "render_app_header", lambda **_kwargs: None)
    monkeypatch.setattr(menu, "create_identity", lambda: "created")
    monkeypatch.setattr(menu, "get_fingerprint", lambda: "ab" * 32)
    monkeypatch.setattr(menu, "pause", lambda: None)

    menu.identity_menu()

    output = capsys.readouterr().out
    assert "Created: created" in output
    assert "Fingerprint:" in output
    assert "ab" * 32 in output


def test_identity_menu_refuses_export_without_private_identity(monkeypatch, tmp_path, capsys):
    _menus(monkeypatch, "export", "back")
    monkeypatch.setattr(menu, "render_app_header", lambda **_kwargs: None)
    monkeypatch.setattr(menu, "PRIVATE", tmp_path / "missing-private-key")
    monkeypatch.setattr(menu, "pause", lambda: None)

    menu.identity_menu()

    assert "ERROR: Create identity first" in capsys.readouterr().out


def test_identity_menu_exports_only_public_identity(monkeypatch, tmp_path, capsys):
    _menus(monkeypatch, "export", "back")
    private_path = tmp_path / "identity.ed25519"
    private_path.write_bytes(b"private-key-placeholder")
    public_path = tmp_path / "identity.public.json"
    monkeypatch.setattr(menu, "PRIVATE", private_path)
    monkeypatch.setattr(menu, "PUBLIC", public_path)
    monkeypatch.setattr(menu, "render_app_header", lambda **_kwargs: None)
    monkeypatch.setattr(menu, "load_private", lambda _path: "private-object")

    def export(_private, path):
        path.write_text("public only", encoding="utf-8")
        return path

    monkeypatch.setattr(menu, "export_public", export)
    monkeypatch.setattr(menu, "pause", lambda: None)

    menu.identity_menu()

    assert public_path.read_text(encoding="utf-8") == "public only"
    assert "Exported:" in capsys.readouterr().out


def test_audio_menu_handles_devices_and_test_actions_without_hardware(monkeypatch, capsys):
    _menus(monkeypatch, "devices", "speaker-test", "mic-test", "record", "back")
    monkeypatch.setattr(menu, "render_app_header", lambda **_kwargs: None)
    monkeypatch.setattr(menu, "load", lambda: SimpleNamespace(input_device=None, output_device=5, sample_rate=48000))
    monkeypatch.setattr(menu, "default_device_names", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("no backend")))
    monkeypatch.setattr(menu, "list_devices", lambda: [(2, "Mic", 1, 0), (5, "Speaker", 0, 2)])
    calls = []
    monkeypatch.setattr(menu, "run_speaker_test", lambda: calls.append("speaker"))
    monkeypatch.setattr(menu, "run_microphone_level_test", lambda: calls.append("mic"))
    monkeypatch.setattr(menu, "record", lambda *_args: np.array([0.0, 0.25, -0.5], dtype=np.float32))
    monkeypatch.setattr(menu, "pause", lambda: None)

    menu.audio_menu()

    output = capsys.readouterr().out
    assert "Mic" in output and "Speaker" in output
    assert "AUDIO ERROR" not in output
    assert "peak 0.5" in output
    assert calls == ["speaker", "mic"]


def test_modem_test_decodes_in_memory_with_mocked_modem(monkeypatch, capsys):
    _menus(monkeypatch, "decode")
    monkeypatch.setattr(menu, "render_app_header", lambda **_kwargs: None)
    monkeypatch.setattr(menu, "load", lambda: SimpleNamespace(
        sample_rate=48000, symbol_rate=200, mark_hz=1800, space_hz=2400,
        gain=0.3, max_payload=1024,
    ))
    monkeypatch.setattr(menu.secrets, "token_bytes", lambda _count: b"k" * 32)
    monkeypatch.setattr(menu, "prompt", lambda _text: "hello modem")

    class FakeChannel:
        def __init__(self, _key):
            pass

        def encrypt(self, payload, _associated_data):
            return b"encrypted:" + payload

        def decrypt(self, _packet, _associated_data):
            return b"hello modem"

    monkeypatch.setattr(menu, "SecureChannel", FakeChannel)
    monkeypatch.setattr(menu, "modulate", lambda payload, *_args: b"waveform")
    monkeypatch.setattr(menu, "demodulate", lambda *_args: b"packet")
    monkeypatch.setattr(menu, "pause", lambda: None)

    menu.modem_test()

    output = capsys.readouterr().out
    assert "Recovered: hello modem" in output
    assert "software loopback only" in output


def test_modem_test_returns_cleanly_when_menu_is_cancelled(monkeypatch):
    _menus(monkeypatch, None)
    monkeypatch.setattr(menu, "render_app_header", lambda **_kwargs: None)
    monkeypatch.setattr(menu, "load", lambda: SimpleNamespace(
        sample_rate=48000, symbol_rate=200, mark_hz=1800, space_hz=2400,
        gain=0.3, max_payload=1024,
    ))
    monkeypatch.setattr(menu.secrets, "token_bytes", lambda _count: b"k" * 32)
    monkeypatch.setattr(menu, "prompt", lambda _text: "test")
    monkeypatch.setattr(menu, "SecureChannel", lambda _key: SimpleNamespace(encrypt=lambda *_a: b"packet"))
    monkeypatch.setattr(menu, "modulate", lambda *_a: b"wave")
    monkeypatch.setattr(menu, "pause", lambda: None)

    menu.modem_test()


def test_main_routes_security_screen_and_exit(monkeypatch, capsys):
    _menus(monkeypatch, "security", "exit")
    monkeypatch.setattr(menu, "render_app_header", lambda **_kwargs: None)
    monkeypatch.setattr(menu, "pause", lambda: None)

    assert menu.main() is None
    output = capsys.readouterr().out
    assert "Ed25519" in output
    assert "ChaCha20-Poly1305" in output
