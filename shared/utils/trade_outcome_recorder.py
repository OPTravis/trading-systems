"""
Trade Outcome Recorder — Phase 0 of Self-Learning System.

Records complete trade lifecycle for future ML optimization:
- Entry: all 11 factor scores, regime, Kelly sizing, market context
- Exit: pnl, time held, exit reason, max drawdown/profit during trade

This is the foundation for:
- Online learning (factor weight updates based on outcomes)
- Parameter optimization (grid search over historical results)
- Strategy evaluation (which factors predict profitable trades)

Storage: state.db trade_outcomes table
"""

import json
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class TradeOutcomeRecorder:
    """Records trade entry/exit data for self-learning pipeline."""

    def __init__(self, db=None):
        if db is None:
            from ..core.state_db import get_state_db
            db = get_state_db()
        self._db = db

    def record_entry(
        self,
        symbol: str,
        entry_price: float,
        qty: float,
        score: float,
        strategy: str,
        # Factor scores (0-100 each)
        f_technical: float = 0,
        f_trend: float = 0,
        f_volume: float = 0,
        f_sentiment: float = 0,
        f_price_action: float = 0,
        f_obv_divergence: float = 0,
        f_consolidation: float = 0,
        f_bb_squeeze: float = 0,
        f_rsi_divergence: float = 0,
        f_onchain: float = 0,
        f_market_sentiment: float = 0,
        # Market context
        regime: str = "",
        fng_score: int = 0,
        fng_label: str = "",
        btc_trend: str = "",
        # Kelly sizing
        kelly_pct: float = 0,
        kelly_win_rate: float = 0,
        kelly_confidence: str = "",
        # Risk parameters
        stop_loss_pct: float = 0,
        tp1_pct: float = 0,
        tp2_pct: float = 0,
        tp3_pct: float = 0,
        max_hold_hours: int = 72,
        # Research
        research_adj: float = 0,
        bear_score: float = 0,
        bear_veto: bool = False,
    ) -> int:
        """Record trade entry with full factor decomposition.

        Returns: row ID for later outcome_update().
        """
        now = time.time()
        date_str = datetime.now().strftime("%Y-%m-%d")

        factors_json = json.dumps({
            "technical": f_technical,
            "trend": f_trend,
            "volume": f_volume,
            "sentiment": f_sentiment,
            "price_action": f_price_action,
            "obv_divergence": f_obv_divergence,
            "consolidation": f_consolidation,
            "bb_squeeze": f_bb_squeeze,
            "rsi_divergence": f_rsi_divergence,
            "onchain": f_onchain,
            "market_sentiment": f_market_sentiment,
        })

        context_json = json.dumps({
            "regime": regime,
            "fng_score": fng_score,
            "fng_label": fng_label,
            "btc_trend": btc_trend,
            "kelly_pct": kelly_pct,
            "kelly_win_rate": kelly_win_rate,
            "kelly_confidence": kelly_confidence,
            "stop_loss_pct": stop_loss_pct,
            "tp1_pct": tp1_pct,
            "tp2_pct": tp2_pct,
            "tp3_pct": tp3_pct,
            "max_hold_hours": max_hold_hours,
            "research_adj": research_adj,
            "bear_score": bear_score,
            "bear_veto": bear_veto,
        })

        rowid = self._db._get_conn().execute(
            """INSERT INTO trade_outcomes
            (symbol, entry_time, entry_date, entry_price, qty, score, strategy,
             factors_json, context_json, status,
             peak_price, trough_price, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)""",
            (
                symbol, now, date_str, entry_price, qty, score, strategy,
                factors_json, context_json,
                entry_price, entry_price,  # peak = trough = entry initially
                now, now,
            ),
        ).lastrowid
        self._db._get_conn().commit()

        logger.info(
            f"OUTCOME_ENTRY: {symbol} entry=${entry_price:.6f} qty={qty} "
            f"score={score} strategy={strategy} rowid={rowid}"
        )
        return rowid

    def update_price_extremes(self, symbol: str, current_price: float):
        """Update peak/trough prices for open positions.

        Called periodically (e.g., by unified-monitor) to track max profit
        and max drawdown during the trade lifetime.
        """
        conn = self._db._get_conn()
        row = conn.execute(
            """SELECT id, peak_price, trough_price, entry_price
            FROM trade_outcomes WHERE symbol = ? AND status = 'open'
            ORDER BY entry_time DESC LIMIT 1""",
            (symbol,),
        ).fetchone()

        if not row:
            return

        row_id = row["id"]
        peak = max(row["peak_price"], current_price)
        trough = min(row["trough_price"], current_price)

        conn.execute(
            """UPDATE trade_outcomes
            SET peak_price = ?, trough_price = ?, updated_at = ?
            WHERE id = ?""",
            (peak, trough, time.time(), row_id),
        )
        conn.commit()

    def record_outcome(
        self,
        symbol: str,
        exit_price: float,
        exit_reason: str = "unknown",
        entry_id: Optional[int] = None,
    ) -> Optional[Dict]:
        """Record trade exit and compute derived metrics.

        Args:
            symbol: Trading pair (e.g., "ENAUSDT")
            exit_price: Actual exit price
            exit_reason: "tp1", "tp2", "tp3", "sl", "trailing", "max_hold", "manual"
            entry_id: Optional row ID from record_entry() for precise matching

        Returns: outcome dict with computed metrics, or None if no open entry found.
        """
        conn = self._db._get_conn()
        if entry_id:
            row = conn.execute(
                "SELECT * FROM trade_outcomes WHERE id = ?",
                (entry_id,),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT * FROM trade_outcomes
                WHERE symbol = ? AND status = 'open'
                ORDER BY entry_time DESC LIMIT 1""",
                (symbol,),
            ).fetchone()

        if not row:
            logger.warning(f"OUTCOME_UPDATE: No open entry for {symbol}")
            return None

        now = time.time()
        entry_price = row["entry_price"]
        entry_time = row["entry_time"]
        qty = row["qty"]
        peak = max(row["peak_price"], exit_price)
        trough = min(row["trough_price"], exit_price)

        # Derived metrics
        pnl_pct = (exit_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
        pnl_absolute = (exit_price - entry_price) * qty if entry_price > 0 else 0
        time_held_hours = (now - entry_time) / 3600
        max_profit_pct = (peak - entry_price) / entry_price * 100 if entry_price > 0 else 0
        max_drawdown_pct = (trough - entry_price) / entry_price * 100 if entry_price > 0 else 0

        # Fees (Binance spot: 0.1% taker, 0.075% with BNB)
        fee_rate = 0.001
        fees_absolute = entry_price * qty * fee_rate * 2  # buy + sell
        net_pnl_absolute = pnl_absolute - fees_absolute
        net_pnl_pct = net_pnl_absolute / (entry_price * qty) * 100 if entry_price * qty > 0 else 0

        # Win/loss classification
        is_win = net_pnl_pct > 0

        conn.execute(
            """UPDATE trade_outcomes SET
                exit_time = ?, exit_price = ?, exit_reason = ?,
                pnl_pct = ?, pnl_absolute = ?,
                net_pnl_pct = ?, net_pnl_absolute = ?,
                time_held_hours = ?,
                max_profit_pct = ?, max_drawdown_pct = ?,
                peak_price = ?, trough_price = ?,
                is_win = ?, status = 'closed',
                updated_at = ?
            WHERE id = ?""",
            (
                now, exit_price, exit_reason,
                round(pnl_pct, 4), round(pnl_absolute, 6),
                round(net_pnl_pct, 4), round(net_pnl_absolute, 6),
                round(time_held_hours, 2),
                round(max_profit_pct, 4), round(max_drawdown_pct, 4),
                peak, trough,
                is_win, now, row["id"],
            ),
        )
        conn.commit()

        outcome = {
            "symbol": symbol,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "pnl_pct": round(pnl_pct, 2),
            "net_pnl_pct": round(net_pnl_pct, 2),
            "net_pnl_absolute": round(net_pnl_absolute, 6),
            "time_held_hours": round(time_held_hours, 1),
            "max_profit_pct": round(max_profit_pct, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "is_win": is_win,
            "score": row["score"],
            "strategy": row["strategy"],
        }

        logger.info(
            f"OUTCOME_CLOSE: {symbol} {exit_reason} pnl={net_pnl_pct:+.2f}% "
            f"time={time_held_hours:.1f}h score={row['score']} → {'WIN' if is_win else 'LOSS'}"
        )
        return outcome

    def get_open_entries(self) -> List[Dict]:
        """Get all open (unclosed) trade entries."""
        rows = self._db._get_conn().execute(
            "SELECT * FROM trade_outcomes WHERE status = 'open' ORDER BY entry_time DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_closed_outcomes(self, limit: int = 50, strategy: Optional[str] = None) -> List[Dict]:
        """Get closed trade outcomes for analysis."""
        if strategy:
            rows = self._db._get_conn().execute(
                """SELECT * FROM trade_outcomes
                WHERE status = 'closed' AND strategy = ?
                ORDER BY exit_time DESC LIMIT ?""",
                (strategy, limit),
            ).fetchall()
        else:
            rows = self._db._get_conn().execute(
                """SELECT * FROM trade_outcomes
                WHERE status = 'closed'
                ORDER BY exit_time DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_factor_stats(self, min_trades: int = 5) -> Optional[Dict]:
        """Compute factor-level statistics from closed trades.

        Returns per-factor correlation with PnL, and avg score for winners vs losers.
        Used by the learning layer to adjust factor weights.
        """
        rows = self._db._get_conn().execute(
            "SELECT * FROM trade_outcomes WHERE status = 'closed'"
        ).fetchall()

        if len(rows) < min_trades:
            return None

        # Convert sqlite3.Row to dict for .get() access
        rows = [dict(r) for r in rows]

        winners = [r for r in rows if r["is_win"]]
        losers = [r for r in rows if not r["is_win"]]

        factors = [
            "technical", "trend", "volume", "sentiment", "price_action",
            "obv_divergence", "consolidation", "bb_squeeze", "rsi_divergence",
            "onchain", "market_sentiment",
        ]

        stats = {}
        for factor in factors:
            # Parse factor scores from JSON
            w_scores = []
            l_scores = []
            all_scores = []
            all_pnl = []

            for r in rows:
                factors_data = json.loads(r["factors_json"]) if r["factors_json"] else {}
                score = factors_data.get(factor, 0)
                all_scores.append(score)
                all_pnl.append(r.get("net_pnl_pct", 0))
                if r["is_win"]:
                    w_scores.append(score)
                else:
                    l_scores.append(score)

            avg_winner = sum(w_scores) / len(w_scores) if w_scores else 0
            avg_loser = sum(l_scores) / len(l_scores) if l_scores else 0

            # Simple correlation (Pearson)
            n = len(all_scores)
            if n > 2:
                mean_x = sum(all_scores) / n
                mean_y = sum(all_pnl) / n
                cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(all_scores, all_pnl)) / n
                std_x = (sum((x - mean_x) ** 2 for x in all_scores) / n) ** 0.5
                std_y = (sum((y - mean_y) ** 2 for y in all_pnl) / n) ** 0.5
                correlation = cov / (std_x * std_y) if std_x > 0 and std_y > 0 else 0
            else:
                correlation = 0

            stats[factor] = {
                "avg_winner": round(avg_winner, 1),
                "avg_loser": round(avg_loser, 1),
                "delta": round(avg_winner - avg_loser, 1),
                "correlation": round(correlation, 3),
                "n_trades": n,
            }

        return {
            "total_trades": len(rows),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": round(len(winners) / len(rows) * 100, 1) if rows else 0,
            "factors": stats,
        }

    def get_summary(self) -> Dict:
        """Get overall outcome summary statistics."""
        conn = self._db._get_conn()

        open_count = conn.execute(
            "SELECT COUNT(*) FROM trade_outcomes WHERE status = 'open'"
        ).fetchone()[0]

        closed_rows = conn.execute(
            "SELECT * FROM trade_outcomes WHERE status = 'closed'"
        ).fetchall()

        # Convert sqlite3.Row to dict for consistent access
        closed_rows = [dict(r) for r in closed_rows]

        if not closed_rows:
            return {
                "open_trades": open_count,
                "closed_trades": 0,
                "win_rate": 0,
                "avg_pnl_pct": 0,
                "avg_time_hours": 0,
                "best_trade": None,
                "worst_trade": None,
            }

        wins = [r for r in closed_rows if r["is_win"]]
        pnls = [r["net_pnl_pct"] for r in closed_rows]
        times = [r["time_held_hours"] for r in closed_rows if r["time_held_hours"]]

        best = max(closed_rows, key=lambda r: r["net_pnl_pct"])
        worst = min(closed_rows, key=lambda r: r["net_pnl_pct"])

        return {
            "open_trades": open_count,
            "closed_trades": len(closed_rows),
            "win_rate": round(len(wins) / len(closed_rows) * 100, 1),
            "avg_pnl_pct": round(sum(pnls) / len(pnls), 2),
            "avg_time_hours": round(sum(times) / len(times), 1) if times else 0,
            "best_trade": {
                "symbol": best["symbol"],
                "pnl_pct": round(best["net_pnl_pct"], 2),
                "score": best["score"],
            },
            "worst_trade": {
                "symbol": worst["symbol"],
                "pnl_pct": round(worst["net_pnl_pct"], 2),
                "score": worst["score"],
            },
        }
