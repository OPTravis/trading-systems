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
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
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
        self._orders: dict[int, Order] = {}
        self._next_order_id = 1
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

    # ── Order Management ─────────────────────────────────────────────────

    async def place_order(self, order: Order) -> Order:
        """Place and immediately fill an order (paper simulation)."""
        order_id = self._next_order_id
        self._next_order_id += 1
        order.order_id = order_id

        # Reject SELL orders when there is no position to sell
        if order.side == OrderSide.SELL:
            symbol = order.contract.symbol
            pos = self._positions.get(symbol)
            if pos is None or pos.quantity < order.quantity:
                order.status = OrderStatus.REJECTED
                self._orders[order_id] = order
                logger.warning(
                    "Paper order rejected: SELL %s x%.0f but position is %s",
                    symbol,
                    order.quantity,
                    f"{pos.quantity}" if pos else "none",
                )
                return order

        price = self._market_prices.get(order.contract.symbol, 100.0)

        # Apply slippage
        slippage = price * (self._slippage_bps / 10000)
        if order.side == OrderSide.BUY:
            fill_price = price + slippage
        else:
            fill_price = price - slippage

        # Use limit price if limit order
        if order.order_type == OrderType.LIMIT and order.limit_price:
            if order.side == OrderSide.BUY and order.limit_price < price:
                order.status = OrderStatus.SUBMITTED
                self._orders[order_id] = order
                return order
            elif order.side == OrderSide.SELL and order.limit_price > price:
                order.status = OrderStatus.SUBMITTED
                self._orders[order_id] = order
                return order
            fill_price = order.limit_price

        # Calculate commission
        commission = abs(order.quantity) * self._commission

        # Update balance
        cost = fill_price * order.quantity
        if order.side == OrderSide.BUY:
            self._balance -= cost + commission
        else:
            self._balance += cost - commission

        # Update position
        symbol = order.contract.symbol
        if symbol in self._positions:
            pos = self._positions[symbol]
            if order.side == OrderSide.BUY:
                total_cost = pos.avg_cost * pos.quantity + fill_price * order.quantity
                pos.quantity += order.quantity
                pos.avg_cost = total_cost / pos.quantity if pos.quantity else 0
            else:
                pos.quantity -= order.quantity
                if pos.quantity == 0:
                    del self._positions[symbol]
        elif order.side == OrderSide.BUY:
            self._positions[symbol] = Position(
                contract=order.contract,
                quantity=order.quantity,
                avg_cost=fill_price,
            )

        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.avg_fill_price = fill_price
        order.commission = commission
        self._orders[order_id] = order

        logger.info(
            f"Paper fill: {order.side.value} {order.quantity} {symbol} "
            f"@ {fill_price:.2f} (commission: {commission:.2f})"
        )
        return order

    async def cancel_order(self, order_id: int) -> None:
        if order_id in self._orders:
            self._orders[order_id].status = OrderStatus.CANCELLED
            logger.info(f"Paper order {order_id} cancelled")
        else:
            logger.warning(f"Paper order {order_id} not found")

    async def modify_order(
        self,
        order_id: int,
        quantity: Optional[float] = None,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
    ) -> Order:
        if order_id not in self._orders:
            raise ValueError(f"Order {order_id} not found")

        order = self._orders[order_id]
        if quantity is not None:
            order.quantity = quantity
        if limit_price is not None:
            order.limit_price = limit_price
        if stop_price is not None:
            order.stop_price = stop_price

        logger.info(f"Paper order {order_id} modified")
        return order

    async def get_open_orders(self) -> list[Order]:
        return [
            o
            for o in self._orders.values()
            if o.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED)
        ]

    async def get_order(self, order_id: int) -> Optional[Order]:
        """Get a specific order by ID."""
        return self._orders.get(order_id)

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

    # ── Paper-specific methods ───────────────────────────────────────────

    def set_market_price(self, symbol: str, price: float) -> None:
        """Manually set a market price for testing."""
        self._market_prices[symbol] = price

    def reset(self) -> None:
        """Reset to starting state."""
        self._balance = self._starting_balance
        self._positions.clear()
        self._orders.clear()
        self._next_order_id = 1
        logger.info("Paper trading account reset")
