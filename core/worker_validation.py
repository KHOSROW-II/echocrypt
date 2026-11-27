"""Machine-local approval gate for parallel Direct preparation workers."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

TEST_VERSION = 1


def _approval_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "echocrypt" / "worker-hardware-approval.json"


def _device_snapshot(sd, selector, direction):
    try:
        info = sd.query_devices(kind=direction) if selector is None else sd.query_devices(selector, kind=direction)
    except TypeError:
        info = sd.query_devices(selector)
    if not info:
        raise RuntimeError(f"No {direction} audio device is available")
    host_index = int(info.get("hostapi", -1))
    try:
        host = sd.query_hostapis(host_index).get("name", "unknown")
    except Exception:
        host = "unknown"
    channels_key = "max_input_channels" if direction == "input" else "max_output_channels"
    if int(info.get(channels_key, 0)) < 1:
        raise RuntimeError(f"Selected {direction} device has no usable {direction} channel")
    return {
        "name": str(info.get("name", "unknown")),
        "host_api": str(host),
        "channels": int(info.get(channels_key, 0)),
        "default_samplerate": float(info.get("default_samplerate", 0.0)),
    }


def hardware_fingerprint(cfg, sd=None) -> str:
    """Bind approval to both endpoint identities and every relevant modem setting."""
    if sd is None:
        from core.audio import _sounddevice
        sd = _sounddevice(allow_install=False)
    from core.modem import HOP_OFFSETS

    bases = (
        (int(cfg.ultrasonic_mark_hz), int(cfg.ultrasonic_space_hz))
        if bool(getattr(cfg, "ultrasonic_enabled", False))
        else (int(cfg.mark_hz), int(cfg.space_hz))
    )
    snapshot = {
        "test_version": TEST_VERSION,
        "input": _device_snapshot(sd, cfg.input_device, "input"),
        "output": _device_snapshot(sd, cfg.output_device, "output"),
        "sample_rate": int(cfg.sample_rate),
        "playback_gain": float(getattr(cfg, "gain", 0.35)),
        "frequencies": sorted({
            int(base + offset) for base in bases for offset in HOP_OFFSETS
        }),
        "ultrasonic_enabled": bool(getattr(cfg, "ultrasonic_enabled", False)),
        "full_duplex_audio": bool(getattr(cfg, "full_duplex_audio", False)),
    }
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def worker_hardware_approval_status(cfg, sd=None):
    """Return (approved, reason); stale approvals never silently remain valid."""
    try:
        current = hardware_fingerprint(cfg, sd=sd)
    except Exception as exc:
        return False, f"Audio endpoints could not be inspected: {exc}"
    try:
        stored = json.loads(_approval_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, "The dedicated microphone/speaker worker test has not been run."
    except (OSError, ValueError, TypeError) as exc:
        return False, f"Worker hardware approval is unreadable: {exc}"
    if stored.get("test_version") != TEST_VERSION:
        return False, "The worker hardware test version changed; run the test again."
    if stored.get("fingerprint") != current:
        return False, "Audio devices or modem settings changed; run the worker hardware test again."
    if stored.get("passed") is not True:
        return False, "The last worker hardware test did not pass."
    return True, f"Approved on {stored.get('approved_at', 'a previous run')}"


def approve_worker_hardware(cfg, result):
    """Persist approval only after every configured test tone was detected."""
    if not isinstance(result, dict) or result.get("passed") is not True:
        raise ValueError("A failed or incomplete hardware test cannot approve workers")
    fingerprint = hardware_fingerprint(cfg)
    path = _approval_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "test_version": TEST_VERSION,
        "fingerprint": fingerprint,
        "passed": True,
        "approved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tone_count": len(result.get("tones", ())),
        "sample_rate": int(cfg.sample_rate),
        "input_device": str(cfg.input_device if cfg.input_device is not None else "system-default"),
        "output_device": str(cfg.output_device if cfg.output_device is not None else "system-default"),
    }
    fd, temporary = tempfile.mkstemp(prefix=".worker-approval.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return payload


def effective_worker_count(cfg) -> int:
    """Return the local cap; an unapproved device always advertises one worker."""
    if not bool(getattr(cfg, "multi_worker_enabled", False)):
        return 1
    approved, _reason = worker_hardware_approval_status(cfg)
    if not approved:
        return 1
    count = int(getattr(cfg, "multi_worker_count", 1))
    return count if 2 <= count <= 8 else 1
