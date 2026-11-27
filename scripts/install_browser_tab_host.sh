#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: bash scripts/install_browser_tab_host.sh <extension-id> [chrome|chromium|brave]" >&2
}

EXTENSION_ID="${1:-}"
BROWSER="${2:-chrome}"

if [[ ! "$EXTENSION_ID" =~ ^[a-p]{32}$ ]]; then
    usage
    echo "The extension ID must be the 32-character ID shown by chrome://extensions." >&2
    exit 2
fi

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_NAME="org.echocrypt.tab_audio"
HOST_WRAPPER="$ROOT/scripts/ECHOCRYPT_tab_host"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/ECHOCRYPT"

case "$BROWSER" in
    chrome)
        HOST_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/google-chrome/NativeMessagingHosts"
        ;;
    chromium)
        HOST_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/chromium/NativeMessagingHosts"
        ;;
    brave)
        HOST_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/BraveSoftware/Brave-Browser/NativeMessagingHosts"
        ;;
    *)
        usage
        exit 2
        ;;
esac

mkdir -p "$HOST_DIR" "$CONFIG_DIR"
chmod +x "$HOST_WRAPPER"

python3 - "$HOST_DIR/$HOST_NAME.json" "$HOST_WRAPPER" "$EXTENSION_ID" "$CONFIG_DIR/browser-native-host.json" "$BROWSER" <<'PY'
import json
import os
import sys
from pathlib import Path

manifest_path, host_path, extension_id, config_path, browser = sys.argv[1:]
manifest = {
    "name": "org.echocrypt.tab_audio",
    "description": "ECHOCRYPT selected browser-tab PCM bridge",
    "path": os.path.abspath(host_path),
    "type": "stdio",
    "allowed_origins": [f"chrome-extension://{extension_id}/"],
}
Path(manifest_path).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
config_file = Path(config_path)
try:
    stored = json.loads(config_file.read_text(encoding="utf-8"))
except (OSError, ValueError, TypeError):
    stored = {}
extensions = stored.get("extensions", {})
if not isinstance(extensions, dict):
    extensions = {}
extensions[browser] = extension_id
config_file.write_text(
    json.dumps({"extensions": extensions}, indent=2) + "\n",
    encoding="utf-8",
)
PY
echo "ECHOCRYPT Native Messaging host registered for $BROWSER."
echo "Restart $BROWSER completely, then click the ECHOCRYPT extension icon on the tab to capture."
