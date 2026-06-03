"""
Comprehensive tests for src/walk_forward.py

Covers:
- WalkForwardValidator: validate, _generate_windows, _optimize_params,
  _calculate_metrics, _aggregate, _compute_param_stability
- WalkForwardReport: summary()
- quick_walk_forward() convenience function
- Edge cases: empty data, insufficient data, no trades
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.walk_forward import (
    WalkForwardReport,
    WalkForwardValidator,
    WindowResult,
    quick_walk_forward,
)

# ─── Helpers / Fixtures ──────────────────────────────────────────────────────


def _make_ohlcv(n: int = 500, seed: int = 42) -> pd.DataFrame:
    """Create synthetic daily OHLCV data with a DatetimeIndex."""
    rng = np.random.RandomState(seed)
    dates = pd.bdate_range("2018-01-02", periods=n, freq="B")
    close = 100.0 + np.cumsum(rng.randn(n) * 0.5)
    high = close + rng.uniform(0.5, 2.0, n)
    low = close - rng.uniform(0.5, 2.0, n)
    opn = close + rng.uniform(-1.0, 1.0, n)
    volume = rng.randint(1_000_000, 10_000_000, n).astype(float)
    return pd.DataFrame(
        {"open": opn, "high": high, "low": low, "close": close, "volume": volume},
        index=dates,
    )


def _mock_strategy(train_data, test_data, params):
    """
    Simple mock strategy that produces deterministic trades from test data.

    Uses a simple mean-reversion signal on close prices.
    Returns (trades, equity_curve).
    """
    lookback = params.get("lookback", 10)
    threshold = params.get("threshold", 0.01)
    closes = test_data["close"].values
    trades = []
    equity = [100_000.0]
    capital = 100_000.0

    for i in range(lookback, len(closes)):
        ma = np.mean(closes[i - lookback : i])
        pct_diff = (closes[i] - ma) / ma
        if abs(pct_diff) > threshold:
            pnl = -pct_diff * capital * 0.01  # mean-reversion: fade the move
            trades.append({"pnl": pnl, "return_pct": pnl / capital * 100})
            capital += pnl
        equity.append(capital)

    return trades, equity


def _always_profitable_strategy(train_data, test_data, params):
    """Strategy that always generates profitable trades."""
    n = max(len(test_data) // 5, 10)
    trades = [{"pnl": 100.0, "return_pct": 0.1} for _ in range(n)]
    equity = [100_000.0 + 100.0 * i for i in range(n + 1)]
    return trades, equity


def _always_losing_strategy(train_data, test_data, params):
    """Strategy that always generates losing trades."""
    n = max(len(test_data) // 5, 10)
    trades = [{"pnl": -50.0, "return_pct": -0.05} for _ in range(n)]
    equity = [100_000.0 - 50.0 * i for i in range(n + 1)]
    return trades, equity


def _empty_strategy(train_data, test_data, params):
    """Strategy that produces no trades."""
    return [], []


def _crashing_strategy(train_data, test_data, params):
    """Strategy that raises an exception."""
    raise RuntimeError("Strategy exploded")


# ─── WalkForwardValidator Tests ──────────────────────────────────────────────


class TestWalkForwardValidatorInit:
    """Tests for WalkForwardValidator construction."""

    def test_default_params(self):
        v = WalkForwardValidator()
        assert v.train_days == 252
        assert v.test_days == 63
        assert v.step_days == 63
        assert v.min_trades == 5

    def test_custom_params(self):
        v = WalkForwardValidator(
            train_days=100, test_days=20, step_days=10, min_trades=3
        )
        assert v.train_days == 100
        assert v.test_days == 20
        assert v.step_days == 10
        assert v.min_trades == 3


class TestGenerateWindows:
    """Tests for _generate_windows()."""

    def test_correct_window_count(self):
        """Standard parameters on 500 bars should yield a known count."""
        v = WalkForwardValidator(train_days=100, test_days=30, step_days=30)
        data = _make_ohlcv(500)
        windows = v._generate_windows(data)
        # start=0 -> 0+100+30=130 <= 500, start=30 -> 160, ... start=330 -> 460, start=360 -> 490, start=390 -> 520 > 500
        # starts: 0,30,60,...,360 -> 13 windows (0 through 360 in steps of 30)
        assert len(windows) == 13
        assert all(len(w) == 4 for w in windows)

    def test_window_boundaries_do_not_overlap(self):
        """Train end should be before test start; test start should be train_end + 1 day."""
        v = WalkForwardValidator(train_days=50, test_days=20, step_days=20)
        data = _make_ohlcv(300)
        windows = v._generate_windows(data)
        for train_start, train_end, test_start, test_end in windows:
            assert train_start <= train_end
            assert train_end < test_start
            assert test_start <= test_end

    def test_train_and_test_lengths(self):
        """Each window should cover approximately the requested number of bars."""
        v = WalkForwardValidator(train_days=50, test_days=15, step_days=15)
        data = _make_ohlcv(300)
        windows = v._generate_windows(data)
        for train_start, train_end, test_start, test_end in windows:
            train_count = len(data[train_start:train_end])
            test_count = len(data[test_start:test_end])
            assert train_count == 50  # exact
            assert test_count == 15

    def test_insufficient_data_returns_empty(self):
        """If data is shorter than train+test, no windows should be produced."""
        v = WalkForwardValidator(train_days=200, test_days=100, step_days=50)
        data = _make_ohlcv(250)  # 250 < 200+100=300
        windows = v._generate_windows(data)
        assert windows == []

    def test_step_size_changes_window_count(self):
        """Larger step should produce fewer windows."""
        data = _make_ohlcv(500)
        v1 = WalkForwardValidator(train_days=100, test_days=30, step_days=10)
        v2 = WalkForwardValidator(train_days=100, test_days=30, step_days=60)
        w1 = v1._generate_windows(data)
        w2 = v2._generate_windows(data)
        assert len(w1) > len(w2)


class TestCalculateMetrics:
    """Tests for _calculate_metrics()."""

    def test_basic_metrics(self):
        v = WalkForwardValidator()
        trades = [
            {"pnl": 100.0, "return_pct": 0.1},
            {"pnl": -50.0, "return_pct": -0.05},
            {"pnl": 200.0, "return_pct": 0.2},
            {"pnl": -30.0, "return_pct": -0.03},
            {"pnl": 150.0, "return_pct": 0.15},
            {"pnl": 80.0, "return_pct": 0.08},
        ]
        equity = [100_000, 100_100, 100_050, 100_250, 100_220, 100_370, 100_450]
        result = v._calculate_metrics(
            window_id=0,
            train_start="2020-01-01",
            train_end="2020-06-01",
            test_start="2020-06-02",
            test_end="2020-12-01",
            trades=trades,
            equity_curve=equity,
            optimal_params={"lookback": 10},
        )

        assert isinstance(result, WindowResult)
        assert result.window_id == 0
        assert result.total_trades == 6
        assert result.win_rate == pytest.approx(4 / 6)  # 4 positive trades
        assert result.total_return == pytest.approx(450.0 / 100_000)
        assert result.sharpe_ratio != 0
        assert result.sortino_ratio != 0
        assert result.max_drawdown > 0  # equity dipped at some point
        assert result.profit_factor > 0
        assert result.optimal_params == {"lookback": 10}

    def test_no_trades(self):
        """Zero trades should yield zeroed metrics."""
        v = WalkForwardValidator()
        result = v._calculate_metrics(
            window_id=0,
            train_start="2020-01-01",
            train_end="2020-06-01",
            test_start="2020-06-02",
            test_end="2020-12-01",
            trades=[],
            equity_curve=[],
            optimal_params={},
        )
        assert result.total_trades == 0
        assert result.win_rate == 0
        assert result.total_return == 0
        assert result.sharpe_ratio == 0
        assert result.sortino_ratio == 0

    def test_all_winning_trades(self):
        v = WalkForwardValidator()
        trades = [{"pnl": 100.0}] * 10
        equity = [100_000 + 100 * i for i in range(11)]
        result = v._calculate_metrics(0, "a", "b", "c", "d", trades, equity, {})
        assert result.win_rate == 1.0
        assert result.sortino_ratio == 10.0  # no losing trades -> capped at 10
        assert result.profit_factor == 10.0  # no loss -> capped at 10

    def test_all_losing_trades(self):
        v = WalkForwardValidator()
        trades = [{"pnl": -50.0}] * 10
        equity = [100_000 - 50 * i for i in range(11)]
        result = v._calculate_metrics(0, "a", "b", "c", "d", trades, equity, {})
        assert result.win_rate == 0.0
        assert result.profit_factor == 0.0

    def test_max_drawdown_computation(self):
        """Verify drawdown from a known equity curve."""
        v = WalkForwardValidator()
        # equity: 100 -> 110 (peak) -> 99 (drawdown ~10%) -> 105
        equity = [100, 110, 99, 105]
        trades = [{"pnl": 10}, {"pnl": -11}, {"pnl": 6}, {"pnl": 0}]
        result = v._calculate_metrics(0, "a", "b", "c", "d", trades, equity, {})
        expected_dd = (110 - 99) / 110
        assert result.max_drawdown == pytest.approx(expected_dd, abs=1e-6)


class TestComputeSharpe:
    """Tests for _compute_sharpe() standalone logic."""

    def test_single_trade_returns_zero(self):
        v = WalkForwardValidator()
        assert v._compute_sharpe([100.0]) == 0.0

    def test_empty_returns_zero(self):
        v = WalkForwardValidator()
        assert v._compute_sharpe([]) == 0.0

    def test_constant_returns_zero_sharpe(self):
        """Zero standard deviation should give 0 Sharpe."""
        v = WalkForwardValidator()
        assert v._compute_sharpe([10.0, 10.0, 10.0]) == 0.0

    def test_positive_sharpe_for_positive_mean(self):
        v = WalkForwardValidator()
        sharpe = v._compute_sharpe([10, 20, 5, 15, 25])
        assert sharpe > 0

    def test_negative_sharpe_for_negative_mean(self):
        v = WalkForwardValidator()
        sharpe = v._compute_sharpe([-10, -20, -5, -15, -25])
        assert sharpe < 0


class TestComputeSortino:
    """Tests for _compute_sortino()."""

    def test_no_losing_trades(self):
        v = WalkForwardValidator()
        assert v._compute_sortino([10, 20, 30]) == 10.0

    def test_single_trade_returns_zero(self):
        v = WalkForwardValidator()
        assert v._compute_sortino([100.0]) == 0.0

    def test_normal_sortino(self):
        v = WalkForwardValidator()
        s = v._compute_sortino([10, -5, 20, -3, 15])
        assert s > 0


class TestComputeMaxDrawdown:
    """Tests for _compute_max_drawdown()."""

    def test_monotonically_increasing(self):
        v = WalkForwardValidator()
        assert v._compute_max_drawdown([100, 110, 120, 130]) == pytest.approx(0.0)

    def test_none_returns_zero(self):
        v = WalkForwardValidator()
        assert v._compute_max_drawdown(None) == 0.0

    def test_single_value_returns_zero(self):
        v = WalkForwardValidator()
        assert v._compute_max_drawdown([100]) == 0.0

    def test_known_drawdown(self):
        v = WalkForwardValidator()
        # peak=1000, trough=700 -> dd = 30%
        dd = v._compute_max_drawdown([1000, 900, 700, 800, 950])
        assert dd == pytest.approx(0.3, abs=1e-6)

    def test_zero_peak_handled(self):
        """Peak of zero should not cause division by zero."""
        v = WalkForwardValidator()
        dd = v._compute_max_drawdown([0, 0, 0])
        assert dd == 0.0


class TestComputeProfitFactor:
    """Tests for _compute_profit_factor()."""

    def test_empty(self):
        v = WalkForwardValidator()
        assert v._compute_profit_factor([]) == 0.0

    def test_only_wins(self):
        v = WalkForwardValidator()
        assert v._compute_profit_factor([10, 20, 30]) == 10.0

    def test_only_losses(self):
        v = WalkForwardValidator()
        assert v._compute_profit_factor([-10, -20]) == 0.0

    def test_mixed(self):
        v = WalkForwardValidator()
        pf = v._compute_profit_factor([100, 200, -50, -30])
        assert pf == pytest.approx(300 / 80)


class TestOptimizeParams:
    """Tests for _optimize_params() grid search."""

    def test_picks_best_sharpe_params(self):
        """Grid search should find parameters that maximize Sharpe."""
        v = WalkForwardValidator()
        data = _make_ohlcv(200)
        train = data.iloc[:100]
        test = data.iloc[100:]
        param_grid = {"lookback": [5, 10], "threshold": [0.005, 0.02]}

        best = v._optimize_params(_mock_strategy, train, test, param_grid)
        assert isinstance(best, dict)
        assert "lookback" in best
        assert "threshold" in best
        assert best["lookback"] in [5, 10]
        assert best["threshold"] in [0.005, 0.02]

    def test_empty_grid_returns_empty(self):
        v = WalkForwardValidator()
        data = _make_ohlcv(100)
        best = v._optimize_params(_mock_strategy, data, data, {})
        assert best == {}

    def test_strategy_exception_is_skipped(self):
        """If a param combo crashes, it should be skipped gracefully."""
        v = WalkForwardValidator()
        data = _make_ohlcv(100)
        param_grid = {"mode": ["crash", "ok"]}

        def selective_strategy(train, test, params):
            if params.get("mode") == "crash":
                raise ValueError("boom")
            return [{"pnl": 100}] * 10, [100_000 + i * 100 for i in range(11)]

        best = v._optimize_params(selective_strategy, data, data, param_grid)
        assert best == {"mode": "ok"}

    def test_no_trades_skipped(self):
        """Param combos that produce no trades are skipped."""
        v = WalkForwardValidator()
        data = _make_ohlcv(100)
        param_grid = {"mode": ["empty", "trades"]}

        def selective_strategy(train, test, params):
            if params.get("mode") == "empty":
                return [], []
            return [{"pnl": 50}] * 10, [100_000 + i * 50 for i in range(11)]

        best = v._optimize_params(selective_strategy, data, data, param_grid)
        assert best == {"mode": "trades"}


class TestAggregate:
    """Tests for _aggregate()."""

    def test_empty_results(self):
        v = WalkForwardValidator()
        report = v._aggregate([], [], "SPY", "test_strat")
        assert report.total_windows == 0
        assert report.strategy == "test_strat"
        assert report.symbol == "SPY"

    def test_compound_return(self):
        """Total return should be compounded across windows."""
        v = WalkForwardValidator()
        r1 = WindowResult(
            window_id=0,
            train_start="",
            train_end="",
            test_start="",
            test_end="",
            total_return=0.10,
            sharpe_ratio=1.5,
            sortino_ratio=2.0,
            max_drawdown=0.05,
            win_rate=0.6,
            profit_factor=1.5,
            total_trades=10,
            avg_trade_pnl=100,
        )
        r2 = WindowResult(
            window_id=1,
            train_start="",
            train_end="",
            test_start="",
            test_end="",
            total_return=0.05,
            sharpe_ratio=1.0,
            sortino_ratio=1.2,
            max_drawdown=0.03,
            win_rate=0.55,
            profit_factor=1.2,
            total_trades=8,
            avg_trade_pnl=60,
        )
        report = v._aggregate([r1, r2], [{}, {}], "X", "s")
        # compound: (1+0.10)*(1+0.05) - 1 = 0.155
        assert report.total_return == pytest.approx(0.155, abs=1e-6)
        assert report.avg_sharpe == pytest.approx(1.25)
        assert report.total_windows == 2

    def test_metrics_populated(self):
        v = WalkForwardValidator()
        results = [
            WindowResult(
                window_id=i,
                train_start="",
                train_end="",
                test_start="",
                test_end="",
                total_return=0.01 * i,
                sharpe_ratio=float(i),
                sortino_ratio=float(i),
                max_drawdown=0.01,
                win_rate=0.5,
                profit_factor=1.0,
                total_trades=10,
                avg_trade_pnl=10.0,
            )
            for i in range(1, 4)
        ]
        report = v._aggregate(results, [{"k": i} for i in range(3)], "A", "b")
        assert report.avg_sharpe == pytest.approx(2.0)
        assert report.median_sharpe == pytest.approx(2.0)
        assert report.avg_sortino == pytest.approx(2.0)


class TestComputeParamStability:
    """Tests for _compute_param_stability()."""

    def test_no_params_returns_one(self):
        v = WalkForwardValidator()
        assert v._compute_param_stability([]) == 1.0

    def test_single_param_set_returns_one(self):
        v = WalkForwardValidator()
        assert v._compute_param_stability([{"k": 5}]) == 1.0

    def test_identical_params_full_stability(self):
        v = WalkForwardValidator()
        params = [{"lookback": 20, "threshold": 0.02}] * 5
        assert v._compute_param_stability(params) == pytest.approx(1.0)

    def test_varying_params_reduces_stability(self):
        v = WalkForwardValidator()
        params = [
            {"lookback": 10},
            {"lookback": 50},
            {"lookback": 100},
            {"lookback": 200},
        ]
        stability = v._compute_param_stability(params)
        assert 0.0 <= stability < 1.0

    def test_empty_dict_params(self):
        """All empty dicts -> no keys -> stability 1.0."""
        v = WalkForwardValidator()
        assert v._compute_param_stability([{}, {}, {}]) == 1.0

    def test_non_numeric_params_skipped(self):
        v = WalkForwardValidator()
        params = [{"mode": "a"}, {"mode": "b"}, {"mode": "c"}]
        assert v._compute_param_stability(params) == 1.0


class TestComputeParamRanges:
    """Tests for _compute_param_ranges()."""

    def test_empty(self):
        v = WalkForwardValidator()
        assert v._compute_param_ranges([]) == {}

    def test_numeric_ranges(self):
        v = WalkForwardValidator()
        params = [{"lookback": 10}, {"lookback": 50}, {"lookback": 30}]
        ranges = v._compute_param_ranges(params)
        assert ranges["lookback"] == (10.0, 50.0)

    def test_non_numeric_skipped(self):
        v = WalkForwardValidator()
        params = [{"mode": "fast"}, {"mode": "slow"}]
        ranges = v._compute_param_ranges(params)
        assert "mode" not in ranges


# ─── Integration: validate() ────────────────────────────────────────────────


class TestValidateIntegration:
    """End-to-end tests for validate()."""

    def test_full_run_with_mock_strategy(self):
        data = _make_ohlcv(500)
        v = WalkForwardValidator(
            train_days=100, test_days=30, step_days=30, min_trades=1
        )
        report = v.validate(
            strategy_fn=_mock_strategy,
            data=data,
            symbol="TEST",
            strategy_name="mean_revert",
            param_grid={"lookback": [5, 10], "threshold": [0.005, 0.02]},
        )
        assert isinstance(report, WalkForwardReport)
        assert report.total_windows > 0
        assert report.strategy == "mean_revert"
        assert report.symbol == "TEST"
        assert report.avg_sharpe is not None
        assert len(report.windows) == report.total_windows

    def test_insufficient_data_returns_empty_report(self):
        data = _make_ohlcv(50)
        v = WalkForwardValidator(train_days=252, test_days=63)
        report = v.validate(_mock_strategy, data)
        assert report.total_windows == 0
        assert report.windows == []

    def test_empty_dataframe(self):
        empty_df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        v = WalkForwardValidator(train_days=10, test_days=5)
        report = v.validate(_mock_strategy, empty_df)
        assert report.total_windows == 0

    def test_no_param_grid(self):
        """Running without a param grid should still work."""
        data = _make_ohlcv(400)
        v = WalkForwardValidator(
            train_days=100, test_days=30, step_days=30, min_trades=1
        )
        report = v.validate(_mock_strategy, data, param_grid=None)
        assert report.total_windows > 0

    def test_crashing_strategy_produces_empty_report(self):
        """If strategy crashes on every window, report should have 0 windows."""
        data = _make_ohlcv(400)
        v = WalkForwardValidator(
            train_days=100, test_days=30, step_days=30, min_trades=1
        )
        report = v.validate(_crashing_strategy, data)
        assert report.total_windows == 0

    def test_min_trades_filtering(self):
        """Windows with fewer trades than min_trades should be excluded."""
        data = _make_ohlcv(500)
        v = WalkForwardValidator(
            train_days=100, test_days=30, step_days=30, min_trades=1000
        )
        report = v.validate(_mock_strategy, data)
        # With such a high min_trades, most/all windows should be filtered
        assert report.total_windows == 0

    def test_nan_data_forward_filled(self):
        """Data with NaN values should be forward/back filled and processed."""
        data = _make_ohlcv(400)
        data.iloc[50, 2] = np.nan  # inject a NaN in 'low'
        data.iloc[100, 3] = np.nan  # inject a NaN in 'close'
        v = WalkForwardValidator(
            train_days=100, test_days=30, step_days=30, min_trades=1
        )
        report = v.validate(_mock_strategy, data)
        # Should still produce results without crashing
        assert isinstance(report, WalkForwardReport)


# ─── WalkForwardReport Tests ────────────────────────────────────────────────


class TestWalkForwardReport:
    """Tests for WalkForwardReport.summary()."""

    def test_summary_contains_key_fields(self):
        report = WalkForwardReport(
            strategy="test_strat",
            symbol="SPY",
            total_windows=5,
            avg_sharpe=1.23,
            avg_sortino=1.87,
            avg_max_drawdown=0.05,
            avg_win_rate=0.65,
            avg_profit_factor=1.50,
            median_sharpe=1.10,
            total_return=0.25,
            param_stability=0.85,
        )
        text = report.summary()
        assert "test_strat" in text
        assert "SPY" in text
        assert "5" in text
        assert "1.23" in text
        assert "1.87" in text
        assert "65.0%" in text
        assert "25.00%" in text
        assert "0.85" in text

    def test_summary_format(self):
        report = WalkForwardReport(
            strategy="s",
            symbol="X",
            total_windows=1,
            avg_sharpe=0.0,
            avg_sortino=0.0,
            avg_max_drawdown=0.0,
            avg_win_rate=0.0,
            avg_profit_factor=0.0,
            median_sharpe=0.0,
            total_return=0.0,
            param_stability=0.0,
        )
        lines = report.summary().strip().split("\n")
        assert lines[0].startswith("Walk-Forward Report:")
        assert all(line.startswith("  ") for line in lines[1:])


# ─── quick_walk_forward() ───────────────────────────────────────────────────


class TestQuickWalkForward:
    """Tests for the convenience function."""

    def test_basic_usage(self):
        data = _make_ohlcv(400)
        report = quick_walk_forward(
            data=data,
            strategy_fn=_mock_strategy,
            symbol="SPY",
            train_days=100,
            test_days=30,
            param_grid={"lookback": [5, 10], "threshold": [0.01]},
        )
        assert isinstance(report, WalkForwardReport)
        assert report.symbol == "SPY"
        assert report.total_windows > 0

    def test_defaults(self):
        """Default params should produce a valid report without errors."""
        data = _make_ohlcv(500)
        report = quick_walk_forward(
            data=data,
            strategy_fn=_mock_strategy,
        )
        assert isinstance(report, WalkForwardReport)
        assert report.symbol == "SPY"

    def test_insufficient_data(self):
        data = _make_ohlcv(10)
        report = quick_walk_forward(data=data, strategy_fn=_mock_strategy)
        assert report.total_windows == 0


# ─── WindowResult dataclass ─────────────────────────────────────────────────


class TestWindowResult:
    """Quick smoke test for the WindowResult dataclass."""

    def test_defaults(self):
        wr = WindowResult(
            window_id=0,
            train_start="a",
            train_end="b",
            test_start="c",
            test_end="d",
        )
        assert wr.total_return == 0.0
        assert wr.sharpe_ratio == 0.0
        assert wr.optimal_params == {}
        assert wr.equity_curve == []

    def test_custom_values(self):
        wr = WindowResult(
            window_id=5,
            train_start="a",
            train_end="b",
            test_start="c",
            test_end="d",
            total_return=0.15,
            sharpe_ratio=2.0,
            total_trades=20,
        )
        assert wr.window_id == 5
        assert wr.total_return == 0.15
        assert wr.total_trades == 20
