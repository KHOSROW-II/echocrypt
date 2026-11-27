#!/usr/bin/env python3
"""Chrome Native Messaging host that forwards selected-tab PCM to ECHOCRYPT.

The host accepts messages only from the extension ID registered by the install
script. It writes PCM to the dedicated echocrypt_tab_rx PulseAudio sink.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time

HOST_NAME = "org.echocrypt.tab_audio"
SINK_NAME = "echocrypt_tab_rx"
SAMPLE_RATE = 48000
MAX_MESSAGE_BYTES = 65536
MAX_PCM_BYTES = 8192


def _config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "echocrypt" / "browser-native-host.json"


def _read_exact(stream, count: int) -> bytes | None:
    chunks = []
    remaining = count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            if not chunks:
                return None
            raise EOFError("Native Messaging frame ended unexpectedly")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_message(stream):
    header = _read_exact(stream, 4)
    if header is None:
        return None
    (length,) = struct.unpack("=I", header)
    if length <= 0 or length > MAX_MESSAGE_BYTES:
        raise ValueError("Native Messaging message length is outside the allowed range")
    payload = _read_exact(stream, length)
    if payload is None:
        raise EOFError("Native Messaging message payload is missing")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Native Messaging payload is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Native Messaging payload must be a JSON object")
    return value


def write_message(stream, message):
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if len(payload) > 1024 * 1024:
        raise ValueError("Native host response exceeds the browser message limit")
    stream.write(struct.pack("=I", len(payload)))
    stream.write(payload)
    stream.flush()


def origin_is_allowed(origin: str) -> bool:
    try:
        stored = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False

    extension_ids = set()
    legacy_id = str(stored.get("extension_id", "")).strip()
    if legacy_id:
        extension_ids.add(legacy_id)
    listed = stored.get("extension_ids", [])
    if isinstance(listed, list):
        extension_ids.update(str(item).strip() for item in listed if str(item).strip())
    per_browser = stored.get("extensions", {})
    if isinstance(per_browser, dict):
        extension_ids.update(
            str(item).strip() for item in per_browser.values() if str(item).strip()
        )
    return origin in {f"chrome-extension://{extension_id}/" for extension_id in extension_ids}


def _sink_exists() -> bool:
    if shutil.which("pactl") is None:
        raise RuntimeError("pactl is required. Install PulseAudio utilities or PipeWire-Pulse tools.")
    result = subprocess.run(
        ["pactl", "list", "short", "sinks"],
        check=False, capture_output=True, text=True, timeout=5,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or "Could not query the PulseAudio-compatible server").strip())
    return any(
        len(row := line.split("\t")) > 1 and row[1] == SINK_NAME
        for line in result.stdout.splitlines()
    )


def _decode_pcm(message) -> bytes:
    if message.get("sample_rate") != SAMPLE_RATE:
        raise ValueError("Tab PCM must use a 48000 Hz sample rate")
    encoded = message.get("pcm_s16le")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("PCM message is missing pcm_s16le data")
    try:
        pcm = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("PCM payload is not valid Base64") from exc
    if not pcm or len(pcm) > MAX_PCM_BYTES or len(pcm) % 2:
        raise ValueError("PCM frame length is invalid or exceeds the per-frame limit")
    return pcm


def _write_all(stream, payload: bytes):
    view = memoryview(payload)
    while view:
        written = stream.write(view)
        if written is None or written <= 0:
            raise BrokenPipeError("The native PCM sink accepted no audio samples")
        view = view[written:]


def _stop_player(player):
    if player is None:
        return
    try:
        if player.stdin:
            player.stdin.close()
    except OSError:
        pass
    if player.poll() is None:
        player.terminate()
        try:
            player.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            player.kill()
            player.wait(timeout=1.0)


def run_native_host(origin: str, input_stream=None, output_stream=None):
    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout.buffer

    if not origin_is_allowed(origin):
        write_message(output_stream, {
            "type": "error",
            "message": "This extension ID is not registered for the ECHOCRYPT native host."
        })
        return 2

    player = None
    try:
        while True:
            message = read_message(input_stream)
            if message is None:
                break

            kind = message.get("type")
            if kind == "start":
                if message.get("sample_rate") != SAMPLE_RATE:
                    raise ValueError("The browser bridge currently requires 48000 Hz PCM.")
                if not _sink_exists():
                    raise RuntimeError(
                        "ECHOCRYPT browser-tab input is not configured. In ECHOCRYPT choose "
                        "Configuration → Audio settings → Use Chromium selected-tab capture as ECHOCRYPT RX."
                    )
                if shutil.which("pacat") is None:
                    raise RuntimeError("pacat is missing. Install the PulseAudio client utilities.")
                if player is not None:
                    _stop_player(player)
                player = subprocess.Popen(
                    [
                        "pacat", "--playback", "--raw",
                        "--format=s16le", "--rate=48000", "--channels=1",
                        f"--device={SINK_NAME}",
                        "--client-name=ECHOCRYPT_Tab_Audio",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
                time.sleep(0.08)
                if player.poll() is not None:
                    player = None
                    raise RuntimeError("pacat could not open the ECHOCRYPT browser-tab sink.")
                write_message(output_stream, {"type": "ready", "sample_rate": SAMPLE_RATE})

            elif kind == "pcm":
                if player is None or player.poll() is not None or player.stdin is None:
                    raise RuntimeError("The ECHOCRYPT PCM playback stream is not running.")
                pcm = _decode_pcm(message)
                try:
                    _write_all(player.stdin, pcm)
                except (BrokenPipeError, OSError) as exc:
                    raise RuntimeError("The ECHOCRYPT PCM playback stream closed unexpectedly.") from exc

            elif kind == "stop":
                _stop_player(player)
                player = None
                write_message(output_stream, {"type": "stopped"})

            else:
                raise ValueError("Unknown Native Messaging command")

    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:
        try:
            write_message(output_stream, {"type": "error", "message": str(exc)})
        except Exception:
            pass
        return 1
    finally:
        _stop_player(player)
    return 0


def main() -> int:
    origin = sys.argv[1] if len(sys.argv) > 1 else ""
    return run_native_host(origin)


if __name__ == "__main__":
    raise SystemExit(main())
