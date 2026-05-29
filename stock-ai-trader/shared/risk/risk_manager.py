"""Lightweight RiskManager base class for stock-ai-trader.

The actual risk logic lives in src/risk/stock_risk_manager.py.
This base class enforces that callers must provide a real RiskManager
implementation — it will NOT silently approve orders.
"""
from typing import Tuple


class RiskManager:
    """Base risk manager — must be subclassed with real logic.

    Calling check_order_allowed() on this base class raises NotImplementedError
    to prevent silent approval of all orders.
    """

    def check_order_allowed(self, symbol: str, side: str, quantity: float) -> Tuple[bool, str]:
        """Check if an order is allowed. MUST be overridden by subclass.

        Raises:
            NotImplementedError: If subclass doesn't override this method.
        """
        raise NotImplementedError(
            "RiskManager.check_order_allowed() must be implemented by a subclass. "
            "Use src/risk/stock_risk_manager.StockRiskManager for real risk checks."
        )
