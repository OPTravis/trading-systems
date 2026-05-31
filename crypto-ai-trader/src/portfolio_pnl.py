"""
Portfolio PnL calculations — mixin for PortfolioManager.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class PnlMixin:
    """PnL calculation methods for PortfolioManager."""

    positions: Dict[str, Any]

    def calculate_pnl(
        self, symbol: str, current_price_override: Optional[float] = None
    ) -> Dict:
        """Calculate PnL for a position."""
        pos = self.positions.get(symbol)
        if not pos:
            return {}

        entry = pos["entry_price"]
        current = (
            current_price_override
            if current_price_override is not None
            else pos.get("current_price", entry)
        )
        qty = pos["quantity"]

        pnl_value = (current - entry) * qty
        pnl_pct = ((current - entry) / entry) * 100 if entry > 0 else 0
        position_value = current * qty

        return {
            "symbol": symbol,
            "entry_price": entry,
            "current_price": current,
            "quantity": qty,
            "pnl_value": pnl_value,
            "pnl_pct": pnl_pct,
            "position_value": position_value,
        }

    def get_total_exposure(self) -> float:
        """Get total portfolio exposure"""
        total = 0
        for pos in self.positions.values():
            total += pos["current_price"] * pos["quantity"]
        return total

    def get_total_pnl(self) -> float:
        """Get total portfolio PnL"""
        total = 0
        for pos in self.positions.values():
            entry = pos["entry_price"]
            current = pos["current_price"]
            qty = pos["quantity"]
            total += (current - entry) * qty
        return total
