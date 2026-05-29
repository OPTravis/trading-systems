"""Lightweight RiskManager stub for stock-ai-trader.
The actual risk logic lives in src/risk/stock_risk_manager.py.
This stub satisfies the import from shared.risk.risk_manager."""
from typing import Tuple

class RiskManager:
    def check_order_allowed(self, symbol: str, side: str, quantity: float) -> Tuple[bool, str]:
        return True, "OK"
