#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p data
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "python3 not found. Install Python 3.8+ first." >&2
    exit 1
fi
if [ -f data/monitor.pid ]; then
    pid=$(cat data/monitor.pid 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "Monitor is already running (PID $pid): http://127.0.0.1:18769/"
        exit 0
    fi
fi
nohup "$PYTHON" vpn_monitor.py >>data/bootstrap.log 2>&1 &
echo "Monitor started: http://127.0.0.1:18769/"
