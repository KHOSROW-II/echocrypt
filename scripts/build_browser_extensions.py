#!/usr/bin/env python3
"""Build deterministic ZIPs for Chrome, Edge, and Firefox."""
import argparse
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "browser_extension"

def build(browser: str, output: Path) -> Path:
    if browser not in ("chrome", "edge", "firefox"):
        raise ValueError(browser)
    files = (
        ["manifest.json", "service_worker.js", "offscreen.html", "offscreen.js", "pcm_worklet.js", "native_host.py"]
        if browser in ("chrome", "edge")
        else ["firefox/manifest.json", "firefox/background.js", "firefox/content.js", "pcm_worklet.js", "native_host.py"]
    )
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"echocrypt-{browser}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in files:
            source = EXT / item
            if not source.is_file():
                raise FileNotFoundError(source)
            archive.write(source, Path("echocrypt") / Path(item).name)
        archive.writestr("echocrypt/BUILD-METADATA.json", json.dumps({"browser": browser}, indent=2) + "\n")
    with zipfile.ZipFile(target) as archive:
        if archive.testzip():
            raise RuntimeError(f"Invalid archive: {target}")
    return target

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", choices=("chrome", "edge", "firefox", "all"), default="all")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "extensions")
    args = parser.parse_args()
    names = ("chrome", "edge", "firefox") if args.browser == "all" else (args.browser,)
    for name in names:
        print(build(name, args.output))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
