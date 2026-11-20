"""Termux:API microphone permission bootstrap helpers.

The API recorder is used only to request/check Android permission. It does not
replace EchoCrypt's live PortAudio PCM input stream.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid


def _run(command: list[str], *, timeout: float = 8.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _combined_output(result: subprocess.CompletedProcess) -> str:
    return "\\n".join(
        part.strip() for part in (result.stdout, result.stderr)
        if part and part.strip()
    )


def request_microphone_permission(*, recorder: str | None = None) -> tuple[str, str]:
    """Request RECORD_AUDIO and verify a one-second recording.

    Returns ("permission_required", message) when Termux:API launches Android's
    permission dialog. The caller should let the user grant permission and call
    this function again. Returns ("granted", message) only after the bounded
    test recording has finalized and produced a non-empty file.
    """
    recorder = recorder or shutil.which("termux-microphone-record")
    if not recorder:
        raise RuntimeError(
            "The Termux-side command is missing. Run 'pkg install termux-api' "
            "and install the matching Termux:API Android companion app."
        )

    temp_dir = Path(tempfile.gettempdir())
    temp_dir.mkdir(parents=True, exist_ok=True)
    output_file = temp_dir / (
        f"echocrypt-mic-check-{os.getpid()}-{uuid.uuid4().hex}.m4a"
    )
    command = [
        recorder,
        "-f", str(output_file),
        "-l", "1",
        "-e", "aac",
        "-r", "48000",
        "-c", "1",
    ]

    try:
        try:
            started = _run(command, timeout=15.0)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "Termux:API did not answer the microphone request. "
                "Check the Android permission prompt and the companion app."
            ) from exc

        output = _combined_output(started)
        lowered = output.lower()
        if "record_audio" in lowered and ("grant" in lowered or "permission" in lowered):
            return (
                "permission_required",
                "Android's microphone permission prompt was requested. "
                "Choose Allow, return to EchoCrypt, and retry this action.",
            )
        if started.returncode != 0:
            raise RuntimeError(
                output or "Termux:API failed to start the microphone permission check."
            )
        try:
            response = json.loads(started.stdout.strip())
        except (json.JSONDecodeError, TypeError):
            response = None
        if isinstance(response, dict) and response.get("error"):
            error = str(response["error"])
            if "record_audio" in error.lower():
                return (
                    "permission_required",
                    "Android's microphone permission prompt was requested. "
                    "Choose Allow, return to EchoCrypt, and retry this action.",
                )
            raise RuntimeError(error)

        if "recording started" not in lowered:
            raise RuntimeError(
                output or "Termux:API did not confirm that the test recording started."
            )

        deadline = time.monotonic() + 8.0
        recording_finished = False
        last_info = ""
        while time.monotonic() < deadline:
            try:
                info = _run([recorder, "-i"], timeout=5.0)
            except subprocess.TimeoutExpired:
                time.sleep(0.2)
                continue
            last_info = _combined_output(info)
            if info.returncode == 0:
                try:
                    state = json.loads(info.stdout.strip())
                except (json.JSONDecodeError, TypeError):
                    state = None
                if isinstance(state, dict) and state.get("isRecording") is False:
                    recording_finished = True
                    break
            time.sleep(0.2)

        if not recording_finished:
            try:
                _run([recorder, "-q"], timeout=5.0)
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise RuntimeError(
                "The one-second Termux:API test recording did not finish cleanly. "
                "Check the Termux:API app and retry. " + last_info[-500:]
            )

        if not output_file.is_file() or output_file.stat().st_size <= 0:
            raise RuntimeError(
                "Termux:API completed but the temporary audio file is missing or empty. "
                "Check the app signature/source, microphone permission, and private file access."
            )
        return (
            "granted",
            "Termux:API confirmed microphone access. The temporary test recording was removed.",
        )
    finally:
        try:
            output_file.unlink(missing_ok=True)
        except OSError:
            pass
