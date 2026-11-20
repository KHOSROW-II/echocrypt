"""Pinned acoustic peers. Only public identity documents are stored here."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from security.identity import load_public

ROOT = Path(__file__).resolve().parents[1]
CONTACTS_PATH = ROOT / "config" / "contacts.json"
PEERS_DIR = ROOT / "keys" / "peers"


@dataclass(frozen=True)
class Contact:
    fingerprint: str
    name: str
    identity_path: Path
    added_at: str = ""

    @property
    def short_fingerprint(self) -> str:
        return self.fingerprint[:12]


def _read_contacts() -> list[dict]:
    if not CONTACTS_PATH.exists():
        return []
    try:
        value = json.loads(CONTACTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Contacts database is unreadable: {exc}") from exc
    if not isinstance(value, list):
        raise ValueError("Contacts database must contain a list")
    return [x for x in value if isinstance(x, dict)]


def _write_contacts(entries: list[dict]) -> None:
    CONTACTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".contacts.", suffix=".json", dir=CONTACTS_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(entries, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, CONTACTS_PATH)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _safe_name(name: str, fingerprint: str) -> str:
    raw = str(name or "")
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in " _.-")
    cleaned = cleaned.strip(" .")[:32]
    return cleaned or f"Peer-{fingerprint[:6]}"


def list_contacts() -> list[Contact]:
    results = []
    for row in _read_contacts():
        fp = str(row.get("fingerprint", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", fp):
            continue
        path = PEERS_DIR / f"{fp}.json"
        if not path.is_file():
            continue
        try:
            _raw, actual = load_public(path)
        except Exception:
            continue
        if actual != fp:
            continue
        results.append(Contact(fp, _safe_name(row.get("name", ""), fp), path,
                               str(row.get("added_at", ""))))
    return sorted(results, key=lambda x: x.name.casefold())


def add_contact(identity_path: str | Path, name: str = "", confirmed_fingerprint: str | None = None) -> Contact:
    source = Path(identity_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Public identity file not found: {source}")
    public_raw, fingerprint = load_public(source)
    if len(public_raw) != 32:
        raise ValueError("Peer Ed25519 public key must contain exactly 32 bytes")
    if confirmed_fingerprint is not None and str(confirmed_fingerprint).lower() != fingerprint:
        raise ValueError("Fingerprint confirmation does not match the public identity")

    PEERS_DIR.mkdir(parents=True, exist_ok=True)
    target = PEERS_DIR / f"{fingerprint}.json"
    # Validate then copy only the public identity JSON; never copy private key material.
    document = json.loads(source.read_text(encoding="utf-8"))
    fd, temp_name = tempfile.mkstemp(prefix=".peer.", suffix=".json", dir=PEERS_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)

    entries = _read_contacts()
    safe_name = _safe_name(name, fingerprint)
    found = False
    for row in entries:
        if str(row.get("fingerprint", "")).lower() == fingerprint:
            row["name"] = safe_name
            row["added_at"] = row.get("added_at") or datetime.now(timezone.utc).isoformat()
            found = True
            break
    if not found:
        entries.append({"fingerprint": fingerprint, "name": safe_name,
                        "added_at": datetime.now(timezone.utc).isoformat()})
    _write_contacts(entries)
    return Contact(fingerprint, safe_name, target)


def remove_contact(fingerprint: str) -> bool:
    fingerprint = str(fingerprint).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("Invalid peer fingerprint")
    entries = _read_contacts()
    filtered = [row for row in entries if str(row.get("fingerprint", "")).lower() != fingerprint]
    removed = len(filtered) != len(entries)
    if removed:
        _write_contacts(filtered)
        # The peer JSON is public, but remove the managed copy to keep the store tidy.
        (PEERS_DIR / f"{fingerprint}.json").unlink(missing_ok=True)
    return removed
