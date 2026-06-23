"""
Strategy Evolver — Phase 5 Self-Evolution.

Automatically promotes/demotes strategies based on performance:
- Disables strategies with <40% win rate after 15+ trades
- Re-enables strategies that recover to >55% win rate
- Logs all changes to audit_log for transparency
- Reads/writes strategy enablement in state.db kv table

P2-fix improvements:
- HMM regime-aware thresholds: bull/bear market sensitivity
- Minimum sample size increased from 10 to 15
- Profit factor consideration: high RR protects from disablement
- Profit factor threshold: PF > 2.0 prevents disable even if WR < threshold

This completes the self-learning loop:
Phase 0 (record) → Phase 1 (learn weights) → Phase 2 (optimize params) →
Phase 3 (multi-strategy) → Phase 4 (data expansion) → Phase 5 (evolve)
"""

import json
import logging
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Base performance thresholds (adjusted by regime)
DISABLE_WIN_RATE = 40.0  # Disable if WR < 40% after MIN_TRADES
RECOVER_WIN_RATE = 55.0  # Re-enable if WR > 55% after being disabled
MIN_TRADES_TO_EVALUATE = 15  # P2-fix: increased from 10 to 15
MIN_TRADES_TO_RECOVER = 5  # Need 5 more trades after disable to consider recovery
PROFIT_FACTOR_PROTECT = 2.0  # P2-fix: PF > 2.0 protects strategy from disablement

# Regime-specific threshold adjustments
# Bull markets are more forgiving (lower disable threshold), bear markets stricter
REGIME_THRESHOLD_ADJUSTMENTS = {
    "BULL_TREND": {"disable_wr_adj": -5.0, "recover_wr_adj": -3.0},  # More forgiving
    "BEAR_TREND": {"disable_wr_adj": +5.0, "recover_wr_adj": +3.0},  # Stricter
    "RANGE_BOUND": {"disable_wr_adj": 0.0, "recover_wr_adj": 0.0},   # Neutral
    "HIGH_VOL": {"disable_wr_adj": +3.0, "recover_wr_adj": +2.0},    # Slightly stricter
}

# All known strategies
ALL_STRATEGIES = ["rsi", "bollinger", "vwap", "trend", "dca", "grid"]


def _get_hmm_regime() -> Optional[str]:
    """Get current HMM regime from cached prediction.

    Returns regime label (BULL_TREND, BEAR_TREND, RANGE_BOUND, HIGH_VOL)
    or None if unavailable.
    """
    try:
        from src.hmm_regime import HMMRegimeDetector

        detector = HMMRegimeDetector()
        cached = detector.get_cached_prediction()
        if cached and cached.get("confidence", 0) > 0.4:
            return cached.get("regime")
    except Exception:
        logger.debug("HMM regime unavailable for strategy evolver")
    return None


def compute_profit_factor(trade_pnls: List[float]) -> float:
    """Compute profit factor from a list of PnL percentages.

    Profit factor = gross_profit / |gross_loss|
    Returns float('inf') if no losses.
    """
    gross_profit = sum(p for p in trade_pnls if p > 0)
    gross_loss = abs(sum(p for p in trade_pnls if p < 0))
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


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

    def _get_regime_adjusted_thresholds(self, regime: Optional[str] = None) -> Dict:
        """Get thresholds adjusted for current HMM market regime.

        P2-fix: Bull markets are more forgiving, bear markets stricter.
        """
        if regime is None:
            regime = _get_hmm_regime()

        adj = REGIME_THRESHOLD_ADJUSTMENTS.get(regime, REGIME_THRESHOLD_ADJUSTMENTS["RANGE_BOUND"])

        disable_wr = DISABLE_WIN_RATE + adj["disable_wr_adj"]
        recover_wr = RECOVER_WIN_RATE + adj["recover_wr_adj"]

        return {
            "disable_wr": disable_wr,
            "recover_wr": recover_wr,
            "regime": regime or "UNKNOWN",
            "adjustment": adj,
        }

    def evaluate_and_evolve(self, regime: Optional[str] = None) -> List[Dict]:
        """Evaluate all strategies and auto-promote/demote.

        P2-fix: Now uses regime-aware thresholds, profit factor protection,
        and minimum sample size of 15.

        Args:
            regime: Optional HMM regime override. If None, reads from cache.

        Returns list of changes made.
        """
        conn = self._db._get_conn()

        # Get per-strategy performance (including per-trade PnL for profit factor)
        rows = conn.execute("""SELECT strategy, COUNT(*) as trades,
                      SUM(CASE WHEN is_win = 1 THEN 1 ELSE 0 END) as wins,
                      AVG(net_pnl_pct) as avg_pnl
            FROM trade_outcomes
            WHERE status = 'closed' AND strategy IS NOT NULL
            GROUP BY strategy""").fetchall()

        if not rows:
            return []

        # Get per-trade PnL for profit factor calculation
        trade_pnls: Dict[str, List[float]] = {}
        try:
            pnl_rows = conn.execute(
                """SELECT strategy, net_pnl_pct
                FROM trade_outcomes
                WHERE status = 'closed' AND strategy IS NOT NULL AND net_pnl_pct IS NOT NULL
                ORDER BY exit_time DESC"""
            ).fetchall()
            for r in pnl_rows:
                s = r["strategy"]
                if s not in trade_pnls:
                    trade_pnls[s] = []
                trade_pnls[s].append(r["net_pnl_pct"])
        except Exception:
            logger.warning("Failed to fetch per-trade PnL for profit factor", exc_info=True)

        disabled = self.get_disabled_strategies()
        changes = []

        # P2-fix: Get regime-adjusted thresholds
        thresholds = self._get_regime_adjusted_thresholds(regime)
        disable_wr = thresholds["disable_wr"]
        recover_wr = thresholds["recover_wr"]
        regime_label = thresholds["regime"]

        logger.info(
            f"StrategyEvolver: regime={regime_label}, "
            f"disable_WR={disable_wr:.1f}%, recover_WR={recover_wr:.1f}%, "
            f"min_trades={MIN_TRADES_TO_EVALUATE}, PF_protect={PROFIT_FACTOR_PROTECT}"
        )

        for row in rows:
            strategy = row["strategy"]
            trades = row["trades"]
            wins = row["wins"] or 0
            avg_pnl = row["avg_pnl"] or 0
            win_rate = (wins / trades * 100) if trades > 0 else 0

            if strategy not in ALL_STRATEGIES:
                continue

            is_disabled = strategy in disabled

            # P2-fix: Compute profit factor for this strategy
            pf = compute_profit_factor(trade_pnls.get(strategy, []))
            pf_protected = pf >= PROFIT_FACTOR_PROTECT

            # Check for disable
            if not is_disabled and trades >= MIN_TRADES_TO_EVALUATE:
                if win_rate < disable_wr:
                    # P2-fix: Profit factor protection — high PF strategies survive
                    if pf_protected:
                        logger.info(
                            f"StrategyEvolver: {strategy} WR={win_rate:.1f}% < {disable_wr:.1f}% "
                            f"but PF={pf:.2f} >= {PROFIT_FACTOR_PROTECT} — PROTECTED"
                        )
                        changes.append(
                            {
                                "action": "PROTECTED",
                                "strategy": strategy,
                                "reason": (
                                    f"WR={win_rate:.1f}% < {disable_wr:.1f}% "
                                    f"but PF={pf:.2f} >= {PROFIT_FACTOR_PROTECT} (regime={regime_label})"
                                ),
                            }
                        )
                        self._log_audit(
                            "strategy_protected",
                            f"{strategy}: WR={win_rate:.1f}%, PF={pf:.2f}, regime={regime_label} — protected",
                        )
                    else:
                        disabled[strategy] = {
                            "disabled_at": time.time(),
                            "reason": f"WR={win_rate:.1f}% < {disable_wr:.1f}% after {trades} trades (regime={regime_label})",
                            "win_rate": round(win_rate, 1),
                            "avg_pnl": round(avg_pnl, 2),
                            "trades": trades,
                            "trades_at_disable": trades,
                            "profit_factor": round(pf, 2),
                            "regime_at_disable": regime_label,
                        }
                        changes.append(
                            {
                                "action": "DISABLED",
                                "strategy": strategy,
                                "reason": (
                                    f"WR={win_rate:.1f}% ({wins}/{trades}), "
                                    f"avg PnL={avg_pnl:+.2f}%, PF={pf:.2f} "
                                    f"(regime={regime_label}, threshold={disable_wr:.1f}%)"
                                ),
                            }
                        )
                        self._log_audit(
                            "strategy_disabled",
                            f"{strategy}: WR={win_rate:.1f}% after {trades} trades, PF={pf:.2f}, regime={regime_label}",
                        )

            # Check for re-enable
            elif is_disabled:
                # Only consider recovery if enough new trades since disable
                trades_at_disable = disabled[strategy].get("trades_at_disable", 0)
                trades_since_disable = trades - trades_at_disable

                if trades_since_disable >= MIN_TRADES_TO_RECOVER and win_rate > recover_wr:
                    del disabled[strategy]
                    changes.append(
                        {
                            "action": "RECOVERED",
                            "strategy": strategy,
                            "reason": f"WR={win_rate:.1f}% > {recover_wr:.1f}% (regime={regime_label})",
                        }
                    )
                    self._log_audit(
                        "strategy_recovered",
                        f"{strategy}: WR={win_rate:.1f}% after {trades} trades, regime={regime_label}",
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

        # Get per-trade PnL for profit factor
        trade_pnls: Dict[str, List[float]] = {}
        try:
            pnl_rows = conn.execute(
                """SELECT strategy, net_pnl_pct
                FROM trade_outcomes
                WHERE status = 'closed' AND strategy IS NOT NULL AND net_pnl_pct IS NOT NULL
                ORDER BY exit_time DESC"""
            ).fetchall()
            for r in pnl_rows:
                s = r["strategy"]
                if s not in trade_pnls:
                    trade_pnls[s] = []
                trade_pnls[s].append(r["net_pnl_pct"])
        except Exception:
            pass

        disabled = self.get_disabled_strategies()
        thresholds = self._get_regime_adjusted_thresholds()

        lines = [
            "## 策略進化報告",
            f"**Regime**: {thresholds['regime']} | "
            f"**Disable WR**: {thresholds['disable_wr']:.1f}% | "
            f"**Recover WR**: {thresholds['recover_wr']:.1f}% | "
            f"**Min Trades**: {MIN_TRADES_TO_EVALUATE}",
            "",
        ]

        if not rows:
            lines.append("尚無閉合交易數據")
            return "\n".join(lines)

        for row in rows:
            strategy = row["strategy"]
            trades = row["trades"]
            wins = row["wins"] or 0
            avg_pnl = row["avg_pnl"] or 0
            win_rate = (wins / trades * 100) if trades > 0 else 0
            pf = compute_profit_factor(trade_pnls.get(strategy, []))

            if strategy not in ALL_STRATEGIES:
                continue

            is_disabled = strategy in disabled
            status = "🚫 已停用" if is_disabled else "✅ 啟用"

            if is_disabled:
                reason = disabled[strategy].get("reason", "")
                lines.append(
                    f"- **{strategy}**: {status} | WR={win_rate:.1f}% ({wins}/{trades}) | "
                    f"avg={avg_pnl:+.2f}% | PF={pf:.2f} | {reason}"
                )
            else:
                indicator = "🟢" if win_rate > 55 else "🔴" if win_rate < 40 else "⚪"
                pf_note = f" | PF={pf:.2f}" if pf >= 2.0 else ""
                lines.append(
                    f"- {indicator} **{strategy}**: {status} | WR={win_rate:.1f}% ({wins}/{trades}) | "
                    f"avg={avg_pnl:+.2f}%{pf_note}"
                )

        # Show disabled strategies not in trade data
        for s in disabled:
            if s not in [r["strategy"] for r in rows]:
                reason = disabled[s].get("reason", "")
                lines.append(f"- 🚫 **{s}**: 已停用 | {reason}")

        return "\n".join(lines)
