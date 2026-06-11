#!/bin/bash
set -e

BASEDIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$BASEDIR/logs/scan.log"
mkdir -p "$BASEDIR/logs"

# Ensure sing-box is running
if ! pgrep -x sing-box > /dev/null; then
    echo "[setup] Starting sing-box proxy..."
    nohup sing-box run -c /etc/sing-box/config.json > /tmp/sing-box.log 2>&1 &
    sleep 2
fi

# Load .env
set -a
source "$BASEDIR/.env"
set +a

# Run scan
cd "$BASEDIR"
python main.py scan 2>&1 | tee "$LOG"
