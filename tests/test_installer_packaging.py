from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import build_installers


INSTALLER_MAGIC = {
    ".deb": b"!<arch>\n",
    ".rpm": bytes.fromhex("ed ab ee db"),
    ".msi": bytes.fromhex("D0CF11E0A1B11AE1"),
    ".pkg": b"xar!",
}


def make_bundle(root: Path, target: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    windows = target.startswith("windows-")
    (root / ("ECHOCRYPT.exe" if windows else "ECHOCRYPT")).write_bytes(b"cli-binary")
    (root / ("ECHOCRYPT-NativeHost.exe" if windows else "ECHOCRYPT-NativeHost")).write_bytes(b"host-binary")
    for folder in ("extensions", "scripts", "docs"):
        (root / folder).mkdir()
        (root / folder / "placeholder.txt").write_text(folder, encoding="utf-8")
    for filename in ("README.md", "CHANGELOG.md", "LICENSE"):
        (root / filename).write_text(filename, encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("target", "expected_arch", "expected_formats"),
    [
        ("linux-x64", "amd64", ("deb", "rpm")),
        ("linux-arm64", "arm64", ("deb", "rpm")),
        ("windows-x64", "amd64", ("msi",)),
        ("windows-arm64", "arm64", ("msi",)),
    ],
)
def test_nfpm_config_selects_native_architecture_and_installer_formats(
    tmp_path: Path, target: str, expected_arch: str, expected_formats: tuple[str, ...]
):
    bundle = make_bundle(tmp_path / target, target)
    config = build_installers.create_nfpm_config(bundle, target, "abcdef1234567890")

    assert config["arch"] == expected_arch
    assert config["version"] == build_installers.project_version()
    if target.startswith("linux-"):
        assert config["platform"] == "linux"
        assert {item["dst"] for item in config["contents"] if "dst" in item}
        assert config["depends"] == ["libportaudio2"]
        assert config["overrides"]["rpm"]["depends"] == ["portaudio"]
    else:
        assert config["msi"]["arch"] == ("arm64" if target.endswith("arm64") else "x64")
        assert config["msi"]["install_dir"] == "ECHOCRYPT"
        assert any(item.get("type") == "license" for item in config["contents"])
    assert expected_formats


def test_nfpm_installer_builder_emits_and_validates_deb_rpm_and_msi(
    monkeypatch, tmp_path: Path
):
    bundle = make_bundle(tmp_path / "linux", "linux-x64")
    output = tmp_path / "out"
    calls = []

    def fake_run(command, check):
        calls.append(command)
        target = Path(command[command.index("--target") + 1])
        target.write_bytes(INSTALLER_MAGIC[target.suffix])
        return None

    monkeypatch.setattr(build_installers.subprocess, "run", fake_run)
    artifacts = build_installers.build_nfpm_installers(
        bundle, "linux-x64", output, "abcdef123456"
    )
    assert {path.suffix for path in artifacts} == {".deb", ".rpm"}
    assert all(path.read_bytes().startswith(INSTALLER_MAGIC[path.suffix]) for path in artifacts)

    windows_bundle = make_bundle(tmp_path / "windows", "windows-arm64")
    windows_artifacts = build_installers.build_nfpm_installers(
        windows_bundle, "windows-arm64", output, "abcdef123456"
    )
    assert len(windows_artifacts) == 1
    assert windows_artifacts[0].suffix == ".msi"
    assert windows_artifacts[0].read_bytes().startswith(INSTALLER_MAGIC[".msi"])
    assert len(calls) == 3


def test_macos_pkg_builder_stages_cli_host_extensions_and_docs(
    monkeypatch, tmp_path: Path
):
    bundle = make_bundle(tmp_path / "macos", "macos-arm64")
    output = tmp_path / "out"

    def fake_run(command, check):
        assert command[0] == "pkgbuild"
        root = Path(command[command.index("--root") + 1])
        assert (root / "usr/local/bin/echocrypt").read_bytes() == b"cli-binary"
        assert (root / "usr/local/lib/echocrypt/ECHOCRYPT-NativeHost").read_bytes() == b"host-binary"
        assert (root / "usr/local/share/echocrypt/extensions/placeholder.txt").exists()
        assert (root / "usr/local/share/doc/echocrypt/README.md").exists()
        Path(command[-1]).write_bytes(INSTALLER_MAGIC[".pkg"] + b"fixture")

    monkeypatch.setattr(build_installers.subprocess, "run", fake_run)
    result = build_installers.build_macos_pkg(
        bundle, "macos-arm64", output
    )
    assert result.name == "echocrypt-macos-arm64.pkg"
    assert result.read_bytes().startswith(b"xar!")


def test_installer_builder_rejects_unknown_and_incomplete_targets(tmp_path: Path):
    with pytest.raises(build_installers.InstallerBuildError, match="Unsupported"):
        build_installers.build_installers(tmp_path, "linux-mips", tmp_path / "out")

    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    with pytest.raises(build_installers.InstallerBuildError, match="incomplete"):
        build_installers.create_nfpm_config(incomplete, "linux-x64", "abc123")


def test_nfpm_config_is_valid_json_for_nfpm_yaml_parser(tmp_path: Path):
    bundle = make_bundle(tmp_path / "linux", "linux-x64")
    config = build_installers.create_nfpm_config(bundle, "linux-x64", "abc123")
    # JSON is a strict YAML 1.2 subset and can be consumed by nFPM's YAML loader.
    assert json.loads(json.dumps(config))["name"] == "echocrypt"
