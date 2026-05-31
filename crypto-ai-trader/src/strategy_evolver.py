"""
Strategy Evolver — Phase 5 Self-Evolution.

Automatically promotes/demotes strategies based on performance:
- Disables strategies with <40% win rate after 10+ trades
- Re-enables strategies that recover to >55% win rate
- Logs all changes to audit_log for transparency
- Reads/writes strategy enablement in state.db kv table

This completes the self-learning loop:
Phase 0 (record) → Phase 1 (learn weights) → Phase 2 (optimize params) →
Phase 3 (multi-strategy) → Phase 4 (data expansion) → Phase 5 (evolve)
"""

import json
import logging
import time
from typing import Dict, List

logger = logging.getLogger(__name__)

# Performance thresholds
DISABLE_WIN_RATE = 40.0  # Disable if WR < 40% after MIN_TRADES
RECOVER_WIN_RATE = 55.0  # Re-enable if WR > 55% after being disabled
MIN_TRADES_TO_EVALUATE = 10  # Need at least 10 trades to evaluate
MIN_TRADES_TO_RECOVER = 5  # Need 5 more trades after disable to consider recovery

# All known strategies
ALL_STRATEGIES = ["rsi", "bollinger", "vwap", "trend", "dca", "grid"]


class StrategyEvolver:
    """Auto-promote/demote strategies based on performance."""

    def __init__(self, db=None):
        if db is None:
            from src.state_db import get_state_db

            db = get_state_db()
        self._db = db

    def get_disabled_strategies(self) -> Dict[str, Dict]:
        """Get strategies that were auto-disabled by the evolver.

        Returns: {strategy_name: {"disabled_at": timestamp, "reason": str}}
        """
        conn = self._db._get_conn()
        row = conn.execute(
            "SELECT value FROM kv WHERE key = 'evolved_disabled'"
        ).fetchone()

        if row:
            try:
                return json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Failed to parse evolved_disabled JSON from StateDB", exc_info=True
                )
        return {}

    def _set_disabled(self, disabled: Dict[str, Dict]):
        """Store disabled strategies."""
        conn = self._db._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO kv (key, value, updated_at)
            VALUES ('evolved_disabled', ?, ?)""",
            (json.dumps(disabled), time.time()),
        )
        conn.commit()

    def _log_audit(self, action: str, details: str):
        """Log evolution action to audit_log."""
        conn = self._db._get_conn()
        conn.execute(
            """INSERT INTO audit_log (timestamp, action, old_value, new_value, source)
            VALUES (?, ?, ?, ?, 'strategy_evolver')""",
            (time.time(), action, None, details),
        )
        conn.commit()

    def evaluate_and_evolve(self) -> List[Dict]:
        """Evaluate all strategies and auto-promote/demote.

        Returns list of changes made.
        """
        conn = self._db._get_conn()

        # Get per-strategy performance
        rows = conn.execute("""SELECT strategy, COUNT(*) as trades,
                      SUM(CASE WHEN is_win = 1 THEN 1 ELSE 0 END) as wins,
                      AVG(net_pnl_pct) as avg_pnl
            FROM trade_outcomes
            WHERE status = 'closed' AND strategy IS NOT NULL
            GROUP BY strategy""").fetchall()

        if not rows:
            return []

        disabled = self.get_disabled_strategies()
        changes = []

        for row in rows:
            strategy = row["strategy"]
            trades = row["trades"]
            wins = row["wins"] or 0
            avg_pnl = row["avg_pnl"] or 0
            win_rate = (wins / trades * 100) if trades > 0 else 0

            if strategy not in ALL_STRATEGIES:
                continue

            is_disabled = strategy in disabled

            # Check for disable
            if not is_disabled and trades >= MIN_TRADES_TO_EVALUATE:
                if win_rate < DISABLE_WIN_RATE:
                    disabled[strategy] = {
                        "disabled_at": time.time(),
                        "reason": f"WR={win_rate:.1f}% < {DISABLE_WIN_RATE}% after {trades} trades",
                        "win_rate": round(win_rate, 1),
                        "avg_pnl": round(avg_pnl, 2),
                        "trades": trades,
                    }
                    changes.append(
                        {
                            "action": "DISABLED",
                            "strategy": strategy,
                            "reason": f"WR={win_rate:.1f}% ({wins}/{trades}), avg PnL={avg_pnl:+.2f}%",
                        }
                    )
                    self._log_audit(
                        "strategy_disabled",
                        f"{strategy}: WR={win_rate:.1f}% after {trades} trades",
                    )

            # Check for re-enable
            elif is_disabled:
                # Only consider recovery if enough new trades since disable
                disabled[strategy]

                if trades >= MIN_TRADES_TO_EVALUATE and win_rate > RECOVER_WIN_RATE:
                    del disabled[strategy]
                    changes.append(
                        {
                            "action": "RECOVERED",
                            "strategy": strategy,
                            "reason": f"WR={win_rate:.1f}% > {RECOVER_WIN_RATE}%",
                        }
                    )
                    self._log_audit(
                        "strategy_recovered",
                        f"{strategy}: WR={win_rate:.1f}% after {trades} trades",
                    )

        self._set_disabled(disabled)
        return changes

    def get_evolution_report(self) -> str:
        """Format evolution status as report."""
        conn = self._db._get_conn()

        # Get per-strategy stats
        rows = conn.execute("""SELECT strategy, COUNT(*) as trades,
                      SUM(CASE WHEN is_win = 1 THEN 1 ELSE 0 END) as wins,
                      AVG(net_pnl_pct) as avg_pnl
            FROM trade_outcomes
            WHERE status = 'closed' AND strategy IS NOT NULL
            GROUP BY strategy""").fetchall()

        disabled = self.get_disabled_strategies()
        lines = ["## 策略進化報告", ""]

        if not rows:
            lines.append("尚無閉合交易數據")
            return "\n".join(lines)

        for row in rows:
            strategy = row["strategy"]
            trades = row["trades"]
            wins = row["wins"] or 0
            avg_pnl = row["avg_pnl"] or 0
            win_rate = (wins / trades * 100) if trades > 0 else 0

            if strategy not in ALL_STRATEGIES:
                continue

            is_disabled = strategy in disabled
            status = "🚫 已停用" if is_disabled else "✅ 啟用"

            if is_disabled:
                reason = disabled[strategy].get("reason", "")
                lines.append(
                    f"- **{strategy}**: {status} | WR={win_rate:.1f}% ({wins}/{trades}) | avg={avg_pnl:+.2f}% | {reason}"
                )
            else:
                indicator = "🟢" if win_rate > 55 else "🔴" if win_rate < 40 else "⚪"
                lines.append(
                    f"- {indicator} **{strategy}**: {status} | WR={win_rate:.1f}% ({wins}/{trades}) | avg={avg_pnl:+.2f}%"
                )

        # Show disabled strategies not in trade data
        for s in disabled:
            if s not in [r["strategy"] for r in rows]:
                reason = disabled[s].get("reason", "")
                lines.append(f"- 🚫 **{s}**: 已停用 | {reason}")

        return "\n".join(lines)
