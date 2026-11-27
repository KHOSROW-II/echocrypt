from dataclasses import dataclass
from pathlib import Path
import json
import math
import os
import re
import tempfile

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "default.toml"


@dataclass(frozen=True)
class Config:
    sample_rate: int = 48000
    symbol_rate: int = 200
    mark_hz: int = 1800
    space_hz: int = 2400
    input_device: int | str | None = None
    output_device: int | str | None = None
    full_duplex_audio: bool = False
    record_seconds: float = 30.0
    gain: float = 0.35
    max_payload: int = 1024
    ack_timeout_seconds: float = 15.0
    max_retries: int = 3
    listen_timeout_seconds: float = 120.0
    retry_listen_seconds: float = 18.0
    fast_symbol_rate: int = 800
    voice_symbol_rate: int = 1200
    emergency_symbol_rate: int = 1500
    ultrasonic_enabled: bool = False
    ultrasonic_mark_hz: int = 20700
    ultrasonic_space_hz: int = 22200
    multi_worker_enabled: bool = False
    multi_worker_count: int = 4
    debug_enabled: bool = False


def _device_value(value):
    """None means: let PortAudio use the operating system's default device."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in {"default", "auto", "system"}:
        return None
    if isinstance(value, str):
        selector = value.strip()
        return None if not selector or selector.lower() in {"default", "auto", "system"} else selector
    number = int(value)
    return None if number < 0 else number


def load() -> Config:
    raw = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    a, m, pr = raw.get("audio", {}), raw.get("modem", {}), raw.get("protocol", {})
    transfer = raw.get("transfer", {})
    c = Config(
        sample_rate=int(a.get("sample_rate", 48000)),
        symbol_rate=int(m.get("symbol_rate", 200)),
        mark_hz=int(m.get("mark_hz", 1800)),
        space_hz=int(m.get("space_hz", 2400)),
        input_device=_device_value(a.get("input_device", "default")),
        output_device=_device_value(a.get("output_device", "default")),
        full_duplex_audio=a.get("full_duplex", False),
        record_seconds=float(a.get("record_seconds", 30)),
        gain=float(a.get("gain", 0.35)),
        max_payload=int(pr.get("max_payload", 1024)),
        ack_timeout_seconds=float(pr.get("ack_timeout_seconds", 15.0)),
        max_retries=int(pr.get("max_retries", 3)),
        listen_timeout_seconds=float(pr.get("listen_timeout_seconds", 120.0)),
        retry_listen_seconds=float(pr.get("retry_listen_seconds", 18.0)),
        fast_symbol_rate=int(m.get("fast_symbol_rate", 800)),
        voice_symbol_rate=int(m.get("voice_symbol_rate", 1200)),
        emergency_symbol_rate=int(m.get("emergency_symbol_rate", 1500)),
        ultrasonic_enabled=m.get("ultrasonic_enabled", False),
        ultrasonic_mark_hz=int(m.get("ultrasonic_mark_hz", 20700)),
        ultrasonic_space_hz=int(m.get("ultrasonic_space_hz", 22200)),
        multi_worker_enabled=transfer.get("multi_worker_enabled", False),
        multi_worker_count=int(transfer.get("multi_worker_count", 4)),
        debug_enabled=raw.get("debug", {}).get("enabled", False),
    )
    _validate(c)
    return c


def _validate(c: Config):
    if not isinstance(c.debug_enabled, bool):
        raise ValueError("debug.enabled must be true or false")
    if not isinstance(c.full_duplex_audio, bool):
        raise ValueError("audio.full_duplex must be true or false")
    if not isinstance(c.ultrasonic_enabled, bool):
        raise ValueError("modem.ultrasonic_enabled must be true or false")
    if not isinstance(c.multi_worker_enabled, bool):
        raise ValueError("transfer.multi_worker_enabled must be true or false")
    if isinstance(c.multi_worker_count, bool) or not isinstance(c.multi_worker_count, int) or not 2 <= c.multi_worker_count <= 8:
        raise ValueError("transfer.multi_worker_count must be between 2 and 8")
    if (c.ultrasonic_mark_hz <= 20000 or c.ultrasonic_space_hz <= 20000
            or c.ultrasonic_mark_hz == c.ultrasonic_space_hz):
        raise ValueError("Ultrasonic modem tones must be distinct frequencies above 20000 Hz")
    if c.ultrasonic_enabled:
        # Keep the full ±600 Hz FHSS excursion above 20 kHz and leave a
        # 1 kHz guard below Nyquist to avoid operating on the filter edge.
        if min(c.ultrasonic_mark_hz, c.ultrasonic_space_hz) - 600 <= 20000:
            raise ValueError("Ultrasonic tones must remain above 20000 Hz across frequency hopping")
        if max(c.ultrasonic_mark_hz, c.ultrasonic_space_hz) + 600 >= c.sample_rate / 2 - 1000:
            raise ValueError("Ultrasonic mode needs a sample rate with at least 1 kHz Nyquist headroom")
    if c.sample_rate <= 0 or c.symbol_rate <= 0 or c.sample_rate % c.symbol_rate:
        raise ValueError("sample_rate must be positive and divisible by symbol_rate")
    if c.sample_rate // c.symbol_rate < 32:
        raise ValueError("At least 32 samples per symbol are required")
    for name, rate in (("fast_symbol_rate", c.fast_symbol_rate),
                       ("voice_symbol_rate", c.voice_symbol_rate),
                       ("emergency_symbol_rate", c.emergency_symbol_rate)):
        if rate <= 0 or c.sample_rate % rate or c.sample_rate // rate < 32:
            raise ValueError(f"{name} must divide sample_rate and leave at least 32 samples per symbol")
    if c.mark_hz == c.space_hz or min(c.mark_hz, c.space_hz) <= 0 or max(c.mark_hz, c.space_hz) >= c.sample_rate / 2:
        raise ValueError("Invalid modem frequencies")
    if not math.isfinite(c.gain) or not 0.05 <= c.gain <= 0.95:
        raise ValueError("gain must be finite and between 0.05 and 0.95")
    if not math.isfinite(c.record_seconds) or not 1 <= c.record_seconds <= 600:
        raise ValueError("record_seconds must be between 1 and 600")
    if not 122 <= c.max_payload <= 65535:
        raise ValueError("max_payload must be between 122 and 65535 bytes")
    for name, value in (("ack_timeout_seconds", c.ack_timeout_seconds),
                        ("listen_timeout_seconds", c.listen_timeout_seconds),
                        ("retry_listen_seconds", c.retry_listen_seconds)):
        if not math.isfinite(value) or not 2.0 <= value <= 600.0:
            raise ValueError(f"{name} must be finite and between 2 and 600 seconds")
    if not 0 <= c.max_retries <= 10:
        raise ValueError("max_retries must be between 0 and 10")


def _toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("TOML numeric values must be finite")
        return repr(value)
    raise TypeError(f"Unsupported TOML value type: {type(value).__name__}")


def save_setting(section: str, key: str, value):
    """Atomically update one known setting and validate the resulting config."""
    if section not in {"audio", "modem", "protocol", "transfer", "debug"}:
        raise ValueError("Unknown configuration section")
    raw = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    raw.setdefault(section, {})[key] = value
    text_parts = []
    for section_name in ("audio", "modem", "protocol", "transfer", "debug"):
        values = raw.get(section_name, {})
        if not values:
            continue
        text_parts.append(f"[{section_name}]")
        for name, item in values.items():
            text_parts.append(f"{name} = {_toml_value(item)}")
        text_parts.append("")
    rendered = "\n".join(text_parts).rstrip() + "\n"
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    old = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=".default.", suffix=".toml", dir=CONFIG_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, CONFIG_PATH)
        try:
            load()
        except Exception:
            if old is None:
                CONFIG_PATH.unlink(missing_ok=True)
            else:
                CONFIG_PATH.write_text(old, encoding="utf-8")
            raise
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
