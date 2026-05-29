"""
Broker Protocol - Abstract interface for all broker implementations.

Defines the standard interface that any broker client must implement
to be used by the trading system. This enables easy swapping between
IBKR, Alpaca, paper trading, etc.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, AsyncIterator, Optional


# ─── Data Models ────────────────────────────────────────────────────────────


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MKT"
    LIMIT = "LMT"
    STOP = "STP"
    STOP_LIMIT = "STPLMT"
    TRAILING_STOP = "TRAIL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class TimeInForce(str, Enum):
    DAY = "DAY"
    GTC = "GTC"  # Good Till Cancelled
    IOC = "IOC"  # Immediate or Cancel
    FOK = "FOK"  # Fill or Kill


class BarSize(str, Enum):
    ONE_MIN = "1 min"
    FIVE_MIN = "5 mins"
    FIFTEEN_MIN = "15 mins"
    THIRTY_MIN = "30 mins"
    ONE_HOUR = "1 hour"
    ONE_DAY = "1 day"


@dataclass
class Contract:
    """Represents a tradeable instrument."""
    symbol: str
    exchange: str = "SMART"
    currency: str = "USD"
    sec_type: str = "STK"  # STK, OPT, FUT, CASH, etc.
    contract_id: Optional[int] = None
    local_symbol: Optional[str] = None
    expiry: Optional[str] = None
    strike: Optional[float] = None
    right: Optional[str] = None  # C or P for options
    multiplier: Optional[float] = None
    # Populated after qualification
    qualified: bool = False
    min_tick: Optional[float] = None
    lot_size: Optional[int] = None


@dataclass
class Order:
    """Represents a trade order."""
    contract: Contract
    side: OrderSide
    order_type: OrderType
    quantity: float
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    time_in_force: TimeInForce = TimeInForce.DAY
    transmit: bool = True
    parent_id: Optional[int] = None
    order_id: Optional[int] = None
    # Populated after submission
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    commission: float = 0.0


@dataclass
class Bar:
    """OHLCV bar data."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    bar_count: int = 0
    wap: float = 0.0  # Weighted Average Price


@dataclass
class Tick:
    """Real-time tick data."""
    timestamp: datetime
    last_price: float
    bid: float = 0.0
    ask: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    last_size: float = 0.0
    volume: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    open: float = 0.0


@dataclass
class Position:
    """Account position."""
    contract: Contract
    quantity: float
    avg_cost: float
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0


@dataclass
class AccountSummary:
    """Account balance and margin info."""
    account_id: str
    net_liquidation: float
    total_cash: float
    available_funds: float
    buying_power: float
    gross_position_value: float
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    maintenance_margin: float = 0.0
    excess_liquidity: float = 0.0
    currency: str = "USD"


@dataclass
class ContractDetails:
    """Extended contract information."""
    contract: Contract
    long_name: str = ""
    industry: str = ""
    category: str = ""
    subcategory: str = ""
    market_name: str = ""
    trading_hours: str = ""
    time_zone: str = ""
    min_tick: float = 0.0
    price_magnifier: float = 1.0
    contract_month: str = ""
    # Market data
    market_rule_ids: list[str] = field(default_factory=list)


# ─── Protocol Interface ─────────────────────────────────────────────────────


class BrokerProtocol(ABC):
    """
    Abstract protocol that all broker clients must implement.

    Usage:
        broker: BrokerProtocol = IBKRClient(host="localhost", port=4001)
        await broker.connect()
        bars = await broker.get_historical_bars(contract, duration="1 D", bar_size=BarSize.ONE_MIN)
        order = await broker.place_order(order)
        await broker.disconnect()
    """

    # ── Connection ───────────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to broker. Raises on failure."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully disconnect from broker."""
        ...

    @abstractmethod
    async def is_connected(self) -> bool:
        """Return True if connected and authenticated."""
        ...

    # ── Market Data ──────────────────────────────────────────────────────

    @abstractmethod
    async def get_market_data(
        self, contract: Contract, snapshot: bool = False
    ) -> Tick:
        """
        Get real-time or snapshot market data for a contract.

        Args:
            contract: The instrument to get data for.
            snapshot: If True, return a single snapshot; otherwise stream.

        Returns:
            Current Tick with bid/ask/last prices.
        """
        ...

    @abstractmethod
    async def get_historical_bars(
        self,
        contract: Contract,
        duration: str = "1 D",
        bar_size: BarSize = BarSize.ONE_MIN,
        end_date: Optional[datetime] = None,
        what_to_show: str = "TRADES",
        use_rth: bool = True,
    ) -> list[Bar]:
        """
        Get historical OHLCV bars.

        Args:
            contract: The instrument.
            duration: How far back (e.g. "1 D", "1 W", "1 M").
            bar_size: Bar granularity.
            end_date: End of data (default: now).
            what_to_show: TRADES, MIDPOINT, BID, ASK, etc.
            use_rth: Regular Trading Hours only.

        Returns:
            List of Bar objects, oldest first.
        """
        ...

    @abstractmethod
    def stream_market_data(
        self, contract: Contract
    ) -> AsyncIterator[Tick]:
        """Async generator yielding real-time ticks."""
        ...

    # ── Account & Positions ──────────────────────────────────────────────

    @abstractmethod
    async def get_account(self) -> AccountSummary:
        """Get current account summary (balances, margin)."""
        ...

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Get all current positions."""
        ...

    @abstractmethod
    async def get_portfolio(self) -> list[Position]:
        """Get portfolio with real-time P&L."""
        ...

    # ── Order Management ─────────────────────────────────────────────────

    @abstractmethod
    async def place_order(self, order: Order) -> Order:
        """
        Submit an order to the broker.

        Args:
            order: Order with contract, side, type, quantity, prices.

        Returns:
            Updated Order with broker-assigned order_id and status.
        """
        ...

    @abstractmethod
    async def cancel_order(self, order_id: int) -> None:
        """Cancel a pending/submitted order by ID."""
        ...

    @abstractmethod
    async def modify_order(
        self, order_id: int, quantity: Optional[float] = None,
        limit_price: Optional[float] = None, stop_price: Optional[float] = None
    ) -> Order:
        """Modify a pending order's parameters."""
        ...

    @abstractmethod
    async def get_open_orders(self) -> list[Order]:
        """Get all currently open orders."""
        ...

    # ── Contract Utilities ───────────────────────────────────────────────

    @abstractmethod
    async def get_contract_details(self, contract: Contract) -> ContractDetails:
        """Get detailed information about a contract."""
        ...

    @abstractmethod
    async def qualify_contract(self, contract: Contract) -> Contract:
        """
        Resolve a partial contract to a fully qualified one.
        Populates contract_id, exchange details, etc.
        """
        ...
