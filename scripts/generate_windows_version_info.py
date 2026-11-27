#!/usr/bin/env python3
"""Generate a PyInstaller PE version resource for ECHOCRYPT Windows executables."""
from __future__ import annotations

import argparse
from pathlib import Path
import re

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 support
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
EXECUTABLES = {
    "ECHOCRYPT": "ECHOCRYPT command-line application",
    "ECHOCRYPT-NativeHost": "ECHOCRYPT browser native messaging host",
}


def project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(data["project"]["version"])
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"Windows file version requires numeric semver, got {version!r}")
    parts = tuple(int(part) for part in version.split("."))
    if any(part > 65535 for part in parts):
        raise ValueError(f"Windows version components must be <= 65535, got {version!r}")
    return version


def render_version_info(name: str, version: str | None = None) -> str:
    """Render a valid PyInstaller version-file source for one executable."""
    if name not in EXECUTABLES:
        raise ValueError(f"Unsupported Windows executable name: {name!r}")
    selected_version = version or project_version()
    if not re.fullmatch(r"\d+\.\d+\.\d+", selected_version):
        raise ValueError(f"Windows file version requires numeric semver, got {selected_version!r}")
    parts = tuple(int(part) for part in selected_version.split("."))
    if any(part > 65535 for part in parts):
        raise ValueError(f"Windows version components must be <= 65535, got {selected_version!r}")
    fixed_version = (*parts, 0)
    display_version = ".".join(str(part) for part in fixed_version)
    description = EXECUTABLES[name]
    return f'''# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={fixed_version!r},
    prodvers={fixed_version!r},
    mask=0x3f,
    flags=0x0,
    OS=0x4,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'KHOSROW-II'),
          StringStruct('FileDescription', {description!r}),
          StringStruct('FileVersion', {display_version!r}),
          StringStruct('InternalName', {name!r}),
          StringStruct('LegalCopyright', 'Copyright (c) KHOSROW-II'),
          StringStruct('OriginalFilename', {f"{name}.exe"!r}),
          StringStruct('ProductName', 'ECHOCRYPT'),
          StringStruct('ProductVersion', {display_version!r})
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
'''


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, choices=sorted(EXECUTABLES))
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(render_version_info(args.name), encoding="utf-8")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
