#!/usr/bin/env python3
"""
Sync trade outcomes with actual portfolio state.

Detects positions that were closed by SL/TP order fills on Binance
(which don't trigger explicit code paths in ensure_tp_sl.py).

Logic:
1. Get all open outcomes
2. Check if corresponding portfolio position still exists
3. If position gone → mark outcome as closed with exit_reason="order_fill"
4. Update price extremes for open positions

Run via cron (unified-monitor) or manually.
"""

import sys
import os
import json
import time
import logging

sys.path.insert(0, os.path.expanduser("~/trading-systems/crypto-ai-trader"))

from src.binance_client import BinanceClient
from src.state_db import get_state_db
from src.trade_outcome_recorder import TradeOutcomeRecorder

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger(__name__)


def _determine_exit_reason(db, sym, exit_price, entry):
    """Determine actual exit reason from trailing_stop state and exit price.
    
    Checks:
    1. trailing_stop activated + triggered → 'trailing'
    2. exit_price near SL → 'sl'
    3. exit_price near TP levels → 'tp1'/'tp2'/'tp3'
    4. exceeded max_hold_hours → 'max_hold'
    5. fallback → 'order_fill'
    """
    conn = db._get_conn()
    
    # Check trailing_stop state
    try:
        ts_row = conn.execute(
            "SELECT activated, sl_price FROM trailing_stop WHERE symbol = ?", (sym,)
        ).fetchone()
        if ts_row and ts_row[0]:  # activated = True
            return "trailing"
    except Exception:
        pass
    
    # Check if exit price is near SL (within 1% tolerance)
    entry_price = entry.get("entry_price", 0)
    if entry_price > 0 and exit_price > 0:
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        
        # SL hit: exit at a loss > 2%
        if pnl_pct < -2.0:
            return "sl"
        
        # TP hit: check against known TP levels (3%, 5%, 8%, 10%, 15%)
        tp_levels = [3.0, 5.0, 8.0, 10.0, 15.0]
        for tp in tp_levels:
            if abs(pnl_pct - tp) < 1.0:  # within 1% of TP level
                return f"tp{tp_levels.index(tp)+1}"
    
    # Check max_hold: if time held > 24h (conservative), likely max_hold
    opened_at = entry.get("opened_at") or entry.get("entry_time")
    if opened_at:
        try:
            from datetime import datetime
            if isinstance(opened_at, str):
                opened_dt = datetime.fromisoformat(opened_at)
            else:
                opened_dt = datetime.fromtimestamp(opened_at)
            hold_hours = (datetime.now() - opened_dt).total_seconds() / 3600
            if hold_hours > 24:
                return "max_hold"
        except Exception:
            pass
    
    return "order_fill"


def sync_outcomes():
    """Check open outcomes against portfolio, close stale ones."""
    db = get_state_db()
    recorder = TradeOutcomeRecorder(db=db)
    client = BinanceClient(testnet=False)

    open_entries = recorder.get_open_entries()
    if not open_entries:
        return {"synced": 0, "closed": 0, "updated": 0}

    # Get current portfolio positions
    portfolio_symbols = set()
    try:
        conn = db._get_conn()
        rows = conn.execute("SELECT symbol FROM portfolio WHERE quantity > 0").fetchall()
        portfolio_symbols = {r["symbol"] for r in rows}
    except Exception as e:
        logger.warning(f"Portfolio query failed: {e}")

    # Get current prices for open positions
    price_map = {}
    try:
        all_tickers = client.get_24hr_stats()
        if isinstance(all_tickers, list):
            price_map = {
                t["symbol"]: float(t.get("last_price", 0))
                for t in all_tickers
                if "symbol" in t
            }
    except Exception:
        pass

    closed = 0
    updated = 0

    for entry in open_entries:
        sym = entry["symbol"]

        # Update price extremes for still-open positions
        if sym in portfolio_symbols:
            current_price = price_map.get(sym, 0)
            if current_price > 0:
                recorder.update_price_extremes(sym, current_price)
                updated += 1
            continue

        # Position gone from portfolio → outcome was closed (SL/TP order fill)
        # Determine actual exit reason from trailing_stop state and exit price
        current_price = price_map.get(sym, 0)
        exit_reason = _determine_exit_reason(db, sym, current_price, entry)
        if current_price > 0:
            outcome = recorder.record_outcome(
                symbol=sym,
                exit_price=current_price,
                exit_reason=exit_reason,
            )
            if outcome:
                closed += 1
                logger.info(
                    f"OUTCOME_SYNC: {sym} closed by {exit_reason} "
                    f"pnl={outcome['net_pnl_pct']:+.2f}%"
                )
        else:
            # Can't get price — use entry price as fallback (0% PnL)
            entry_price = entry["entry_price"]
            outcome = recorder.record_outcome(
                symbol=sym,
                exit_price=entry_price,
                exit_reason="order_fill_unknown_price",
            )
            if outcome:
                closed += 1

    result = {"synced": len(open_entries), "closed": closed, "updated": updated}
    return result


if __name__ == "__main__":
    result = sync_outcomes()
    print(json.dumps(result))
