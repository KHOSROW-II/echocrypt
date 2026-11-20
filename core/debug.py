"""Opt-in acoustic receive diagnostics. Never logs plaintext or key material."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import threading


class AcousticDebug:
    def __init__(self, enabled: bool = False, log_path: str | Path | None = None):
        self.enabled = bool(enabled)
        self.log_path = Path(log_path) if log_path else Path(__file__).resolve().parents[1] / "logs" / "acoustic-debug.log"
        self._lock = threading.Lock()
        self._last_stage = None

    def emit(self, stage: str, message: str, **fields):
        if not self.enabled:
            return
        timestamp = datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3]
        suffix = " ".join(f"{key}={value}" for key, value in fields.items())
        line = f"[{timestamp}] [ACOUSTIC:{stage.upper()}] {message}"
        if stage.upper() == "LEVEL" and "rms" in fields:
            try:
                import math
                rms = max(float(fields["rms"]), 1e-7)
                dbfs = 20.0 * math.log10(rms)
                percent = max(0, min(100, int((dbfs + 60.0) / 60.0 * 100)))
                filled = round(percent / 5)
                ratio = float(fields.get("tone_ratio", 0.0))
                state = "TONE BAND" if ratio >= 0.12 else "NO MODEM TONE"
                line += f" | MIC [{'█' * filled}{'·' * (20 - filled)}] {percent:3d}% {dbfs:5.1f} dBFS | {state}"
            except (TypeError, ValueError):
                pass
        if suffix:
            line += " | " + suffix
        # Diagnostics are deliberately file-only.  Printing here corrupts the
        # prompt_toolkit full-screen chat by writing below its managed layout.
        # The user can inspect the persisted trace from Settings -> View latest
        # acoustic debug log, but no live trace is ever streamed to the chat UI.
        with self._lock:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                if self.log_path.exists() and self.log_path.stat().st_size > 1_000_000:
                    rotated = self.log_path.with_suffix(".log.1")
                    rotated.unlink(missing_ok=True)
                    self.log_path.replace(rotated)
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                # Never fall back to stdout: diagnostics must not leak into the
                # full-screen chat. The primary application logger can report
                # filesystem failures outside the chat lifecycle if needed.
                pass

    def __call__(self, stage: str, message: str, **fields):
        self.emit(stage, message, **fields)


def for_config(cfg) -> AcousticDebug:
    return AcousticDebug(bool(getattr(cfg, "debug_enabled", False)))
