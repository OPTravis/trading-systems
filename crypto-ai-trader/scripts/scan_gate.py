#!/usr/bin/env python3
"""Dynamic scan gate: skip scan when market conditions don't warrant it.

Reduces API/LLM waste during extended low-activity periods (e.g. extreme fear).

Logic:
- Read last scan timestamp from state DB
- Read current Fear & Greed index
- If F&G < 30 (extreme fear) and last scan < 3h ago → skip
- If F&G 30-45 (fear) and last scan < 2h ago → skip
- Otherwise → allow

Exit code 0 = run scan, 1 = skip scan
"""
import sys
import os
import time
import json
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Dynamic frequency intervals (seconds)
FREQ_MAP = {
    # F&G range: (min_interval_hours, label)
    (0, 25):   (4, "EXTREME_FEAR"),   # every 4h
    (25, 40):  (2, "FEAR"),           # every 2h
    (40, 60):  (1, "NEUTRAL"),        # every 1h (current default)
    (60, 75):  (1, "GREED"),          # every 1h
    (75, 101): (0.5, "EXTREME_GREED"),  # every 30min
}

LAST_SCAN_FILE = "data/last_scan_ts.json"


def get_last_scan_ts():
    """Get last scan timestamp from file."""
    try:
        with open(LAST_SCAN_FILE) as f:
            data = json.load(f)
            return data.get("timestamp", 0)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


def save_scan_ts():
    """Save current time as last scan timestamp."""
    try:
        os.makedirs("data", exist_ok=True)
        with open(LAST_SCAN_FILE, "w") as f:
            json.dump({"timestamp": time.time()}, f)
    except Exception:
        pass


def get_fng():
    """Get current Fear & Greed index."""
    # Try reading from cache DB first (fast, no API call)
    try:
        import sqlite3
        conn = sqlite3.connect("data/cache.db")
        row = conn.execute(
            "SELECT value FROM fng_history ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row and row[0] is not None:
            # fng_history stores value as integer directly
            val = int(row[0])
            logger.info(f"DynamicGate: F&G = {val} (from cache)")
            return val
    except Exception:
        pass

    # Fallback: try live API
    try:
        from src.binance_client import BinanceClient
        from src.data_feed import DataFeedManager
        client = BinanceClient(testnet=False)
        feed = DataFeedManager()
        fg = feed.get_fear_greed()
        logger.info(f"DynamicGate: F&G = {fg} (from API)")
        return fg
    except Exception:
        pass

    # Last resort: neutral
    logger.info("DynamicGate: F&G unknown, defaulting to 50 (neutral)")
    return 50


def main():
    fng = get_fng()

    # Determine required interval
    interval_hours = 1  # default
    label = "NEUTRAL"
    for (lo, hi), (hrs, lbl) in FREQ_MAP.items():
        if lo <= fng < hi:
            interval_hours = hrs
            label = lbl
            break

    last_ts = get_last_scan_ts()
    elapsed_hours = (time.time() - last_ts) / 3600 if last_ts else 999

    if elapsed_hours < interval_hours:
        remaining = interval_hours - elapsed_hours
        logger.info(
            f"DynamicGate: SKIP — F&G={fng} ({label}), "
            f"elapsed={elapsed_hours:.1f}h < interval={interval_hours}h, "
            f"next scan in {remaining:.1f}h"
        )
        sys.exit(1)
    else:
        logger.info(
            f"DynamicGate: RUN — F&G={fng} ({label}), "
            f"elapsed={elapsed_hours:.1f}h >= interval={interval_hours}h"
        )
        save_scan_ts()
        sys.exit(0)


if __name__ == "__main__":
    main()
