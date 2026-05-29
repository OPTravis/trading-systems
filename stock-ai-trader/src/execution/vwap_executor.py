"""
VWAP (Volume-Weighted Average Price) executor.

Splits orders based on historical intraday volume profile to track
the VWAP benchmark. Typically: ~10% first hour, ~20% mid-day, ~10% last hour.
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

# ─── Typical US equity intraday volume profile (fraction of daily volume) ────
# Based on empirical data for S&P 500 stocks. Each bucket = 30 min.
# Market hours: 9:30–16:00 ET = 13 buckets of 30 min.

DEFAULT_VOLUME_PROFILE = [
    0.055,  # 09:30–10:00  (opening surge)
    0.045,  # 10:00–10:30
    0.038,  # 10:30–11:00
    0.033,  # 11:00–11:30
    0.030,  # 11:30–12:00
    0.028,  # 12:00–12:30  (lunch lull)
    0.027,  # 12:30–13:00
    0.030,  # 13:00–13:30
    0.035,  # 13:30–14:00
    0.040,  # 14:00–14:30
    0.045,  # 14:30–15:00  (afternoon pickup)
    0.060,  # 15:00–15:30
    0.080,  # 15:30–16:00  (closing surge)
]
# Remaining ~45.4% distributed — normalize to sum to 1.0
_TOTAL = sum(DEFAULT_VOLUME_PROFILE)
VOLUME_PROFILE = [v / _TOTAL for v in DEFAULT_VOLUME_PROFILE]


@dataclass
class VWAPResult:
    """Aggregated result of a VWAP execution."""
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


class VWAPExecutor:
    """
    Volume-Weighted Average Price execution algorithm.

    Distributes order quantity proportionally to historical intraday
    volume patterns. Slices align with 30-minute buckets to match the
    market's natural volume rhythm.

    Best for:
    - Large orders where minimizing VWAP slippage is the goal
    - Liquid stocks with predictable intraday volume patterns
    - Situations where passive fills are acceptable
    """

    def __init__(self, broker: BrokerProtocol, risk_manager: Optional[RiskManager] = None):
        self.order_executor = OrderExecutor(broker, risk_manager)

    # NOTE: This method uses blocking time.sleep() and synchronous broker calls.
    # In the future, this should be refactored to an async method (async def)
    # and called from an async context, using asyncio.sleep() and await on
    # broker calls. The parent caller is now async.
    def execute_vwap(
        self,
        symbol: str,
        side: str,
        quantity: float,
        duration_minutes: int = 390,  # Full US trading day (6.5 hours)
        volume_profile: Optional[List[float]] = None,
    ) -> VWAPResult:
        """
        Execute a VWAP order.

        Args:
            symbol: Stock ticker.
            side: "BUY" or "SELL".
            quantity: Total shares to execute.
            duration_minutes: Total execution window (default = full trading day).
            volume_profile: Optional custom volume weights. Must sum to ~1.0.
                If None, uses DEFAULT_VOLUME_PROFILE (30-min US equity buckets).

        Returns:
            VWAPResult with aggregated fill details.
        """
        profile = volume_profile or VOLUME_PROFILE
        num_slices = len(profile)
        slice_interval = (duration_minutes * 60) / num_slices

        logger.info(
            "VWAP start: %s %s %.0f shares over %d min using %d volume-weighted slices",
            side, symbol, quantity, duration_minutes, num_slices,
        )

        start_time = datetime.utcnow()
        results: List[OrderResult] = []
        total_filled = 0.0
        total_commission = 0.0
        weighted_price_sum = 0.0

        for i, weight in enumerate(profile):
            slice_start = time.time()

            # Compute slice quantity from volume weight
            slice_qty = round(quantity * weight)
            if slice_qty < 1:
                continue

            # Last slice gets remainder
            if i == num_slices - 1:
                slice_qty = round(quantity - total_filled)
            if slice_qty <= 0:
                continue

            logger.info(
                "VWAP slice %d/%d (%.1f%% vol): %s %s x%d",
                i + 1, num_slices, weight * 100, side, symbol, slice_qty,
            )

            result = self.order_executor.place_order(
                symbol=symbol,
                side=side,
                quantity=slice_qty,
                order_type="LMT",  # Use limit orders for VWAP to control price
                limit_price=self._get_limit_price(symbol, side),
            )
            results.append(result)

            if result.success:
                total_filled += result.filled_qty
                total_commission += result.commission
                weighted_price_sum += result.avg_fill_price * result.filled_qty
            else:
                logger.warning("VWAP slice %d failed: %s", i + 1, result.error)

            # Wait for next time bucket (except after last slice)
            if i < num_slices - 1:
                elapsed = time.time() - slice_start
                sleep_time = max(0, slice_interval - elapsed)
                if sleep_time > 0:
                    logger.debug("VWAP sleeping %.1fs until next slice", sleep_time)
                    time.sleep(sleep_time)

        end_time = datetime.utcnow()
        avg_price = weighted_price_sum / total_filled if total_filled > 0 else 0.0
        success = total_filled >= quantity * 0.95  # 95% fill threshold

        vwap_result = VWAPResult(
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
            "VWAP done: %s %s — filled %.0f/%.0f (%.1f%%) avg $%.2f in %d slices",
            side, symbol, total_filled, quantity,
            (total_filled / quantity * 100) if quantity else 0,
            avg_price, len(results),
        )

        return vwap_result

    @staticmethod
    def _get_limit_price(symbol: str, side: str) -> Optional[float]:
        """
        Get a slightly aggressive limit price for the VWAP slice.
        For buys: slight premium to mid; for sells: slight discount.
        Returns None to fall back to market order if price unavailable.
        """
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            info = ticker.fast_info
            price = getattr(info, "last_price", None)
            if price is None:
                return None
            # Add 5bps aggression
            if side.upper() == "BUY":
                return round(price * 1.0005, 2)
            else:
                return round(price * 0.9995, 2)
        except Exception:
            return None
