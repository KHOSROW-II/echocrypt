"""Linux virtual audio endpoints for isolated ECHOCRYPT TX/RX routing.

Uses the PulseAudio protocol exposed by PulseAudio or PipeWire-Pulse. It never
changes the system-wide default devices.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from contextlib import contextmanager


TX_SINK = "echocrypt_tx"
RX_SINK = "echocrypt_rx"
TX_MIC_DESCRIPTION = "ECHOCRYPT Virtual Microphone"
TX_OUTPUT_DESCRIPTION = "ECHOCRYPT Virtual Microphone TX"
RX_SPEAKER_DESCRIPTION = "ECHOCRYPT Virtual Speaker"
RX_MONITOR_DESCRIPTION = "ECHOCRYPT Virtual Speaker Monitor"


def is_supported() -> bool:
    return os.name == "posix" and shutil.which("pactl") is not None


def _state_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "echocrypt" / "virtual-audio.json"


def _pactl(*args: str, check: bool = True) -> str:
    if not is_supported():
        raise RuntimeError(
            "Virtual audio endpoints require Linux with pactl and a running "
            "PulseAudio or PipeWire-Pulse server."
        )
    try:
        result = subprocess.run(
            ["pactl", *map(str, args)],
            check=False, capture_output=True, text=True, timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not execute pactl: {exc}") from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "pactl returned an error").strip()
        raise RuntimeError(detail)
    return (result.stdout or "").strip()


def _rows(kind: str):
    rows = []
    for line in _pactl("list", "short", kind).splitlines():
        fields = line.split("\\t")
        if len(fields) < 2:
            fields = line.split(None, 2)
        if len(fields) >= 2:
            rows.append(fields)
    return rows


def _module_rows():
    rows = []
    for line in _pactl("list", "short", "modules").splitlines():
        fields = line.split("\\t", 2)
        if len(fields) < 3:
            fields = line.split(None, 2)
        if len(fields) >= 3:
            try:
                rows.append((int(fields[0]), fields[1], fields[2]))
            except ValueError:
                continue
    return rows


def _find_module(module_name: str, marker: str):
    for module_id, name, arguments in _module_rows():
        if name == module_name and marker in arguments:
            return module_id
    return None


def _ensure_module(module_name: str, arguments: str, marker: str, previously_owned):
    existing = _find_module(module_name, marker)
    if existing is not None:
        return existing, existing in previously_owned
    module_id = int(_pactl("load-module", module_name, *arguments.split()))
    return module_id, True


def _write_state(state):
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".virtual-audio.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_state():
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def device_selectors():
    """Stable PulseAudio description selectors used by PortAudio and call apps."""
    return {
        "input_device": RX_MONITOR_DESCRIPTION,
        "output_device": TX_OUTPUT_DESCRIPTION,
        "application_microphone": TX_MIC_DESCRIPTION,
        "application_speaker": RX_SPEAKER_DESCRIPTION,
    }


def virtual_audio_status():
    if not is_supported():
        return {"supported": False, "active": False, "reason": "pactl unavailable"}
    try:
        sink_names = {row[1] for row in _rows("sinks")}
        source_names = {row[1] for row in _rows("sources")}
        active = (
            TX_SINK in sink_names and RX_SINK in sink_names
            and f"{TX_SINK}.monitor" in source_names
            and f"{RX_SINK}.monitor" in source_names
        )
        server = _pactl("info")
        return {
            "supported": True,
            "active": active,
            "server": next(
                (line.split(":", 1)[1].strip() for line in server.splitlines()
                 if line.lower().startswith("server name:")),
                "PulseAudio-compatible server",
            ),
            "sinks": sorted(sink_names),
            "sources": sorted(source_names),
        }
    except Exception as exc:
        return {"supported": True, "active": False, "reason": str(exc)}


def setup_virtual_audio(*, monitor_to_physical_output=True):
    """Create a TX microphone source and an RX speaker sink.

    Only applications explicitly selecting ECHOCRYPT Virtual Speaker are routed
    through the RX monitor. An optional loopback mirrors just that sink to the
    current physical default output. System defaults are never replaced.
    """
    if not is_supported():
        raise RuntimeError(
            "Virtual audio setup is supported on Linux with pactl and a running "
            "PulseAudio or PipeWire-Pulse session. It is not available in stock Termux."
        )

    previous = _read_state()
    previously_owned = {int(value) for value in previous.get("owned_module_ids", [])}
    owned = set(previously_owned)
    created_now = []
    loopback_warning = None
    loopback_id = None
    try:
        _pactl("info")
        tx_id, tx_owned = _ensure_module(
            "module-null-sink",
            f"sink_name={TX_SINK} rate=48000 channels=2 "
            f"channel_map=front-left,front-right "
            f"sink_properties=device.description={TX_OUTPUT_DESCRIPTION.replace(' ', '_')}",
            f"sink_name={TX_SINK}", previously_owned,
        )
        rx_id, rx_owned = _ensure_module(
            "module-null-sink",
            f"sink_name={RX_SINK} rate=48000 channels=2 "
            f"channel_map=front-left,front-right "
            f"sink_properties=device.description={RX_SPEAKER_DESCRIPTION.replace(' ', '_')}",
            f"sink_name={RX_SINK}", previously_owned,
        )
        if tx_owned:
            owned.add(tx_id)
        if rx_owned:
            owned.add(rx_id)
        if tx_id not in previously_owned:
            created_now.append(tx_id)
        if rx_id not in previously_owned:
            created_now.append(rx_id)

        _pactl(
            "update-sink-proplist", TX_SINK,
            f"device.description={TX_OUTPUT_DESCRIPTION}",
        )
        _pactl(
            "update-source-proplist", f"{TX_SINK}.monitor",
            f"device.description={TX_MIC_DESCRIPTION}",
        )
        _pactl(
            "update-source-proplist", f"{RX_SINK}.monitor",
            f"device.description={RX_MONITOR_DESCRIPTION}",
        )
        _pactl(
            "update-sink-proplist", RX_SINK,
            f"device.description={RX_SPEAKER_DESCRIPTION}",
        )

        if monitor_to_physical_output:
            try:
                loopback_id, loopback_owned = _ensure_module(
                    "module-loopback",
                    f"source={RX_SINK}.monitor sink=@DEFAULT_SINK@ latency_msec=40",
                    f"source={RX_SINK}.monitor",
                    previously_owned,
                )
                if loopback_owned:
                    owned.add(loopback_id)
                if loopback_id not in previously_owned:
                    created_now.append(loopback_id)
            except Exception as exc:
                loopback_warning = str(exc)

        status = virtual_audio_status()
        if not status.get("active"):
            raise RuntimeError(
                "The audio server did not expose both ECHOCRYPT virtual endpoints. "
                + str(status.get("reason", "Check the PulseAudio/PipeWire session."))
            )
        _write_state({
            "version": 1,
            "owned_module_ids": sorted(owned),
            "tx_sink": TX_SINK,
            "rx_sink": RX_SINK,
            "monitor_to_physical_output": bool(loopback_id),
        })
        return {
            **device_selectors(),
            "server": status.get("server"),
            "monitor_to_physical_output": bool(loopback_id),
            "loopback_warning": loopback_warning,
        }
    except Exception:
        for module_id in reversed(created_now):
            _pactl("unload-module", str(module_id), check=False)
        raise


@contextmanager
def temporary_worker_test_route():
    """Temporarily loop TX into RX for a local virtual-endpoint test."""
    status = virtual_audio_status()
    if not status.get("active"):
        raise RuntimeError("ECHOCRYPT virtual audio endpoints are not active")
    module_id = int(_pactl(
        "load-module", "module-loopback",
        f"source={TX_SINK}.monitor", f"sink={RX_SINK}",
        "latency_msec=20",
    ))
    try:
        yield
    finally:
        _pactl("unload-module", str(module_id), check=False)


def remove_virtual_audio():
    """Unload only modules created and recorded by ECHOCRYPT."""
    state = _read_state()
    removed = []
    errors = []
    for module_id in reversed(state.get("owned_module_ids", [])):
        try:
            _pactl("unload-module", str(int(module_id)))
            removed.append(int(module_id))
        except Exception as exc:
            errors.append(str(exc))
    try:
        _state_path().unlink(missing_ok=True)
    except OSError as exc:
        errors.append(str(exc))
    return {"removed_module_ids": removed, "errors": errors}
