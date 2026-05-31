"""
ExchangeClient Protocol — Abstract interface for exchange API clients.

All trading modules should depend on this Protocol rather than the concrete
BinanceClient. This enables:
  - Easy testing with mock clients
  - Future migration to other exchanges (OKX, Bybit, etc.)
  - Cleaner dependency injection

Usage:
    from src.exchange_client import ExchangeClient

    def my_strategy(client: ExchangeClient):
        price = client.get_ticker_price("BTCUSDT")
        ...
"""

from typing import Any, Dict, List, Optional, Protocol


class ExchangeClient(Protocol):
    """Abstract exchange client interface."""

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------
    def get_account(self) -> Dict[str, Any]: ...
    def get_free_balance(self, asset: str) -> float: ...

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def get_klines(
        self, symbol: str, interval: str, limit: int
    ) -> List[Dict[str, Any]]: ...
    def get_24hr_stats(self, symbol: Optional[str] = None) -> Any: ...
    def get_ticker_price(self, symbol: str) -> float: ...
    def get_exchange_info(self) -> Dict[str, Any]: ...

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def place_market_buy(
        self, symbol: str, quantity: float
    ) -> Optional[Dict[str, Any]]: ...
    def place_market_sell(
        self, symbol: str, quantity: float
    ) -> Optional[Dict[str, Any]]: ...
    def place_limit_buy(
        self, symbol: str, quantity: float, price: float
    ) -> Optional[Dict[str, Any]]: ...
    def place_limit_sell(
        self, symbol: str, quantity: float, price: float
    ) -> Optional[Dict[str, Any]]: ...
    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]: ...
    def place_oco(
        self,
        symbol: str,
        quantity: float,
        tp_price: float,
        sl_price: float,
        sl_limit_price: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]: ...
    def cancel_order(self, symbol: str, order_id: int) -> Optional[Dict[str, Any]]: ...
    def cancel_all_orders(self, symbol: str) -> bool: ...
    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]: ...
    def get_order(self, symbol: str, order_id: int) -> Optional[Dict[str, Any]]: ...

    # ------------------------------------------------------------------
    # Precision / filters
    # ------------------------------------------------------------------
    def get_price_precision(self, symbol: str) -> int: ...
    def get_quantity_precision(self, symbol: str) -> int: ...
    def get_symbol_filters(self, symbol: str) -> Dict[str, Any]: ...
    def format_price(self, symbol: str, price: float) -> str: ...
    def format_quantity(self, symbol: str, quantity: float) -> str: ...
    def validate_symbol(self, symbol: str) -> bool: ...
    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]: ...
    def place_stop_loss_limit(
        self, symbol: str, quantity: float, price: float, stop_price: float
    ) -> Optional[Dict[str, Any]]: ...

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def close(self) -> None: ...
