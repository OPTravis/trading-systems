"""
Alpaca Client - Backup broker implementation (stub).

Implements BrokerProtocol for Alpaca Markets API.
This is a stub — implement the actual API calls when needed as a fallback.

Alpaca API docs: https://alpaca.markets/docs/api-references/
Uses: alpaca-py library (pip install alpaca-py)
"""

from __future__ import annotations

import logging
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
    Position,
    Tick,
)

logger = logging.getLogger(__name__)


class AlpacaClient(BrokerProtocol):
    """
    Alpaca Markets broker client (stub implementation).

    TODO: Implement using alpaca-py or direct REST API calls.
    Alpaca provides commission-free US stock and crypto trading.

    Key differences from IBKR:
    - Simpler API (REST + WebSocket)
    - No options/futures support
    - Fractional shares supported
    - Paper trading at paper-api.alpaca.markets
    """

    def __init__(
        self,
        api_key: str = "",
        secret_key: str = "",
        paper: bool = True,
    ):
        self._api_key = api_key
        self._secret_key = secret_key
        self._paper = paper
        self._connected = False
        self._base_url = (
            "https://paper-api.alpaca.markets"
            if paper
            else "https://api.alpaca.markets"
        )

    # ── Connection ───────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to Alpaca API."""
        # TODO: Validate credentials with a test request
        logger.info(f"Alpaca client connected ({'paper' if self._paper else 'live'})")
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        logger.info("Alpaca client disconnected")

    async def is_connected(self) -> bool:
        return self._connected

    # ── Market Data ──────────────────────────────────────────────────────

    async def get_market_data(self, contract: Contract, snapshot: bool = False) -> Tick:
        raise NotImplementedError("AlpacaClient.get_market_data not yet implemented")

    async def get_historical_bars(
        self,
        contract: Contract,
        duration: str = "1 D",
        bar_size: BarSize = BarSize.ONE_MIN,
        end_date: Optional[datetime] = None,
        what_to_show: str = "TRADES",
        use_rth: bool = True,
    ) -> list[Bar]:
        raise NotImplementedError(
            "AlpacaClient.get_historical_bars not yet implemented"
        )

    async def stream_market_data(self, contract: Contract) -> AsyncIterator[Tick]:
        # Make this a generator, then raise
        yield  # type: ignore[misc]
        raise NotImplementedError("AlpacaClient.stream_market_data not yet implemented")

    # ── Account & Positions ──────────────────────────────────────────────

    async def get_account(self) -> AccountSummary:
        raise NotImplementedError("AlpacaClient.get_account not yet implemented")

    async def get_positions(self) -> list[Position]:
        raise NotImplementedError("AlpacaClient.get_positions not yet implemented")

    async def get_portfolio(self) -> list[Position]:
        raise NotImplementedError("AlpacaClient.get_portfolio not yet implemented")

    # ── Order Management ─────────────────────────────────────────────────

    async def place_order(self, order: Order) -> Order:
        raise NotImplementedError("AlpacaClient.place_order not yet implemented")

    async def cancel_order(self, order_id: int) -> None:
        raise NotImplementedError("AlpacaClient.cancel_order not yet implemented")

    async def modify_order(
        self,
        order_id: int,
        quantity: Optional[float] = None,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
    ) -> Order:
        raise NotImplementedError("AlpacaClient.modify_order not yet implemented")

    async def get_open_orders(self) -> list[Order]:
        raise NotImplementedError("AlpacaClient.get_open_orders not yet implemented")

    # ── Contract Utilities ───────────────────────────────────────────────

    async def get_contract_details(self, contract: Contract) -> ContractDetails:
        return ContractDetails(
            contract=contract,
            long_name=contract.symbol,
            market_name="ALPACA",
        )

    async def qualify_contract(self, contract: Contract) -> Contract:
        contract.qualified = True
        return contract
