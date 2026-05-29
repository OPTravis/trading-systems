"""
VIX Position Scale - VIX-based position size scaling.
"""
import logging
import math
from typing import Tuple

logger = logging.getLogger(__name__)


class VIXPositionScale:
    """Scales position sizes based on VIX level."""

    # VIX thresholds and multipliers
    THRESHOLDS = [
        (40.0, 0.0, "FROZEN"),    # VIX >= 40: freeze all new positions
        (30.0, 0.5, "HIGH"),      # VIX >= 30: 50% position size
        (25.0, 0.7, "ELEVATED"),  # VIX >= 25: 70% position size
        (20.0, 0.85, "NORMAL"),   # VIX >= 20: 85% position size
        (0.0, 1.0, "LOW"),        # VIX < 20: full position size
    ]

    def get_multiplier(self, vix: float) -> float:
        """Get position size multiplier for current VIX level."""
        if vix is None or (isinstance(vix, float) and math.isnan(vix)) or vix < 0:
            logger.warning(f"VIX value invalid ({vix}): returning 0.0 (frozen/conservative)")
            return 0.0
        for threshold, multiplier, regime in self.THRESHOLDS:
            if vix >= threshold:
                if multiplier == 0.0:
                    logger.warning(f"VIX {vix:.1f} >= 40: TRADING FROZEN")
                else:
                    logger.info(f"VIX {vix:.1f}: {regime} regime, multiplier={multiplier}")
                return multiplier
        return 1.0

    def get_regime(self, vix: float) -> str:
        """Get current volatility regime name."""
        for threshold, _, regime in self.THRESHOLDS:
            if vix >= threshold:
                return regime
        return "LOW"

    def is_trading_allowed(self, vix: float) -> bool:
        """Check if trading is allowed at current VIX level."""
        return self.get_multiplier(vix) > 0.0

    def get_threshold_info(self) -> list:
        """Get all VIX thresholds and their effects."""
        return [
            {'min_vix': t, 'multiplier': m, 'regime': r}
            for t, m, r in self.THRESHOLDS
        ]
