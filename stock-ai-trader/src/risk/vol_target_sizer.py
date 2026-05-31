"""
Volatility Target Sizer - Position sizing based on volatility targeting.
Targets 15% annualized portfolio volatility.
"""

import logging
import math
from typing import Dict, Optional

logger = logging.getLogger(__name__)

TARGET_VOL = 0.15  # 15% annualized volatility target
TRADING_DAYS_PER_YEAR = 252
DEFAULT_VOL = 0.25  # Default assumed volatility if unknown


class VolTargetSizer:
    """Volatility-target position sizing."""

    def __init__(self, target_vol: float = TARGET_VOL):
        self.target_vol = target_vol

    def calculate(
        self,
        symbol: str,
        portfolio: Optional[Dict] = None,
        current_vol: Optional[float] = None,
    ) -> float:
        """Calculate position size as fraction of portfolio.

        Args:
            symbol: Stock symbol
            portfolio: Portfolio context {total_value, current_positions, ...}
            current_vol: Current realized volatility (annualized)

        Returns:
            Position size as fraction of portfolio (0.0 to max_allocation)
        """
        portfolio = portfolio or {}
        total_value = portfolio.get("total_value", 100000.0)
        n_positions = max(1, portfolio.get("n_positions", 10))

        vol = current_vol or DEFAULT_VOL
        if vol <= 0:
            vol = DEFAULT_VOL

        # Position size = (target_vol / stock_vol) * (1 / sqrt(n))
        # The sqrt(n) factor accounts for diversification
        raw_size = (self.target_vol / vol) / math.sqrt(n_positions)

        # Cap at reasonable limits
        max_per_position = 0.20  # Max 20% in single position
        position_frac = min(max_per_position, max(0.01, raw_size))

        dollar_amount = total_value * position_frac
        logger.info(
            f"VolTargetSizer: {symbol} vol={vol:.2%}, size={position_frac:.2%} "
            f"(${dollar_amount:,.0f})"
        )
        return position_frac

    def calculate_dollar(
        self,
        symbol: str,
        portfolio: Optional[Dict] = None,
        current_vol: Optional[float] = None,
    ) -> float:
        """Calculate position size in dollars."""
        portfolio = portfolio or {}
        total_value = portfolio.get("total_value", 100000.0)
        frac = self.calculate(symbol, portfolio, current_vol)
        return total_value * frac

    def get_portfolio_vol_estimate(
        self, positions: Dict[str, float], vols: Dict[str, float]
    ) -> float:
        """Estimate portfolio volatility (simplified, assumes zero correlation)."""
        if not positions or not vols:
            return 0.0

        total_value = sum(positions.values())
        if total_value == 0:
            return 0.0

        var_sum = 0.0
        for symbol, value in positions.items():
            weight = value / total_value
            vol = vols.get(symbol, DEFAULT_VOL)
            var_sum += (weight * vol) ** 2

        portfolio_vol = math.sqrt(var_sum)
        if portfolio_vol < 0.05:
            logger.warning(
                f"Portfolio vol estimate suspiciously low ({portfolio_vol:.2%}). "
                "Note: calculation assumes zero correlation between positions."
            )
        return portfolio_vol
