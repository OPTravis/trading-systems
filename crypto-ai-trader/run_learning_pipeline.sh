#!/bin/bash
# Learning Pipeline wrapper — weekly model retraining
set -euo pipefail

BASEDIR="$(cd "$(dirname "$0")" && pwd)"
LOGDIR="$BASEDIR/logs"
mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/learning_pipeline.log"

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

cd "$BASEDIR"

echo "========== $(date) - learning-pipeline ==========" >> "$LOGFILE"
set +e
python3 scripts/learning_pipeline.py >> "$LOGFILE" 2>&1
EXIT_CODE=$?
set -e
echo "========== Exit: $EXIT_CODE ==========" >> "$LOGFILE"

# Record failure for monitoring
if [ $EXIT_CODE -ne 0 ]; then
    echo "{\"timestamp\":\"$(date -Iseconds)\",\"job\":\"weekly-learning\",\"exit_code\":$EXIT_CODE}" >> "$LOGDIR/cron_failures.jsonl"
fi

# Rotate log if > 5MB
if [ -f "$LOGFILE" ] && [ $(stat -c%s "$LOGFILE" 2>/dev/null || echo 0) -gt 5242880 ]; then
    mv "$LOGFILE" "$LOGFILE.old"
fi

exit $EXIT_CODE
