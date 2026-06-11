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
python3 scripts/health_check.py >> "$LOGFILE" 2>&1
EXIT_CODE=$?
echo "========== Exit: $EXIT_CODE ==========" >> "$LOGFILE"

exit $EXIT_CODE
