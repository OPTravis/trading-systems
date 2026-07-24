#!/bin/bash
# Health Check wrapper
set -euo pipefail

BASEDIR="$(cd "$(dirname "$0")" && pwd)"
LOGDIR="$BASEDIR/logs"
mkdir -p "$LOGDIR"

LOGFILE="$LOGDIR/health_check.log"

# Load .env
set -a
source "$BASEDIR/.env"
set +a

cd "$BASEDIR"

echo "========== $(date) - health-check ==========" >> "$LOGFILE"
set +e
python3 scripts/health_check.py >> "$LOGFILE" 2>&1
EXIT_CODE=$?
set -e
echo "========== Exit: $EXIT_CODE ==========" >> "$LOGFILE"

# Record failure for monitoring
if [ $EXIT_CODE -ne 0 ]; then
    echo "{\"timestamp\":\"$(date -Iseconds)\",\"job\":\"health-check\",\"exit_code\":$EXIT_CODE}" >> "$LOGDIR/cron_failures.jsonl"
fi

exit $EXIT_CODE
