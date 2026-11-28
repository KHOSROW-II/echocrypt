"""Render download-first release notes around GitHub-generated release notes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

DOWNLOAD_ROWS = (
    ("Linux", "x86-64", ("deb", "rpm"), "linux-x64"),
    ("Linux", "ARM64", ("deb", "rpm"), "linux-arm64"),
    ("Windows", "x86-64", ("msi",), "windows-x64"),
    ("Windows", "ARM64", ("msi",), "windows-arm64"),
    ("macOS", "Intel x86-64", ("pkg",), "macos-x64"),
    ("macOS", "Apple Silicon ARM64", ("pkg",), "macos-arm64"),
)


def _download_url(
    repository: str,
    tag: str,
    filename: str,
    asset_urls: dict[str, str] | None = None,
) -> str:
    """Prefer GitHub's actual browser_download_url for an uploaded release asset."""
    if asset_urls is None:
        return f"https://github.com/{repository}/releases/download/{tag}/{filename}"

    url = asset_urls.get(filename)
    if not url:
        raise ValueError(f"GitHub Release asset is missing: {filename}")
    parsed = urlsplit(url)
    expected_prefix = f"/{repository}/releases/download/"
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or not parsed.path.startswith(expected_prefix)
        or unquote(parsed.path.rsplit("/", 1)[-1]) != filename
    ):
        raise ValueError(f"Invalid GitHub browser_download_url for {filename}: {url}")
    return url


def load_release_asset_urls(path: Path) -> dict[str, str]:
    """Read GitHub Release API JSON and map exact asset names to browser_download_url."""
    release = json.loads(path.read_text(encoding="utf-8"))
    assets = release.get("assets") if isinstance(release, dict) else None
    if not isinstance(assets, list):
        raise ValueError("Asset manifest must be a GitHub Release API object with an assets array")
    result: dict[str, str] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError("Invalid asset entry in GitHub Release API response")
        name, url = asset.get("name"), asset.get("browser_download_url")
        if not isinstance(name, str) or not isinstance(url, str) or name in result:
            raise ValueError("Release asset entry is missing a unique name or browser_download_url")
        result[name] = url
    return result


def render_download_table(
    repository: str, tag: str, asset_urls: dict[str, str] | None = None
) -> str:
    """Render native installers and portable ZIP links from real uploaded assets when available."""
    rows = [
        "| Operating system | Architecture | Installer | Portable |",
        "|---|---|---|---|",
    ]
    for operating_system, architecture, formats, target in DOWNLOAD_ROWS:
        installers = [
            f"[.{extension}]({_download_url(repository, tag, f'echocrypt-{target}.{extension}', asset_urls)})"
            for extension in formats
        ]
        portable = f"[ZIP]({_download_url(repository, tag, f'echocrypt-{target}.zip', asset_urls)})"
        rows.append(
            f"| {operating_system} | {architecture} | {' · '.join(installers)} | {portable} |"
        )
    # Android is a separate native application, not one of the desktop
    # portable bundles. Keep its explicit development-build status visible.
    rows.append(
        f"| Android | All supported | "
        f"[Debug APK (unsigned)]({_download_url(repository, tag, 'echocrypt-android.apk', asset_urls)}) | — |"
    )
    rows.append(
        f"| All platforms | Checksums | "
        f"[SHA256SUMS]({_download_url(repository, tag, 'SHA256SUMS', asset_urls)}) | — |"
    )
    return "\n".join(rows)


def render_extension_downloads(
    repository: str, tag: str, asset_urls: dict[str, str] | None = None
) -> str:
    """Render direct links to the independently installable browser extension archives."""
    rows = [
        "| Browser | Extension package |",
        "|---|---|",
    ]
    for browser, label in (
        ("chrome", "Chrome / Chromium"),
        ("edge", "Microsoft Edge"),
        ("firefox", "Mozilla Firefox"),
    ):
        filename = f"echocrypt-{browser}.zip"
        rows.append(
            f"| {label} | [Download ZIP]({_download_url(repository, tag, filename, asset_urls)}) |"
        )
    return "\n".join(rows)


def render_release_notes(
    tag: str,
    repository: str,
    generated_notes: str,
    previous_tag: str | None = None,
    asset_urls: dict[str, str] | None = None,
) -> str:
    """Create concise release notes; never dump raw repository commit history."""
    lines = [
        f"# ECHOCRYPT {tag}",
        "",
        "## Downloads",
        "",
        "Choose the installer for your operating system and CPU architecture. "
        "The portable ZIP contains the standalone CLI, Native Messaging host, "
        "browser extensions, and documentation. The Android app is provided "
        "separately as an unsigned debug APK.",
        "",
        render_download_table(repository, tag, asset_urls),
        "",
        "## Browser extensions",
        "",
        "The browser add-ons are also available as separate ZIP downloads. Extract the selected archive, then load the included echocrypt/ directory using your browser's extension developer tools. Firefox temporary installation is available through about:debugging, This Firefox, Load Temporary Add-on.",
        "",
        render_extension_downloads(repository, tag, asset_urls),
        "",
        f"Verify desktop packages, the Android APK, and browser extensions with the [SHA256SUMS]({_download_url(repository, tag, 'SHA256SUMS', asset_urls)}) file.",
        "",
        "## What's changed",
        "",
    ]
    if previous_tag:
        lines.extend([f"GitHub-generated changes since {previous_tag}:", ""])
    notes = generated_notes.strip()
    lines.extend([
        notes if notes else "No pull-request summaries were generated for this release range.",
        "",
        "---",
        "",
        "## Packaging notes",
        "",
        "- Linux installers: Debian/Ubuntu .deb and Fedora/RHEL-family .rpm packages.",
        "- Windows installer: .msi for the matching CPU architecture.",
        "- macOS installer: .pkg for Intel or Apple Silicon.",
        "- Portable .zip packages are provided for every supported desktop target.",
        "- The Android download is an unsigned Debug APK for development/testing, not a production-signed build.",
        "- Desktop installers are currently unsigned; macOS packages are not notarized.",
        "- This release remains a Draft until it is explicitly published.",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY", "KHOSROW-II/echocrypt"),
    )
    parser.add_argument("--previous-tag")
    parser.add_argument("--generated-notes", type=Path, required=True)
    parser.add_argument(
        "--asset-manifest",
        type=Path,
        help="GitHub Release API JSON; use each asset's actual browser_download_url.",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    if "/" not in args.repository or not all(args.repository.split("/", 1)):
        parser.error("--repository must be OWNER/REPOSITORY")

    generated = args.generated_notes.read_text(encoding="utf-8")
    try:
        asset_urls = load_release_asset_urls(args.asset_manifest) if args.asset_manifest else None
        content = render_release_notes(
            args.tag, args.repository, generated, args.previous_tag, asset_urls
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    print(f"Generated download table and GitHub release notes for {args.tag}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
