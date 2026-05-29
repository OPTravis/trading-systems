#!/bin/bash
# ──────────────────────────────────────────────────────────────────────
# run_daily_report.sh - Generate and send daily portfolio report
#
# Usage:
#   ./scripts/run_daily_report.sh
#
# Cron example (run at 5PM ET on trading days):
#   0 17 * * 1-5 /home/travis/trading-systems/stock-ai-trader/scripts/run_daily_report.sh
# ──────────────────────────────────────────────────────────────────────

set -euo pipefail

# Project directory
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# Log directory
LOG_DIR="$PROJECT_DIR/data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/daily_report_$(date +%Y%m%d).log"

# Activate virtual environment if it exists
if [ -f "$PROJECT_DIR/venv/bin/activate" ]; then
    source "$PROJECT_DIR/venv/bin/activate"
elif [ -f "$PROJECT_DIR/.venv/bin/activate" ]; then
    source "$PROJECT_DIR/.venv/bin/activate"
fi

echo "=== Daily report started at $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"

# Generate status report
python main.py status --detailed >> "$LOG_FILE" 2>&1

# Send report via notifier
python -c "
import sys
sys.path.insert(0, '$PROJECT_DIR')
from src.notifier import FeishuNotifier
from datetime import datetime

notifier = FeishuNotifier()
report = {
    'date': datetime.now().strftime('%Y-%m-%d'),
    'total_return_pct': 0.0,
    'daily_pnl': 0.0,
    'total_trades': 0,
    'win_rate': 0.0,
    'positions': [],
    'risk_status': {'mode': 'paper', 'status': 'active'},
}
success = notifier.send_daily_report(report)
print(f'Daily report sent: {success}')
" >> "$LOG_FILE" 2>&1

EXIT_CODE=$?
echo "=== Daily report finished at $(date '+%Y-%m-%d %H:%M:%S') (exit: $EXIT_CODE) ===" >> "$LOG_FILE"

exit $EXIT_CODE
