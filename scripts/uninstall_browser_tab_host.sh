#!/usr/bin/env bash
set -euo pipefail

BROWSER="${1:-chrome}"
HOST_NAME="org.echocrypt.tab_audio"
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
        echo "Usage: bash scripts/uninstall_browser_tab_host.sh [chrome|chromium|brave]" >&2
        exit 2
        ;;
esac

rm -f "$HOST_DIR/$HOST_NAME.json"
python3 - "${XDG_CONFIG_HOME:-$HOME/.config}/echocrypt/browser-native-host.json" "$BROWSER" <<'PY'
import json
import sys
from pathlib import Path

config_path, browser = sys.argv[1:]
path = Path(config_path)
try:
    stored = json.loads(path.read_text(encoding="utf-8"))
except (OSError, ValueError, TypeError):
    stored = {}
extensions = stored.get("extensions", {})
if isinstance(extensions, dict):
    extensions.pop(browser, None)
if extensions:
    path.write_text(json.dumps({"extensions": extensions}, indent=2) + "\\n", encoding="utf-8")
else:
    path.unlink(missing_ok=True)
PY
echo "ECHOCRYPT Native Messaging host registration removed for $BROWSER."
echo "The browser extension can also be removed from its extensions page."
