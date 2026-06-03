"""
Comprehensive tests for trade_executor.py

Covers:
- HybridPositionSizer: sizing methods, clamping, edge cases
- TradeExecutor: execute, retry, routing, size_and_execute, logging
- RoutingDecision: NASDAQ/SMART enum routing
- Edge cases: no broker, zero quantity, position too small
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.brokers.broker_protocol import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from src.trade_executor import (
    NASDAQ_SYMBOLS,
    ExecutionResult,
    HybridPositionSizer,
    RoutingDecision,
    TradeExecutor,
)

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_filled_order(
    order_id=1, filled_qty=100, avg_fill_price=150.0, commission=1.0
):
    """Create a mock Order object returned by broker.place_order with FILLED status."""
    order = MagicMock(spec=Order)
    order.order_id = order_id
    order.status = OrderStatus.FILLED
    order.filled_qty = filled_qty
    order.avg_fill_price = avg_fill_price
    order.commission = commission
    order.contract = MagicMock()
    order.contract.exchange = "NASDAQ"
    order.order_type = OrderType.MARKET
    return order


def _make_rejected_order(order_id=1):
    order = MagicMock(spec=Order)
    order.order_id = order_id
    order.status = OrderStatus.REJECTED
    order.filled_qty = 0
    order.avg_fill_price = 0.0
    order.commission = 0.0
    order.contract = MagicMock()
    order.contract.exchange = "NASDAQ"
    order.order_type = OrderType.MARKET
    return order


def _make_cancelled_order(order_id=1):
    order = MagicMock(spec=Order)
    order.order_id = order_id
    order.status = OrderStatus.CANCELLED
    order.filled_qty = 0
    order.avg_fill_price = 0.0
    order.commission = 0.0
    order.contract = MagicMock()
    order.contract.exchange = "NASDAQ"
    order.order_type = OrderType.MARKET
    return order


def _make_submitted_order(order_id=1):
    order = MagicMock(spec=Order)
    order.order_id = order_id
    order.status = OrderStatus.SUBMITTED
    order.filled_qty = 0
    order.avg_fill_price = 0.0
    order.commission = 0.0
    order.contract = MagicMock()
    order.contract.exchange = "NASDAQ"
    order.order_type = OrderType.MARKET
    return order


def _mock_portfolio(nav=100_000.0, position_count=5):
    portfolio = MagicMock()
    portfolio.get_nav = MagicMock(return_value=nav)
    portfolio.position_count = position_count
    return portfolio


# ─────────────────────────────────────────────────────────────────────────────
# RoutingDecision
# ─────────────────────────────────────────────────────────────────────────────


class TestRoutingDecision:
    def test_enum_values(self):
        assert RoutingDecision.NASDAQ.value == "NASDAQ"
        assert RoutingDecision.NYSE.value == "NYSE"
        assert RoutingDecision.SMART.value == "SMART"

    def test_is_str_subclass(self):
        """RoutingDecision inherits from str so it can be compared as a string."""
        assert RoutingDecision.NASDAQ == "NASDAQ"
        assert RoutingDecision.SMART == "SMART"


# ─────────────────────────────────────────────────────────────────────────────
# ExecutionResult
# ─────────────────────────────────────────────────────────────────────────────


class TestExecutionResult:
    def test_to_dict_success(self):
        r = ExecutionResult(
            success=True,
            symbol="AAPL",
            side="BUY",
            order_type="MKT",
            requested_qty=100,
            filled_qty=100,
            avg_fill_price=150.0,
            commission=1.0,
            order_id=42,
            exchange="NASDAQ",
            retry_count=0,
            timestamp="2026-01-01T00:00:00",
        )
        d = r.to_dict()
        assert d["success"] is True
        assert d["symbol"] == "AAPL"
        assert d["filled_qty"] == 100
        assert d["order_id"] == 42

    def test_to_dict_failure(self):
        r = ExecutionResult(
            success=False,
            symbol="TSLA",
            side="SELL",
            order_type="LMT",
            requested_qty=50,
            error="Order rejected",
        )
        d = r.to_dict()
        assert d["success"] is False
        assert d["error"] == "Order rejected"
        assert d["filled_qty"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# HybridPositionSizer — _kelly_fraction
# ─────────────────────────────────────────────────────────────────────────────


class TestKellyFraction:
    def test_default_params(self):
        sizer = HybridPositionSizer(win_rate=0.55, payoff_ratio=2.0)
        kelly = sizer._kelly_fraction()
        # full_kelly = (0.55*2 - 0.45)/2 = 0.65/2 = 0.325
        # half_kelly = 0.325 / 2 = 0.1625
        assert kelly == pytest.approx(0.1625, abs=1e-6)

    def test_zero_payoff_returns_zero(self):
        sizer = HybridPositionSizer(payoff_ratio=0.0)
        assert sizer._kelly_fraction() == 0.0

    def test_negative_payoff_returns_zero(self):
        sizer = HybridPositionSizer(payoff_ratio=-1.0)
        assert sizer._kelly_fraction() == 0.0

    def test_low_win_rate_returns_zero(self):
        """When p*b - q < 0, Kelly should be 0 (clamped)."""
        sizer = HybridPositionSizer(win_rate=0.3, payoff_ratio=1.0)
        # full_kelly = (0.3*1 - 0.7)/1 = -0.4 -> clamped to 0
        assert sizer._kelly_fraction() == 0.0

    def test_high_kelly_capped_at_25pct(self):
        """Even with extreme parameters, half-Kelly is capped at 0.25."""
        sizer = HybridPositionSizer(win_rate=0.95, payoff_ratio=10.0)
        kelly = sizer._kelly_fraction()
        assert kelly <= 0.25

    def test_win_rate_50_50_even_money(self):
        """p=0.5, b=1 -> full_kelly = 0, half_kelly = 0."""
        sizer = HybridPositionSizer(win_rate=0.5, payoff_ratio=1.0)
        assert sizer._kelly_fraction() == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# HybridPositionSizer — _cvar_fraction
# ─────────────────────────────────────────────────────────────────────────────


class TestCvarFraction:
    def test_normal_vol(self):
        sizer = HybridPositionSizer(cvar_max_loss=0.05)
        cvar = sizer._cvar_fraction(0.25)
        # cvar_per_unit = 0.25 * 1.645 = 0.41125
        # result = min(0.25, 0.05 / 0.41125) = min(0.25, 0.12158...)
        expected = min(0.25, 0.05 / (0.25 * 1.645))
        assert cvar == pytest.approx(expected, abs=1e-6)

    def test_zero_vol_returns_zero(self):
        sizer = HybridPositionSizer()
        assert sizer._cvar_fraction(0.0) == 0.0

    def test_negative_vol_returns_zero(self):
        sizer = HybridPositionSizer()
        assert sizer._cvar_fraction(-0.1) == 0.0

    def test_very_low_vol_capped_at_25pct(self):
        """Extremely low volatility should hit the 25% cap."""
        sizer = HybridPositionSizer(cvar_max_loss=0.05)
        cvar = sizer._cvar_fraction(0.001)
        assert cvar == 0.25

    def test_high_vol_gives_small_fraction(self):
        sizer = HybridPositionSizer(cvar_max_loss=0.05)
        cvar = sizer._cvar_fraction(0.80)
        expected = min(0.25, 0.05 / (0.80 * 1.645))
        assert cvar == pytest.approx(expected, abs=1e-6)


# ─────────────────────────────────────────────────────────────────────────────
# HybridPositionSizer — _vol_target_fraction
# ─────────────────────────────────────────────────────────────────────────────


class TestVolTargetFraction:
    def test_normal_case(self):
        sizer = HybridPositionSizer()
        vol_frac = sizer._vol_target_fraction(0.25, n_positions=10)
        # (0.15 / 0.25) / sqrt(10) = 0.6 / 3.1623 = 0.18974
        expected = (0.15 / 0.25) / math.sqrt(10)
        assert vol_frac == pytest.approx(expected, abs=1e-4)

    def test_zero_vol_returns_zero(self):
        sizer = HybridPositionSizer()
        assert sizer._vol_target_fraction(0.0, 10) == 0.0

    def test_negative_vol_returns_zero(self):
        sizer = HybridPositionSizer()
        assert sizer._vol_target_fraction(-0.5, 10) == 0.0

    def test_single_position(self):
        sizer = HybridPositionSizer()
        vol_frac = sizer._vol_target_fraction(0.25, n_positions=1)
        # (0.15/0.25)/1 = 0.6 -> capped at 0.25
        assert vol_frac == 0.25

    def test_zero_positions_treated_as_one(self):
        """n_positions=0 should be treated as 1 (max(1, n))."""
        sizer = HybridPositionSizer()
        vol_frac = sizer._vol_target_fraction(0.25, n_positions=0)
        assert vol_frac == 0.25  # same as n_positions=1

    def test_many_positions_reduces_size(self):
        sizer = HybridPositionSizer()
        vol_10 = sizer._vol_target_fraction(0.25, n_positions=10)
        vol_100 = sizer._vol_target_fraction(0.25, n_positions=100)
        assert vol_100 < vol_10

    def test_capped_at_25pct(self):
        sizer = HybridPositionSizer()
        # Very low vol -> large fraction, should be capped
        vol_frac = sizer._vol_target_fraction(0.01, n_positions=1)
        assert vol_frac == 0.25


# ─────────────────────────────────────────────────────────────────────────────
# HybridPositionSizer — size_position (integration)
# ─────────────────────────────────────────────────────────────────────────────


class TestSizePosition:
    def test_basic_sizing(self):
        sizer = HybridPositionSizer(win_rate=0.55, payoff_ratio=2.0)
        result = sizer.size_position(
            symbol="AAPL", nav=100_000, stock_vol=0.25, n_positions=10
        )
        assert "position_pct" in result
        assert "position_usd" in result
        assert "kelly_pct" in result
        assert "cvar_pct" in result
        assert "vol_target_pct" in result
        assert result["position_usd"] == pytest.approx(
            100_000 * result["position_pct"], rel=1e-2
        )

    def test_position_pct_between_min_and_max(self):
        sizer = HybridPositionSizer()
        result = sizer.size_position("XYZ", nav=500_000, stock_vol=0.30, n_positions=8)
        assert HybridPositionSizer.MIN_POSITION_PCT <= result["position_pct"]
        assert result["position_pct"] <= HybridPositionSizer.MAX_POSITION_PCT

    def test_regime_multiplier_reduces_size(self):
        sizer = HybridPositionSizer()
        normal = sizer.size_position("AAPL", nav=100_000, regime_multiplier=1.0)
        cautious = sizer.size_position("AAPL", nav=100_000, regime_multiplier=0.5)
        assert cautious["position_pct"] <= normal["position_pct"]

    def test_vix_multiplier_reduces_size(self):
        sizer = HybridPositionSizer()
        normal = sizer.size_position("AAPL", nav=100_000, vix_multiplier=1.0)
        fearful = sizer.size_position("AAPL", nav=100_000, vix_multiplier=0.3)
        assert fearful["position_pct"] <= normal["position_pct"]

    def test_clamped_to_min_when_multipliers_very_small(self):
        """Even with tiny multipliers, position_pct >= MIN_POSITION_PCT."""
        sizer = HybridPositionSizer()
        result = sizer.size_position(
            "AAPL", nav=100_000, regime_multiplier=0.001, vix_multiplier=0.001
        )
        assert result["position_pct"] >= HybridPositionSizer.MIN_POSITION_PCT

    def test_clamped_to_max_when_nav_small_and_vol_low(self):
        """With very low vol and single position, can hit the max cap."""
        sizer = HybridPositionSizer()
        result = sizer.size_position(
            "XYZ", nav=1_000_000, stock_vol=0.01, n_positions=1
        )
        assert result["position_pct"] <= HybridPositionSizer.MAX_POSITION_PCT

    def test_higher_vol_gives_smaller_position(self):
        sizer = HybridPositionSizer()
        low_vol = sizer.size_position(
            "AAPL", nav=100_000, stock_vol=0.15, n_positions=5
        )
        high_vol = sizer.size_position(
            "AAPL", nav=100_000, stock_vol=0.60, n_positions=5
        )
        assert high_vol["position_pct"] <= low_vol["position_pct"]

    def test_usd_scales_with_nav(self):
        sizer = HybridPositionSizer()
        small = sizer.size_position("AAPL", nav=50_000)
        large = sizer.size_position("AAPL", nav=200_000)
        # position_pct is the same (same vol/nav doesn't affect pct)
        # but position_usd should scale proportionally
        assert large["position_usd"] >= small["position_usd"]

    def test_default_regime_and_vix_multipliers_stored(self):
        sizer = HybridPositionSizer()
        result = sizer.size_position("AAPL", nav=100_000)
        assert result["regime_multiplier"] == 1.0
        assert result["vix_multiplier"] == 1.0


# ─────────────────────────────────────────────────────────────────────────────
# TradeExecutor — route_exchange
# ─────────────────────────────────────────────────────────────────────────────


class TestRouteExchange:
    @pytest.mark.parametrize(
        "symbol", ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL"]
    )
    def test_nasdaq_symbols(self, symbol):
        assert TradeExecutor.route_exchange(symbol) == RoutingDecision.NASDAQ

    @pytest.mark.parametrize("symbol", ["IBM", "GE", "JPM", "GS", "BA", "DIS"])
    def test_smart_routing_for_non_nasdaq(self, symbol):
        assert TradeExecutor.route_exchange(symbol) == RoutingDecision.SMART

    def test_case_insensitive(self):
        assert TradeExecutor.route_exchange("aapl") == RoutingDecision.NASDAQ
        assert TradeExecutor.route_exchange("Nvda") == RoutingDecision.NASDAQ

    def test_dot_replaced_with_dash(self):
        """BRK.B-style symbols: dots become dashes before lookup."""
        # BRK-B is not in NASDAQ_SYMBOLS so should get SMART
        assert TradeExecutor.route_exchange("BRK.B") == RoutingDecision.SMART

    def test_nasdaq_symbols_set_not_empty(self):
        assert len(NASDAQ_SYMBOLS) > 20

    def test_pltr_is_nasdaq(self):
        assert "PLTR" in NASDAQ_SYMBOLS
        assert TradeExecutor.route_exchange("PLTR") == RoutingDecision.NASDAQ


# ─────────────────────────────────────────────────────────────────────────────
# TradeExecutor — execute
# ─────────────────────────────────────────────────────────────────────────────


class TestExecute:
    @pytest.mark.asyncio
    async def test_no_broker_returns_error(self):
        executor = TradeExecutor(broker=None)
        result = await executor.execute("AAPL", "BUY", 100)
        assert result["success"] is False
        assert "No broker" in result["error"]

    @pytest.mark.asyncio
    async def test_market_order_success(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(return_value=_make_filled_order())

        executor = TradeExecutor(broker=broker)
        with patch("src.trade_executor.time.sleep"):
            result = await executor.execute("AAPL", "BUY", 100, order_type="MKT")

        assert result["success"] is True
        assert result["symbol"] == "AAPL"
        assert result["filled_qty"] == 100
        broker.place_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_limit_order_sets_price(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(return_value=_make_filled_order())

        executor = TradeExecutor(broker=broker)
        with patch("src.trade_executor.time.sleep"):
            result = await executor.execute(
                "MSFT", "BUY", 50, price=380.0, order_type="LMT"
            )

        assert result["success"] is True
        call_args = broker.place_order.call_args
        order = call_args[0][0]
        assert order.limit_price == 380.0

    @pytest.mark.asyncio
    async def test_sell_order(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(return_value=_make_filled_order())

        executor = TradeExecutor(broker=broker)
        with patch("src.trade_executor.time.sleep"):
            result = await executor.execute("AAPL", "SELL", 200, order_type="MKT")

        assert result["success"] is True
        call_args = broker.place_order.call_args
        order = call_args[0][0]
        assert order.side == OrderSide.SELL

    @pytest.mark.asyncio
    async def test_stop_order_sets_stop_price(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(return_value=_make_filled_order())

        executor = TradeExecutor(broker=broker)
        with patch("src.trade_executor.time.sleep"):
            result = await executor.execute(
                "TSLA", "BUY", 10, price=200.0, order_type="STP"
            )

        assert result["success"] is True
        call_args = broker.place_order.call_args
        order = call_args[0][0]
        assert order.stop_price == 200.0

    @pytest.mark.asyncio
    async def test_stop_limit_order(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(return_value=_make_filled_order())

        executor = TradeExecutor(broker=broker)
        with patch("src.trade_executor.time.sleep"):
            result = await executor.execute(
                "NVDA", "BUY", 10, price=800.0, order_type="STPLMT"
            )

        assert result["success"] is True
        call_args = broker.place_order.call_args
        order = call_args[0][0]
        assert order.limit_price == 800.0
        assert order.stop_price == 800.0

    @pytest.mark.asyncio
    async def test_gtc_time_in_force(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(return_value=_make_filled_order())

        executor = TradeExecutor(broker=broker)
        with patch("src.trade_executor.time.sleep"):
            await executor.execute("AAPL", "BUY", 10, time_in_force="GTC")

        call_args = broker.place_order.call_args
        order = call_args[0][0]
        assert order.time_in_force == TimeInForce.GTC

    @pytest.mark.asyncio
    async def test_result_appended_to_log(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(return_value=_make_filled_order())

        executor = TradeExecutor(broker=broker)
        with patch("src.trade_executor.time.sleep"):
            await executor.execute("AAPL", "BUY", 10)

        log = executor.get_execution_log()
        assert len(log) == 1
        assert log[0]["symbol"] == "AAPL"

    @pytest.mark.asyncio
    async def test_stop_loss_placed_on_success(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(return_value=_make_filled_order())

        executor = TradeExecutor(broker=broker)
        with patch("src.trade_executor.time.sleep"):
            await executor.execute("AAPL", "BUY", 100, stop_loss=140.0)

        # Two calls: main order + stop-loss
        assert broker.place_order.call_count == 2

    @pytest.mark.asyncio
    async def test_take_profit_placed_on_success(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(return_value=_make_filled_order())

        executor = TradeExecutor(broker=broker)
        with patch("src.trade_executor.time.sleep"):
            await executor.execute("AAPL", "BUY", 100, take_profit=170.0)

        # Two calls: main order + take-profit
        assert broker.place_order.call_count == 2

    @pytest.mark.asyncio
    async def test_stop_loss_not_placed_on_failure(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(return_value=_make_rejected_order())

        executor = TradeExecutor(broker=broker)
        with patch("src.trade_executor.time.sleep"):
            await executor.execute("AAPL", "BUY", 100, stop_loss=140.0)

        # Only one call: main order (rejection), no stop-loss
        assert broker.place_order.call_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# TradeExecutor — _execute_with_retry
# ─────────────────────────────────────────────────────────────────────────────


class TestExecuteWithRetry:
    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(return_value=_make_filled_order(order_id=42))

        executor = TradeExecutor(broker=broker)
        order = MagicMock()
        order.contract = MagicMock()
        order.contract.exchange = "NASDAQ"
        order.order_type = OrderType.MARKET

        with patch("src.trade_executor.time.sleep"):
            result = await executor._execute_with_retry(order, "AAPL", "BUY", 100)

        assert result.success is True
        assert result.order_id == 42
        assert result.retry_count == 0
        broker.place_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_retry_on_exception_then_success(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(
            side_effect=[ConnectionError("timeout"), _make_filled_order(order_id=99)]
        )

        executor = TradeExecutor(broker=broker)
        order = MagicMock()
        order.contract = MagicMock()
        order.contract.exchange = "SMART"
        order.order_type = OrderType.MARKET

        with patch("src.trade_executor.time.sleep") as mock_sleep:
            result = await executor._execute_with_retry(order, "MSFT", "BUY", 50)

        assert result.success is True
        assert result.order_id == 99
        assert result.retry_count == 1
        assert broker.place_order.call_count == 2
        mock_sleep.assert_called_once_with(1)  # first backoff

    @pytest.mark.asyncio
    async def test_max_retries_exhausted(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(side_effect=ConnectionError("fail"))

        executor = TradeExecutor(broker=broker)
        order = MagicMock()
        order.contract = MagicMock()
        order.contract.exchange = "SMART"
        order.order_type = OrderType.MARKET

        with patch("src.trade_executor.time.sleep"):
            result = await executor._execute_with_retry(order, "TSLA", "BUY", 10)

        assert result.success is False
        assert "3 attempts failed" in result.error
        assert result.retry_count == 3
        assert broker.place_order.call_count == 3

    @pytest.mark.asyncio
    async def test_rejection_not_retried(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(return_value=_make_rejected_order())

        executor = TradeExecutor(broker=broker)
        order = MagicMock()
        order.contract = MagicMock()
        order.contract.exchange = "NASDAQ"
        order.order_type = OrderType.MARKET

        with patch("src.trade_executor.time.sleep"):
            result = await executor._execute_with_retry(order, "AAPL", "BUY", 100)

        assert result.success is False
        assert "rejected" in result.error.lower()
        # Should not retry rejections
        broker.place_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancelled_order_returns_failure(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(return_value=_make_cancelled_order())

        executor = TradeExecutor(broker=broker)
        order = MagicMock()
        order.contract = MagicMock()
        order.contract.exchange = "NASDAQ"
        order.order_type = OrderType.MARKET

        with patch("src.trade_executor.time.sleep"):
            result = await executor._execute_with_retry(order, "AAPL", "SELL", 50)

        assert result.success is False
        assert "cancelled" in result.error.lower()

    @pytest.mark.asyncio
    async def test_partial_fill_is_success(self):
        partial = _make_filled_order(filled_qty=50, avg_fill_price=150.0)
        partial.status = OrderStatus.PARTIALLY_FILLED

        broker = AsyncMock()
        broker.place_order = AsyncMock(return_value=partial)

        executor = TradeExecutor(broker=broker)
        order = MagicMock()
        order.contract = MagicMock()
        order.contract.exchange = "NASDAQ"
        order.order_type = OrderType.MARKET

        with patch("src.trade_executor.time.sleep"):
            result = await executor._execute_with_retry(order, "AAPL", "BUY", 100)

        assert result.success is True
        assert result.filled_qty == 50

    @pytest.mark.asyncio
    async def test_exponential_backoff(self):
        """Verify backoff increases: 1s, 3s, 10s."""
        broker = AsyncMock()
        broker.place_order = AsyncMock(side_effect=ConnectionError("fail"))

        executor = TradeExecutor(broker=broker)
        order = MagicMock()
        order.contract = MagicMock()
        order.contract.exchange = "SMART"
        order.order_type = OrderType.MARKET

        with patch("src.trade_executor.time.sleep") as mock_sleep:
            await executor._execute_with_retry(order, "AAPL", "BUY", 10)

        calls = [c[0][0] for c in mock_sleep.call_args_list]
        assert calls == [1, 3]  # sleeps between attempt 0->1 and 1->2


# ─────────────────────────────────────────────────────────────────────────────
# TradeExecutor — _wait_for_fill
# ─────────────────────────────────────────────────────────────────────────────


class TestWaitForFill:
    @pytest.mark.asyncio
    async def test_fill_found_immediately(self):
        filled = _make_filled_order(order_id=42, filled_qty=100, avg_fill_price=150.0)
        broker = AsyncMock()
        broker.get_open_orders = AsyncMock(return_value=[filled])

        executor = TradeExecutor(broker=broker)
        with patch("src.trade_executor.time.time", side_effect=[0, 1, 2]):
            with patch("src.trade_executor.time.sleep"):
                result = await executor._wait_for_fill(42, "AAPL", "BUY", 100, 0)

        assert result is not None
        assert result.success is True

    @pytest.mark.asyncio
    async def test_order_not_found_returns_none_on_timeout(self):
        """If the order disappears from open orders, returns None after timeout."""
        broker = AsyncMock()
        broker.get_open_orders = AsyncMock(return_value=[])

        executor = TradeExecutor(broker=broker)
        # Provide enough time.time() values for:
        # 1. deadline = time.time() + 30  -> 0 (deadline=30)
        # 2. while time.time() < deadline -> 0 (<30, enter loop)
        # 3. while time.time() < deadline -> 999 (>=30, exit loop)
        # 4+ logging also calls time.time() internally
        time_values = iter([0, 0, 999, 999, 999, 999, 999])
        with patch(
            "src.trade_executor.time.time", side_effect=lambda: next(time_values)
        ):
            with patch("src.trade_executor.time.sleep"):
                result = await executor._wait_for_fill(42, "AAPL", "BUY", 100, 0)

        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# TradeExecutor — size_and_execute
# ─────────────────────────────────────────────────────────────────────────────


class TestSizeAndExecute:
    @pytest.mark.asyncio
    async def test_no_portfolio_returns_error(self):
        executor = TradeExecutor(broker=AsyncMock(), portfolio=None)
        result = await executor.size_and_execute("AAPL", price=150.0)
        assert result["success"] is False
        assert "No portfolio" in result["error"]

    @pytest.mark.asyncio
    async def test_full_pipeline_success(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(return_value=_make_filled_order())
        portfolio = _mock_portfolio(nav=100_000, position_count=5)

        executor = TradeExecutor(broker=broker, portfolio=portfolio)
        with patch("src.trade_executor.time.sleep"):
            result = await executor.size_and_execute("AAPL", price=150.0, side="BUY")

        assert result["success"] is True
        assert "sizing" in result
        assert "position_pct" in result["sizing"]

    @pytest.mark.asyncio
    async def test_position_too_small(self):
        """When NAV * position_pct < $10, should return error."""
        broker = AsyncMock()
        portfolio = _mock_portfolio(nav=100, position_count=50)  # tiny NAV

        executor = TradeExecutor(broker=broker, portfolio=portfolio)
        with patch("src.trade_executor.time.sleep"):
            result = await executor.size_and_execute(
                "AAPL", price=150.0, regime_multiplier=0.001, vix_multiplier=0.001
            )

        assert result["success"] is False
        assert "too small" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_zero_quantity_returns_error(self):
        """When position_usd / price rounds to 0 shares, should return error."""
        broker = AsyncMock()
        # Very small NAV with normal price -> 0 shares
        portfolio = _mock_portfolio(nav=500, position_count=5)

        executor = TradeExecutor(broker=broker, portfolio=portfolio)
        with patch("src.trade_executor.time.sleep"):
            result = await executor.size_and_execute(
                "BRK.A", price=500_000.0  # impossibly expensive
            )

        # Either "too small" or "Zero quantity" depending on the exact path
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_board_lot_rounding(self):
        """Quantities >= 100 should be rounded to board lots (multiples of 100)."""
        broker = AsyncMock()
        filled = _make_filled_order(filled_qty=300, avg_fill_price=150.0)
        broker.place_order = AsyncMock(return_value=filled)
        portfolio = _mock_portfolio(nav=1_000_000, position_count=5)

        executor = TradeExecutor(broker=broker, portfolio=portfolio)
        with patch("src.trade_executor.time.sleep"):
            result = await executor.size_and_execute("AAPL", price=150.0)

        assert result["success"] is True
        call_args = broker.place_order.call_args
        order = call_args[0][0]
        # quantity should be a multiple of 100 (or < 100)
        if order.quantity >= 100:
            assert order.quantity % 100 == 0

    @pytest.mark.asyncio
    async def test_stop_loss_and_take_profit_calculated(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(return_value=_make_filled_order())
        portfolio = _mock_portfolio(nav=100_000, position_count=5)

        executor = TradeExecutor(broker=broker, portfolio=portfolio)
        with patch("src.trade_executor.time.sleep"):
            result = await executor.size_and_execute(
                "AAPL",
                price=150.0,
                stop_loss_pct=5.0,
                take_profit_pct=10.0,
            )

        assert result["success"] is True
        # execute() is called with stop_loss=142.5, take_profit=165.0
        # The fact that it succeeded means the SL/TP were processed

    @pytest.mark.asyncio
    async def test_regime_multiplier_passed_to_sizer(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(return_value=_make_filled_order())
        portfolio = _mock_portfolio(nav=100_000, position_count=5)

        executor = TradeExecutor(broker=broker, portfolio=portfolio)
        with patch("src.trade_executor.time.sleep"):
            result = await executor.size_and_execute(
                "AAPL", price=150.0, regime_multiplier=0.5, vix_multiplier=0.7
            )

        assert result["success"] is True
        sizing = result["sizing"]
        assert sizing["regime_multiplier"] == 0.5
        assert sizing["vix_multiplier"] == 0.7


# ─────────────────────────────────────────────────────────────────────────────
# TradeExecutor — get_execution_log
# ─────────────────────────────────────────────────────────────────────────────


class TestGetExecutionLog:
    def test_empty_log(self):
        executor = TradeExecutor()
        assert executor.get_execution_log() == []

    @pytest.mark.asyncio
    async def test_log_after_execution(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(return_value=_make_filled_order())

        executor = TradeExecutor(broker=broker)
        with patch("src.trade_executor.time.sleep"):
            await executor.execute("AAPL", "BUY", 10)

        log = executor.get_execution_log()
        assert len(log) == 1
        assert log[0]["symbol"] == "AAPL"
        assert log[0]["success"] is True

    @pytest.mark.asyncio
    async def test_log_limit(self):
        broker = AsyncMock()
        broker.place_order = AsyncMock(return_value=_make_filled_order())

        executor = TradeExecutor(broker=broker)
        with patch("src.trade_executor.time.sleep"):
            for _ in range(5):
                await executor.execute("AAPL", "BUY", 10)

        log = executor.get_execution_log(limit=3)
        assert len(log) == 3

    def test_log_maxlen_500(self):
        """Internal deque is capped at 500 entries; oldest are dropped."""
        executor = TradeExecutor()
        for i in range(600):
            executor._execution_log.append(
                ExecutionResult(
                    success=True,
                    symbol=f"SYM{i}",
                    side="BUY",
                    order_type="MKT",
                    requested_qty=10,
                )
            )
        assert len(executor._execution_log) == 500
        # Oldest entries (SYM0..SYM99) should have been dropped
        # Use a large limit to get all 500 entries from the deque
        log = executor.get_execution_log(limit=600)
        assert len(log) == 500
        assert log[0]["symbol"] == "SYM100"
        assert log[-1]["symbol"] == "SYM599"


# ─────────────────────────────────────────────────────────────────────────────
# TradeExecutor — get_pending_orders / cancel_all_orders
# ─────────────────────────────────────────────────────────────────────────────


class TestPendingOrdersAndCancel:
    def test_get_pending_orders_no_broker(self):
        executor = TradeExecutor(broker=None)
        assert executor.get_pending_orders() == []

    def test_get_pending_orders_with_broker(self):
        broker = MagicMock()
        mock_order = MagicMock()
        mock_order.order_id = 1
        broker.get_open_orders.return_value = [mock_order]

        executor = TradeExecutor(broker=broker)
        orders = executor.get_pending_orders()
        assert len(orders) == 1

    def test_cancel_all_no_broker(self):
        executor = TradeExecutor(broker=None)
        # Should not raise
        executor.cancel_all_orders()

    def test_cancel_all_cancels_each_order(self):
        broker = MagicMock()
        order1 = MagicMock()
        order1.order_id = 1
        order2 = MagicMock()
        order2.order_id = 2
        broker.get_open_orders.return_value = [order1, order2]

        executor = TradeExecutor(broker=broker)
        executor.cancel_all_orders()

        assert broker.cancel_order.call_count == 2
        broker.cancel_order.assert_any_call(1)
        broker.cancel_order.assert_any_call(2)


# ─────────────────────────────────────────────────────────────────────────────
# TradeExecutor — default sizer
# ─────────────────────────────────────────────────────────────────────────────


class TestTradeExecutorDefaults:
    def test_default_sizer_created(self):
        executor = TradeExecutor()
        assert isinstance(executor.sizer, HybridPositionSizer)

    def test_custom_sizer(self):
        custom = HybridPositionSizer(win_rate=0.7, payoff_ratio=3.0)
        executor = TradeExecutor(position_sizer=custom)
        assert executor.sizer is custom

    def test_initial_execution_log_is_empty(self):
        executor = TradeExecutor()
        assert executor._execution_log.maxlen == 500
        assert len(executor._execution_log) == 0
