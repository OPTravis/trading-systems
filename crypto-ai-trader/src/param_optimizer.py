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

WO-1003-6 (2026-09-30): Bayesian (TPE) optimisation + robust loss functions
+ staged promotion. The old grid path wrote kv directly on success; the new
path always lands results in kv 'optimized_params_staged' first and requires
an explicit promote (walk-forward gate + dry-run gate) before the live scan
chain (StrategyRegistry rebuilds per scan and reads 'optimized_params')
picks them up. Full history in kv 'hyperopt:history' (last 20 entries).
"""

import json
import logging
import time
from itertools import product
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Default parameters (fallback when no optimization has run)
DEFAULT_PARAMS = {
    "rsi_oversold": 30,
    "rsi_overbought": 65,
    "stop_loss_pct": 5.0,
    "take_profit_pct": 8.0,
    "score_threshold": 65,
    "trailing_activation_atr": 1.5,
    "trailing_distance_atr": 0.5,
}

# Grid search space: parameter name → list of values to test
SEARCH_SPACE = {
    "rsi_oversold": [25, 28, 30, 32],
    "rsi_overbought": [60, 65, 70, 75],
    "stop_loss_pct": [3.0, 4.0, 5.0, 6.0],
    "take_profit_pct": [5.0, 8.0, 10.0, 12.0],
    "score_threshold": [40, 50, 60, 75],
}

# WO-1003-6: continuous Bayesian search space (TPE shines on continuous
# ranges where the grid above only samples a few discrete points).
# spec: param -> (kind, low, high); kind in {"int", "float"}
BAYES_SEARCH_SPACE = {
    "rsi_oversold": ("int", 20, 35),
    "rsi_overbought": ("int", 55, 80),
    "stop_loss_pct": ("float", 2.0, 8.0),
    "take_profit_pct": ("float", 4.0, 15.0),
    "score_threshold": ("int", 35, 80),
}

# staged/promoted kv keys + history retention
KV_STAGED = "optimized_params_staged"
KV_LIVE = "optimized_params"
KV_HISTORY = "hyperopt:history"
HISTORY_KEEP = 20
_NO_TRIALS_GUARD = 1e5  # all-failed / degenerate guard for best_trial.value

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
        # P6-B3: kv_get with kv_get parse semantics (raw string on parse
        # failure -> the warning branch, None -> silent defaults — the
        # original branch shape preserved).
        params = self._db.kv_get("optimized_params")
        if isinstance(params, dict):
            if all(k in params for k in DEFAULT_PARAMS):
                return params
        elif params is not None:
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
        # Reuse cached engine instance to leverage klines cache across grid combos
        if not hasattr(self, '_backtest_engine') or self._backtest_engine is None:
            self._backtest_engine = BacktestEngine(client)
        engine = self._backtest_engine

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
        # Reuse cached engine instance
        if not hasattr(self, '_backtest_engine') or self._backtest_engine is None:
            self._backtest_engine = BacktestEngine(client)
        engine = self._backtest_engine

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
        max_combos: int = 30,
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

                # No sleep needed — klines are cached, no API rate limit concern
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

        # Step 3: Store (P6-B3: kv_set)
        self._db.kv_set("optimized_params", best_params)

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

    # ------------------------------------------------------------------
    # WO-1003-6: Bayesian (TPE) optimisation + staged promotion
    # ------------------------------------------------------------------

    def _append_history(self, entry: Dict) -> None:
        """Append to kv 'hyperopt:history' (bounded to HISTORY_KEEP)."""
        try:
            hist = self._db.kv_get(KV_HISTORY)
            hist = hist if isinstance(hist, list) else []
        except Exception:
            hist = []
        hist.append(entry)
        self._db.kv_set(KV_HISTORY, hist[-HISTORY_KEEP:])

    def bayesian_optimize(
        self,
        symbols: Optional[List[str]] = None,
        loss: str = "sharpe_daily",
        n_trials: int = 40,
        days: int = BACKTEST_DAYS,
        seed: int = 42,
        search_space: Optional[Dict] = None,
    ) -> Dict:
        """TPE (Bayesian) parameter search with a robust loss function.

        Results are ALWAYS staged (kv 'optimized_params_staged') — never
        written straight to the live key. Promotion is a separate, gated
        step (see promote_staged_params). Returns the staged record.
        """
        import optuna

        from src.hyperopt_loss import get_loss

        if symbols is None:
            symbols = DEFAULT_SYMBOLS
        space = search_space or BAYES_SEARCH_SPACE
        loss_fn = get_loss(loss)

        optuna.logging.set_verbosity(optuna.logging.WARNING)
        study = optuna.create_study(
            direction="minimize",
            sampler=optuna.samplers.TPESampler(seed=seed),
        )

        def _objective(trial):
            params = {}
            for k, (kind, lo, hi) in space.items():
                if kind == "int":
                    params[k] = trial.suggest_int(k, lo, hi)
                else:
                    params[k] = trial.suggest_float(k, lo, hi)
            full = {**DEFAULT_PARAMS, **params}
            metrics = self._run_backtest_with_params(full, symbols, days)
            trial.set_user_attr("metrics", metrics)
            trial.set_user_attr("full_params", full)
            return loss_fn(metrics)

        # Backtest failures degrade a trial instead of killing the study.
        study.optimize(_objective, n_trials=n_trials, catch=(Exception,))

        completed = [
            t for t in study.trials
            if t.state == optuna.trial.TrialState.COMPLETE
        ]
        if not completed:
            return {"status": "no_result",
                    "reason": "all trials failed"}
        best = study.best_trial
        if best.value >= _NO_TRIALS_GUARD:
            return {"status": "no_result",
                    "reason": "degenerate best (under-trading penalty)"}

        best_params = best.user_attrs["full_params"]
        best_metrics = best.user_attrs.get("metrics", {})

        # Walk-forward OOS gate — same validator as the grid path (no
        # separate pipeline; validation thresholds unchanged).
        validation = self.validate_best(best_params, symbols)

        staged = {
            "status": "staged",
            "params": best_params,
            "metrics": best_metrics,
            "loss_name": loss,
            "loss_value": best.value,
            "n_trials": len(study.trials),
            "validation": validation,
            "dry_run_verified": False,
            "staged_at": time.time(),
        }
        self._db.kv_set(KV_STAGED, staged)
        self._append_history({
            "event": "staged", "at": staged["staged_at"],
            "loss": loss, "loss_value": best.value,
            "n_trials": len(study.trials),
            "validated": validation.get("validated", False),
            "params": best_params,
        })
        logger.info(
            "Bayesian optimize: staged (loss=%s %.4f, %d trials, "
            "wf_validated=%s)", loss, best.value, len(study.trials),
            validation.get("validated"),
        )
        return staged

    def promote_staged_params(
        self, force: bool = False, require_wf: bool = True
    ) -> Dict:
        """Promote staged params to the live key (hot-reload next scan).

        Gates (WO-1003-6):
          1. walk-forward validation must pass (require_wf, default True);
          2. dry-run verification must be flagged on the staged record
             (the WO-1003-5 dry-run harness sets it). force=True skips
             gate 2 ONLY for explicit human-approved emergencies —
             gate 1 is never forceable.
        """
        staged = self._db.kv_get(KV_STAGED)
        if not isinstance(staged, dict) or "params" not in staged:
            return {"status": "no_staged",
                    "reason": "nothing staged (run bayesian_optimize first)"}

        validation = staged.get("validation", {})
        if require_wf and not validation.get("validated", False):
            return {"status": "rejected",
                    "reason": f"walk-forward validation failed: "
                              f"{validation.get('reason', 'unknown')}"}

        if not staged.get("dry_run_verified", False) and not force:
            return {"status": "rejected",
                    "reason": "dry-run gate: staged params not dry-run "
                              "verified (WO-1003-5); use force=True only "
                              "with explicit approval"}

        old_params = self.get_current_params()
        self._db.kv_set(KV_LIVE, staged["params"])
        self._append_history({
            "event": "promoted", "at": time.time(),
            "old_params": old_params, "new_params": staged["params"],
            "forced": force,
        })
        self._db.kv_remove(KV_STAGED)
        logger.info("Promoted staged params (forced=%s)", force)
        return {"status": "promoted", "old_params": old_params,
                "new_params": staged["params"], "forced": force}

    def mark_dry_run_verified(self, note: str = "") -> Dict:
        """Flag the staged record as dry-run verified (called by the
        WO-1003-5 dry-run harness once deviation checks pass)."""
        staged = self._db.kv_get(KV_STAGED)
        if not isinstance(staged, dict) or "params" not in staged:
            return {"status": "no_staged"}
        staged["dry_run_verified"] = True
        staged["dry_run_note"] = note
        staged["dry_run_verified_at"] = time.time()
        self._db.kv_set(KV_STAGED, staged)
        return {"status": "ok"}

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
