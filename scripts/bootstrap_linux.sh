#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# Compatibility wrapper. The application owns bootstrap logic now.
if [[ $# -eq 2 && "$1" == "--mode" ]]; then
  exec python3 echocrypt.py --bootstrap "$2"
elif [[ $# -eq 1 && "$1" != "--help" ]]; then
  exec python3 echocrypt.py --bootstrap "$1"
elif [[ $# -eq 1 && "$1" == "--help" ]]; then
  exec python3 echocrypt.py --help
elif [[ $# -eq 0 ]]; then
  exec python3 echocrypt.py --bootstrap
else
  echo "Usage: $0 [--mode] [full|standby|manual|rootless]" >&2
  exit 2
fi
