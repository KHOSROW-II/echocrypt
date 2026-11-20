import hashlib
import json
import os
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from security.identity import (
    create_private,
    export_public,
    fingerprint,
    load_private,
    load_public,
)


class IdentityKeyTests(unittest.TestCase):
    def test_private_key_is_created_owner_only_and_loadable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "keys" / "identity.ed25519"
            expected_fingerprint = create_private(path)
            self.assertEqual(len(path.read_bytes()), 32)
            self.assertEqual(fingerprint(load_private(path).public_key()), expected_fingerprint)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_private_key_creation_never_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "identity.ed25519"
            path.write_bytes(b"existing key material")
            with self.assertRaises(FileExistsError):
                create_private(path)
            self.assertEqual(path.read_bytes(), b"existing key material")

    def test_concurrent_private_key_creation_has_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "identity.ed25519"

            def attempt():
                try:
                    return create_private(path)
                except FileExistsError:
                    return None

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _index: attempt(), range(8)))

            winners = [value for value in results if value is not None]
            self.assertEqual(len(winners), 1)
            self.assertEqual(len(path.read_bytes()), 32)
            self.assertEqual(fingerprint(load_private(path).public_key()), winners[0])

    def test_public_identity_roundtrip_and_bad_key_length_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            private_path = Path(temp) / "identity.ed25519"
            public_path = Path(temp) / "identity.public.json"
            create_private(private_path)
            key = load_private(private_path)
            expected = fingerprint(key.public_key())
            self.assertEqual(export_public(key, public_path), expected)
            public_bytes, actual = load_public(public_path)
            self.assertEqual(len(public_bytes), 32)
            self.assertEqual(actual, expected)

            malformed = {
                "format": "ECHOCRYPT-IDENTITY-v1",
                "public_key": "00",
                "fingerprint": hashlib.sha256(b"\x00").hexdigest(),
            }
            public_path.write_text(json.dumps(malformed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exactly 32 bytes"):
                load_public(public_path)


if __name__ == "__main__":
    unittest.main()
