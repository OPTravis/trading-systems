"""
Integration tests against live IBKR Gateway and CPG.

These tests connect to the running Docker services:
- IBKR Gateway on localhost:4001 (paper account)
- CPG on localhost:5000

Run with: pytest tests/test_integration_live.py -v
"""

from unittest.mock import MagicMock

import pytest

from src.brokers.broker_protocol import (
    Contract,
    Order,
    OrderSide,
    OrderType,
)
from src.brokers.cpg_client import CPGClient
from src.brokers.ibkr_client import IBKRClient
from src.brokers.sync_ibkr_wrapper import SyncIBKRWrapper

# ── IBKR Client (async) ───────────────────────────────────────────────


class TestIBKRClientIntegration:
    """Async IBKR client tests against live Gateway."""

    @pytest.mark.asyncio
    async def test_connect_disconnect(self):
        c = IBKRClient(host="127.0.0.1", port=4001, client_id=51)
        await c.connect()
        assert await c.is_connected()
        await c.disconnect()
        assert not await c.is_connected()

    @pytest.mark.asyncio
    async def test_reconnect_on_disconnect(self):
        c = IBKRClient(host="127.0.0.1", port=4001, client_id=52)
        await c.connect()
        assert await c.is_connected()
        # Simulate disconnect and reconnect
        c._connected = False
        await c.connect()
        assert await c.is_connected()
        await c.disconnect()

    @pytest.mark.asyncio
    async def test_rate_limiter(self):
        from src.brokers.ibkr_client import RateLimiter

        rl = RateLimiter(max_per_second=10)
        await rl.acquire()
        assert rl._tokens < 10

    @pytest.mark.asyncio
    async def test_pacing_limiter(self):
        from src.brokers.ibkr_client import PacingLimiter

        pl = PacingLimiter(max_per_10min=55)
        await pl.acquire()
        assert len(pl._timestamps) == 1

    def test_map_order_status(self):
        c = IBKRClient()
        from src.brokers.broker_protocol import OrderStatus

        assert c._map_order_status("Filled") == OrderStatus.FILLED
        assert c._map_order_status("Submitted") == OrderStatus.SUBMITTED
        assert c._map_order_status("Cancelled") == OrderStatus.CANCELLED
        assert c._map_order_status("Inactive") == OrderStatus.REJECTED
        assert c._map_order_status("PreSubmitted") == OrderStatus.SUBMITTED
        assert c._map_order_status("ApiCancelled") == OrderStatus.CANCELLED
        assert c._map_order_status("Unknown") == OrderStatus.PENDING

    def test_to_ib_contract(self):
        c = IBKRClient()
        contract = Contract(symbol="AAPL", exchange="SMART", currency="USD")
        ib = c._to_ib_contract(contract)
        assert ib.symbol == "AAPL"

    def test_from_ib_contract(self):
        c = IBKRClient()
        ib = MagicMock()
        ib.symbol = "AAPL"
        ib.exchange = "SMART"
        ib.currency = "USD"
        ib.secType = "STK"
        ib.conId = 12345
        ib.localSymbol = "AAPL"
        contract = c._from_ib_contract(ib)
        assert contract.symbol == "AAPL"
        assert contract.contract_id == 12345

    def test_to_ib_order(self):
        c = IBKRClient()
        order = Order(
            contract=Contract(symbol="AAPL"),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=100,
        )
        ib_order = c._to_ib_order(order)
        assert ib_order.action == "BUY"
        assert ib_order.totalQuantity == 100

    def test_to_ib_order_limit(self):
        c = IBKRClient()
        order = Order(
            contract=Contract(symbol="AAPL"),
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=50,
            limit_price=150.0,
        )
        ib_order = c._to_ib_order(order)
        assert ib_order.action == "SELL"
        assert ib_order.lmtPrice == 150.0


# ── SyncIBKRWrapper ───────────────────────────────────────────────────


class TestSyncIBKRWrapperIntegration:
    """Sync wrapper tests against live Gateway."""

    @pytest.fixture
    def wrapper(self):
        w = SyncIBKRWrapper(host="127.0.0.1", port=4001, client_id=53)
        w.connect()
        yield w
        w.disconnect()

    def test_connect_disconnect(self):
        w = SyncIBKRWrapper(host="127.0.0.1", port=4001, client_id=54)
        w.connect()
        assert w.is_connected()
        w.disconnect()
        assert not w.is_connected()

    def test_get_account(self, wrapper):
        account = wrapper.get_account()
        assert account.account_id is not None
        assert account.net_liquidation > 0
        assert account.total_cash > 0

    def test_get_portfolio(self, wrapper):
        portfolio = wrapper.get_portfolio()
        assert isinstance(portfolio, list)

    def test_get_market_data(self, wrapper):
        data = wrapper.get_market_data("AAPL")
        assert "symbol" in data
        assert data["symbol"] == "AAPL"
        assert "price" in data

    def test_get_historical_bars(self, wrapper):
        bars = wrapper.get_historical_bars("AAPL", duration="5 D", bar_size="1 day")
        # IBKR may reject if another session is connected from different IP
        if not bars:
            pytest.skip(
                "IBKR historical data unavailable (different IP session conflict)"
            )
        assert "open" in bars[0]
        assert bars[0]["open"] > 0

    def test_get_historical_bars_msft(self, wrapper):
        bars = wrapper.get_historical_bars("MSFT", duration="5 D", bar_size="1 day")
        if not bars:
            pytest.skip(
                "IBKR historical data unavailable (different IP session conflict)"
            )

    def test_multiple_symbols(self, wrapper):
        for sym in ["AAPL", "MSFT", "GOOGL"]:
            data = wrapper.get_market_data(sym)
            assert data["symbol"] == sym


# ── CPG Client ────────────────────────────────────────────────────────


class TestCPGClientIntegration:
    """Tests against live CPG (Client Portal Gateway)."""

    @pytest.fixture
    def cpg(self):
        return CPGClient(base_url="https://localhost:5000")

    def test_is_session_active(self, cpg):
        result = cpg.is_session_active()
        assert isinstance(result, bool)

    def test_get_accounts(self, cpg):
        accounts = cpg.get_accounts()
        assert isinstance(accounts, list)

    def test_get_account_summary(self, cpg):
        accounts = cpg.get_accounts()
        if not accounts:
            pytest.skip("CPG session not active")
        summary = cpg.get_account_summary(accounts[0])
        assert summary is not None

    def test_get_positions(self, cpg):
        accounts = cpg.get_accounts()
        if not accounts:
            pytest.skip("CPG session not active")
        positions = cpg.get_positions(accounts[0])
        assert isinstance(positions, list)

    def test_get_live_status(self, cpg):
        accounts = cpg.get_accounts()
        if not accounts:
            pytest.skip("CPG session not active")
        status = cpg.get_live_status(accounts[0])
        assert status is not None
        assert "summary" in status
        assert "positions" in status

    def test_connection_error(self):
        cpg = CPGClient(base_url="https://localhost:9999")
        assert cpg.is_session_active() is False
