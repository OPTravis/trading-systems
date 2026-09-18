"""Rolling per-strategy performance stats (Phase 2-A, 2026-09-18).

Refreshed on every closed trade (record_outcome path — normal exits AND
the reconciler fill-booking path share it), cached in state DB kv for
downstream consumers (Phase 2-B strategy auto-switch).

Window semantics: the intersection of "most recent WINDOW_TRADES closed
trades" and "closed within WINDOW_DAYS" — i.e. at most 30 trades, none
older than 7 days. Stats below MIN_SAMPLE_TRADES are still written but
flagged insufficient=True so consumers can apply their own sample floor.

All functions fail safe: any error returns/leaves cache untouched —
learning must never break trading.
"""
from __future__ import annotations

import json
import logging
import math
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

WINDOW_TRADES = 30
WINDOW_DAYS = 7.0
MIN_SAMPLE_TRADES = 5
STORAGE_KEY = "strategy_rolling_stats"


def compute_profit_factor(pnls: List[float]) -> float:
    """Gross profit / gross loss. 0 when no losses and no profit; math.inf
    when profitable with zero losses."""
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    if gross_loss <= 0:
        return math.inf if gross_win > 0 else 0.0
    return gross_win / gross_loss


def _window_rows(rows: List[Dict], now: float) -> List[Dict]:
    """rows must be ORDER BY exit_time DESC. Keep the intersection of the
    last WINDOW_TRADES and the WINDOW_DAYS band."""
    recent = rows[:WINDOW_TRADES]
    cutoff = now - WINDOW_DAYS * 86400
    kept = [r for r in recent if (r["exit_time"] or 0) >= cutoff]
    return kept


def refresh_rolling_stats(db=None) -> Dict[str, Dict]:
    """Recompute rolling stats for every strategy; persist to kv."""
    if db is None:
        from src.state_db import get_state_db
        db = get_state_db()
    now = time.time()
    stats: Dict[str, Dict] = {}
    try:
        rows = db._get_conn().execute(
            """SELECT strategy, exit_time, net_pnl_pct, is_win
            FROM trade_outcomes
            WHERE status = 'closed' AND strategy IS NOT NULL
            ORDER BY exit_time DESC"""
        ).fetchall()
        by_strategy: Dict[str, List[Dict]] = {}
        for r in rows:
            by_strategy.setdefault(r["strategy"], []).append(dict(r))
        for strat, srows in by_strategy.items():
            kept = _window_rows(srows, now)
            pnls = [float(r["net_pnl_pct"]) for r in kept
                    if r["net_pnl_pct"] is not None]
            n = len(kept)
            wins = sum(1 for r in kept if r["is_win"])
            stats[strat] = {
                "n": n,
                "wins": wins,
                "wr": round(wins / n * 100, 1) if n else 0.0,
                "pf": round(compute_profit_factor(pnls), 2)
                    if math.isfinite(compute_profit_factor(pnls)) else "inf",
                "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
                "insufficient": n < MIN_SAMPLE_TRADES,
                "window": {"trades": WINDOW_TRADES, "days": WINDOW_DAYS},
                "computed_at": round(now, 3),
            }
        db.kv_set(STORAGE_KEY, stats)
        return stats
    except Exception:
        logger.warning("rolling stats refresh failed (non-critical)",
                       exc_info=True)
        return {}


def get_rolling_stats(db=None) -> Dict[str, Dict]:
    """Read cached rolling stats; empty dict when absent/corrupt."""
    if db is None:
        from src.state_db import get_state_db
        db = get_state_db()
    try:
        raw = db.kv_get(STORAGE_KEY)
        if raw and isinstance(raw, dict):
            return raw
    except Exception:
        logger.warning("rolling stats read failed", exc_info=True)
    return {}
