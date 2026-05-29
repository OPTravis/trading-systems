"""
IBKR Client - Interactive Brokers implementation using ib_async.

Connects to IBKR Gateway (TWS) running in Docker. Supports:
- Real-time and historical market data
- Full order management
- Account and position queries
- Auto-reconnect with exponential backoff
- Rate limiting (50 msgs/sec) and historical data pacing (60 req/10min)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timedelta
from typing import AsyncIterator, Optional

from ib_async import IB, Contract as IBContract, Order as IBOrder, Trade
from ib_async import MarketOrder, LimitOrder, StopOrder, StopLimitOrder
from ib_async import Stock, Forex, Future, Option
from ib_async.util import dataclassAsDict

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
    TimeInForce,
)

logger = logging.getLogger(__name__)


class RateLimiter:
    """Token bucket rate limiter for IBKR API messages."""

    def __init__(self, max_per_second: float = 45):
        self._rate = max_per_second
        self._tokens = max_per_second
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            self._last_refill = now

            if self._tokens < 1:
                wait = (1 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1


class PacingLimiter:
    """
    Historical data pacing limiter.
    IBKR limits: 60 requests per 10 minutes for identical data.
    Also enforces minimum 15s between requests for same contract+bar_size.
    """

    def __init__(self, max_per_10min: int = 55):  # 55 to leave margin
        self._max = max_per_10min
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()

            # Remove timestamps older than 10 minutes
            cutoff = now - 600
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()

            # If at limit, wait until oldest entry expires
            if len(self._timestamps) >= self._max:
                wait_until = self._timestamps[0] + 600
                wait_secs = wait_until - now
                if wait_secs > 0:
                    logger.info(f"Pacing limit reached, waiting {wait_secs:.1f}s")
                    await asyncio.sleep(wait_secs)

            self._timestamps.append(time.monotonic())


class IBKRClient(BrokerProtocol):
    """
    Interactive Brokers client using ib_async library.

    Connects to IBKR Gateway running in Docker (see docker-compose.yml).
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4001,
        client_id: int = 1,
        max_reconnect_attempts: int = 5,
        account_id: Optional[str] = None,
    ):
        self._host = host
        self._port = port
        self._client_id = client_id
        self._account_id = account_id
        self._max_reconnect = max_reconnect_attempts
        self._ib = IB()
        self._rate_limiter = RateLimiter(max_per_second=45)
        self._pacing_limiter = PacingLimiter()
        self._reconnect_attempts = 0
        self._connected = False

        # Event callbacks
        self._ib.disconnectedEvent += self._on_disconnect
        self._ib.errorEvent += self._on_error

    # ── Connection ───────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to IBKR Gateway with retry logic."""
        for attempt in range(1, self._max_reconnect + 1):
            try:
                logger.info(
                    f"Connecting to IBKR Gateway at {self._host}:{self._port} "
                    f"(attempt {attempt}/{self._max_reconnect})"
                )
                await self._ib.connectAsync(
                    host=self._host,
                    port=self._port,
                    clientId=self._client_id,
                    timeout=20,
                )
                self._connected = True
                self._reconnect_attempts = 0
                logger.info(f"Connected to IBKR Gateway (client_id={self._client_id})")
                return
            except Exception as e:
                logger.warning(f"Connection attempt {attempt} failed: {e}")
                if attempt < self._max_reconnect:
                    backoff = min(2 ** attempt, 30)
                    logger.info(f"Retrying in {backoff}s...")
                    await asyncio.sleep(backoff)

        raise ConnectionError(
            f"Failed to connect to IBKR Gateway after {self._max_reconnect} attempts"
        )

    async def disconnect(self) -> None:
        """Gracefully disconnect."""
        if self._ib.isConnected():
            self._ib.disconnect()
        self._connected = False
        logger.info("Disconnected from IBKR Gateway")

    async def is_connected(self) -> bool:
        return self._connected and self._ib.isConnected()

    def _on_disconnect(self) -> None:
        """Handle unexpected disconnection with auto-reconnect."""
        self._connected = False
        logger.warning("Disconnected from IBKR Gateway, attempting reconnect...")
        asyncio.create_task(self._auto_reconnect())

    async def _auto_reconnect(self) -> None:
        """Auto-reconnect with exponential backoff."""
        self._reconnect_attempts += 1
        if self._reconnect_attempts > self._max_reconnect:
            logger.error("Max reconnect attempts reached, giving up")
            return

        backoff = min(2 ** self._reconnect_attempts, 60)
        await asyncio.sleep(backoff)
        try:
            await self.connect()
        except ConnectionError:
            logger.error("Auto-reconnect failed")

    def _on_error(self, reqId: int, errorCode: int, errorString: str, contract) -> None:
        """Log IBKR errors."""
        # Codes 2104/2106/2158 are informational market data farm messages
        if errorCode in (2104, 2106, 2158):
            logger.debug(f"IBKR info: {errorCode} - {errorString}")
        elif errorCode in (502, 504, 1100, 1300):
            logger.error(f"IBKR connection error: {errorCode} - {errorString}")
        else:
            logger.warning(f"IBKR error {errorCode} (reqId={reqId}): {errorString}")

    # ── Contract Helpers ─────────────────────────────────────────────────

    def _to_ib_contract(self, contract: Contract) -> IBContract:
        """Convert our Contract to ib_async Contract."""
        if contract.sec_type == "STK":
            return Stock(contract.symbol, contract.exchange, contract.currency)
        elif contract.sec_type == "CASH":
            return Forex(contract.symbol)
        elif contract.sec_type == "FUT":
            return Future(
                contract.symbol, contract.expiry or "",
                contract.exchange, contract.currency,
                multiplier=contract.multiplier,
            )
        elif contract.sec_type == "OPT":
            return Option(
                contract.symbol, contract.expiry or "",
                contract.strike or 0, contract.right or "C",
                contract.exchange, contract.currency,
            )
        else:
            return IBContract(
                symbol=contract.symbol,
                secType=contract.sec_type,
                exchange=contract.exchange,
                currency=contract.currency,
            )

    def _from_ib_contract(self, ib_contract: IBContract) -> Contract:
        """Convert ib_async Contract to our Contract."""
        return Contract(
            symbol=ib_contract.symbol,
            exchange=ib_contract.exchange or "SMART",
            currency=ib_contract.currency or "USD",
            sec_type=ib_contract.secType or "STK",
            contract_id=ib_contract.conId,
            local_symbol=getattr(ib_contract, "localSymbol", None),
        )

    # ── Market Data ──────────────────────────────────────────────────────

    async def get_market_data(self, contract: Contract, snapshot: bool = False) -> Tick:
        """Get real-time or snapshot market data."""
        await self._rate_limiter.acquire()
        ib_contract = self._to_ib_contract(contract)

        ticker = self._ib.reqMktData(ib_contract, "", snapshot=snapshot, regulatorySnapshot=False)

        # Wait for initial data
        for _ in range(50):
            await asyncio.sleep(0.1)
            if ticker.last or ticker.bid or ticker.ask:
                break

        return Tick(
            timestamp=datetime.now(),
            last_price=ticker.last or 0.0,
            bid=ticker.bid or 0.0,
            ask=ticker.ask or 0.0,
            bid_size=ticker.bidSize or 0.0,
            ask_size=ticker.askSize or 0.0,
            last_size=ticker.lastSize or 0.0,
            volume=ticker.volume or 0.0,
            high=ticker.high or 0.0,
            low=ticker.low or 0.0,
            close=ticker.close or 0.0,
            open=ticker.open or 0.0,
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
        """Get historical OHLCV bars with pacing control."""
        await self._rate_limiter.acquire()
        await self._pacing_limiter.acquire()

        ib_contract = self._to_ib_contract(contract)
        end_str = end_date.strftime("%Y%m%d %H:%M:%S") if end_date else ""

        bars = self._ib.reqHistoricalData(
            ib_contract,
            endDateTime=end_str,
            durationStr=duration,
            barSizeSetting=bar_size.value,
            whatToShow=what_to_show,
            useRTH=int(use_rth),
            formatDate=1,
        )

        return [
            Bar(
                timestamp=b.date if isinstance(b.date, datetime) else datetime.now(),
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                bar_count=b.barCount,
                wap=b.average,
            )
            for b in bars
        ]

    async def stream_market_data(self, contract: Contract) -> AsyncIterator[Tick]:
        """Async generator yielding real-time ticks."""
        await self._rate_limiter.acquire()
        ib_contract = self._to_ib_contract(contract)
        ticker = self._ib.reqMktData(ib_contract, "", snapshot=False, regulatorySnapshot=False)

        try:
            while self._connected:
                await asyncio.sleep(0.25)
                yield Tick(
                    timestamp=datetime.now(),
                    last_price=ticker.last or 0.0,
                    bid=ticker.bid or 0.0,
                    ask=ticker.ask or 0.0,
                    bid_size=ticker.bidSize or 0.0,
                    ask_size=ticker.askSize or 0.0,
                    last_size=ticker.lastSize or 0.0,
                    volume=ticker.volume or 0.0,
                    high=ticker.high or 0.0,
                    low=ticker.low or 0.0,
                    close=ticker.close or 0.0,
                    open=ticker.open or 0.0,
                )
        finally:
            self._ib.cancelMktData(ib_contract)

    # ── Account & Positions ──────────────────────────────────────────────

    async def get_account(self) -> AccountSummary:
        """Get account summary."""
        await self._rate_limiter.acquire()

        # Request account summary
        tags = [
            "NetLiquidation", "TotalCashValue", "AvailableFunds",
            "BuyingPower", "GrossPositionValue", "UnrealizedPnL",
            "RealizedPnL", "MaintMarginReq", "ExcessLiquidity",
        ]
        account = self._account_id or self._ib.managedAccounts()[0].split(",")[0]

        summary = self._ib.reqAccountSummary(account, tags)
        values = {s.tag: float(s.value) for s in summary}

        return AccountSummary(
            account_id=account,
            net_liquidation=values.get("NetLiquidation", 0),
            total_cash=values.get("TotalCashValue", 0),
            available_funds=values.get("AvailableFunds", 0),
            buying_power=values.get("BuyingPower", 0),
            gross_position_value=values.get("GrossPositionValue", 0),
            unrealized_pnl=values.get("UnrealizedPnL", 0),
            realized_pnl=values.get("RealizedPnL", 0),
            maintenance_margin=values.get("MaintMarginReq", 0),
            excess_liquidity=values.get("ExcessLiquidity", 0),
        )

    async def get_positions(self) -> list[Position]:
        """Get all current positions."""
        await self._rate_limiter.acquire()
        positions = self._ib.positions()

        return [
            Position(
                contract=self._from_ib_contract(p.contract),
                quantity=p.position,
                avg_cost=p.avgCost,
                market_value=p.position * p.avgCost,  # Approximate
            )
            for p in positions
        ]

    async def get_portfolio(self) -> list[Position]:
        """Get portfolio with real-time P&L."""
        await self._rate_limiter.acquire()
        # Portfolio items come from account updates
        positions = self._ib.positions()

        result = []
        for p in positions:
            contract = self._from_ib_contract(p.contract)
            result.append(Position(
                contract=contract,
                quantity=p.position,
                avg_cost=p.avgCost,
                market_value=p.position * p.avgCost,
            ))
        return result

    # ── Order Management ─────────────────────────────────────────────────

    def _to_ib_order(self, order: Order) -> IBOrder:
        """Convert our Order to ib_async Order."""
        action = order.side.value

        if order.order_type == OrderType.MARKET:
            ib_order = MarketOrder(action, order.quantity)
        elif order.order_type == OrderType.LIMIT:
            ib_order = LimitOrder(action, order.quantity, order.limit_price)
        elif order.order_type == OrderType.STOP:
            ib_order = StopOrder(action, order.quantity, order.stop_price)
        elif order.order_type == OrderType.STOP_LIMIT:
            ib_order = StopLimitOrder(
                action, order.quantity, order.limit_price, order.stop_price
            )
        else:
            ib_order = MarketOrder(action, order.quantity)

        ib_order.tif = order.time_in_force.value
        ib_order.transmit = order.transmit
        if order.parent_id:
            ib_order.parentId = order.parent_id

        return ib_order

    async def place_order(self, order: Order) -> Order:
        """Submit an order to IBKR."""
        await self._rate_limiter.acquire()

        ib_contract = self._to_ib_contract(order.contract)
        ib_order = self._to_ib_order(order)

        trade: Trade = self._ib.placeOrder(ib_contract, ib_order)

        order.order_id = trade.order.orderId
        order.status = self._map_order_status(trade.orderStatus.status)

        logger.info(
            f"Order placed: {order.order_id} {order.side.value} "
            f"{order.quantity} {order.contract.symbol} @ {order.order_type.value}"
        )
        return order

    async def cancel_order(self, order_id: int) -> None:
        """Cancel an order by ID."""
        await self._rate_limiter.acquire()
        # Find the trade by order ID
        for trade in self._ib.openOrders():
            if trade.order.orderId == order_id:
                self._ib.cancelOrder(trade.order)
                logger.info(f"Order {order_id} cancelled")
                return
        logger.warning(f"Order {order_id} not found in open orders")

    async def modify_order(
        self, order_id: int, quantity: Optional[float] = None,
        limit_price: Optional[float] = None, stop_price: Optional[float] = None
    ) -> Order:
        """Modify an existing order."""
        await self._rate_limiter.acquire()

        for trade in self._ib.openOrders():
            if trade.order.orderId == order_id:
                if quantity is not None:
                    trade.order.totalQuantity = quantity
                if limit_price is not None:
                    trade.order.lmtPrice = limit_price
                if stop_price is not None:
                    trade.order.auxPrice = stop_price

                self._ib.placeOrder(trade.contract, trade.order)

                return Order(
                    contract=self._from_ib_contract(trade.contract),
                    side=OrderSide.BUY if trade.order.action == "BUY" else OrderSide.SELL,
                    order_type=OrderType(trade.order.orderType),
                    quantity=trade.order.totalQuantity,
                    limit_price=getattr(trade.order, "lmtPrice", None),
                    stop_price=getattr(trade.order, "auxPrice", None),
                    order_id=order_id,
                    status=OrderStatus.SUBMITTED,
                )

        raise ValueError(f"Order {order_id} not found")

    async def get_open_orders(self) -> list[Order]:
        """Get all open orders."""
        await self._rate_limiter.acquire()
        orders = []

        for trade in self._ib.openOrders():
            orders.append(Order(
                contract=self._from_ib_contract(trade.contract),
                side=OrderSide.BUY if trade.order.action == "BUY" else OrderSide.SELL,
                order_type=OrderType(trade.order.orderType),
                quantity=trade.order.totalQuantity,
                limit_price=getattr(trade.order, "lmtPrice", None),
                stop_price=getattr(trade.order, "auxPrice", None),
                order_id=trade.order.orderId,
                status=self._map_order_status(trade.orderStatus.status),
            ))
        return orders

    def _map_order_status(self, ib_status: str) -> OrderStatus:
        """Map IBKR order status to our enum."""
        mapping = {
            "PendingSubmit": OrderStatus.PENDING,
            "PendingCancel": OrderStatus.SUBMITTED,
            "PreSubmitted": OrderStatus.SUBMITTED,
            "Submitted": OrderStatus.SUBMITTED,
            "Filled": OrderStatus.FILLED,
            "Inactive": OrderStatus.REJECTED,
            "Cancelled": OrderStatus.CANCELLED,
            "ApiCancelled": OrderStatus.CANCELLED,
        }
        return mapping.get(ib_status, OrderStatus.PENDING)

    # ── Contract Utilities ───────────────────────────────────────────────

    async def get_contract_details(self, contract: Contract) -> ContractDetails:
        """Get detailed contract information."""
        await self._rate_limiter.acquire()
        ib_contract = self._to_ib_contract(contract)

        details = self._ib.reqContractDetails(ib_contract)
        if not details:
            raise ValueError(f"No details found for {contract.symbol}")

        d = details[0]
        return ContractDetails(
            contract=contract,
            long_name=d.longName or "",
            industry=d.industry or "",
            category=d.category or "",
            subcategory=d.subcategory or "",
            market_name=d.marketName or "",
            trading_hours=d.tradingHours or "",
            time_zone=d.timeZoneId or "",
            min_tick=d.minTick,
            price_magnifier=d.priceMagnifier or 1,
        )

    async def qualify_contract(self, contract: Contract) -> Contract:
        """Resolve a partial contract to a fully qualified one."""
        await self._rate_limiter.acquire()
        ib_contract = self._to_ib_contract(contract)

        qualified = self._ib.qualifyContracts(ib_contract)
        if not qualified:
            raise ValueError(f"Could not qualify contract: {contract.symbol}")

        q = qualified[0]
        contract.contract_id = q.conId
        contract.exchange = q.exchange or contract.exchange
        contract.qualified = True

        return contract
