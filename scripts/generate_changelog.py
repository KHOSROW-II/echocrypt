"""Render download-first release notes around GitHub-generated release notes."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

DOWNLOAD_ROWS = (
    ("Linux", "x86-64", ("deb", "rpm"), "linux-x64"),
    ("Linux", "ARM64", ("deb", "rpm"), "linux-arm64"),
    ("Windows", "x86-64", ("msi",), "windows-x64"),
    ("Windows", "ARM64", ("msi",), "windows-arm64"),
    ("macOS", "Intel x86-64", ("pkg",), "macos-x64"),
    ("macOS", "Apple Silicon ARM64", ("pkg",), "macos-arm64"),
)


def _download_url(repository: str, tag: str, filename: str) -> str:
    return f"https://github.com/{repository}/releases/download/{tag}/{filename}"


def render_download_table(repository: str, tag: str) -> str:
    """Render direct links for native installers and portable ZIP bundles."""
    rows = [
        "| Operating system | Architecture | Installer | Portable |",
        "|---|---|---|---|",
    ]
    for operating_system, architecture, formats, target in DOWNLOAD_ROWS:
        installers = [
            f"[.{extension}]({_download_url(repository, tag, f'echocrypt-{target}.{extension}')})"
            for extension in formats
        ]
        portable = f"[ZIP]({_download_url(repository, tag, f'echocrypt-{target}.zip')})"
        rows.append(
            f"| {operating_system} | {architecture} | {' · '.join(installers)} | {portable} |"
        )
    rows.append(
        f"| All platforms | Checksums | "
        f"[SHA256SUMS]({_download_url(repository, tag, 'SHA256SUMS')}) | — |"
    )
    return "\n".join(rows)


def render_release_notes(
    tag: str,
    repository: str,
    generated_notes: str,
    previous_tag: str | None = None,
) -> str:
    """Create concise release notes; never dump raw repository commit history."""
    lines = [
        f"# ECHOCRYPT {tag}",
        "",
        "## Downloads",
        "",
        "Choose the installer for your operating system and CPU architecture. "
        "The portable ZIP contains the standalone CLI, Native Messaging host, "
        "browser extensions, and documentation.",
        "",
        render_download_table(repository, tag),
        "",
        f"Verify downloads with the [SHA256SUMS]({_download_url(repository, tag, 'SHA256SUMS')}) file.",
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
        "- Portable .zip packages are provided for every supported target.",
        "- Installers are currently unsigned; macOS packages are not notarized.",
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
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    if "/" not in args.repository or not all(args.repository.split("/", 1)):
        parser.error("--repository must be OWNER/REPOSITORY")

    generated = args.generated_notes.read_text(encoding="utf-8")
    content = render_release_notes(
        args.tag, args.repository, generated, args.previous_tag
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    print(f"Generated download table and GitHub release notes for {args.tag}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
