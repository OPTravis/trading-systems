#!/usr/bin/env python3
"""
Sync trade outcomes with actual portfolio state.

Detects positions that were closed by SL/TP order fills on Binance
(which don't trigger explicit code paths in the main trade executor).

Key improvements over the original:
1. Queries actual Binance trade history for real exit price & timestamp
2. Uses context_json TP/SL levels (not hardcoded) for exit_reason classification
3. Records SELL trades in the trades table
4. Uses actual exit time from Binance, not sync detection time

Run via cron (unified-monitor) or manually.
"""

import sys
import os
import json
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src._binance_sdk_client import BinanceClient
from src.state_db import get_state_db
from src.trade_outcome_recorder import TradeOutcomeRecorder

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger(__name__)


def _get_actual_exit_from_binance(client, symbol, since_ts):
    """Query Binance trade history to find actual SELL fills after entry.

    Returns dict with exit_price, exit_qty, exit_time, exit_value, commission
    or None if no sells found.
    """
    try:
        trades = client.get_my_trades(symbol, limit=50)
    except Exception as e:
        logger.warning(f"Failed to get trades for {symbol}: {e}")
        return None

    # Filter: SELL trades after the entry timestamp
    sells = [t for t in trades if not t.get("isBuyer", False) and t["time"] / 1000 >= since_ts - 60]
    if not sells:
        return None

    total_qty = sum(float(t["qty"]) for t in sells)
    total_value = sum(float(t["quoteQty"]) for t in sells)
    total_commission = sum(float(t["commission"]) for t in sells)
    # Weighted average exit price
    avg_price = total_value / total_qty if total_qty > 0 else 0
    # Earliest sell = exit time (for TP1; latest = for full close)
    exit_time = min(t["time"] for t in sells) / 1000
    commission_asset = sells[0].get("commissionAsset", "USDT")

    return {
        "exit_price": avg_price,
        "exit_qty": total_qty,
        "exit_time": exit_time,
        "exit_value": total_value,
        "commission": total_commission,
        "commission_asset": commission_asset,
        "num_fills": len(sells),
    }


def _determine_exit_reason(exit_info, entry):
    """Determine exit reason using actual Binance fill data and context TP/SL levels.

    Args:
        exit_info: dict from _get_actual_exit_from_binance
        entry: trade_outcomes row dict
    """
    exit_price = exit_info["exit_price"]
    entry_price = entry["entry_price"]

    if entry_price <= 0 or exit_price <= 0:
        return "order_fill"

    pnl_pct = (exit_price - entry_price) / entry_price * 100

    # Parse context_json for actual TP/SL levels
    context = {}
    try:
        if entry.get("context_json"):
            context = json.loads(entry["context_json"]) if isinstance(entry["context_json"], str) else entry["context_json"]
    except Exception:
        pass

    sl_pct = context.get("stop_loss_pct", 0)
    tp_levels = {
        "tp1": context.get("tp1_pct", 0),
        "tp2": context.get("tp2_pct", 0),
        "tp3": context.get("tp3_pct", 0),
    }

    # Check SL first (exit at a loss)
    if sl_pct > 0 and pnl_pct < 0:
        expected_sl_pnl = -sl_pct
        if abs(pnl_pct - expected_sl_pnl) < 2.0:  # within 2% tolerance
            return "sl"

    # Check TP levels (exit at a profit matching a TP target)
    for tp_name, tp_pct in tp_levels.items():
        if tp_pct > 0 and abs(pnl_pct - tp_pct) < 1.5:  # within 1.5% tolerance
            return tp_name

    # Check max_hold: if actual hold time exceeded configured max_hold
    max_hold = context.get("max_hold_hours", 48)
    opened_at = entry.get("entry_time", 0)
    if opened_at and exit_info.get("exit_time"):
        actual_hold = (exit_info["exit_time"] - opened_at) / 3600
        if actual_hold >= max_hold * 0.95:  # within 5% of max_hold
            return "max_hold"

    # Generic fill that doesn't match known reasons
    if pnl_pct > 0:
        return "tp_fill"  # profitable but didn't match expected TP level
    else:
        return "sl_fill"  # loss but didn't match expected SL level


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

    conn = db._get_conn()

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
        # Query actual Binance trade history for accurate exit data
        exit_info = _get_actual_exit_from_binance(
            client, sym, entry["entry_time"]
        )

        if exit_info:
            exit_reason = _determine_exit_reason(exit_info, entry)

            # Use TradeOutcomeRecorder but with actual exit price from Binance
            outcome = recorder.record_outcome(
                symbol=sym,
                exit_price=exit_info["exit_price"],
                exit_reason=exit_reason,
            )

            if outcome:
                # Fix exit_time to actual Binance fill time (not sync detection time)
                conn.execute(
                    "UPDATE trade_outcomes SET exit_time = ? WHERE id = ?",
                    (exit_info["exit_time"], outcome.get("id")),
                )

                closed += 1
                logger.info(
                    f"OUTCOME_SYNC: {sym} closed by {exit_reason} "
                    f"@ ${exit_info['exit_price']:.6f} "
                    f"({exit_info['num_fills']} fills) "
                    f"pnl={outcome['net_pnl_pct']:+.2f}%"
                )

                # Record SELL trade in trades table
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO trades (symbol, side, qty, price, pnl, timestamp)
                           VALUES (?, 'SELL', ?, ?, ?, ?)""",
                        (
                            sym,
                            exit_info["exit_qty"],
                            exit_info["exit_price"],
                            outcome.get("net_pnl_absolute", 0),
                            exit_info["exit_time"],
                        ),
                    )
                    conn.commit()
                except Exception as e:
                    logger.warning(f"Failed to record SELL trade for {sym}: {e}")

            else:
                logger.warning(f"OUTCOME_SYNC: record_outcome returned None for {sym}")
        else:
            # Can't get Binance trades — use current price as fallback
            current_price = price_map.get(sym, 0)
            if current_price > 0:
                outcome = recorder.record_outcome(
                    symbol=sym,
                    exit_price=current_price,
                    exit_reason="order_fill_unknown_price",
                )
                if outcome:
                    closed += 1
                    logger.warning(
                        f"OUTCOME_SYNC: {sym} closed (fallback, no Binance trades found) "
                        f"@ ${current_price:.6f}"
                    )
            else:
                logger.warning(f"OUTCOME_SYNC: {sym} closed but no price available")

    conn.commit()
    result = {"synced": len(open_entries), "closed": closed, "updated": updated}
    return result


if __name__ == "__main__":
    result = sync_outcomes()
    print(json.dumps(result))
