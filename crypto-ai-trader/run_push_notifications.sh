#!/bin/bash
# Push notifications wrapper — outputs pending notifications for heartbeat pickup
set -euo pipefail

BASEDIR="$(cd "$(dirname "$0")" && pwd)"
LOGDIR="$BASEDIR/logs"
mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/push_notifications.log"

# Load .env
set -a
source "$BASEDIR/.env"
set +a

cd "$BASEDIR"

echo "========== $(date) - push-notifications ==========" >> "$LOGFILE"
python3 scripts/push_notifications.py >> "$LOGFILE" 2>&1
EXIT_CODE=$?
echo "========== Exit: $EXIT_CODE ==========" >> "$LOGFILE"

# Rotate log if > 5MB
if [ -f "$LOGFILE" ] && [ $(stat -c%s "$LOGFILE" 2>/dev/null || echo 0) -gt 5242880 ]; then
    mv "$LOGFILE" "$LOGFILE.old"
fi

exit $EXIT_CODE
