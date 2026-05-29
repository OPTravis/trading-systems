#!/bin/bash
# ──────────────────────────────────────────────────────────────────────
# run_scan.sh - Cron entry point for market scanning
#
# Usage:
#   ./scripts/run_scan.sh                    # Default: scan sp500 US market
#   ./scripts/run_scan.sh --universe nasdaq  # Custom universe
#
# Cron example (run every hour during market hours):
#   0 9-16 * * 1-5 /home/travis/trading-systems/stock-ai-trader/scripts/run_scan.sh
# ──────────────────────────────────────────────────────────────────────

set -euo pipefail

# Project directory
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# Log directory
LOG_DIR="$PROJECT_DIR/data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/scan_$(date +%Y%m%d).log"

# Activate virtual environment if it exists
if [ -f "$PROJECT_DIR/venv/bin/activate" ]; then
    source "$PROJECT_DIR/venv/bin/activate"
elif [ -f "$PROJECT_DIR/.venv/bin/activate" ]; then
    source "$PROJECT_DIR/.venv/bin/activate"
fi

# Run scan with arguments
echo "=== Scan started at $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
python main.py scan "$@" >> "$LOG_FILE" 2>&1
EXIT_CODE=$?
echo "=== Scan finished at $(date '+%Y-%m-%d %H:%M:%S') (exit: $EXIT_CODE) ===" >> "$LOG_FILE"

exit $EXIT_CODE
