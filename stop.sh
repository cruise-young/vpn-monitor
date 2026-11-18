#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p data
touch data/stop.request
pid=$(cat data/monitor.pid 2>/dev/null || true)
if [ -z "$pid" ]; then
    echo "Monitor is not running."
    exit 0
fi
i=0
while [ "$i" -lt 30 ] && kill -0 "$pid" 2>/dev/null; do
    sleep 0.5
    i=$((i + 1))
done
if kill -0 "$pid" 2>/dev/null; then
    echo "Stop requested; process $pid is still finishing."
else
    echo "Monitor stopped."
fi
