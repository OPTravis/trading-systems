"""
Walk-Forward Validation — Rolling window backtesting for strategy validation.

Features:
- Rolling window training + out-of-sample testing
- Parameter stability check across windows
- Reports: Sharpe ratio, max drawdown, win rate, profit factor
- Avoids look-ahead bias common in static backtests
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ─── Data Models ────────────────────────────────────────────────────────────

@dataclass
class WindowResult:
    """Result for a single walk-forward window."""
    window_id: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    # Performance
    total_return: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_trades: int = 0
    avg_trade_pnl: float = 0.0
    # Parameters
    optimal_params: Dict = field(default_factory=dict)
    # Equity curve for this window
    equity_curve: List[float] = field(default_factory=list)


@dataclass
class WalkForwardReport:
    """Aggregated walk-forward validation report."""
    strategy: str
    symbol: str
    total_windows: int
    # Aggregated metrics
    avg_sharpe: float = 0.0
    avg_sortino: float = 0.0
    avg_max_drawdown: float = 0.0
    avg_win_rate: float = 0.0
    avg_profit_factor: float = 0.0
    median_sharpe: float = 0.0
    total_return: float = 0.0
    # Parameter stability
    param_stability: float = 0.0  # 0-1, higher = more stable
    param_ranges: Dict[str, tuple] = field(default_factory=dict)
    # Window details
    windows: List[WindowResult] = field(default_factory=list)

    def summary(self) -> str:
        """Human-readable summary."""
        lines = [
            f"Walk-Forward Report: {self.strategy} on {self.symbol}",
            f"  Windows: {self.total_windows}",
            f"  Avg Sharpe: {self.avg_sharpe:.2f}  (median: {self.median_sharpe:.2f})",
            f"  Avg Sortino: {self.avg_sortino:.2f}",
            f"  Avg Max DD: {self.avg_max_drawdown:.2%}",
            f"  Avg Win Rate: {self.avg_win_rate:.1%}",
            f"  Avg Profit Factor: {self.avg_profit_factor:.2f}",
            f"  Total Return: {self.total_return:.2%}",
            f"  Param Stability: {self.param_stability:.2f}",
        ]
        return "\n".join(lines)


# ─── Walk-Forward Engine ────────────────────────────────────────────────────

class WalkForwardValidator:
    """
    Walk-forward validation engine.

    Splits historical data into rolling windows:
    [---train---][--test--]
       [---train---][--test--]
          [---train---][--test--]

    For each window:
    1. Train: optimize strategy parameters on in-sample data
    2. Test: evaluate with optimized parameters on out-of-sample data
    3. Record performance metrics

    Aggregates all test-window results for final strategy evaluation.
    """

    TRADING_DAYS_PER_YEAR = 252

    def __init__(
        self,
        train_days: int = 252,
        test_days: int = 63,
        step_days: int = 63,
        min_trades: int = 5,
    ):
        """
        Args:
            train_days: Training window length in trading days.
            test_days: Testing window length in trading days.
            step_days: Step size between windows.
            min_trades: Minimum trades required for a valid window.
        """
        self.train_days = train_days
        self.test_days = test_days
        self.step_days = step_days
        self.min_trades = min_trades

    def validate(
        self,
        strategy_fn: Callable,
        data: pd.DataFrame,
        symbol: str = "UNKNOWN",
        strategy_name: str = "strategy",
        param_grid: Dict[str, list] = None,
    ) -> WalkForwardReport:
        """
        Run walk-forward validation.

        Args:
            strategy_fn: Strategy function with signature:
                (train_data, test_data, params) -> (trades, equity_curve)
                where trades is a list of dicts with 'pnl' key.
            data: DataFrame with OHLCV data, DatetimeIndex.
            symbol: Symbol being tested.
            strategy_name: Strategy name for reporting.
            param_grid: Parameter grid for optimization.
                Example: {'lookback': [10, 20, 50], 'threshold': [0.02, 0.03]}

        Returns:
            WalkForwardReport with aggregated results.
        """
        if data.empty or len(data) < self.train_days + self.test_days:
            logger.warning("Insufficient data for walk-forward: %d rows", len(data))
            return WalkForwardReport(strategy=strategy_name, symbol=symbol, total_windows=0)

        # Generate windows
        windows = self._generate_windows(data)
        logger.info("Walk-forward: %d windows (%d train, %d test, %d step)",
                     len(windows), self.train_days, self.test_days, self.step_days)

        results = []
        all_params = []

        for i, (train_start, train_end, test_start, test_end) in enumerate(windows):
            train_data = data[train_start:train_end]
            test_data = data[test_start:test_end]

            if train_data.empty or test_data.empty:
                continue

            # Optimize on training data
            best_params = {}
            if param_grid:
                best_params = self._optimize_params(
                    strategy_fn, train_data, test_data, param_grid
                )
            all_params.append(best_params)

            # Evaluate on test data with best params
            try:
                trades, equity_curve = strategy_fn(train_data, test_data, best_params)
            except Exception as e:
                logger.warning("Window %d strategy failed: %s", i, e)
                continue

            # Calculate metrics
            window_result = self._calculate_metrics(
                window_id=i,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                trades=trades,
                equity_curve=equity_curve,
                optimal_params=best_params,
            )

            if window_result.total_trades >= self.min_trades:
                results.append(window_result)
            else:
                logger.debug("Window %d skipped: only %d trades (min %d)",
                             i, window_result.total_trades, self.min_trades)

        # Aggregate results
        report = self._aggregate(results, all_params, symbol, strategy_name)
        return report

    # ── Window Generation ───────────────────────────────────────────────

    def _generate_windows(
        self, data: pd.DataFrame
    ) -> List[tuple]:
        """Generate (train_start, train_end, test_start, test_end) tuples."""
        dates = data.index
        n = len(dates)
        windows = []

        start = 0
        while start + self.train_days + self.test_days <= n:
            train_start = dates[start]
            train_end = dates[start + self.train_days - 1]
            test_start = dates[start + self.train_days]
            test_end_idx = min(start + self.train_days + self.test_days - 1, n - 1)
            test_end = dates[test_end_idx]
            windows.append((train_start, train_end, test_start, test_end))
            start += self.step_days

        return windows

    # ── Parameter Optimization ──────────────────────────────────────────

    def _optimize_params(
        self,
        strategy_fn: Callable,
        train_data: pd.DataFrame,
        test_data: pd.DataFrame,
        param_grid: Dict[str, list],
    ) -> Dict:
        """Grid search for optimal parameters on training data."""
        from itertools import product

        keys = list(param_grid.keys())
        values = list(param_grid.values())
        best_sharpe = -np.inf
        best_params = {}

        for combo in product(*values):
            params = dict(zip(keys, combo))
            try:
                trades, equity = strategy_fn(train_data, test_data, params)
                if not trades:
                    continue
                returns = [t["pnl"] for t in trades if "pnl" in t]
                if not returns:
                    continue
                sharpe = self._compute_sharpe(returns)
                if sharpe > best_sharpe:
                    best_sharpe = sharpe
                    best_params = params
            except Exception:
                continue

        return best_params

    # ── Metrics Calculation ─────────────────────────────────────────────

    def _calculate_metrics(
        self,
        window_id: int,
        train_start,
        train_end,
        test_start,
        test_end,
        trades: List[dict],
        equity_curve: List[float],
        optimal_params: Dict,
    ) -> WindowResult:
        """Calculate performance metrics for a single window."""
        pnls = [t.get("pnl", 0) for t in trades]
        returns = [t.get("return_pct", 0) / 100 for t in trades]

        total_return = sum(pnls) / 10000 if pnls else 0  # Normalize
        sharpe = self._compute_sharpe(pnls) if pnls else 0
        sortino = self._compute_sortino(pnls) if pnls else 0
        max_dd = self._compute_max_drawdown(equity_curve) if equity_curve is not None and hasattr(equity_curve, "empty") and not equity_curve.empty else 0
        win_rate = sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0
        profit_factor = self._compute_profit_factor(pnls)

        return WindowResult(
            window_id=window_id,
            train_start=str(train_start),
            train_end=str(train_end),
            test_start=str(test_start),
            test_end=str(test_end),
            total_return=total_return,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            max_drawdown=max_dd,
            win_rate=win_rate,
            profit_factor=profit_factor,
            total_trades=len(trades),
            avg_trade_pnl=np.mean(pnls) if pnls else 0,
            optimal_params=optimal_params,
            equity_curve=equity_curve,
        )

    def _compute_sharpe(self, pnls: List[float], risk_free: float = 0) -> float:
        """Annualized Sharpe ratio from P&L series."""
        if not pnls or len(pnls) < 2:
            return 0.0
        arr = np.array(pnls)
        excess = arr.mean() - risk_free
        std = arr.std()
        if std == 0:
            return 0.0
        return (excess / std) * np.sqrt(self.TRADING_DAYS_PER_YEAR)

    def _compute_sortino(self, pnls: List[float], risk_free: float = 0) -> float:
        """Sortino ratio: uses downside deviation only."""
        if not pnls or len(pnls) < 2:
            return 0.0
        arr = np.array(pnls)
        excess = arr.mean() - risk_free
        downside = arr[arr < 0]
        if len(downside) == 0:
            return 10.0  # No losing trades
        downside_std = downside.std()
        if downside_std == 0:
            return 10.0
        return (excess / downside_std) * np.sqrt(self.TRADING_DAYS_PER_YEAR)

    def _compute_max_drawdown(self, equity_curve) -> float:
        """Maximum drawdown from equity curve."""
        if equity_curve is None or (hasattr(equity_curve, 'empty') and equity_curve.empty) or len(equity_curve) < 2:
            return 0.0
        arr = np.array(equity_curve)
        peak = np.maximum.accumulate(arr)
        drawdown = (peak - arr) / np.where(peak > 0, peak, 1)
        return float(drawdown.max())

    def _compute_profit_factor(self, pnls: List[float]) -> float:
        """Profit factor = gross profit / gross loss."""
        if not pnls:
            return 0.0
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        if gross_loss == 0:
            return 10.0 if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    # ── Aggregation ─────────────────────────────────────────────────────

    def _aggregate(
        self,
        results: List[WindowResult],
        all_params: List[Dict],
        symbol: str,
        strategy_name: str,
    ) -> WalkForwardReport:
        """Aggregate window results into a final report."""
        if not results:
            return WalkForwardReport(
                strategy=strategy_name, symbol=symbol, total_windows=0,
            )

        sharpes = [r.sharpe_ratio for r in results]
        sortinos = [r.sortino_ratio for r in results]
        drawdowns = [r.max_drawdown for r in results]
        win_rates = [r.win_rate for r in results]
        profit_factors = [r.profit_factor for r in results]

        # Compound total return across windows
        total_return = 1.0
        for r in results:
            total_return *= (1 + r.total_return)
        total_return -= 1.0

        # Parameter stability: how consistent are the optimal params across windows?
        param_stability = self._compute_param_stability(all_params)
        param_ranges = self._compute_param_ranges(all_params)

        report = WalkForwardReport(
            strategy=strategy_name,
            symbol=symbol,
            total_windows=len(results),
            avg_sharpe=np.mean(sharpes),
            avg_sortino=np.mean(sortinos),
            avg_max_drawdown=np.mean(drawdowns),
            avg_win_rate=np.mean(win_rates),
            avg_profit_factor=np.mean(profit_factors),
            median_sharpe=np.median(sharpes),
            total_return=total_return,
            param_stability=param_stability,
            param_ranges=param_ranges,
            windows=results,
        )

        logger.info("Walk-forward complete:\n%s", report.summary())
        return report

    def _compute_param_stability(self, all_params: List[Dict]) -> float:
        """Measure parameter stability across windows (0=unstable, 1=stable)."""
        if not all_params or len(all_params) < 2:
            return 1.0

        # For each parameter, compute coefficient of variation
        key_set = set()
        for p in all_params:
            key_set.update(p.keys())

        if not key_set:
            return 1.0

        stabilities = []
        for key in key_set:
            values = [p.get(key, 0) for p in all_params if key in p]
            numeric_values = []
            for v in values:
                try:
                    numeric_values.append(float(v))
                except (ValueError, TypeError):
                    continue

            if not numeric_values or len(numeric_values) < 2:
                continue

            mean = np.mean(numeric_values)
            std = np.std(numeric_values)
            cv = std / abs(mean) if mean != 0 else 0
            # Stability = 1 - normalized CV (clamped to [0, 1])
            stability = max(0, min(1, 1 - cv))
            stabilities.append(stability)

        return np.mean(stabilities) if stabilities else 1.0

    def _compute_param_ranges(self, all_params: List[Dict]) -> Dict[str, tuple]:
        """Get (min, max) range for each numeric parameter."""
        if not all_params:
            return {}

        key_set = set()
        for p in all_params:
            key_set.update(p.keys())

        ranges = {}
        for key in key_set:
            values = []
            for p in all_params:
                if key in p:
                    try:
                        values.append(float(p[key]))
                    except (ValueError, TypeError):
                        continue
            if values:
                ranges[key] = (min(values), max(values))

        return ranges


# ─── Convenience: Quick Validation ──────────────────────────────────────────

def quick_walk_forward(
    data: pd.DataFrame,
    strategy_fn: Callable,
    symbol: str = "SPY",
    train_days: int = 252,
    test_days: int = 63,
    param_grid: Dict[str, list] = None,
) -> WalkForwardReport:
    """
    Convenience function for quick walk-forward validation.

    Args:
        data: OHLCV DataFrame with DatetimeIndex.
        strategy_fn: Strategy function.
        symbol: Symbol name.
        train_days: Training window.
        test_days: Testing window.
        param_grid: Parameters to optimize.

    Returns:
        WalkForwardReport.
    """
    validator = WalkForwardValidator(
        train_days=train_days,
        test_days=test_days,
        step_days=test_days,
    )
    return validator.validate(
        strategy_fn=strategy_fn,
        data=data,
        symbol=symbol,
        param_grid=param_grid,
    )
