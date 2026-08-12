"""
Online Learner — Phase 1 of Self-Learning System.

Computes optimal factor weights from closed trade outcomes using
correlation-based Bayesian updating. The scanner reads these weights
instead of hardcoded values.

Algorithm:
1. For each factor, compute Pearson correlation with net_pnl_pct
2. Factors with positive correlation → increase weight
3. Factors with negative correlation → decrease weight
4. Constrain: each weight ∈ [2%, 25%]
5. Normalize: weights sum to 100%
6. Store in state.db kv table as JSON

Storage: state.db kv key='learned_factor_weights'
"""

import json
import sqlite3
import logging
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Hardcoded defaults (fallback when no learning data exists)
DEFAULT_WEIGHTS = {
    "technical": 16.0,
    "trend": 16.0,
    "volume": 11.0,
    "sentiment": 9.0,
    "price_action": 8.0,
    "obv_divergence": 8.0,
    "consolidation": 8.0,
    "bb_squeeze": 4.0,
    "rsi_divergence": 4.0,
    "onchain": 8.0,
    "market_sentiment": 5.0,
    "orderbook": 3.0,
}

# Factor names in order matching _calculate_weighted_score
FACTOR_NAMES = [
    "technical",
    "trend",
    "volume",
    "sentiment",
    "price_action",
    "obv_divergence",
    "consolidation",
    "bb_squeeze",
    "rsi_divergence",
    "onchain",
    "market_sentiment",
    "orderbook",
]

# Weight constraints
MIN_WEIGHT = 2.0  # No factor below 2%
MAX_WEIGHT = 25.0  # No factor above 25%
MIN_TRADES = 10  # Need at least 10 closed trades to learn

# Learning rate: how aggressively to adjust weights per cycle
# 0.0 = no change, 1.0 = fully replace with learned weights
LEARNING_RATE = 0.3


class OnlineLearner:
    """Computes optimal factor weights from trade outcomes."""

    def __init__(self, db=None):
        if db is None:
            from src.state_db import get_state_db

            db = get_state_db()
        self._db = db

    def get_current_weights(self, _retry: int = 0) -> Dict[str, float]:
        """Get current factor weights (learned or default).

        Includes retry logic for transient SQLite errors (disk I/O on
        network filesystems).
        """
        try:
            conn = self._db._get_conn()
            row = conn.execute(
                "SELECT value FROM kv WHERE key = 'learned_factor_weights'"
            ).fetchone()

            if row:
                try:
                    weights = json.loads(row["value"])
                    # Validate: must have all factors
                    if all(f in weights for f in FACTOR_NAMES):
                        return weights
                except (json.JSONDecodeError, TypeError):
                    logger.error("Failed to parse factor weights from DB", exc_info=True)

            return dict(DEFAULT_WEIGHTS)

        except sqlite3.OperationalError as e:
            if _retry < 2:
                logger.warning(
                    f"OnlineLearner: SQLite error (attempt {_retry+1}/3): {e}, retrying..."
                )
                # Force connection recycle on retry
                try:
                    if hasattr(self._db._local, "conn") and self._db._local.conn:
                        self._db._local.conn.close()
                        self._db._local.conn = None
                except Exception:
                    pass
                import time as _time
                _time.sleep(1 * (_retry + 1))  # 1s, 2s backoff
                return self.get_current_weights(_retry=_retry + 1)
            else:
                logger.error(
                    f"OnlineLearner: SQLite error after 3 attempts, using defaults: {e}"
                )
                return dict(DEFAULT_WEIGHTS)

    def _compute_optimal_weights(
        self, min_trades: int = 5, max_trades: Optional[int] = None
    ) -> Optional[Dict]:
        """Compute optimal factor weights from closed trade outcomes.

        Args:
            min_trades: Minimum trades required for computation.
            max_trades: If set, only use the most recent N trades.

        Returns dict with:
        - weights: new factor weights (dict)
        - stats: per-factor statistics
        - meta: learning metadata (n_trades, learning_rate, etc.)
        Or None if insufficient data.
        """
        conn = self._db._get_conn()
        query = "SELECT * FROM trade_outcomes WHERE status = 'closed' ORDER BY exit_time DESC"
        rows = conn.execute(query).fetchall()

        if max_trades is not None and len(rows) > max_trades:
            rows = rows[:max_trades]

        if len(rows) < min_trades:
            logger.info(f"Insufficient trades for learning: {len(rows)}/{min_trades}")
            return None

        # Convert to dicts
        rows = [dict(r) for r in rows]

        # Extract factor scores and PnL
        factor_scores: Dict[str, List[float]] = {f: [] for f in FACTOR_NAMES}
        pnl_values = []

        for row in rows:
            factors_json = row.get("factors_json")
            if not factors_json:
                continue
            try:
                factors = json.loads(factors_json)
            except (json.JSONDecodeError, TypeError):
                logger.error(
                    "Failed to parse trade factors JSON, skipping trade", exc_info=True
                )
                continue

            pnl = row.get("net_pnl_pct", 0)
            pnl_values.append(pnl)

            for factor in FACTOR_NAMES:
                factor_scores[factor].append(factors.get(factor, 0))

        n = len(pnl_values)
        if n < min_trades:
            return None

        # Compute per-factor statistics
        stats = {}
        for factor in FACTOR_NAMES:
            scores = factor_scores[factor]
            if len(scores) != n:
                continue

            # Pearson correlation
            mean_x = sum(scores) / n
            mean_y = sum(pnl_values) / n
            cov = (
                sum((x - mean_x) * (y - mean_y) for x, y in zip(scores, pnl_values)) / n
            )
            std_x = (sum((x - mean_x) ** 2 for x in scores) / n) ** 0.5
            std_y = (sum((y - mean_y) ** 2 for y in pnl_values) / n) ** 0.5
            correlation = cov / (std_x * std_y) if std_x > 0 and std_y > 0 else 0

            # Winner vs loser averages
            w_scores = [s for s, p in zip(scores, pnl_values) if p > 0]
            l_scores = [s for s, p in zip(scores, pnl_values) if p <= 0]
            avg_winner = sum(w_scores) / len(w_scores) if w_scores else 0
            avg_loser = sum(l_scores) / len(l_scores) if l_scores else 0

            stats[factor] = {
                "correlation": round(correlation, 4),
                "avg_winner": round(avg_winner, 1),
                "avg_loser": round(avg_loser, 1),
                "delta": round(avg_winner - avg_loser, 1),
            }

        # Compute new weights based on correlations
        # Formula: new_weight = default_weight * (1 + learning_rate * correlation)
        # This increases weight for positively-correlated factors,
        # decreases for negatively-correlated ones.
        current = self.get_current_weights()
        raw_weights = {}

        for factor in FACTOR_NAMES:
            corr = stats.get(factor, {}).get("correlation", 0)
            base = current.get(factor, DEFAULT_WEIGHTS[factor])

            # Adjust: positive correlation → increase, negative → decrease
            adjustment = 1.0 + LEARNING_RATE * corr
            raw_weights[factor] = base * adjustment

        # Constrain to [MIN_WEIGHT, MAX_WEIGHT]
        for factor in FACTOR_NAMES:
            raw_weights[factor] = max(MIN_WEIGHT, min(MAX_WEIGHT, raw_weights[factor]))

        # Normalize to sum = 100
        total = sum(raw_weights.values())
        if total > 0:
            weights = {f: round(w / total * 100, 2) for f, w in raw_weights.items()}
        else:
            weights = dict(DEFAULT_WEIGHTS)

        # Verify normalization (fix rounding)
        diff = 100.0 - sum(weights.values())
        if abs(diff) > 0.01:
            # Adjust the largest weight to absorb rounding error
            max_factor = max(weights, key=lambda k: weights[k])
            weights[max_factor] = round(weights[max_factor] + diff, 2)

        return {
            "weights": weights,
            "stats": stats,
            "meta": {
                "n_trades": n,
                "n_winners": sum(1 for p in pnl_values if p > 0),
                "n_losers": sum(1 for p in pnl_values if p <= 0),
                "win_rate": round(sum(1 for p in pnl_values if p > 0) / n * 100, 1),
                "avg_pnl": round(sum(pnl_values) / n, 2),
                "learning_rate": LEARNING_RATE,
                "timestamp": time.time(),
            },
        }

    def compute_optimal_weights(self) -> Optional[Dict]:
        """Public API — compute optimal weights using all trades."""
        return self._compute_optimal_weights(min_trades=MIN_TRADES)

    def learn_and_store(self) -> Optional[Dict]:
        """Run learning algorithm and store new weights in DB.

        Returns the full result dict, or None if insufficient data.
        """
        # Capture old weights BEFORE computing new ones
        old_weights = self.get_current_weights()

        result = self.compute_optimal_weights()
        if not result:
            return None

        weights = result["weights"]

        # Store in kv table
        conn = self._db._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO kv (key, value, updated_at)
            VALUES ('learned_factor_weights', ?, ?)""",
            (json.dumps(weights), time.time()),
        )
        conn.commit()

        # Log the change
        changes = []
        for f in FACTOR_NAMES:
            old = old_weights.get(f, 0)
            new = weights[f]
            if abs(old - new) > 0.1:
                changes.append(f"{f}: {old:.1f}% → {new:.1f}%")

        if changes:
            logger.info(f"LEARNED_WEIGHTS updated: {', '.join(changes)}")
        else:
            logger.info("LEARNED_WEIGHTS: no significant changes")

        result["changes"] = changes

        # Also update strategy weights (Phase 3)
        try:
            from src.strategy_registry import StrategyRegistry

            registry = StrategyRegistry(db=self._db)
            sw_result = registry.update_strategy_weights()
            if sw_result and sw_result.get("changes"):
                result["strategy_weight_changes"] = sw_result["changes"]
                logger.info(f"Strategy weights updated: {sw_result['changes']}")
        except Exception as e:
            logger.debug(f"Strategy weight update skipped: {e}")

        # Also run strategy evolution (Phase 5)
        try:
            from src.strategy_evolver import StrategyEvolver

            evolver = StrategyEvolver(db=self._db)
            evo_changes = evolver.evaluate_and_evolve()
            if evo_changes:
                result["evolution_changes"] = evo_changes
                for c in evo_changes:
                    logger.info(
                        f"STRATEGY_EVOLVED: {c['action']} {c['strategy']} — {c['reason']}"
                    )
        except Exception as e:
            logger.debug(f"Strategy evolution skipped: {e}")

        # Also trigger HMM auto-retrain if conditions are met
        try:
            from src.hmm_regime import HMMRegimeDetector
            from src.binance_client import BinanceClient

            hmm = HMMRegimeDetector(db=self._db)
            retrain_check = hmm.should_retrain()
            if retrain_check["should_retrain"]:
                logger.info(
                    f"HMM retrain triggered: {retrain_check['reason']} — "
                    f"fetching klines for retraining"
                )
                try:
                    client = BinanceClient(testnet=False)
                    klines = client.get_klines("BTCUSDT", "1h", limit=720)
                    if klines and len(klines) >= 50:
                        hmm.auto_retrain(klines_1h=klines)
                    else:
                        logger.warning("HMM retrain: insufficient kline data (%s)", len(klines) if klines else 0)
                except Exception as e:
                    logger.warning(f"HMM retrain data fetch failed: {e}")
        except Exception as e:
            logger.debug(f"HMM auto-retrain check skipped: {e}")

        # Also run concept drift detection (Phase 7)
        try:
            from src.concept_drift import ConceptDriftDetector

            drift_detector = ConceptDriftDetector(db=self._db)
            drift_result = drift_detector.detect_drift()
            if drift_result and drift_result.get("drift_detected"):
                result["drift_detection"] = drift_result
                logger.warning(
                    f"CONCEPT_DRIFT: severity={drift_result['severity']} "
                    f"signals={drift_result['drift_signals']}/3 — {drift_result['recommendation']}"
                )
                # Re-optimize weights using only recent trades to adapt to new regime
                logger.warning(
                    "Concept drift detected — blending recent-trade weights with full-sample weights"
                )
                optimal = self._compute_optimal_weights(min_trades=10, max_trades=20)
                if optimal:
                    # Blend: 60% recent + 40% full-sample to avoid ping-pong
                    current_weights = result.get("weights", DEFAULT_WEIGHTS)
                    blended = {}
                    for factor in optimal["weights"]:
                        recent_w = optimal["weights"][factor]
                        full_w = current_weights.get(
                            factor, DEFAULT_WEIGHTS.get(factor, 5.0)
                        )
                        blended[factor] = round(0.6 * recent_w + 0.4 * full_w, 2)
                    # Store the blended weights
                    conn = self._db._get_conn()
                    conn.execute(
                        """INSERT OR REPLACE INTO kv (key, value, updated_at)
                        VALUES ('learned_factor_weights', ?, ?)""",
                        (json.dumps(blended), time.time()),
                    )
                    conn.commit()
                    result["drift_reoptimized"] = blended
                    logger.info(
                        "Blended drift-adapted weights (60%% recent + 40%% full-sample)"
                    )
        except Exception as e:
            logger.debug(f"Drift detection skipped: {e}")

        return result

    def get_weight_history(self) -> List[Dict]:
        """Get history of weight changes (from audit_log)."""
        conn = self._db._get_conn()
        rows = conn.execute("""SELECT * FROM audit_log
            WHERE action = 'learned_weights_update'
            ORDER BY timestamp DESC LIMIT 20""").fetchall()
        return [dict(r) for r in rows]

    def format_report(self, result: Dict) -> str:
        """Format learning result as human-readable report."""
        if not result:
            return "學習數據不足（需要至少 10 筆閉合交易）"

        lines = ["## 因子權重學習報告", ""]

        # Meta
        meta = result["meta"]
        lines.append(f"**交易數**: {meta['n_trades']} (勝率 {meta['win_rate']}%)")
        lines.append(f"**平均 PnL**: {meta['avg_pnl']:+.2f}%")
        lines.append("")

        # Weight changes
        if result.get("changes"):
            lines.append("**權重調整**:")
            for change in result["changes"]:
                lines.append(f"- {change}")
            lines.append("")

        # Factor stats
        lines.append("**因子相關性**:")
        for factor, stat in result["stats"].items():
            corr = stat["correlation"]
            indicator = "🟢" if corr > 0.1 else "🔴" if corr < -0.1 else "⚪"
            lines.append(
                f"- {indicator} {factor}: r={corr:+.3f} "
                f"(贏家均分={stat['avg_winner']:.0f}, 輸家均分={stat['avg_loser']:.0f})"
            )

        return "\n".join(lines)
