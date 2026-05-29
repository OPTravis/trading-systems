#!/bin/bash
# Market scan cron wrapper
# Run market scan and send results to Feishu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Source secrets
source ~/crypto-ai-trader/crypto-secrets.env 2>/dev/null

python3 main.py cron-scan >> "$SCRIPT_DIR/logs/cron_scan.log" 2>&1
