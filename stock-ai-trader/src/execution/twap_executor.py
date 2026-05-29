"""
TWAP (Time-Weighted Average Price) executor.

Splits large orders into equal time slices to minimize market impact.
Used when order size exceeds ~5% of average daily volume.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from shared.core.state_db import get_state_db
from shared.risk.risk_manager import RiskManager
from src.brokers.broker_protocol import BrokerProtocol
from src.execution.order_executor import OrderExecutor, OrderResult

logger = logging.getLogger(__name__)


@dataclass
class TWAPResult:
    """Aggregated result of a TWAP execution."""
    success: bool
    symbol: str
    side: str
    total_requested: float
    total_filled: float = 0.0
    avg_fill_price: float = 0.0
    num_slices: int = 0
    slice_results: List[OrderResult] = field(default_factory=list)
    total_commission: float = 0.0
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    error: str = ""


class TWAPExecutor:
    """
    Time-Weighted Average Price execution algorithm.

    Splits a large order into equal-sized slices executed at regular
    time intervals. Each slice is a market order for (total_qty / num_slices)
    shares, spaced (duration / num_slices) seconds apart.

    Best for:
    - Orders > 5% of ADV (average daily volume)
    - Less liquid names where market impact is a concern
    - Situations where urgency is moderate
    """

    def __init__(self, broker: BrokerProtocol, risk_manager: Optional[RiskManager] = None):
        self.order_executor = OrderExecutor(broker, risk_manager)

    def execute_twap(
        self,
        symbol: str,
        side: str,
        quantity: float,
        duration_minutes: int = 30,
        num_slices: int = 6,
    ) -> TWAPResult:
        """
        Execute a TWAP order.

        Args:
            symbol: Stock ticker.
            side: "BUY" or "SELL".
            quantity: Total shares to execute.
            duration_minutes: Total execution window in minutes.
            num_slices: Number of equal slices to split into.

        Returns:
            TWAPResult with aggregated fill details.
        """
        if num_slices < 1:
            num_slices = 1
        if duration_minutes < 1:
            duration_minutes = 1

        slice_qty = quantity / num_slices
        interval_seconds = (duration_minutes * 60) / num_slices

        logger.info(
            "TWAP start: %s %s %.0f shares over %d min in %d slices (%.0f shares every %.0fs)",
            side, symbol, quantity, duration_minutes, num_slices, slice_qty, interval_seconds,
        )

        start_time = datetime.utcnow()
        results: List[OrderResult] = []
        total_filled = 0.0
        total_commission = 0.0
        weighted_price_sum = 0.0

        for i in range(num_slices):
            slice_start = time.time()

            # Round quantity to whole shares
            this_qty = round(slice_qty)
            if i == num_slices - 1:
                # Last slice gets any rounding remainder
                this_qty = round(quantity - total_filled)
            if this_qty <= 0:
                continue

            logger.info("TWAP slice %d/%d: %s %s x%d", i + 1, num_slices, side, symbol, this_qty)

            result = self.order_executor.place_order(
                symbol=symbol,
                side=side,
                quantity=this_qty,
                order_type="MKT",
                time_in_force="IOC",  # Immediate or cancel for each slice
            )
            results.append(result)

            if result.success:
                total_filled += result.filled_qty
                total_commission += result.commission
                weighted_price_sum += result.avg_fill_price * result.filled_qty
            else:
                logger.warning("TWAP slice %d failed: %s", i + 1, result.error)

            # Wait for next slice interval (except after last slice)
            if i < num_slices - 1:
                elapsed = time.time() - slice_start
                sleep_time = max(0, interval_seconds - elapsed)
                if sleep_time > 0:
                    logger.debug("TWAP sleeping %.1fs until next slice", sleep_time)
                    time.sleep(sleep_time)

        end_time = datetime.utcnow()
        avg_price = weighted_price_sum / total_filled if total_filled > 0 else 0.0
        success = total_filled >= quantity * 0.95  # 95% fill threshold

        twap_result = TWAPResult(
            success=success,
            symbol=symbol,
            side=side,
            total_requested=quantity,
            total_filled=total_filled,
            avg_fill_price=round(avg_price, 4),
            num_slices=num_slices,
            slice_results=results,
            total_commission=total_commission,
            start_time=start_time,
            end_time=end_time,
        )

        logger.info(
            "TWAP done: %s %s — filled %.0f/%.0f (%.1f%%) avg $%.2f in %d slices",
            side, symbol, total_filled, quantity,
            (total_filled / quantity * 100) if quantity else 0,
            avg_price, len(results),
        )

        return twap_result
