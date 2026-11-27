from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile

import pytest

from scripts.package_release import (
    EXPECTED_TARGETS,
    ReleasePackageError,
    collect_release_assets,
    create_checksum_manifest,
    create_platform_archive,
    validate_release_asset_set,
)


def make_archive(path: Path, member: str = "ECHOCRYPT/PROGRAM") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(member, b"program")


def complete_release_tree(root: Path) -> list[Path]:
    archives = []
    for target in EXPECTED_TARGETS:
        archive = root / f"echocrypt-{target}" / f"echocrypt-{target}.zip"
        make_archive(archive, f"ECHOCRYPT-{target}/binary")
        archives.append(archive)
    (root / "RELEASE_NOTES.md").write_text("draft notes", encoding="utf-8")
    create_checksum_manifest(root)
    return archives


def test_platform_archive_contains_complete_package_tree(tmp_path: Path) -> None:
    package = tmp_path / "package"
    (package / "extensions").mkdir(parents=True)
    (package / "ECHOCRYPT").write_bytes(b"linux binary")
    (package / "README.md").write_text("package notes", encoding="utf-8")
    (package / "extensions" / "echocrypt-firefox.zip").write_bytes(b"extension archive")
    archive = create_platform_archive(package, "linux-x64", tmp_path / "release")
    assert archive.name == "echocrypt-linux-x64.zip"
    assert zipfile.is_zipfile(archive)
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.testzip() is None
        assert set(bundle.namelist()) == {
            "ECHOCRYPT-linux-x64/ECHOCRYPT",
            "ECHOCRYPT-linux-x64/README.md",
            "ECHOCRYPT-linux-x64/extensions/echocrypt-firefox.zip",
        }
        assert bundle.read("ECHOCRYPT-linux-x64/ECHOCRYPT") == b"linux binary"


def test_platform_archive_rejects_invalid_target_and_empty_package(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ReleasePackageError, match="Invalid platform target"):
        create_platform_archive(empty, "../windows", tmp_path / "out")
    with pytest.raises(ReleasePackageError, match="empty"):
        create_platform_archive(empty, "windows-x64", tmp_path / "out")


def test_platform_archive_rejects_missing_source_directory(tmp_path: Path) -> None:
    with pytest.raises(ReleasePackageError, match="does not exist"):
        create_platform_archive(tmp_path / "missing", "windows-x64", tmp_path / "out")


def test_checksum_manifest_hashes_platform_archives_recursively(tmp_path: Path) -> None:
    archives = complete_release_tree(tmp_path)
    manifest = tmp_path / "SHA256SUMS"
    create_checksum_manifest(tmp_path, manifest)
    lines = manifest.read_text(encoding="ascii").splitlines()
    records = {name: digest for digest, name in (line.split("  ", 1) for line in lines)}
    assert len(records) == 4
    assert set(records) == {archive.name for archive in archives}
    for archive in archives:
        expected = hashlib.sha256(archive.read_bytes()).hexdigest()
        assert records[archive.name] == expected


def test_checksum_manifest_rejects_missing_or_invalid_archives(tmp_path: Path) -> None:
    with pytest.raises(ReleasePackageError, match="No platform ZIP"):
        create_checksum_manifest(tmp_path)
    bad = tmp_path / "echocrypt-linux-x64.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(ReleasePackageError, match="not a valid ZIP"):
        create_checksum_manifest(tmp_path)


def test_release_asset_discovery_never_returns_directories(tmp_path: Path) -> None:
    folder = tmp_path / "echocrypt-linux-x64"
    (folder / "extensions").mkdir(parents=True)
    archive = folder / "echocrypt-linux-x64.zip"
    make_archive(archive)
    (folder / "extensions" / "echocrypt-extension.zip").write_bytes(b"inner")
    (tmp_path / "RELEASE_NOTES.md").write_text("notes", encoding="utf-8")
    (tmp_path / "SHA256SUMS").write_text("checksum", encoding="ascii")
    assets = collect_release_assets(tmp_path)
    assert all(path.is_file() for path in assets)
    assert archive in assets
    assert (tmp_path / "RELEASE_NOTES.md") in assets
    assert (tmp_path / "SHA256SUMS") in assets


def test_release_validation_requires_exact_four_platform_archives(tmp_path: Path) -> None:
    complete_release_tree(tmp_path)
    assets = validate_release_asset_set(tmp_path)
    assert len(assets) == 6
    assert {item.name for item in assets} == {
        *(f"echocrypt-{target}.zip" for target in EXPECTED_TARGETS),
        "SHA256SUMS", "RELEASE_NOTES.md",
    }


def test_release_validation_rejects_missing_or_extra_archives(tmp_path: Path) -> None:
    complete_release_tree(tmp_path)
    (tmp_path / "echocrypt-linux-arm64.zip").write_bytes(b"extra")
    with pytest.raises(ReleasePackageError, match="Platform archive set mismatch"):
        validate_release_asset_set(tmp_path)


def test_release_validation_rejects_missing_metadata(tmp_path: Path) -> None:
    complete_release_tree(tmp_path)
    (tmp_path / "SHA256SUMS").unlink()
    with pytest.raises(ReleasePackageError, match="SHA256SUMS"):
        validate_release_asset_set(tmp_path)
