from __future__ import annotations

import base64
import io
import json
import sys
from types import SimpleNamespace

import pytest

from browser_extension import native_host


def pack_messages(*messages):
    stream = io.BytesIO()
    for message in messages:
        native_host.write_message(stream, message)
    stream.seek(0)
    return stream


def unpack_messages(stream):
    stream.seek(0)
    messages = []
    while True:
        message = native_host.read_message(stream)
        if message is None:
            return messages
        messages.append(message)


def test_native_host_reports_truncated_native_message_payload():
    frame = io.BytesIO((3).to_bytes(4, byteorder=sys.byteorder) + b"{}")
    with pytest.raises(EOFError, match="ended unexpectedly"):
        native_host.read_message(frame)


def test_native_host_resolves_sounddevice_output_by_name_or_index():
    devices = [
        {"name": "Loopback Capture", "max_output_channels": 2},
        {"name": "Speakers", "max_output_channels": 2},
    ]
    backend = SimpleNamespace(query_devices=lambda: devices)
    assert native_host._resolve_sounddevice_output(backend, "Speakers") == 1
    assert native_host._resolve_sounddevice_output(backend, "0") == 0
    with pytest.raises(RuntimeError, match="matched 0"):
        native_host._resolve_sounddevice_output(backend, "missing")
    with pytest.raises(ValueError, match="not a valid output"):
        native_host._resolve_sounddevice_output(backend, "9")


def test_native_host_sounddevice_stream_transmits_pcm_without_pacat_api(monkeypatch, tmp_path):
    events = []
    class FakeRawOutputStream:
        def __init__(self, **kwargs):
            events.append(("construct", kwargs))
            self.active = False
        def start(self):
            self.active = True
            events.append(("start",))
        def write(self, data):
            events.append(("write", bytes(data)))
        def stop(self):
            self.active = False
            events.append(("stop",))
        def close(self):
            events.append(("close",))

    backend = SimpleNamespace(
        query_devices=lambda: [{"name": "Virtual Output", "max_output_channels": 2}],
        RawOutputStream=FakeRawOutputStream,
    )
    monkeypatch.setitem(sys.modules, "sounddevice", backend)
    config_path = tmp_path / "browser-native-host.json"
    config_path.write_text(json.dumps({"audio_output_device": "Virtual Output"}), encoding="utf-8")
    monkeypatch.setattr(native_host, "_config_path", lambda: config_path)
    monkeypatch.setattr(native_host, "origin_is_allowed", lambda _origin: True)
    monkeypatch.setattr(native_host.sys, "platform", "win32")

    pcm = b"\x01\x00\x02\x00"
    incoming = pack_messages(
        {"type": "start", "sample_rate": native_host.SAMPLE_RATE},
        {"type": "pcm", "sample_rate": native_host.SAMPLE_RATE,
         "pcm_s16le": base64.b64encode(pcm).decode("ascii")},
        {"type": "stop"},
    )
    outgoing = io.BytesIO()
    result = native_host.run_native_host(
        "chrome-extension://" + "a" * 32 + "/", input_stream=incoming, output_stream=outgoing
    )
    assert result == 0
    assert ("write", pcm) in events
    assert ("start",) in events and ("stop",) in events and ("close",) in events
    assert [item["type"] for item in unpack_messages(outgoing)] == ["ready", "stopped"]


def test_native_host_refuses_pcm_before_stream_start(monkeypatch):
    monkeypatch.setattr(native_host, "origin_is_allowed", lambda _origin: True)
    incoming = pack_messages({
        "type": "pcm",
        "sample_rate": native_host.SAMPLE_RATE,
        "pcm_s16le": base64.b64encode(b"\x00\x00").decode("ascii"),
    })
    outgoing = io.BytesIO()
    result = native_host.run_native_host(
        "chrome-extension://" + "b" * 32 + "/", input_stream=incoming, output_stream=outgoing
    )
    assert result == 1
    response = unpack_messages(outgoing)[0]
    assert response["type"] == "error"
    assert "not running" in response["message"]
