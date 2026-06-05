"""
Paper Trading Client - Simulated broker for testing and development.

Implements BrokerProtocol with an in-memory order book and simulated fills.
Does not require any external connection.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from typing import AsyncIterator, Optional

from .broker_protocol import (
    AccountSummary,
    Bar,
    BarSize,
    BrokerProtocol,
    Contract,
    ContractDetails,
    Position,
    Tick,
)

logger = logging.getLogger(__name__)


class PaperClient(BrokerProtocol):
    """
    Simulated broker for paper trading.

    - Maintains a virtual account with configurable starting balance
    - Simulates market/limit order fills at provided market prices
    - Tracks positions and P&L
    - No external connections required
    """

    def __init__(
        self,
        starting_balance: float = 100_000.0,
        fill_slippage_bps: float = 5.0,
        commission_per_share: float = 0.005,
    ):
        self._balance = starting_balance
        self._starting_balance = starting_balance
        self._slippage_bps = fill_slippage_bps
        self._commission = commission_per_share
        self._connected = False
        self._positions: dict[str, Position] = {}
        self._market_prices: dict[str, float] = {}  # symbol -> last price

    # ── Connection ───────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._connected = True
        logger.info("Paper trading client connected")

    async def disconnect(self) -> None:
        self._connected = False
        logger.info("Paper trading client disconnected")

    async def is_connected(self) -> bool:
        return self._connected

    # ── Market Data ──────────────────────────────────────────────────────

    async def get_market_data(self, contract: Contract, snapshot: bool = False) -> Tick:
        """Return simulated tick data. Uses last known price or placeholder."""
        price = self._market_prices.get(contract.symbol, 100.0)
        spread = price * 0.001  # 10 bps spread

        return Tick(
            timestamp=datetime.now(),
            last_price=price,
            bid=price - spread / 2,
            ask=price + spread / 2,
            bid_size=100,
            ask_size=100,
            last_size=100,
            volume=1_000_000,
        )

    async def get_historical_bars(
        self,
        contract: Contract,
        duration: str = "1 D",
        bar_size: BarSize = BarSize.ONE_MIN,
        end_date: Optional[datetime] = None,
        what_to_show: str = "TRADES",
        use_rth: bool = True,
    ) -> list[Bar]:
        """Return synthetic historical bars for testing."""
        # Generate placeholder bars
        now = end_date or datetime.now()
        base_price = self._market_prices.get(contract.symbol, 100.0)
        bars = []

        num_bars = {"1 D": 390, "1 W": 1950, "1 M": 7800}.get(duration, 390)
        _bar_minutes = {
            BarSize.ONE_MIN: 1,
            BarSize.FIVE_MIN: 5,
            BarSize.FIFTEEN_MIN: 15,
            BarSize.THIRTY_MIN: 30,
            BarSize.ONE_HOUR: 60,
            BarSize.ONE_DAY: 1440,
        }.get(bar_size, 1)

        for i in range(min(num_bars, 500)):
            ts = now
            noise = random.uniform(-0.02, 0.02)
            price = base_price * (1 + noise)
            bars.append(
                Bar(
                    timestamp=ts,
                    open=price * 0.999,
                    high=price * 1.005,
                    low=price * 0.995,
                    close=price,
                    volume=random.randint(1000, 100000),
                )
            )

        return bars

    async def stream_market_data(self, contract: Contract) -> AsyncIterator[Tick]:
        """Yield simulated ticks at ~4Hz."""
        price = self._market_prices.get(contract.symbol, 100.0)

        while self._connected:
            price *= 1 + random.uniform(-0.0005, 0.0005)
            self._market_prices[contract.symbol] = price
            spread = price * 0.001

            yield Tick(
                timestamp=datetime.now(),
                last_price=price,
                bid=price - spread / 2,
                ask=price + spread / 2,
                bid_size=random.randint(100, 500),
                ask_size=random.randint(100, 500),
                last_size=random.randint(1, 200),
            )
            await asyncio.sleep(0.25)

    # ── Account & Positions ──────────────────────────────────────────────

    async def get_account(self) -> AccountSummary:
        # NAV = cash balance + total market value of positions (not avg_cost * qty)
        # Using market_value avoids double-counting unrealized P&L
        positions = list(self._positions.values())
        net_liquidation = self._balance + sum(
            position.market_value for position in positions
        )
        unrealized = sum(p.unrealized_pnl for p in positions)

        return AccountSummary(
            account_id="PAPER-001",
            net_liquidation=net_liquidation,
            total_cash=self._balance,
            available_funds=self._balance,
            buying_power=self._balance * 2,  # 2x margin
            gross_position_value=sum(abs(p.market_value) for p in positions),
            unrealized_pnl=unrealized,
        )

    async def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def get_portfolio(self) -> list[Position]:
        return list(self._positions.values())

    # ── Contract Utilities ───────────────────────────────────────────────

    async def get_contract_details(self, contract: Contract) -> ContractDetails:
        return ContractDetails(
            contract=contract,
            long_name=f"{contract.symbol} (Paper)",
            market_name="PAPER",
        )

    async def qualify_contract(self, contract: Contract) -> Contract:
        contract.contract_id = hash(contract.symbol) % 1_000_000
        contract.qualified = True
        return contract

    # ── Order Management (stubs — analysis only, no execution) ────────────

    async def place_order(self, order):
        """Not supported — analysis-only tool."""
        raise NotImplementedError("PaperClient.place_order is removed — analysis only")

    async def cancel_order(self, order_id: int) -> None:
        """Not supported — analysis-only tool."""
        raise NotImplementedError("PaperClient.cancel_order is removed — analysis only")

    async def modify_order(self, order_id: int, quantity=None, limit_price=None, stop_price=None):
        """Not supported — analysis-only tool."""
        raise NotImplementedError("PaperClient.modify_order is removed — analysis only")

    async def get_open_orders(self) -> list:
        """Not supported — analysis-only tool."""
        raise NotImplementedError("PaperClient.get_open_orders is removed — analysis only")

    async def get_order(self, order_id: int):
        """Not supported — analysis-only tool."""
        raise NotImplementedError("PaperClient.get_order is removed — analysis only")

    # ── Paper-specific methods ───────────────────────────────────────────

    def set_market_price(self, symbol: str, price: float) -> None:
        """Manually set a market price for testing."""
        self._market_prices[symbol] = price

    def reset(self) -> None:
        """Reset to starting state."""
        self._balance = self._starting_balance
        self._positions.clear()
        logger.info("Paper trading account reset")
