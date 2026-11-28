from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile

import pytest

from scripts.generate_changelog import render_download_table, render_release_notes
from scripts.package_release import (
    EXPECTED_TARGETS,
    INSTALLER_MAGIC,
    TARGET_INSTALLERS,
    ReleasePackageError,
    collect_release_assets,
    create_checksum_manifest,
    create_platform_archive,
    expected_release_asset_names,
    validate_release_asset_set,
)


def make_archive(path: Path, member: str = "ECHOCRYPT/PROGRAM") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(member, b"program")


def complete_release_tree(root: Path) -> list[Path]:
    artifacts = []
    for target in EXPECTED_TARGETS:
        target_dir = root / f"echocrypt-{target}"
        archive = target_dir / f"echocrypt-{target}.zip"
        make_archive(archive, f"ECHOCRYPT-{target}/binary")
        artifacts.append(archive)
        for extension in TARGET_INSTALLERS[target]:
            installer = target_dir / f"echocrypt-{target}.{extension}"
            installer.write_bytes(INSTALLER_MAGIC[f".{extension}"] + b"installer-fixture")
            artifacts.append(installer)
    android_dir = root / "echocrypt-android"
    android_apk = android_dir / "echocrypt-android.apk"
    make_archive(android_apk, "AndroidManifest.xml")
    artifacts.append(android_apk)
    for browser in ("chrome", "edge", "firefox"):
        extension_dir = root / f"echocrypt-{browser}"
        extension = extension_dir / f"echocrypt-{browser}.zip"
        make_archive(extension, "echocrypt/manifest.json")
        artifacts.append(extension)
    (root / "RELEASE_NOTES.md").write_text("draft notes", encoding="utf-8")
    create_checksum_manifest(root)
    return artifacts


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


def test_checksum_manifest_hashes_every_portable_archive_and_installer(tmp_path: Path) -> None:
    artifacts = complete_release_tree(tmp_path)
    manifest = tmp_path / "SHA256SUMS"
    create_checksum_manifest(tmp_path, manifest)
    lines = manifest.read_text(encoding="ascii").splitlines()
    records = {name: digest for digest, name in (line.split("  ", 1) for line in lines)}
    assert len(records) == 18
    assert set(records) == {artifact.name for artifact in artifacts}
    for artifact in artifacts:
        expected = hashlib.sha256(artifact.read_bytes()).hexdigest()
        assert records[artifact.name] == expected


def test_checksum_manifest_rejects_missing_or_invalid_archives(tmp_path: Path) -> None:
    with pytest.raises(ReleasePackageError, match="No platform packages"):
        create_checksum_manifest(tmp_path)
    bad = tmp_path / "echocrypt-linux-x64"
    bad.mkdir()
    (bad / "echocrypt-linux-x64.zip").write_bytes(b"not a zip")
    with pytest.raises(ReleasePackageError, match="not a valid ZIP"):
        create_checksum_manifest(tmp_path)


def test_release_asset_discovery_excludes_nested_browser_extensions(tmp_path: Path) -> None:
    folder = tmp_path / "echocrypt-linux-x64"
    (folder / "extensions").mkdir(parents=True)
    archive = folder / "echocrypt-linux-x64.zip"
    make_archive(archive)
    (folder / "extensions" / "echocrypt-firefox.zip").write_bytes(b"inner")
    (tmp_path / "RELEASE_NOTES.md").write_text("notes", encoding="utf-8")
    (tmp_path / "SHA256SUMS").write_text("checksum", encoding="ascii")
    assets = collect_release_assets(tmp_path)
    assert all(path.is_file() for path in assets)
    assert archive in assets
    assert (tmp_path / "RELEASE_NOTES.md") in assets
    assert (tmp_path / "SHA256SUMS") in assets
    assert not any(path.name == "echocrypt-firefox.zip" for path in assets)


def test_release_validation_requires_exact_six_architectures_and_installers(tmp_path: Path) -> None:
    complete_release_tree(tmp_path)
    assets = validate_release_asset_set(tmp_path)
    assert len(assets) == 20
    assert {item.name for item in assets} == set(expected_release_asset_names())
    assert "echocrypt-android.apk" in {item.name for item in assets}
    assert {"echocrypt-chrome.zip", "echocrypt-edge.zip", "echocrypt-firefox.zip"} <= {
        item.name for item in assets
    }


def test_release_validation_rejects_missing_or_extra_archives(tmp_path: Path) -> None:
    complete_release_tree(tmp_path)
    (tmp_path / "echocrypt-linux-i686.zip").write_bytes(b"extra")
    with pytest.raises(ReleasePackageError, match="Release asset set mismatch"):
        validate_release_asset_set(tmp_path)


def test_release_validation_rejects_missing_metadata(tmp_path: Path) -> None:
    complete_release_tree(tmp_path)
    (tmp_path / "SHA256SUMS").unlink()
    with pytest.raises(ReleasePackageError, match="SHA256SUMS"):
        validate_release_asset_set(tmp_path)


def test_release_validation_rejects_corrupt_installer_and_bad_checksum(tmp_path: Path) -> None:
    complete_release_tree(tmp_path)
    rpm = tmp_path / "echocrypt-linux-x64" / "echocrypt-linux-x64.rpm"
    rpm.write_bytes(b"not an rpm")
    with pytest.raises(ReleasePackageError, match="bad file signature"):
        validate_release_asset_set(tmp_path)

    rpm.write_bytes(INSTALLER_MAGIC[".rpm"] + b"restored")
    manifest = tmp_path / "SHA256SUMS"
    lines = manifest.read_text(encoding="ascii").splitlines()
    manifest.write_text(
        "\n".join(
            ("0" * 64 + "  " + rpm.name) if line.endswith("  " + rpm.name) else line
            for line in lines
        ) + "\n",
        encoding="ascii",
    )
    with pytest.raises(ReleasePackageError, match="SHA256SUMS mismatch"):
        validate_release_asset_set(tmp_path)

def test_android_debug_apk_is_listed_in_release_download_table() -> None:
    repository = "KHOSROW-II/echocrypt"
    tag = "nightly-20261128-test1234"
    table = render_download_table(repository, tag)

    expected_link = (
        "| Android | All supported | "
        "[Debug APK (unsigned)]"
        "(https://github.com/KHOSROW-II/echocrypt/releases/download/"
        "nightly-20261128-test1234/echocrypt-android.apk) | — |"
    )
    assert expected_link in table

    notes = render_release_notes(tag, repository, "Generated notes")
    assert "Android app is provided separately as an unsigned debug APK" in notes
    assert "Verify desktop packages, the Android APK, and browser extensions" in notes
    assert "not a production-signed build" in notes

def test_release_downloads_use_actual_github_asset_urls(tmp_path: Path) -> None:
    repository = "KHOSROW-II/echocrypt"
    tag = "nightly-20261128-test1234"
    apk_url = (
        "https://github.com/KHOSROW-II/echocrypt/releases/download/"
        "untagged-a1b2c3d4/echocrypt-android.apk"
    )
    assets = [
        {"name": "echocrypt-android.apk", "browser_download_url": apk_url},
        {
            "name": "echocrypt-linux-x64.deb",
            "browser_download_url": "https://github.com/KHOSROW-II/echocrypt/releases/download/untagged-a1b2c3d4/echocrypt-linux-x64.deb",
        },
        {
            "name": "echocrypt-linux-x64.rpm",
            "browser_download_url": "https://github.com/KHOSROW-II/echocrypt/releases/download/untagged-a1b2c3d4/echocrypt-linux-x64.rpm",
        },
        {
            "name": "echocrypt-linux-x64.zip",
            "browser_download_url": "https://github.com/KHOSROW-II/echocrypt/releases/download/untagged-a1b2c3d4/echocrypt-linux-x64.zip",
        },
        {
            "name": "echocrypt-linux-arm64.deb",
            "browser_download_url": "https://github.com/KHOSROW-II/echocrypt/releases/download/untagged-a1b2c3d4/echocrypt-linux-arm64.deb",
        },
        {
            "name": "echocrypt-linux-arm64.rpm",
            "browser_download_url": "https://github.com/KHOSROW-II/echocrypt/releases/download/untagged-a1b2c3d4/echocrypt-linux-arm64.rpm",
        },
        {
            "name": "echocrypt-linux-arm64.zip",
            "browser_download_url": "https://github.com/KHOSROW-II/echocrypt/releases/download/untagged-a1b2c3d4/echocrypt-linux-arm64.zip",
        },
        {
            "name": "echocrypt-windows-x64.msi",
            "browser_download_url": "https://github.com/KHOSROW-II/echocrypt/releases/download/untagged-a1b2c3d4/echocrypt-windows-x64.msi",
        },
        {
            "name": "echocrypt-windows-x64.zip",
            "browser_download_url": "https://github.com/KHOSROW-II/echocrypt/releases/download/untagged-a1b2c3d4/echocrypt-windows-x64.zip",
        },
        {
            "name": "echocrypt-windows-arm64.msi",
            "browser_download_url": "https://github.com/KHOSROW-II/echocrypt/releases/download/untagged-a1b2c3d4/echocrypt-windows-arm64.msi",
        },
        {
            "name": "echocrypt-windows-arm64.zip",
            "browser_download_url": "https://github.com/KHOSROW-II/echocrypt/releases/download/untagged-a1b2c3d4/echocrypt-windows-arm64.zip",
        },
        {
            "name": "echocrypt-macos-x64.pkg",
            "browser_download_url": "https://github.com/KHOSROW-II/echocrypt/releases/download/untagged-a1b2c3d4/echocrypt-macos-x64.pkg",
        },
        {
            "name": "echocrypt-macos-x64.zip",
            "browser_download_url": "https://github.com/KHOSROW-II/echocrypt/releases/download/untagged-a1b2c3d4/echocrypt-macos-x64.zip",
        },
        {
            "name": "echocrypt-macos-arm64.pkg",
            "browser_download_url": "https://github.com/KHOSROW-II/echocrypt/releases/download/untagged-a1b2c3d4/echocrypt-macos-arm64.pkg",
        },
        {
            "name": "echocrypt-macos-arm64.zip",
            "browser_download_url": "https://github.com/KHOSROW-II/echocrypt/releases/download/untagged-a1b2c3d4/echocrypt-macos-arm64.zip",
        },
        {
            "name": "echocrypt-chrome.zip",
            "browser_download_url": "https://github.com/KHOSROW-II/echocrypt/releases/download/untagged-a1b2c3d4/echocrypt-chrome.zip",
        },
        {
            "name": "echocrypt-edge.zip",
            "browser_download_url": "https://github.com/KHOSROW-II/echocrypt/releases/download/untagged-a1b2c3d4/echocrypt-edge.zip",
        },
        {
            "name": "echocrypt-firefox.zip",
            "browser_download_url": "https://github.com/KHOSROW-II/echocrypt/releases/download/untagged-a1b2c3d4/echocrypt-firefox.zip",
        },
        {
            "name": "SHA256SUMS",
            "browser_download_url": "https://github.com/KHOSROW-II/echocrypt/releases/download/untagged-a1b2c3d4/SHA256SUMS",
        },
    ]
    manifest = tmp_path / "release-assets.json"
    manifest.write_text(__import__("json").dumps({"assets": assets}), encoding="utf-8")
    from scripts.generate_changelog import load_release_asset_urls

    urls = load_release_asset_urls(manifest)
    table = render_download_table(repository, tag, urls)
    assert f"[Debug APK (unsigned)]({apk_url})" in table
    assert f"/download/{tag}/echocrypt-android.apk" not in table

    notes = render_release_notes(tag, repository, "Generated notes", asset_urls=urls)
    assert apk_url in notes


def test_asset_manifest_rejects_missing_or_foreign_asset_url(tmp_path: Path) -> None:
    repository = "KHOSROW-II/echocrypt"
    with pytest.raises(ValueError, match="missing"):
        render_download_table(repository, "nightly-test", {"echocrypt-android.apk": "https://github.com/KHOSROW-II/echocrypt/releases/download/untagged-x/echocrypt-android.apk"})

    with pytest.raises(ValueError, match="Invalid GitHub browser_download_url"):
        from scripts.generate_changelog import _download_url
        _download_url(repository, "nightly-test", "echocrypt-android.apk", {
            "echocrypt-android.apk": "https://evil.example/echocrypt-android.apk"
        })
