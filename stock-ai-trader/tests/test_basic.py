"""
Basic Smoke Tests - Quick validation of core components.

Tests:
- Paper client buy/sell
- Market hours US
- Market calendar holidays
- Trend strategy signal generation
- PDT guard enforcement
- Stock scorer
"""

import asyncio
import pytest
import pytest_asyncio
from datetime import datetime, date
from unittest.mock import MagicMock

import pandas as pd
import numpy as np

from src.brokers.broker_protocol import (
    Contract,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from src.brokers.paper_client import PaperClient
from src.market.market_hours import MarketHours, Market, MarketState
from src.market.market_calendar import MarketCalendar
from src.strategies.trend_strategy import TrendStrategy
from src.strategies.base_strategy import SignalAction
from src.risk.pdt_guard import PDTGuard
from src.scoring.stock_scorer import StockScorer


# ── Paper Client Tests ────────────────────────────────────────────────


class TestPaperClient:
    """Test PaperClient buy/sell operations."""

    @pytest.mark.asyncio
    async def test_paper_client_buy_sell(self, paper_client):
        """Test buying and selling stocks on paper client."""
        await paper_client.connect()
        assert await paper_client.is_connected()

        # Set market price
        paper_client.set_market_price("AAPL", 150.0)

        # Create buy order
        contract = Contract(symbol="AAPL", exchange="SMART")
        buy_order = Order(
            contract=contract,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=100,
        )

        # Execute buy
        filled = await paper_client.place_order(buy_order)
        assert filled.status == OrderStatus.FILLED
        assert filled.filled_qty == 100
        assert filled.avg_fill_price > 0

        # Check position
        positions = await paper_client.get_positions()
        assert len(positions) == 1
        assert positions[0].contract.symbol == "AAPL"
        assert positions[0].quantity == 100

        # Create sell order
        sell_order = Order(
            contract=contract,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=100,
        )

        # Execute sell
        filled = await paper_client.place_order(sell_order)
        assert filled.status == OrderStatus.FILLED
        assert filled.filled_qty == 100

        # Check position closed
        positions = await paper_client.get_positions()
        assert len(positions) == 0

        await paper_client.disconnect()

    @pytest.mark.asyncio
    async def test_paper_client_account(self, paper_client):
        """Test account summary retrieval."""
        await paper_client.connect()

        account = await paper_client.get_account()
        assert account.account_id == "PAPER-001"
        assert account.net_liquidation == 100_000.0
        assert account.total_cash == 100_000.0

        await paper_client.disconnect()

    @pytest.mark.asyncio
    async def test_paper_client_reset(self, paper_client):
        """Test account reset."""
        await paper_client.connect()

        # Buy some stock
        paper_client.set_market_price("AAPL", 150.0)
        contract = Contract(symbol="AAPL")
        await paper_client.place_order(
            Order(contract=contract, side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=100)
        )

        # Reset
        paper_client.reset()
        positions = await paper_client.get_positions()
        assert len(positions) == 0

        account = await paper_client.get_account()
        assert account.net_liquidation == 100_000.0

        await paper_client.disconnect()


# ── Market Hours Tests ────────────────────────────────────────────────


class TestMarketHours:
    """Test market hours functionality."""

    def test_market_hours_us(self, market_hours):
        """Test US market hours detection."""
        # MarketHours should have US market defined
        sessions = market_hours.get_sessions(Market.US)
        assert "timezone" in sessions
        assert sessions["timezone"] == "America/New_York"
        assert "regular" in sessions
        assert "pre_market" in sessions
        assert "post_market" in sessions

    def test_market_state_enum(self):
        """Test MarketState enum values."""
        assert MarketState.OPEN == "OPEN"
        assert MarketState.CLOSED == "CLOSED"
        assert MarketState.PRE_MARKET == "PRE_MARKET"
        assert MarketState.POST_MARKET == "POST_MARKET"

    def test_market_enum(self):
        """Test Market enum values."""
        assert Market.US == "US"
        assert Market.HK == "HK"
        assert Market.CN == "CN"

    def test_minutes_until_open(self, market_hours):
        """Test minutes until open calculation."""
        minutes = market_hours.minutes_until_open(Market.US)
        assert isinstance(minutes, int)
        assert minutes >= 0


# ── Market Calendar Tests ─────────────────────────────────────────────


class TestMarketCalendar:
    """Test market calendar functionality."""

    def test_market_calendar_holidays(self, market_calendar):
        """Test holiday detection."""
        # Test New Year's Day 2026
        new_years = date(2026, 1, 1)
        assert market_calendar.is_holiday(new_years, Market.US)
        assert market_calendar.is_holiday(new_years, Market.HK)
        assert market_calendar.is_holiday(new_years, Market.CN)

        # Test Thanksgiving 2026
        thanksgiving = date(2026, 11, 26)
        assert market_calendar.is_holiday(thanksgiving, Market.US)

        # Test Christmas 2026
        christmas = date(2026, 12, 25)
        assert market_calendar.is_holiday(christmas, Market.US)

    def test_is_trading_day(self, market_calendar):
        """Test trading day detection."""
        # Weekend is not a trading day
        saturday = date(2026, 1, 3)
        assert not market_calendar.is_trading_day(saturday, Market.US)

        # Holiday is not a trading day
        new_years = date(2026, 1, 1)
        assert not market_calendar.is_trading_day(new_years, Market.US)

        # Regular weekday is a trading day
        friday = date(2026, 1, 2)
        assert market_calendar.is_trading_day(friday, Market.US)

    def test_next_trading_day(self, market_calendar):
        """Test next trading day calculation."""
        # After Friday Jan 2, next trading day is Monday Jan 5
        friday = date(2026, 1, 2)
        next_day = market_calendar.next_trading_day(friday, Market.US)
        assert next_day == date(2026, 1, 5)

    def test_get_holidays(self, market_calendar):
        """Test getting holiday list."""
        us_holidays = market_calendar.get_holidays(2026, Market.US)
        assert len(us_holidays) >= 9  # At least 9 US holidays
        assert date(2026, 1, 1) in us_holidays


# ── Trend Strategy Tests ──────────────────────────────────────────────


class TestTrendStrategy:
    """Test trend strategy signal generation."""

    def test_trend_strategy_signal(self, sample_universe):
        """Test that trend strategy can generate signals without errors."""
        strategy = TrendStrategy()

        # Generate signals
        signals = strategy.generate_signals(sample_universe)

        # Should return a list (may be empty if no signals)
        assert isinstance(signals, list)

        # If signals exist, validate structure
        for signal in signals:
            assert signal.symbol in sample_universe
            assert signal.action in [SignalAction.BUY, SignalAction.SELL]
            assert 0 <= signal.strength <= 1.0
            assert signal.strategy == "TrendFollowing"
            assert signal.timestamp is not None

    def test_trend_strategy_params(self):
        """Test strategy parameter defaults."""
        strategy = TrendStrategy()
        params = strategy.get_params()

        assert params["fast_period"] == 10
        assert params["slow_period"] == 30
        assert params["adx_threshold"] == 25
        assert params["atr_period"] == 14

    def test_trend_strategy_custom_params(self):
        """Test strategy with custom parameters."""
        strategy = TrendStrategy(params={"fast_period": 5, "slow_period": 20})
        params = strategy.get_params()

        assert params["fast_period"] == 5
        assert params["slow_period"] == 20

    def test_trend_strategy_no_position_tracking(self):
        """Test that strategy starts with no positions."""
        strategy = TrendStrategy()
        assert not strategy.has_position("AAPL")
        assert len(strategy.get_positions()) == 0


# ── PDT Guard Tests ───────────────────────────────────────────────────


class TestPDTGuard:
    """Test Pattern Day Trader guard."""

    def test_pdt_guard(self):
        """Test PDT guard enforces rules."""
        guard = PDTGuard()

        # Account over $25K: unlimited day trades
        assert guard.can_day_trade(30_000.0)
        assert guard.get_remaining_day_trades(30_000.0) == float("inf")

        # Account under $25K: limited day trades
        assert guard.can_day_trade(10_000.0)
        assert guard.get_remaining_day_trades(10_000.0) == 3

        # Record 3 day trades
        for i in range(3):
            guard.record_day_trade()

        # Should be blocked now
        assert not guard.can_day_trade(10_000.0)
        assert guard.get_remaining_day_trades(10_000.0) == 0

        # But still OK for large account
        assert guard.can_day_trade(30_000.0)

    def test_pdt_guard_reset(self):
        """Test PDT guard reset."""
        guard = PDTGuard()

        # Use up day trades
        for _ in range(3):
            guard.record_day_trade()
        assert not guard.can_day_trade(10_000.0)

        # Reset
        guard.reset()
        assert guard.can_day_trade(10_000.0)
        assert guard.get_remaining_day_trades(10_000.0) == 3


# ── Stock Scorer Tests ────────────────────────────────────────────────


class TestStockScorer:
    """Test stock scoring functionality."""

    def test_stock_scorer(self):
        """Test basic stock scoring."""
        scorer = StockScorer()
        score = scorer.score_stock("AAPL")

        # Should return a StockScore
        assert score.symbol == "AAPL"
        assert 0 <= score.composite <= 100
        assert score.technical >= 0
        assert score.fundamental >= 0
        assert score.momentum >= 0
        assert score.sentiment >= 0
        assert score.quality >= 0
        assert score.value >= 0

    def test_stock_scorer_with_data(self):
        """Test scoring with market data."""
        scorer = StockScorer()

        market_data = {
            "rsi": 25,  # Oversold
            "macd_signal": 1,
            "return_5d": 0.05,
            "return_20d": 0.10,
            "relative_volume": 2.0,
        }

        score = scorer.score_stock("AAPL", market_data)
        assert score.symbol == "AAPL"
        assert score.technical > 50  # Should be above neutral (oversold RSI)
        assert score.momentum > 50   # Positive momentum

    def test_stock_scorer_weights(self):
        """Test that scorer uses weights correctly."""
        scorer = StockScorer()
        weights = scorer._get_weights()

        assert "technical" in weights
        assert "fundamental" in weights
        assert "momentum" in weights
        assert "sentiment" in weights
        assert "quality" in weights
        assert "value" in weights

        # Weights should sum to ~1.0
        total = sum(weights.values())
        assert abs(total - 1.0) < 0.01

    def test_stock_scorer_overbought(self):
        """Test scoring for overbought conditions."""
        scorer = StockScorer()

        market_data = {
            "rsi": 80,  # Overbought
            "macd_signal": -1,
        }

        score = scorer.score_stock("AAPL", market_data)
        assert score.technical < 50  # Should be below neutral
