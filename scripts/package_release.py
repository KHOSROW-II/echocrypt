#!/usr/bin/env python3
"""Create and validate the complete ECHOCRYPT cross-platform release asset set."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import tempfile
import zipfile

TARGET_INSTALLERS = {
    "linux-x64": ("deb", "rpm"),
    "linux-arm64": ("deb", "rpm"),
    "windows-x64": ("msi",),
    "windows-arm64": ("msi",),
    "macos-arm64": ("pkg",),
    "macos-x64": ("pkg",),
}
EXPECTED_TARGETS = tuple(TARGET_INSTALLERS)
BROWSER_EXTENSION_TARGETS = ("chrome", "edge", "firefox")
RELEASE_FILES = {"SHA256SUMS", "RELEASE_NOTES.md"}
ASSET_EXTENSIONS = {".zip", ".deb", ".rpm", ".msi", ".pkg"}
TARGET_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
INSTALLER_MAGIC = {
    ".deb": b"!<arch>\n",
    ".rpm": b"\xed\xab\xee\xdb",
    ".msi": bytes.fromhex("D0CF11E0A1B11AE1"),
    ".pkg": b"xar!",
}


class ReleasePackageError(ValueError):
    """Raised when release input files are incomplete or unsafe."""


def expected_release_asset_names() -> tuple[str, ...]:
    names = {f"echocrypt-{target}.zip" for target in EXPECTED_TARGETS}
    names.update(
        f"echocrypt-{target}.{extension}"
        for target, extensions in TARGET_INSTALLERS.items()
        for extension in extensions
    )
    names.update(f"echocrypt-{browser}.zip" for browser in BROWSER_EXTENSION_TARGETS)
    names.update(RELEASE_FILES)
    return tuple(sorted(names))


def _expected_platform_asset_names() -> set[str]:
    return set(expected_release_asset_names()) - RELEASE_FILES


def create_platform_archive(source: str | Path, target: str, output_dir: str | Path) -> Path:
    """Archive a complete platform package into one unique ZIP file."""
    source_dir = Path(source).resolve()
    output_path = Path(output_dir).resolve()
    if not TARGET_RE.fullmatch(target):
        raise ReleasePackageError(f"Invalid platform target: {target!r}")
    if not source_dir.is_dir():
        raise ReleasePackageError(f"Platform package directory does not exist: {source_dir}")
    files = sorted(
        path for path in source_dir.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    if not files:
        raise ReleasePackageError(f"Platform package is empty: {source_dir}")

    output_path.mkdir(parents=True, exist_ok=True)
    archive = output_path / f"echocrypt-{target}.zip"
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{archive.name}.", suffix=".tmp", dir=output_path
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as bundle:
            prefix = f"ECHOCRYPT-{target}"
            for path in files:
                relative = path.relative_to(source_dir).as_posix()
                bundle.write(path, f"{prefix}/{relative}")
        with zipfile.ZipFile(temporary, "r") as bundle:
            damaged_member = bundle.testzip()
            if damaged_member is not None:
                raise ReleasePackageError(
                    f"Invalid ZIP member after packaging: {damaged_member}"
                )
        os.replace(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    return archive


def _release_candidate(path: Path, root: Path) -> bool:
    if not path.is_file() or path.is_symlink() or path.suffix.lower() not in ASSET_EXTENSIONS:
        return False
    if not path.name.startswith("echocrypt-"):
        return False
    # Artifacts are downloaded into one directory per target. This excludes
    # browser extension ZIPs nested inside a package tree during unit tests.
    return path.parent == root or path.parent.name == path.stem


def collect_release_assets(directory: str | Path) -> list[Path]:
    """Return only release asset files; never return directories or symlinks."""
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ReleasePackageError(f"Release asset directory does not exist: {root}")
    selected = [
        path for path in root.rglob("*")
        if _release_candidate(path, root) or (
            path.parent == root and path.is_file() and path.name in RELEASE_FILES
        )
    ]
    return sorted(selected)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_asset_file(path: Path) -> None:
    extension = path.suffix.lower()
    if extension == ".zip":
        if not zipfile.is_zipfile(path):
            raise ReleasePackageError(f"Platform archive is not a valid ZIP file: {path}")
        with zipfile.ZipFile(path, "r") as bundle:
            damaged_member = bundle.testzip()
        if damaged_member is not None:
            raise ReleasePackageError(f"Invalid ZIP member in {path.name}: {damaged_member}")
        return
    expected_magic = INSTALLER_MAGIC.get(extension)
    if expected_magic is None:
        raise ReleasePackageError(f"Unsupported release asset format: {path.name}")
    with path.open("rb") as stream:
        actual_magic = stream.read(len(expected_magic))
    if actual_magic != expected_magic:
        raise ReleasePackageError(
            f"{path.name} is not a valid {extension} installer (bad file signature)"
        )


def create_checksum_manifest(
    directory: str | Path, output: str | Path | None = None
) -> Path:
    """Write SHA256SUMS for every desktop package and standalone browser extension."""
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ReleasePackageError(f"Release asset directory does not exist: {root}")
    assets = [path for path in collect_release_assets(root) if path.name not in RELEASE_FILES]
    if not assets:
        raise ReleasePackageError("No platform packages were found")
    names = [path.name for path in assets]
    if len(names) != len(set(names)):
        raise ReleasePackageError("Release asset filenames are not unique")
    for asset in assets:
        _validate_asset_file(asset)

    destination = Path(output).resolve() if output else root / "SHA256SUMS"
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{_sha256(path)}  {path.name}" for path in assets]
    content = "\n".join(lines) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _validate_checksum_manifest(root: Path, platform_names: set[str]) -> None:
    manifest = root / "SHA256SUMS"
    if not manifest.is_file():
        raise ReleasePackageError("Required release file is missing: SHA256SUMS")
    try:
        records = {}
        for line in manifest.read_text(encoding="ascii").splitlines():
            digest, name = line.split("  ", 1)
            if not re.fullmatch(r"[0-9a-f]{64}", digest) or name in records:
                raise ValueError("invalid checksum record")
            records[name] = digest
    except (OSError, UnicodeError, ValueError) as exc:
        raise ReleasePackageError("SHA256SUMS is malformed") from exc
    if set(records) != platform_names:
        raise ReleasePackageError("SHA256SUMS does not list the exact release package set")
    assets = {
        path.name: path
        for path in collect_release_assets(root)
        if path.name in platform_names
    }
    for name, digest in records.items():
        if _sha256(assets[name]) != digest:
            raise ReleasePackageError(f"SHA256SUMS mismatch for {name}")


def validate_release_asset_set(directory: str | Path) -> list[Path]:
    """Require all desktop packages, browser extensions, notes, and checksums."""
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ReleasePackageError(f"Release asset directory does not exist: {root}")

    expected = set(expected_release_asset_names())
    assets = collect_release_assets(root)
    names = [path.name for path in assets]
    if len(names) != len(set(names)):
        raise ReleasePackageError("Release asset filenames are not unique")
    actual = set(names)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ReleasePackageError(
            f"Release asset set mismatch; missing={missing}, extra={extra}"
        )

    platform_names = _expected_platform_asset_names()
    for asset in assets:
        if asset.name in platform_names:
            _validate_asset_file(asset)
    if not (root / "RELEASE_NOTES.md").is_file():
        raise ReleasePackageError("Required release file is missing: RELEASE_NOTES.md")
    _validate_checksum_manifest(root, platform_names)
    return assets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    archive_parser = commands.add_parser("archive", help="Create one portable platform ZIP")
    archive_parser.add_argument("--source", required=True)
    archive_parser.add_argument("--target", required=True)
    archive_parser.add_argument("--output-dir", required=True)

    checksum_parser = commands.add_parser("checksums", help="Create SHA256SUMS")
    checksum_parser.add_argument("--directory", required=True)
    checksum_parser.add_argument("--output")

    validate_parser = commands.add_parser("validate", help="Validate the complete Draft release set")
    validate_parser.add_argument("--directory", required=True)

    commands.add_parser("expected", help="Print the exact expected Draft release asset names")

    args = parser.parse_args(argv)
    try:
        if args.command == "archive":
            result = create_platform_archive(args.source, args.target, args.output_dir)
            print(result)
        elif args.command == "checksums":
            result = create_checksum_manifest(args.directory, args.output)
            print(result)
        elif args.command == "expected":
            for name in expected_release_asset_names():
                print(name)
        else:
            for asset in validate_release_asset_set(args.directory):
                print(asset)
    except (OSError, ReleasePackageError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
