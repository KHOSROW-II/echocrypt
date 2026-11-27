#!/usr/bin/env python3
"""Build native installable packages from an already-built ECHOCRYPT bundle."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib


TARGETS = {
    "linux-x64": {"family": "linux", "arch": "amd64"},
    "linux-arm64": {"family": "linux", "arch": "arm64"},
    "windows-x64": {"family": "windows", "arch": "amd64", "msi_arch": "x64"},
    "windows-arm64": {"family": "windows", "arch": "arm64", "msi_arch": "arm64"},
    "macos-x64": {"family": "macos"},
    "macos-arm64": {"family": "macos"},
}


class InstallerBuildError(RuntimeError):
    """Raised when a native installer cannot be assembled or verified."""


def project_version(project_file: str | Path | None = None) -> str:
    path = Path(project_file) if project_file else Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    version = str(data["project"]["version"])
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise InstallerBuildError(f"Installer version must be numeric semver, got {version!r}")
    return version


def _required_files(package_dir: Path, target: str) -> dict[str, Path]:
    windows = target.startswith("windows-")
    cli = package_dir / ("ECHOCRYPT.exe" if windows else "ECHOCRYPT")
    native_host = package_dir / ("ECHOCRYPT-NativeHost.exe" if windows else "ECHOCRYPT-NativeHost")
    required = {
        "cli": cli,
        "native_host": native_host,
        "extensions": package_dir / "extensions",
        "scripts": package_dir / "scripts",
        "docs": package_dir / "docs",
        "readme": package_dir / "README.md",
        "changelog": package_dir / "CHANGELOG.md",
        "license": package_dir / "LICENSE",
    }
    missing = [str(path) for path in required.values() if not path.exists()]
    if missing:
        raise InstallerBuildError("Platform bundle is incomplete: " + ", ".join(missing))
    return required


def _tree(source: Path, destination: str) -> dict[str, str]:
    return {"src": str(source.resolve()) + os.sep, "dst": destination, "type": "tree"}


def create_nfpm_config(
    package_dir: str | Path,
    target: str,
    build_id: str,
    version: str | None = None,
) -> dict:
    """Create an nFPM config for Linux DEB/RPM or Windows MSI packages."""
    if target not in TARGETS or TARGETS[target]["family"] == "macos":
        raise InstallerBuildError(f"nFPM does not package target {target!r}")
    root = Path(package_dir).resolve()
    files = _required_files(root, target)
    base_version = version or project_version()
    short_build = re.sub(r"[^0-9A-Za-z]", "", build_id)[:12].lower() or "local"
    config = {
        "name": "echocrypt",
        "arch": TARGETS[target]["arch"],
        "version": base_version,
        "version_metadata": f"g{short_build}",
        "maintainer": "KHOSROW-II",
        "vendor": "KHOSROW-II",
        "description": "ECHOCRYPT encrypted acoustic communication CLI and browser native host",
        "homepage": "https://github.com/KHOSROW-II/echocrypt",
        "license": "MIT",
        "contents": [],
    }

    if TARGETS[target]["family"] == "linux":
        config.update({
            "platform": "linux",
            "section": "net",
            "priority": "optional",
            "depends": ["libportaudio2"],
            "overrides": {"rpm": {"depends": ["portaudio"]}},
            "contents": [
                {"src": str(files["cli"]), "dst": "/usr/bin/echocrypt"},
                {"src": str(files["native_host"]), "dst": "/usr/lib/echocrypt/ECHOCRYPT-NativeHost"},
                _tree(files["extensions"], "/usr/share/echocrypt/extensions"),
                _tree(files["scripts"], "/usr/share/echocrypt/scripts"),
                _tree(files["docs"], "/usr/share/doc/echocrypt/reference"),
                {"src": str(files["readme"]), "dst": "/usr/share/doc/echocrypt/README.md"},
                {"src": str(files["changelog"]), "dst": "/usr/share/doc/echocrypt/CHANGELOG.md"},
                {"src": str(files["license"]), "dst": "/usr/share/licenses/echocrypt/LICENSE", "type": "license"},
            ],
        })
    else:
        install_root = "/Program Files/ECHOCRYPT"
        config["contents"] = [
            {"src": str(files["cli"]), "dst": f"{install_root}/ECHOCRYPT.exe"},
            {"src": str(files["native_host"]), "dst": f"{install_root}/ECHOCRYPT-NativeHost.exe"},
            _tree(files["extensions"], f"{install_root}/extensions"),
            _tree(files["scripts"], f"{install_root}/scripts"),
            _tree(files["docs"], f"{install_root}/docs"),
            {"src": str(files["readme"]), "dst": f"{install_root}/README.md"},
            {"src": str(files["changelog"]), "dst": f"{install_root}/CHANGELOG.md"},
            {"src": str(files["license"]), "dst": f"{install_root}/LICENSE", "type": "license"},
        ]
        config["msi"] = {
            "arch": TARGETS[target]["msi_arch"],
            "version": base_version,
            "product_name": "ECHOCRYPT",
            "manufacturer": "KHOSROW-II",
            "install_dir": "ECHOCRYPT",
            "minimal_ui": True,
            "upgrade": {"allow_same_version": True},
            "shortcuts": [
                {
                    "name": "ECHOCRYPT Console",
                    "target": f"{install_root}/ECHOCRYPT.exe",
                    "directory": "ProgramMenuFolder",
                    "description": "Launch ECHOCRYPT terminal application",
                }
            ],
        }
    return config


def _check_magic(path: Path, extension: str) -> None:
    magic = {
        ".deb": b"!<arch>\n",
        ".rpm": b"\xed\xab\xee\xdb",
        ".msi": bytes.fromhex("D0CF11E0A1B11AE1"),
        ".pkg": b"xar!",
    }[extension]
    try:
        with path.open("rb") as stream:
            actual = stream.read(len(magic))
    except OSError as exc:
        raise InstallerBuildError(f"Installer was not created: {path}") from exc
    if actual != magic:
        raise InstallerBuildError(f"{path.name} is not a valid {extension} package (bad file signature)")


def build_nfpm_installers(
    package_dir: str | Path,
    target: str,
    output_dir: str | Path,
    build_id: str,
    version: str | None = None,
) -> list[Path]:
    config = create_nfpm_config(package_dir, target, build_id, version)
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    formats = ("deb", "rpm") if TARGETS[target]["family"] == "linux" else ("msi",)
    results = []
    with tempfile.TemporaryDirectory(prefix="echocrypt-nfpm-") as temporary:
        config_path = Path(temporary) / "nfpm.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        for packager in formats:
            destination = output / f"echocrypt-{target}.{packager}"
            try:
                subprocess.run(
                    ["nfpm", "package", "--config", str(config_path),
                     "--packager", packager, "--target", str(destination)],
                    check=True,
                )
            except FileNotFoundError as exc:
                raise InstallerBuildError("nFPM is required; install github.com/goreleaser/nfpm/v2/cmd/nfpm") from exc
            except subprocess.CalledProcessError as exc:
                raise InstallerBuildError(f"nFPM failed while building {destination.name}") from exc
            _check_magic(destination, f".{packager}")
            results.append(destination)
    return results


def build_macos_pkg(
    package_dir: str | Path,
    target: str,
    output_dir: str | Path,
    version: str | None = None,
) -> Path:
    if target not in {"macos-x64", "macos-arm64"}:
        raise InstallerBuildError(f"Not a macOS target: {target}")
    root = Path(package_dir).resolve()
    files = _required_files(root, target)
    base_version = version or project_version()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"echocrypt-{target}.pkg"

    with tempfile.TemporaryDirectory(prefix="echocrypt-pkgroot-") as temporary:
        stage = Path(temporary)
        bin_dir = stage / "usr" / "local" / "bin"
        lib_dir = stage / "usr" / "local" / "lib" / "echocrypt"
        share_dir = stage / "usr" / "local" / "share" / "echocrypt"
        doc_dir = stage / "usr" / "local" / "share" / "doc" / "echocrypt"
        license_dir = stage / "usr" / "local" / "share" / "licenses" / "echocrypt"
        for folder in (bin_dir, lib_dir, share_dir, doc_dir, license_dir):
            folder.mkdir(parents=True, exist_ok=True)

        shutil.copy2(files["cli"], bin_dir / "echocrypt")
        shutil.copy2(files["native_host"], lib_dir / "ECHOCRYPT-NativeHost")
        shutil.copytree(files["extensions"], share_dir / "extensions")
        shutil.copytree(files["scripts"], share_dir / "scripts")
        shutil.copytree(files["docs"], doc_dir / "reference")
        shutil.copy2(files["readme"], doc_dir / "README.md")
        shutil.copy2(files["changelog"], doc_dir / "CHANGELOG.md")
        shutil.copy2(files["license"], license_dir / "LICENSE")

        os.chmod(bin_dir / "echocrypt", 0o755)
        os.chmod(lib_dir / "ECHOCRYPT-NativeHost", 0o755)
        try:
            subprocess.run(
                ["pkgbuild", "--root", str(stage), "--identifier",
                 "org.echocrypt.cli", "--version", base_version,
                 "--install-location", "/", str(destination)],
                check=True,
            )
        except FileNotFoundError as exc:
            raise InstallerBuildError("pkgbuild is only available on a macOS build runner") from exc
        except subprocess.CalledProcessError as exc:
            raise InstallerBuildError(f"pkgbuild failed while building {destination.name}") from exc

    _check_magic(destination, ".pkg")
    return destination


def build_installers(
    package_dir: str | Path,
    target: str,
    output_dir: str | Path,
    build_id: str = "local",
) -> list[Path]:
    if target not in TARGETS:
        raise InstallerBuildError(f"Unsupported release target: {target}")
    if TARGETS[target]["family"] == "macos":
        return [build_macos_pkg(package_dir, target, output_dir)]
    return build_nfpm_installers(package_dir, target, output_dir, build_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Complete platform bundle directory")
    parser.add_argument("--target", required=True, choices=sorted(TARGETS))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--build-id", default=os.environ.get("GITHUB_SHA", "local"))
    args = parser.parse_args(argv)
    try:
        for artifact in build_installers(args.source, args.target, args.output_dir, args.build_id):
            print(artifact)
    except (OSError, InstallerBuildError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
