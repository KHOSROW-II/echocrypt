from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest

from scripts import build_browser_extensions


@pytest.mark.parametrize(
    ("browser", "expected_manifest_version", "expected_members"),
    [
        (
            "chrome",
            3,
            {
                "echocrypt/manifest.json",
                "echocrypt/service_worker.js",
                "echocrypt/offscreen.html",
                "echocrypt/offscreen.js",
                "echocrypt/pcm_worklet.js",
                "echocrypt/native_host.py",
                "echocrypt/BUILD-METADATA.json",
            },
        ),
        (
            "edge",
            3,
            {
                "echocrypt/manifest.json",
                "echocrypt/service_worker.js",
                "echocrypt/offscreen.html",
                "echocrypt/offscreen.js",
                "echocrypt/pcm_worklet.js",
                "echocrypt/native_host.py",
                "echocrypt/BUILD-METADATA.json",
            },
        ),
        (
            "firefox",
            2,
            {
                "echocrypt/manifest.json",
                "echocrypt/background.js",
                "echocrypt/content.js",
                "echocrypt/pcm_worklet.js",
                "echocrypt/native_host.py",
                "echocrypt/BUILD-METADATA.json",
            },
        ),
    ],
)
def test_browser_extension_archives_are_complete_and_loadable(
    browser: str,
    expected_manifest_version: int,
    expected_members: set[str],
    tmp_path: Path,
) -> None:
    archive_path = build_browser_extensions.build(browser, tmp_path)

    assert archive_path.name == f"echocrypt-{browser}.zip"
    assert zipfile.is_zipfile(archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == expected_members
        manifest = json.loads(archive.read("echocrypt/manifest.json"))
        metadata = json.loads(archive.read("echocrypt/BUILD-METADATA.json"))

    assert manifest["manifest_version"] == expected_manifest_version
    assert metadata == {"browser": browser}
    assert "nativeMessaging" in manifest["permissions"]


def test_browser_extension_builder_rejects_unknown_browser(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_browser_extensions.build("safari", tmp_path)
