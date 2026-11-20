#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Diagnostics only. First launch the self-bootstrapping application with:
#   python echocrypt.py
SYSTEM_PYTHON="$(command -v python 2>/dev/null || true)"
PYTHON_BIN=""

# Native Termux deliberately uses its system Python (no project venv).
if [[ -n "$SYSTEM_PYTHON" ]] && "$SYSTEM_PYTHON" -c 'import os,sys; from pathlib import Path; root="/data/data/com.termux/"; paths=[str(Path(sys.executable).resolve()),str(Path(sys.prefix).resolve())]; raise SystemExit(0 if any(p.startswith(root) for p in paths) and (os.environ.get("TERMUX_VERSION") or os.environ.get("PREFIX", "").startswith(root)) else 1)' >/dev/null 2>&1; then
  PYTHON_CANDIDATES=("$SYSTEM_PYTHON")
else
  PYTHON_CANDIDATES=("$ROOT/.venv/bin/python" "$SYSTEM_PYTHON")
fi

for candidate in "${PYTHON_CANDIDATES[@]}"; do
  [[ -n "$candidate" && -x "$candidate" ]] || continue
  if "$candidate" -c 'import sounddevice as sd; sd.get_portaudio_version()' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  echo "Audio dependencies/PortAudio are not ready in ECHO-CRYPT's Python environment." >&2
  echo "Run 'python echocrypt.py' to execute the built-in bootstrap, then retry." >&2
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import sounddevice as sd
print("PortAudio:", sd.get_portaudio_version())
devices = sd.query_devices()
for i, device in enumerate(devices):
    print(i, device["name"], "IN", device["max_input_channels"], "OUT", device["max_output_channels"])
if not devices:
    print("PortAudio loaded, but no audio endpoints are visible to this process.")
PY
