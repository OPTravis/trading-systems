#!/bin/bash
# Ensure TP/SL wrapper — corrects hardcoded paths for cloud env
set -euo pipefail

BASEDIR="$(cd "$(dirname "$0")" && pwd)"
LOGDIR="$BASEDIR/logs"
mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/ensure_tp_sl.log"

# Ensure sing-box proxy
if ! pgrep -x sing-box > /dev/null; then
    echo "[$(date)] [WARN] sing-box not running, starting..." >> "$LOGFILE"
    nohup sing-box run -c /etc/sing-box/config.json > /tmp/sing-box.log 2>&1 &
    sleep 2
fi

# Load .env
set -a
source "$BASEDIR/.env"
set +a

# Create symlink so hardcoded ~/trading-systems/crypto-ai-trader resolves correctly
if [ ! -d ~/trading-systems ]; then
    mkdir -p ~/trading-systems
fi
if [ ! -L ~/trading-systems/crypto-ai-trader ]; then
    ln -sfn "$BASEDIR" ~/trading-systems/crypto-ai-trader
fi

cd "$BASEDIR"

echo "========== $(date) - ensure-tp-sl ==========" >> "$LOGFILE"
python3 scripts/ensure_tp_sl.py >> "$LOGFILE" 2>&1
EXIT_CODE=$?
echo "========== Exit: $EXIT_CODE ==========" >> "$LOGFILE"

# Rotate log if > 5MB
if [ -f "$LOGFILE" ] && [ $(stat -c%s "$LOGFILE" 2>/dev/null || echo 0) -gt 5242880 ]; then
    mv "$LOGFILE" "$LOGFILE.old"
fi

exit $EXIT_CODE
