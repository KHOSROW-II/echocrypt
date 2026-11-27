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
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/echocrypt/browser-native-host.json"
echo "ECHOCRYPT Native Messaging host registration removed for $BROWSER."
echo "The browser extension can also be removed from its extensions page."
