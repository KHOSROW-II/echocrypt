"""Ed25519 identity key creation, export, import, and fingerprint helpers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


IDENTITY_FORMAT = "ECHOCRYPT-IDENTITY-v1"
PRIVATE_KEY_BYTES = 32


def raw_public(key) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def fingerprint(key) -> str:
    return hashlib.sha256(raw_public(key)).hexdigest()


def create_private(path) -> str:
    """Create a new private key without overwriting an existing path.

    O_EXCL makes the existence check and file creation one atomic operation.
    The file is created with owner-only permissions rather than being chmod'd
    only after the private bytes have already been written.
    """
    private_path = Path(path)
    private_path.parent.mkdir(parents=True, exist_ok=True)

    key = Ed25519PrivateKey.generate()
    private_bytes = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(private_path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        remaining = memoryview(private_bytes)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("Could not write the complete private identity key")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            private_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    else:
        os.close(descriptor)

    return fingerprint(key.public_key())


def load_private(path) -> Ed25519PrivateKey:
    private_bytes = Path(path).read_bytes()
    if len(private_bytes) != PRIVATE_KEY_BYTES:
        raise ValueError("Invalid private key")
    return Ed25519PrivateKey.from_private_bytes(private_bytes)


def export_public(key, path) -> str:
    public_bytes = raw_public(key.public_key())
    result = {
        "format": IDENTITY_FORMAT,
        "public_key": public_bytes.hex(),
        "fingerprint": hashlib.sha256(public_bytes).hexdigest(),
    }
    Path(path).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result["fingerprint"]


def load_public(path) -> tuple[bytes, str]:
    identity = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(identity, dict) or identity.get("format") != IDENTITY_FORMAT:
        raise ValueError("Unsupported identity format")
    try:
        public_bytes = bytes.fromhex(identity["public_key"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Malformed public identity key") from exc
    if len(public_bytes) != PRIVATE_KEY_BYTES:
        raise ValueError("Ed25519 public key must be exactly 32 bytes")
    actual_fingerprint = hashlib.sha256(public_bytes).hexdigest()
    supplied_fingerprint = identity.get("fingerprint")
    if not isinstance(supplied_fingerprint, str) or actual_fingerprint != supplied_fingerprint.lower():
        raise ValueError("Fingerprint mismatch")
    return public_bytes, actual_fingerprint
