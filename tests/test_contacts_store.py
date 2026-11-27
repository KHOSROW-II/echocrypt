from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from core import contacts


def _identity_document(public_bytes: bytes) -> dict:
    return {"public_key_hex": public_bytes.hex(), "kind": "public"}


def _write_identity(path: Path, public_bytes: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_identity_document(public_bytes)), encoding="utf-8")
    return hashlib.sha256(public_bytes).hexdigest()


@pytest.fixture
def isolated_contact_store(tmp_path, monkeypatch):
    contacts_path = tmp_path / "config" / "contacts.json"
    peers_dir = tmp_path / "keys" / "peers"
    monkeypatch.setattr(contacts, "CONTACTS_PATH", contacts_path)
    monkeypatch.setattr(contacts, "PEERS_DIR", peers_dir)

    # Keep the contact-store tests independent of the application's Ed25519
    # serialization format while still exercising the public-store logic.
    def fake_load_public(identity_path):
        document = json.loads(Path(identity_path).read_text(encoding="utf-8"))
        public_bytes = bytes.fromhex(document["public_key_hex"])
        return public_bytes, hashlib.sha256(public_bytes).hexdigest()

    monkeypatch.setattr(contacts, "load_public", fake_load_public)
    return contacts_path, peers_dir


def test_contacts_read_missing_store_as_empty(isolated_contact_store):
    assert contacts._read_contacts() == []


def test_contacts_read_rejects_corrupt_or_non_list_store(isolated_contact_store):
    path, _ = isolated_contact_store
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="Contacts database is unreadable"):
        contacts._read_contacts()

    path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a list"):
        contacts._read_contacts()


def test_contacts_read_filters_non_object_rows(isolated_contact_store):
    path, _ = isolated_contact_store
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([{"fingerprint": "a"}, 3, None, "bad"]), encoding="utf-8")
    assert contacts._read_contacts() == [{"fingerprint": "a"}]


def test_safe_contact_name_removes_unsafe_characters_and_uses_fallback():
    fp = "abcdef012345" + "0" * 52
    assert contacts._safe_name("  Alice/../<admin>  ", fp) == "Alice..admin"
    assert contacts._safe_name(" !!! ", fp) == "Peer-abcdef"
    assert len(contacts._safe_name("x" * 80, fp)) == 32


def test_add_contact_copies_public_document_and_persists_safe_name(isolated_contact_store, tmp_path):
    path, peers = isolated_contact_store
    public = bytes(range(32))
    source = tmp_path / "peer-identity.json"
    fingerprint = _write_identity(source, public)

    added = contacts.add_contact(source, " Alice / Test ", fingerprint.upper())

    assert added.fingerprint == fingerprint
    assert added.name == "Alice  Test"
    assert added.identity_path == peers / f"{fingerprint}.json"
    assert json.loads(added.identity_path.read_text(encoding="utf-8")) == _identity_document(public)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert len(saved) == 1
    assert saved[0]["fingerprint"] == fingerprint
    assert saved[0]["name"] == "Alice  Test"
    assert saved[0]["added_at"]
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_add_contact_rejects_missing_source_and_fingerprint_mismatch(isolated_contact_store, tmp_path):
    with pytest.raises(FileNotFoundError, match="Public identity file not found"):
        contacts.add_contact(tmp_path / "missing.json")

    public = b"p" * 32
    source = tmp_path / "peer.json"
    fingerprint = _write_identity(source, public)
    with pytest.raises(ValueError, match="Fingerprint confirmation does not match"):
        contacts.add_contact(source, "peer", "0" * 64)
    assert not isolated_contact_store[1].exists()


def test_add_contact_updates_existing_contact_without_resetting_added_at(isolated_contact_store, tmp_path):
    contacts_path, _ = isolated_contact_store
    public = b"q" * 32
    source = tmp_path / "peer.json"
    fingerprint = _write_identity(source, public)
    contacts.add_contact(source, "old name")
    rows = json.loads(contacts_path.read_text(encoding="utf-8"))
    old_date = rows[0]["added_at"]

    updated = contacts.add_contact(source, "new/name")
    rows = json.loads(contacts_path.read_text(encoding="utf-8"))

    assert updated.name == "newname"
    assert len(rows) == 1
    assert rows[0]["name"] == "newname"
    assert rows[0]["added_at"] == old_date
    assert rows[0]["fingerprint"] == fingerprint


def test_list_contacts_filters_invalid_missing_and_mismatched_peers(isolated_contact_store, tmp_path, monkeypatch):
    path, peers = isolated_contact_store
    fp_valid = hashlib.sha256(b"v" * 32).hexdigest()
    fp_mismatch = hashlib.sha256(b"m" * 32).hexdigest()
    fp_missing = hashlib.sha256(b"n" * 32).hexdigest()
    peers.mkdir(parents=True)
    (peers / f"{fp_valid}.json").write_text("{}", encoding="utf-8")
    (peers / f"{fp_mismatch}.json").write_text("{}", encoding="utf-8")
    (peers / "not-a-fingerprint.json").write_text("{}", encoding="utf-8")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([
        {"fingerprint": fp_valid, "name": "Zulu"},
        {"fingerprint": fp_mismatch, "name": "Wrong"},
        {"fingerprint": fp_missing, "name": "Missing"},
        {"fingerprint": "invalid", "name": "Invalid"},
    ]), encoding="utf-8")

    def fake_load_public(identity_path):
        if Path(identity_path).name == f"{fp_valid}.json":
            return b"v" * 32, fp_valid
        return b"m" * 32, "f" * 64

    monkeypatch.setattr(contacts, "load_public", fake_load_public)
    listed = contacts.list_contacts()
    assert [item.name for item in listed] == ["Zulu"]
    assert listed[0].short_fingerprint == fp_valid[:12]


def test_list_contacts_sorts_case_insensitively(isolated_contact_store, monkeypatch):
    path, peers = isolated_contact_store
    entries = []
    for name, raw in [("zeta", b"z" * 32), ("Alpha", b"a" * 32)]:
        fp = hashlib.sha256(raw).hexdigest()
        peers.mkdir(parents=True, exist_ok=True)
        (peers / f"{fp}.json").write_text("{}", encoding="utf-8")
        entries.append({"fingerprint": fp, "name": name})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")
    expected = {
        hashlib.sha256(b"a" * 32).hexdigest(): b"a" * 32,
        hashlib.sha256(b"z" * 32).hexdigest(): b"z" * 32,
    }
    monkeypatch.setattr(
        contacts, "load_public",
        lambda p: (expected[Path(p).stem], Path(p).stem),
    )
    assert [item.name for item in contacts.list_contacts()] == ["Alpha", "zeta"]


def test_remove_contact_validates_and_deletes_peer_and_store_entry(isolated_contact_store, tmp_path):
    path, peers = isolated_contact_store
    raw = b"r" * 32
    fp = hashlib.sha256(raw).hexdigest()
    contacts._write_contacts([{"fingerprint": fp, "name": "peer"}])
    peers.mkdir(parents=True)
    peer_file = peers / f"{fp}.json"
    peer_file.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid peer fingerprint"):
        contacts.remove_contact("not-a-fingerprint")
    assert contacts.remove_contact(fp.upper()) is True
    assert contacts._read_contacts() == []
    assert not peer_file.exists()
    assert contacts.remove_contact(fp) is False


def test_add_contact_rejects_public_key_with_wrong_length(isolated_contact_store, tmp_path, monkeypatch):
    source = tmp_path / "short-key.json"
    source.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(contacts, "load_public", lambda _path: (b"x" * 31, "a" * 64))
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        contacts.add_contact(source, "short")
    assert not isolated_contact_store[1].exists()


def test_add_contact_appends_when_existing_store_contains_other_peers(isolated_contact_store, tmp_path):
    contacts_path, _ = isolated_contact_store
    first_source = tmp_path / "first.json"
    second_source = tmp_path / "second.json"
    first_fp = _write_identity(first_source, b"1" * 32)
    second_fp = _write_identity(second_source, b"2" * 32)
    contacts.add_contact(first_source, "first")

    contacts.add_contact(second_source, "second")
    rows = json.loads(contacts_path.read_text(encoding="utf-8"))
    assert [row["fingerprint"] for row in rows] == [first_fp, second_fp]


def test_list_contacts_skips_peer_when_identity_reader_raises(isolated_contact_store, monkeypatch):
    path, peers = isolated_contact_store
    fingerprint = hashlib.sha256(b"e" * 32).hexdigest()
    peers.mkdir(parents=True)
    (peers / f"{fingerprint}.json").write_text("{}", encoding="utf-8")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([{"fingerprint": fingerprint, "name": "broken"}]), encoding="utf-8")

    def fail(_path):
        raise ValueError("invalid public identity")

    monkeypatch.setattr(contacts, "load_public", fail)
    assert contacts.list_contacts() == []


def test_contacts_database_temp_file_is_removed_when_replace_fails(isolated_contact_store, monkeypatch):
    path, _ = isolated_contact_store
    def fail_replace(_source, _target):
        raise OSError("simulated replace failure")
    monkeypatch.setattr(contacts.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        contacts._write_contacts([])
    assert not list(path.parent.glob(".contacts.*.json"))


def test_peer_temp_file_is_removed_when_replace_fails(isolated_contact_store, tmp_path, monkeypatch):
    _, peers = isolated_contact_store
    source = tmp_path / "peer.json"
    _write_identity(source, b"f" * 32)
    def fail_replace(_source, _target):
        raise OSError("simulated replace failure")
    monkeypatch.setattr(contacts.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        contacts.add_contact(source, "peer")
    assert not list(peers.glob(".peer.*.json"))
