#!/bin/bash
# Daily report cron wrapper

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Source secrets
source ~/crypto-ai-trader/crypto-secrets.env 2>/dev/null

python3 main.py cron-report >> "$SCRIPT_DIR/logs/cron_report.log" 2>&1
