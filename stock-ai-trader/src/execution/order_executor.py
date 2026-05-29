"""
Base order executor — market, limit, and stop order placement with retry logic.

Wraps BrokerProtocol to provide a unified place_order interface used by
TWAP and VWAP algorithmic executors as well as direct trade execution.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from shared.core.state_db import get_state_db
from shared.risk.risk_manager import RiskManager
from src.brokers.broker_protocol import (
    BrokerProtocol,
    Contract,
    Order,
    OrderSide,
    OrderType,
    OrderStatus,
    TimeInForce,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY_BASE = 2  # seconds, exponential backoff


@dataclass
class OrderResult:
    """Result of a single order placement attempt."""
    success: bool
    order_id: Optional[int] = None
    symbol: str = ""
    side: str = ""
    order_type: str = ""
    requested_qty: float = 0.0
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    commission: float = 0.0
    status: str = ""
    error: str = ""
    retry_count: int = 0
    timestamp: datetime = field(default_factory=datetime.utcnow)


class OrderExecutor:
    """
    Base order executor that places orders via BrokerProtocol.

    Supports market, limit, and stop orders with automatic retry logic
    for transient network/API errors.
    """

    def __init__(self, broker: BrokerProtocol, risk_manager: Optional[RiskManager] = None):
        self.broker = broker
        self.risk_manager = risk_manager

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "MKT",
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        time_in_force: str = "DAY",
    ) -> OrderResult:
        """
        Place an order with retry logic.

        Args:
            symbol: Stock ticker symbol.
            side: "BUY" or "SELL".
            quantity: Number of shares.
            order_type: "MKT", "LMT", "STP", or "STPLMT".
            limit_price: Limit price (required for LMT/STPLMT).
            stop_price: Stop price (required for STP/STPLMT).
            time_in_force: "DAY", "GTC", "IOC", "FOK".

        Returns:
            OrderResult with fill details or error.
        """
        # Pre-trade risk check
        if self.risk_manager:
            allowed, reason = self.risk_manager.check_order_allowed(symbol, side, quantity)
            if not allowed:
                logger.warning("Risk manager blocked order: %s — %s", symbol, reason)
                return OrderResult(
                    success=False,
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    requested_qty=quantity,
                    error=f"Risk blocked: {reason}",
                )

        contract = Contract(symbol=symbol)
        order_side = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL

        type_map = {
            "MKT": OrderType.MARKET,
            "LMT": OrderType.LIMIT,
            "STP": OrderType.STOP,
            "STPLMT": OrderType.STOP_LIMIT,
        }
        otype = type_map.get(order_type.upper(), OrderType.MARKET)

        tif_map = {
            "DAY": TimeInForce.DAY,
            "GTC": TimeInForce.GTC,
            "IOC": TimeInForce.IOC,
            "FOK": TimeInForce.FOK,
        }
        tif = tif_map.get(time_in_force.upper(), TimeInForce.DAY)

        order = Order(
            contract=contract,
            side=order_side,
            order_type=otype,
            quantity=quantity,
            limit_price=limit_price,
            stop_price=stop_price,
            time_in_force=tif,
        )

        # Retry loop
        last_error = ""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(
                    "Placing %s %s %s x%.0f (attempt %d/%d)",
                    side, symbol, order_type, quantity, attempt, MAX_RETRIES,
                )
                result = self.broker.place_order(order)

                if result and result.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED):
                    logger.info("Order placed: %s %s — status=%s", side, symbol, result.status)
                    return OrderResult(
                        success=True,
                        order_id=result.order_id,
                        symbol=symbol,
                        side=side,
                        order_type=order_type,
                        requested_qty=quantity,
                        filled_qty=result.filled_qty,
                        avg_fill_price=result.avg_fill_price,
                        commission=result.commission,
                        status=result.status.value if isinstance(result.status, OrderStatus) else str(result.status),
                        retry_count=attempt,
                    )
                elif result and result.status == OrderStatus.REJECTED:
                    last_error = getattr(result, "error", "Order rejected")
                    logger.error("Order rejected: %s", last_error)
                    break  # Don't retry rejections
                else:
                    last_error = f"Unexpected status: {getattr(result, 'status', 'unknown')}"
                    logger.warning(last_error)

            except ConnectionError as e:
                last_error = f"Connection error: {e}"
                logger.warning(last_error)
            except TimeoutError as e:
                last_error = f"Timeout: {e}"
                logger.warning(last_error)
            except Exception as e:
                last_error = f"Unexpected error: {e}"
                logger.error(last_error, exc_info=True)
                break  # Don't retry unknown errors

            # Exponential backoff
            if attempt < MAX_RETRIES:
                delay = RETRY_DELAY_BASE ** attempt
                logger.info("Retrying in %ds...", delay)
                time.sleep(delay)

        return OrderResult(
            success=False,
            symbol=symbol,
            side=side,
            order_type=order_type,
            requested_qty=quantity,
            error=last_error,
            retry_count=MAX_RETRIES,
        )

    def cancel_order(self, order_id: int) -> bool:
        """Cancel an open order by ID."""
        try:
            self.broker.cancel_order(order_id)
            logger.info("Cancelled order %d", order_id)
            return True
        except Exception as e:
            logger.error("Cancel failed for order %d: %s", order_id, e)
            return False

    def get_order_status(self, order_id: int) -> Optional[str]:
        """Query the current status of an order."""
        try:
            order = self.broker.get_order(order_id)
            return order.status.value if order else None
        except Exception as e:
            logger.error("Status query failed for order %d: %s", order_id, e)
            return None
