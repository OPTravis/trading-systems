"""
Test Configuration - Fixtures and mocks for stock-ai-trader tests.
"""

import asyncio
import pytest
import pytest_asyncio
from datetime import datetime, date
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import numpy as np

# Add project root to path
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.brokers.broker_protocol import (
    AccountSummary,
    Bar,
    Contract,
    ContractDetails,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Tick,
    TimeInForce,
)
from src.brokers.paper_client import PaperClient


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def paper_client():
    """Create a fresh PaperClient for testing."""
    client = PaperClient(starting_balance=100_000.0)
    return client


@pytest.fixture
def mock_broker():
    """Create a mock broker that implements BrokerProtocol."""
    broker = AsyncMock()
    broker.connect = AsyncMock()
    broker.disconnect = AsyncMock()
    broker.is_connected = AsyncMock(return_value=True)
    broker.get_account = AsyncMock(
        return_value=AccountSummary(
            account_id="MOCK-001",
            net_liquidation=100_000.0,
            total_cash=100_000.0,
            available_funds=100_000.0,
            buying_power=200_000.0,
            gross_position_value=0.0,
        )
    )
    broker.get_positions = AsyncMock(return_value=[])
    broker.place_order = AsyncMock()
    broker.cancel_order = AsyncMock()
    broker.get_open_orders = AsyncMock(return_value=[])
    return broker


@pytest.fixture
def sample_contract():
    """Create a sample stock contract."""
    return Contract(
        symbol="AAPL",
        exchange="SMART",
        currency="USD",
        sec_type="STK",
    )


@pytest.fixture
def sample_order(sample_contract):
    """Create a sample buy order."""
    return Order(
        contract=sample_contract,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100,
        time_in_force=TimeInForce.DAY,
    )


@pytest.fixture
def sample_ohlcv_data():
    """Generate sample OHLCV data for testing strategies."""
    np.random.seed(42)
    n = 100
    dates = pd.date_range(end=datetime.now(), periods=n, freq="D")

    # Generate realistic-looking price data
    base_price = 150.0
    returns = np.random.normal(0.001, 0.02, n)
    prices = base_price * np.cumprod(1 + returns)

    df = pd.DataFrame(
        {
            "open": prices * (1 + np.random.uniform(-0.005, 0.005, n)),
            "high": prices * (1 + np.random.uniform(0.001, 0.02, n)),
            "low": prices * (1 - np.random.uniform(0.001, 0.02, n)),
            "close": prices,
            "volume": np.random.randint(1_000_000, 50_000_000, n),
        },
        index=dates,
    )
    return df


@pytest.fixture
def sample_universe(sample_ohlcv_data):
    """Create a sample universe of stocks."""
    symbols = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"]
    universe = {}
    for symbol in symbols:
        # Create slightly different data for each symbol
        df = sample_ohlcv_data.copy()
        noise = np.random.uniform(0.9, 1.1)
        df["close"] = df["close"] * noise
        df["open"] = df["open"] * noise
        df["high"] = df["high"] * noise
        df["low"] = df["low"] * noise
        universe[symbol] = df
    return universe


@pytest.fixture
def mock_data_feed():
    """Create a mock data feed."""
    feed = MagicMock()
    feed.get_historical = AsyncMock(return_value=[])
    feed.get_realtime = AsyncMock(return_value=None)
    feed.is_connected = MagicMock(return_value=True)
    return feed


@pytest.fixture
def sample_positions():
    """Create sample position data."""
    return [
        Position(
            contract=Contract(symbol="AAPL", exchange="SMART"),
            quantity=100,
            avg_cost=150.0,
            market_value=15_500.0,
            unrealized_pnl=500.0,
        ),
        Position(
            contract=Contract(symbol="MSFT", exchange="SMART"),
            quantity=50,
            avg_cost=380.0,
            market_value=19_500.0,
            unrealized_pnl=500.0,
        ),
    ]


@pytest.fixture
def market_calendar():
    """Create a MarketCalendar instance."""
    from src.market.market_calendar import MarketCalendar
    return MarketCalendar(year=2026)


@pytest.fixture
def market_hours():
    """Create a MarketHours instance."""
    from src.market.market_hours import MarketHours
    return MarketHours()
