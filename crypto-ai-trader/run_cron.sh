#!/bin/bash
# Crypto AI Trader - Cron Wrapper
# Usage: run_cron.sh <subcommand> [args...]
set -euo pipefail

BASEDIR="$(cd "$(dirname "$0")" && pwd)"
LOGDIR="$BASEDIR/logs"
mkdir -p "$LOGDIR"

CMD="${1:?Usage: run_cron.sh <subcommand>}"
shift

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOGFILE="$LOGDIR/${CMD}.log"

# Ensure sing-box proxy is running
if ! pgrep -x sing-box > /dev/null; then
    echo "[$(date)] [WARN] sing-box not running, starting..." >> "$LOGFILE"
    nohup sing-box run -c /etc/sing-box/config.json > /tmp/sing-box.log 2>&1 &
    sleep 2
fi

# Load .env
set -a
source "$BASEDIR/.env"
set +a

cd "$BASEDIR"

echo "========== $(date) - $CMD ==========" >> "$LOGFILE"
python3 main.py "$CMD" "$@" >> "$LOGFILE" 2>&1
EXIT_CODE=$?
echo "========== Exit: $EXIT_CODE ==========" >> "$LOGFILE"

# Rotate log if > 5MB
if [ -f "$LOGFILE" ] && [ $(stat -c%s "$LOGFILE" 2>/dev/null || echo 0) -gt 5242880 ]; then
    mv "$LOGFILE" "$LOGFILE.old"
fi

exit $EXIT_CODE
