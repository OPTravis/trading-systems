"""
Strategy Registry — Phase 3 Multi-Strategy Engine.

Central registry that:
1. Instantiates all strategies with their params
2. Runs all enabled strategies for a given coin/klines
3. Aggregates signals via weighted voting
4. Reads per-strategy performance weights from DB

Replaces the brittle keyword-based strategy selection in scan_orchestrator.
"""

import json
import logging
import time
from typing import Dict, List, Optional, Tuple

from src.strategies.base import SignalType

logger = logging.getLogger(__name__)

# Strategy class mapping
STRATEGY_CLASSES = {
    "rsi": "RSIStrategy",
    "bollinger": "BollingerStrategy",
    "vwap": "VWAPStrategy",
    "trend": "TrendStrategy",
    "dca": "DCAStrategy",
    "grid": "GridStrategy",
}

# Default strategy weights (equal weight, adjusted by performance)
DEFAULT_STRATEGY_WEIGHTS = {
    "rsi": 1.0,
    "bollinger": 1.0,
    "vwap": 1.0,
    "trend": 1.0,
    "dca": 1.0,
    "grid": 1.0,
}

# Min trades before performance-based weighting kicks in
MIN_STRATEGY_TRADES = 5


class StrategyRegistry:
    """Central registry for all trading strategies."""

    def __init__(self, db=None):
        if db is None:
            from src.state_db import get_state_db
            db = get_state_db()
        self._db = db
        self._strategies = {}
        self._init_strategies()

    def _init_strategies(self):
        """Initialize all strategy instances."""
        from src.strategies.rsi_reversion import RSIStrategy
        from src.strategies.bollinger import BollingerStrategy
        from src.strategies.vwap import VWAPStrategy
        from src.strategies.trend import TrendStrategy
        from src.strategies.dca import DCAStrategy
        from src.strategies.grid import GridStrategy

        # Load params from optimized DB or defaults
        opt_params = self._get_optimized_params()

        self._strategies = {
            "rsi": RSIStrategy({
                "rsi_oversold": opt_params.get("rsi_oversold", 35),
                "rsi_overbought": opt_params.get("rsi_overbought", 65),
                "stop_loss_pct": opt_params.get("stop_loss_pct", 5.0),
                "take_profit_pct": opt_params.get("take_profit_pct", 8.0),
            }),
            "bollinger": BollingerStrategy({
                "stop_loss_pct": opt_params.get("stop_loss_pct", 5.0),
                "take_profit_pct": opt_params.get("take_profit_pct", 8.0),
            }),
            "vwap": VWAPStrategy({
                "stop_loss_pct": opt_params.get("stop_loss_pct", 5.0),
                "take_profit_pct": opt_params.get("take_profit_pct", 8.0),
            }),
            "trend": TrendStrategy({
                "stop_loss_pct": opt_params.get("stop_loss_pct", 5.0),
                "take_profit_pct": opt_params.get("take_profit_pct", 8.0),
            }),
            "dca": DCAStrategy({}),
            "grid": GridStrategy({}),
        }

    def _get_optimized_params(self) -> Dict:
        """Read optimized params from DB."""
        try:
            conn = self._db._get_conn()
            row = conn.execute(
                "SELECT value FROM kv WHERE key = 'optimized_params'"
            ).fetchone()
            if row:
                return json.loads(row["value"])
        except Exception:
            logger.error("Failed to parse optimized params from DB", exc_info=True)
        return {}

    def get_strategy_weights(self) -> Dict[str, float]:
        """Get per-strategy weights (performance-adjusted or default)."""
        conn = self._db._get_conn()
        row = conn.execute(
            "SELECT value FROM kv WHERE key = 'strategy_weights'"
        ).fetchone()

        if row:
            try:
                weights = json.loads(row["value"])
                if all(k in weights for k in DEFAULT_STRATEGY_WEIGHTS):
                    return weights
            except (json.JSONDecodeError, TypeError):
                logger.error("Failed to parse strategy weights from DB", exc_info=True)

        return dict(DEFAULT_STRATEGY_WEIGHTS)

    def compute_strategy_weights(self) -> Optional[Dict[str, float]]:
        """Compute performance-based strategy weights from trade outcomes.

        Uses win rate and avg PnL per strategy to adjust weights.
        Returns None if insufficient data.
        """
        conn = self._db._get_conn()
        rows = conn.execute(
            """SELECT strategy, net_pnl_pct, is_win
            FROM trade_outcomes WHERE status = 'closed'"""
        ).fetchall()

        if not rows:
            return None

        # Group by strategy
        strategy_data = {}
        for r in rows:
            s = r["strategy"] or "unknown"
            if s not in strategy_data:
                strategy_data[s] = {"trades": 0, "wins": 0, "pnl_sum": 0}
            strategy_data[s]["trades"] += 1
            if r["is_win"]:
                strategy_data[s]["wins"] += 1
            strategy_data[s]["pnl_sum"] += r["net_pnl_pct"] or 0

        # Compute weights
        weights = {}
        for strategy, stats in strategy_data.items():
            if strategy not in DEFAULT_STRATEGY_WEIGHTS:
                continue
            if stats["trades"] < MIN_STRATEGY_TRADES:
                weights[strategy] = 1.0  # Not enough data, keep default
                continue

            win_rate = stats["wins"] / stats["trades"]
            avg_pnl = stats["pnl_sum"] / stats["trades"]

            # Weight = base * (1 + win_rate_bonus + pnl_bonus)
            # win_rate 50% → 1.0, 70% → 1.4, 30% → 0.6
            # avg_pnl 0% → 1.0, +5% → 1.5, -5% → 0.5
            wr_factor = 0.5 + win_rate  # 0.5-1.5
            pnl_factor = max(0.5, min(1.5, 1.0 + avg_pnl / 10))
            weights[strategy] = round(wr_factor * pnl_factor, 3)

        # Fill missing strategies with default
        for s in DEFAULT_STRATEGY_WEIGHTS:
            if s not in weights:
                weights[s] = 1.0

        # Normalize to mean=1.0
        mean_w = sum(weights.values()) / len(weights)
        if mean_w > 0:
            weights = {s: round(w / mean_w, 3) for s, w in weights.items()}

        return weights

    def update_strategy_weights(self) -> Optional[Dict]:
        """Compute and store updated strategy weights."""
        old_weights = self.get_strategy_weights()
        new_weights = self.compute_strategy_weights()

        if not new_weights:
            return None

        conn = self._db._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO kv (key, value, updated_at)
            VALUES ('strategy_weights', ?, ?)""",
            (json.dumps(new_weights), time.time()),
        )
        conn.commit()

        changes = []
        for s in DEFAULT_STRATEGY_WEIGHTS:
            old = old_weights.get(s, 1.0)
            new = new_weights[s]
            if abs(old - new) > 0.05:
                changes.append(f"{s}: {old:.2f} → {new:.2f}")

        return {"weights": new_weights, "changes": changes}

    def run_all(
        self,
        symbol: str,
        klines: List[Dict],
        enabled_strategies: List[str] = None,
        position: Optional[Dict] = None,
    ) -> List[Tuple[str, float, str, Dict]]:
        """Run all enabled strategies and return their signals.

        Args:
            symbol: Trading pair
            klines: Kline data
            enabled_strategies: List of strategy names to run (None = all)
            position: Current position (for sell signals)

        Returns: List of (strategy_name, confidence, reason, metadata)
        """
        if enabled_strategies is None:
            enabled_strategies = list(self._strategies.keys())

        weights = self.get_strategy_weights()
        results = []

        for name in enabled_strategies:
            strategy = self._strategies.get(name)
            if not strategy:
                continue

            try:
                signal = strategy.analyze_safe(symbol, klines, position)
                if signal.signal in (SignalType.BUY, SignalType.SELL):
                    weight = weights.get(name, 1.0)
                    adjusted_confidence = signal.confidence * weight
                    results.append((
                        name,
                        adjusted_confidence,
                        signal.reason,
                        {
                            "signal": signal.signal.value,
                            "raw_confidence": signal.confidence,
                            "weight": weight,
                            "metadata": signal.metadata,
                        },
                    ))
            except Exception as e:
                logger.debug(f"Strategy {name} failed for {symbol}: {e}")

        # Sort by adjusted confidence (highest first)
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def select_best(
        self,
        symbol: str,
        klines: List[Dict],
        enabled_strategies: List[str] = None,
        position: Optional[Dict] = None,
    ) -> Optional[Tuple[str, float, str, Dict]]:
        """Run all strategies and return the best one.

        Returns: (strategy_name, confidence, reason, metadata) or None
        """
        results = self.run_all(symbol, klines, enabled_strategies, position)
        return results[0] if results else None

    def format_weights_report(self) -> str:
        """Format current strategy weights as report."""
        weights = self.get_strategy_weights()
        lines = ["## 策略權重", ""]

        for name, w in sorted(weights.items(), key=lambda x: -x[1]):
            default = DEFAULT_STRATEGY_WEIGHTS.get(name, 1.0)
            diff = w - default
            indicator = "🟢" if diff > 0.05 else "🔴" if diff < -0.05 else "⚪"
            lines.append(f"- {indicator} **{name}**: {w:.2f}x ({diff:+.2f})")

        return "\n".join(lines)
