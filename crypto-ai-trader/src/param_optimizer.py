"""
Parameter Auto-Optimizer — Phase 2 of Self-Learning System.

Grid search over key trading parameters using the backtest engine.
Validates with walk-forward out-of-sample testing before deploying.

Parameters optimized:
- RSI oversold/overbought thresholds
- TP/SL percentages
- Score threshold for entry
- Trailing stop activation/distance

Storage: state.db kv key='optimized_params'
"""

import json
import logging
import time
from itertools import product
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Default parameters (fallback when no optimization has run)
DEFAULT_PARAMS = {
    "rsi_oversold": 35,
    "rsi_overbought": 65,
    "stop_loss_pct": 5.0,
    "take_profit_pct": 8.0,
    "score_threshold": 65,
    "trailing_activation_atr": 1.5,
    "trailing_distance_atr": 0.5,
}

# Grid search space: parameter name → list of values to test
SEARCH_SPACE = {
    "rsi_oversold": [25, 30, 35, 40],
    "rsi_overbought": [60, 65, 70, 75],
    "stop_loss_pct": [3.0, 4.0, 5.0, 6.0],
    "take_profit_pct": [5.0, 8.0, 10.0, 12.0],
    "score_threshold": [40, 50, 60, 75],
}

# Validation thresholds
MIN_SHARPE = 0.5  # Minimum Sharpe ratio to accept
MIN_OOS_WIN_RATE = 40.0  # Minimum OOS win rate %
MIN_OOS_ROBUSTNESS = 33.0  # Minimum % of OOS splits with positive return
MIN_TRADES = 5  # Minimum number of trades in backtest

# Symbols to backtest on (diverse set for robustness)
DEFAULT_SYMBOLS = ["SOL", "ETH", "AVAX", "BNB", "LINK"]

# Backtest parameters
BACKTEST_DAYS = 90
WALKFORWARD_DAYS = 120
WALKFORWARD_SPLITS = 3


class ParamOptimizer:
    """Grid search parameter optimizer using backtest engine."""

    def __init__(self, db=None, binance_client=None):
        if db is None:
            from src.state_db import get_state_db

            db = get_state_db()
        self._db = db
        self._client = binance_client

    def _get_client(self):
        """Lazy-init Binance client."""
        if self._client is None:
            from src.binance_client import BinanceClient

            self._client = BinanceClient(testnet=False)
        return self._client

    def get_current_params(self) -> Dict[str, float]:
        """Get current optimized params (or defaults)."""
        conn = self._db._get_conn()
        row = conn.execute(
            "SELECT value FROM kv WHERE key = 'optimized_params'"
        ).fetchone()

        if row:
            try:
                params = json.loads(row["value"])
                if all(k in params for k in DEFAULT_PARAMS):
                    return params
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Failed to parse optimized params JSON from StateDB", exc_info=True
                )

        return dict(DEFAULT_PARAMS)

    def _run_backtest_with_params(
        self,
        params: Dict,
        symbols: List[str],
        days: int = BACKTEST_DAYS,
    ) -> Dict:
        """Run backtest with specific parameters and return metrics.

        Returns:
            {
                "sharpe": float,
                "win_rate": float,
                "total_return_pct": float,
                "max_drawdown_pct": float,
                "n_trades": int,
                "profit_factor": float,
            }
        """
        from src.backtest import BacktestEngine

        client = self._get_client()
        engine = BacktestEngine(client)

        # Override engine parameters
        engine.SCORE_THRESHOLD = params.get("score_threshold", 65)
        engine.TRAILING_ACTIVATION_ATR = params.get("trailing_activation_atr", 1.5)
        engine.TRAILING_DISTANCE_ATR = params.get("trailing_distance_atr", 0.5)

        # Run multi-symbol backtest
        results = engine.run_multi(
            symbols=symbols,
            days=days,
            enable_trend_filter=True,
            enable_trailing_stop=True,
        )

        summary = results.get("summary", {})
        return {
            "sharpe": summary.get("avg_sharpe", 0),
            "win_rate": summary.get("avg_win_rate", 0),
            "total_return_pct": summary.get("total_return_pct", 0),
            "max_drawdown_pct": summary.get("max_drawdown_pct", 0),
            "n_trades": summary.get("total_trades", 0),
            "profit_factor": summary.get("avg_profit_factor", 0),
        }

    def _run_walkforward_with_params(
        self,
        params: Dict,
        symbol: str,
        days: int = WALKFORWARD_DAYS,
        n_splits: int = WALKFORWARD_SPLITS,
    ) -> Dict:
        """Run walk-forward validation for a single symbol.

        Returns:
            {
                "oos_sharpe": float,
                "oos_return_pct": float,
                "robustness_pct": float,
                "n_trades": int,
            }
        """
        from src.backtest import BacktestEngine

        client = self._get_client()
        engine = BacktestEngine(client)

        # Override engine parameters
        engine.SCORE_THRESHOLD = params.get("score_threshold", 65)
        engine.TRAILING_ACTIVATION_ATR = params.get("trailing_activation_atr", 1.5)
        engine.TRAILING_DISTANCE_ATR = params.get("trailing_distance_atr", 0.5)

        result = engine.walk_forward(
            symbol=symbol,
            total_days=days,
            n_splits=n_splits,
            enable_trend_filter=True,
            enable_trailing_stop=True,
        )

        oos = result.get("oos_summary", {})
        return {
            "oos_sharpe": oos.get("avg_sharpe", 0),
            "oos_return_pct": oos.get("avg_return_pct", 0),
            "robustness_pct": oos.get("robustness_pct", 0),
            "n_trades": oos.get("total_trades", 0),
        }

    def grid_search(
        self,
        symbols: Optional[List[str]] = None,
        search_space: Optional[Dict] = None,
        days: int = BACKTEST_DAYS,
        max_combos: int = 50,
    ) -> List[Dict]:
        """Run grid search over parameter combinations.

        Args:
            symbols: Symbols to backtest on (default: DEFAULT_SYMBOLS)
            search_space: Parameter grid (default: SEARCH_SPACE)
            days: Backtest period
            max_combos: Maximum combinations to test (random sample if exceeds)

        Returns: List of results sorted by Sharpe ratio (best first).
        """
        if symbols is None:
            symbols = DEFAULT_SYMBOLS
        if search_space is None:
            search_space = SEARCH_SPACE

        # Generate all combinations
        keys = list(search_space.keys())
        values = list(search_space.values())
        all_combos = list(product(*values))

        # Sample if too many
        if len(all_combos) > max_combos:
            import random

            random.seed(42)  # Reproducible
            all_combos = random.sample(all_combos, max_combos)

        logger.info(
            f"Grid search: {len(all_combos)} combinations × {len(symbols)} symbols"
        )

        results = []
        for i, combo in enumerate(all_combos):
            params = dict(zip(keys, combo))
            # Merge with defaults for non-searched params
            full_params = {**DEFAULT_PARAMS, **params}

            try:
                metrics = self._run_backtest_with_params(full_params, symbols, days)
                result = {
                    "params": full_params,
                    "metrics": metrics,
                }
                results.append(result)

                if (i + 1) % 10 == 0:
                    logger.info(f"  Grid search progress: {i+1}/{len(all_combos)}")

                time.sleep(0.5)  # Rate limit
            except Exception as e:
                logger.warning(f"  Backtest failed for {params}: {e}")
                continue

        # Sort by Sharpe ratio
        results.sort(key=lambda r: r["metrics"]["sharpe"], reverse=True)
        return results

    def validate_best(
        self,
        params: Dict,
        symbols: Optional[List[str]] = None,
    ) -> Dict:
        """Validate best parameters with walk-forward OOS testing.

        Returns:
            {
                "validated": bool,
                "reason": str,
                "oos_results": {symbol: oos_result},
                "avg_oos_sharpe": float,
                "avg_robustness": float,
            }
        """
        if symbols is None:
            symbols = DEFAULT_SYMBOLS[:3]  # Use 3 for faster validation

        oos_results = {}
        for sym in symbols:
            try:
                oos = self._run_walkforward_with_params(params, sym)
                oos_results[sym] = oos
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"Walk-forward failed for {sym}: {e}")
                oos_results[sym] = {"oos_sharpe": -999, "robustness_pct": 0}

        # Compute averages
        sharpes = [r.get("oos_sharpe", 0) for r in oos_results.values()]
        robustness = [r.get("robustness_pct", 0) for r in oos_results.values()]
        trades = [r.get("n_trades", 0) for r in oos_results.values()]

        avg_sharpe = sum(sharpes) / len(sharpes) if sharpes else 0
        avg_robustness = sum(robustness) / len(robustness) if robustness else 0
        total_trades = sum(trades)

        # Validation checks
        reasons = []
        if avg_sharpe < MIN_SHARPE:
            reasons.append(f"OOS Sharpe {avg_sharpe:.2f} < {MIN_SHARPE}")
        if avg_robustness < MIN_OOS_ROBUSTNESS:
            reasons.append(
                f"OOS robustness {avg_robustness:.0f}% < {MIN_OOS_ROBUSTNESS}%"
            )
        if total_trades < MIN_TRADES:
            reasons.append(f"Too few trades: {total_trades} < {MIN_TRADES}")

        validated = len(reasons) == 0
        reason = "OK" if validated else "; ".join(reasons)

        return {
            "validated": validated,
            "reason": reason,
            "oos_results": oos_results,
            "avg_oos_sharpe": round(avg_sharpe, 3),
            "avg_robustness": round(avg_robustness, 1),
            "total_trades": total_trades,
        }

    def optimize_and_store(
        self,
        symbols: Optional[List[str]] = None,
        search_space: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Run full optimization pipeline: grid search → validate → store.

        Returns full result dict, or None if no valid params found.
        """
        old_params = self.get_current_params()

        # Step 1: Grid search
        logger.info("Step 1: Grid search...")
        grid_results = self.grid_search(symbols, search_space)

        if not grid_results:
            logger.warning("Grid search returned no results")
            return None

        best = grid_results[0]
        best_params = best["params"]
        best_metrics = best["metrics"]

        logger.info(
            f"Best grid result: Sharpe={best_metrics['sharpe']:.2f} "
            f"WR={best_metrics['win_rate']:.0f}% "
            f"Return={best_metrics['total_return_pct']:+.1f}%"
        )

        # Step 2: Validate with walk-forward
        logger.info("Step 2: Walk-forward validation...")
        validation = self.validate_best(best_params, symbols)

        if not validation["validated"]:
            logger.warning(f"Validation failed: {validation['reason']}")
            return {
                "status": "validation_failed",
                "best_params": best_params,
                "best_metrics": best_metrics,
                "validation": validation,
                "old_params": old_params,
            }

        # Step 3: Store
        conn = self._db._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO kv (key, value, updated_at)
            VALUES ('optimized_params', ?, ?)""",
            (json.dumps(best_params), time.time()),
        )
        conn.commit()

        # Compute changes
        changes = []
        for k in DEFAULT_PARAMS:
            old = old_params.get(k, DEFAULT_PARAMS[k])
            new = best_params[k]
            if old != new:
                changes.append(f"{k}: {old} → {new}")

        logger.info(f"Optimized params stored: {len(changes)} changes")

        return {
            "status": "optimized",
            "best_params": best_params,
            "best_metrics": best_metrics,
            "validation": validation,
            "old_params": old_params,
            "changes": changes,
            "grid_results_count": len(grid_results),
            "timestamp": time.time(),
        }

    def format_report(self, result: Dict) -> str:
        """Format optimization result as human-readable report."""
        if not result:
            return "優化失敗：無結果"

        lines = ["## 參數自動優化報告", ""]

        status = result.get("status", "unknown")
        if status == "validation_failed":
            lines.append("**狀態**: ❌ 驗證失敗")
            lines.append(f"**原因**: {result['validation']['reason']}")
        elif status == "optimized":
            lines.append("**狀態**: ✅ 已優化並存儲")
        else:
            lines.append(f"**狀態**: {status}")

        # Best params
        lines.append("")
        lines.append("**最佳參數**:")
        for k, v in result["best_params"].items():
            old = result.get("old_params", {}).get(k, v)
            marker = " ← 已調整" if old != v else ""
            lines.append(f"- {k}: {v}{marker}")

        # Metrics
        metrics = result.get("best_metrics", {})
        lines.append("")
        lines.append("**回測指標**:")
        lines.append(f"- Sharpe: {metrics.get('sharpe', 0):.2f}")
        lines.append(f"- 勝率: {metrics.get('win_rate', 0):.0f}%")
        lines.append(f"- 收益: {metrics.get('total_return_pct', 0):+.1f}%")
        lines.append(f"- 最大回撤: {metrics.get('max_drawdown_pct', 0):.1f}%")
        lines.append(f"- 交易數: {metrics.get('n_trades', 0)}")

        # Validation
        if "validation" in result:
            val = result["validation"]
            lines.append("")
            lines.append("**OOS 驗證**:")
            lines.append(f"- 平均 OOS Sharpe: {val.get('avg_oos_sharpe', 0):.3f}")
            lines.append(f"- 平均穩健性: {val.get('avg_robustness', 0):.0f}%")
            lines.append(f"- 總交易數: {val.get('total_trades', 0)}")

            for sym, oos in val.get("oos_results", {}).items():
                lines.append(
                    f"  - {sym}: Sharpe={oos.get('oos_sharpe', 0):.2f} "
                    f"robust={oos.get('robustness_pct', 0):.0f}%"
                )

        # Changes
        if result.get("changes"):
            lines.append("")
            lines.append("**變更**:")
            for c in result["changes"]:
                lines.append(f"- {c}")

        return "\n".join(lines)
