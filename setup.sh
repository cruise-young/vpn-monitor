#!/bin/sh
set -eu
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "python3 not found. Install Python 3.8+ from python.org or Xcode Command Line Tools." >&2
    exit 1
fi
"$PYTHON" setup.py "$@"
sh ./start.sh
echo "Open http://127.0.0.1:18769/ ; restart Clash Verge once if the Web UI entry is missing."
