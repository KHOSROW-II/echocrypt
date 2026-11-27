#!/usr/bin/env python3
"""Create deterministic, uniquely named ECHOCRYPT platform release archives."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import tempfile
import zipfile

TARGET_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
EXPECTED_TARGETS = ("linux-x64", "windows-x64", "macos-arm64", "macos-x64")
RELEASE_FILES = {"SHA256SUMS", "RELEASE_NOTES.md"}


class ReleasePackageError(ValueError):
    """Raised when release input files are incomplete or unsafe."""


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_checksum_manifest(
    directory: str | Path, output: str | Path | None = None
) -> Path:
    """Write SHA256SUMS for every platform ZIP found recursively in directory."""
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ReleasePackageError(f"Release asset directory does not exist: {root}")
    archives = sorted(
        path for path in root.rglob("echocrypt-*.zip")
        if path.is_file() and not path.is_symlink()
    )
    if not archives:
        raise ReleasePackageError("No platform ZIP archives were found")
    names = [path.name for path in archives]
    if len(names) != len(set(names)):
        raise ReleasePackageError("Platform archive filenames are not unique")
    for archive in archives:
        if not zipfile.is_zipfile(archive):
            raise ReleasePackageError(
                f"Platform archive is not a valid ZIP file: {archive}"
            )

    destination = Path(output).resolve() if output else root / "SHA256SUMS"
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{_sha256(path)}  {path.name}" for path in archives]
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


def collect_release_assets(directory: str | Path) -> list[Path]:
    """Return only release asset files; never return directories or symlinks."""
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ReleasePackageError(f"Release asset directory does not exist: {root}")
    selected = [
        path for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
        and (
            path.name.startswith("echocrypt-") and path.suffix == ".zip"
            or path.name in RELEASE_FILES
        )
    ]
    return sorted(selected)


def validate_release_asset_set(directory: str | Path) -> list[Path]:
    """Require four unique platform archives plus release notes and checksums."""
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ReleasePackageError(f"Release asset directory does not exist: {root}")
    expected_archives = {f"echocrypt-{target}.zip" for target in EXPECTED_TARGETS}
    archives = [
        path for path in root.rglob("echocrypt-*.zip")
        if path.is_file() and not path.is_symlink()
    ]
    names = [path.name for path in archives]
    if len(names) != len(set(names)):
        raise ReleasePackageError("Platform archive filenames are not unique")
    actual_archives = set(names)
    if actual_archives != expected_archives:
        missing = sorted(expected_archives - actual_archives)
        extra = sorted(actual_archives - expected_archives)
        raise ReleasePackageError(
            f"Platform archive set mismatch; missing={missing}, extra={extra}"
        )
    for archive in archives:
        if not zipfile.is_zipfile(archive):
            raise ReleasePackageError(
                f"Platform archive is not a valid ZIP file: {archive}"
            )
    for required in RELEASE_FILES:
        if not (root / required).is_file():
            raise ReleasePackageError(f"Required release file is missing: {required}")
    assets = collect_release_assets(root)
    expected_names = expected_archives | RELEASE_FILES
    if len(assets) != 6 or {asset.name for asset in assets} != expected_names:
        raise ReleasePackageError("Release upload must contain exactly six regular files")
    return assets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    archive_parser = commands.add_parser("archive", help="Create one platform ZIP archive")
    archive_parser.add_argument("--source", required=True)
    archive_parser.add_argument("--target", required=True)
    archive_parser.add_argument("--output-dir", required=True)
    checksum_parser = commands.add_parser("checksums", help="Create SHA256SUMS")
    checksum_parser.add_argument("--directory", required=True)
    checksum_parser.add_argument("--output")
    validate_parser = commands.add_parser(
        "validate", help="Validate the exact set of Draft release files"
    )
    validate_parser.add_argument("--directory", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "archive":
            result = create_platform_archive(args.source, args.target, args.output_dir)
            print(result)
        elif args.command == "checksums":
            result = create_checksum_manifest(args.directory, args.output)
            print(result)
        else:
            for asset in validate_release_asset_set(args.directory):
                print(asset)
    except (OSError, ReleasePackageError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
