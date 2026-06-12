#!/bin/bash
# Weekly Backtest wrapper
set -euo pipefail

BASEDIR="$(cd "$(dirname "$0")" && pwd)"
LOGDIR="$BASEDIR/logs"
mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/weekly_backtest.log"

# Ensure sing-box proxy
if ! pgrep -x sing-box > /dev/null; then
    nohup sing-box run -c /etc/sing-box/config.json > /tmp/sing-box.log 2>&1 &
    sleep 2
fi

# Load .env
set -a
source "$BASEDIR/.env"
set +a

# Symlinks for hardcoded paths
mkdir -p ~/trading-systems
ln -sfn "$BASEDIR" ~/trading-systems/crypto-ai-trader 2>/dev/null || true
ln -sfn "$BASEDIR" ~/crypto-ai-trader 2>/dev/null || true

cd "$BASEDIR"

echo "========== $(date) - weekly-backtest ==========" >> "$LOGFILE"
python3 scripts/weekly_backtest.py >> "$LOGFILE" 2>&1
EXIT_CODE=$?
echo "========== Exit: $EXIT_CODE ==========" >> "$LOGFILE"

# Rotate log if > 5MB
if [ -f "$LOGFILE" ] && [ $(stat -c%s "$LOGFILE" 2>/dev/null || echo 0) -gt 5242880 ]; then
    mv "$LOGFILE" "$LOGFILE.old"
fi

exit $EXIT_CODE
