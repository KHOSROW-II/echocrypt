from __future__ import annotations

import re

import pytest

from scripts import generate_windows_version_info


@pytest.mark.parametrize(
    ("name", "description"),
    [
        ("ECHOCRYPT", "ECHOCRYPT command-line application"),
        ("ECHOCRYPT-NativeHost", "ECHOCRYPT browser native messaging host"),
    ],
)
def test_windows_version_resource_is_valid_and_contains_fixed_pe_metadata(name, description):
    source = generate_windows_version_info.render_version_info(name)

    # PyInstaller evaluates this source to construct the PE VERSIONINFO resource.
    compile(source, f"{name}.version", "exec")
    version = generate_windows_version_info.project_version()
    expected = tuple(int(part) for part in version.split(".")) + (0,)

    assert f"filevers={expected!r}" in source
    assert f"prodvers={expected!r}" in source
    assert f"StringStruct('FileDescription', {description!r})" in source
    assert f"StringStruct('OriginalFilename', {f'{name}.exe'!r})" in source
    assert f"StringStruct('ProductVersion', {'.'.join(map(str, expected))!r})" in source


def test_windows_version_resource_rejects_invalid_or_unrepresentable_versions():
    with pytest.raises(ValueError, match="numeric semver"):
        generate_windows_version_info.render_version_info("ECHOCRYPT", "0.2.beta")
    with pytest.raises(ValueError, match="<= 65535"):
        generate_windows_version_info.render_version_info("ECHOCRYPT", "1.70000.0")
    with pytest.raises(ValueError, match="Unsupported"):
        generate_windows_version_info.render_version_info("UNKNOWN")
