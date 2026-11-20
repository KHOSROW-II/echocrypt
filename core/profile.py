"""Persistent, non-secret local profile settings."""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "config" / "profile.json"
_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,23}$")


def _write_profile(data: dict) -> None:
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".profile.", suffix=".json", dir=PROFILE_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, PROFILE_PATH)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _load_profile() -> dict:
    if not PROFILE_PATH.exists():
        return {}
    try:
        data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def get_username() -> str:
    """Return the configured name or create a stable anon-NNN name once."""
    data = _load_profile()
    username = data.get("username")
    if isinstance(username, str) and _USERNAME_RE.fullmatch(username):
        return username
    username = f"anon-{secrets.randbelow(900) + 100}"
    data["username"] = username
    _write_profile(data)
    return username


def set_username(username: str) -> str:
    username = str(username).strip()
    if not _USERNAME_RE.fullmatch(username):
        raise ValueError("Username must be 2-24 characters: letters, numbers, '_' or '-'.")
    data = _load_profile()
    data["username"] = username
    _write_profile(data)
    return username
