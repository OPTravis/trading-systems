"""
VWAP Distribution Strategy
Buy below VWAP, sell above VWAP
"""

import logging
from typing import Dict, List, Optional

from .base import BaseStrategy, SignalType, StrategySignal

logger = logging.getLogger(__name__)


class VWAPStrategy(BaseStrategy):
    """VWAP-based trading strategy"""

    def __init__(self, params: Dict):
        super().__init__("VWAP Distribution", params)
        self.vwap_threshold_pct = params.get("vwap_threshold_pct", -1.0)
        self.order_size_pct = params.get("order_size_pct", 10)
        # Validate
        if self.vwap_threshold_pct >= 0:
            logger.warning(
                f"VWAP threshold must be negative, got {self.vwap_threshold_pct}, using default -1.0"
            )
            self.vwap_threshold_pct = -1.0
        if self.order_size_pct <= 0:
            logger.warning("order_size_pct must be positive, using default 10")
            self.order_size_pct = 10
        self.stop_loss_pct = params.get("stop_loss_pct", 2.0)
        self.take_profit_pct = params.get("take_profit_pct", 3.0)

    def analyze(
        self,
        symbol: str,
        klines: List[Dict],
        position: Optional[Dict] = None,
        idx: Optional[int] = None,
    ) -> StrategySignal:
        """Generate VWAP signals"""
        if idx is not None:
            klines = klines[:idx]
        if not klines or len(klines) < 50:
            return StrategySignal(
                signal=SignalType.WAIT,
                confidence=0,
                reason="Insufficient data",
                metadata={},
            )

        # Calculate VWAP
        vwap = self._calculate_vwap(klines)
        current_price = self._current_price(klines)
        current_qty = position.get("total", 0) if position else 0

        # Deviation from VWAP
        deviation_pct = ((current_price - vwap) / vwap) * 100 if vwap > 0 else 0

        # Entry price for PnL
        entry_price = (
            position.get("entry_price", current_price) if position else current_price
        )
        pnl_pct = (
            ((current_price - entry_price) / entry_price) * 100
            if current_qty > 0
            else 0
        )

        if current_qty == 0:
            # No position
            if deviation_pct <= self.vwap_threshold_pct:
                return StrategySignal(
                    signal=SignalType.BUY,
                    confidence=80,
                    reason=f"Price {deviation_pct:.2f}% below VWAP - buy",
                    metadata={
                        "vwap": vwap,
                        "deviation_pct": deviation_pct,
                        "order_size_pct": self.order_size_pct,
                        "stop_loss": vwap * (1 - self.stop_loss_pct / 100),
                    },
                )
            elif deviation_pct < 0:
                return StrategySignal(
                    signal=SignalType.BUY,
                    confidence=50,
                    reason=f"Below VWAP ({deviation_pct:.2f}%) - minor entry",
                    metadata={"vwap": vwap, "deviation_pct": deviation_pct},
                )
            else:
                return StrategySignal(
                    signal=SignalType.WAIT,
                    confidence=40,
                    reason=f"Above VWAP ({deviation_pct:.2f}%) - wait",
                    metadata={"vwap": vwap, "deviation_pct": deviation_pct},
                )
        else:
            # Have position
            if deviation_pct >= -self.vwap_threshold_pct:
                return StrategySignal(
                    signal=SignalType.SELL,
                    confidence=75,
                    reason=f"Above VWAP ({deviation_pct:.2f}%) - take profit",
                    metadata={"pnl_pct": pnl_pct, "sell_pct": 100},
                )
            elif pnl_pct >= self.take_profit_pct:
                return StrategySignal(
                    signal=SignalType.SELL,
                    confidence=80,
                    reason=f"Profit target ({pnl_pct:.1f}%)",
                    metadata={"pnl_pct": pnl_pct, "sell_pct": 100},
                )
            elif pnl_pct <= -self.stop_loss_pct:
                return StrategySignal(
                    signal=SignalType.SELL,
                    confidence=90,
                    reason=f"Stop loss ({pnl_pct:.1f}%)",
                    metadata={"pnl_pct": pnl_pct},
                )
            else:
                return StrategySignal(
                    signal=SignalType.HOLD,
                    confidence=60,
                    reason=f"VWAP: {deviation_pct:.2f}%, PnL: {pnl_pct:.1f}%",
                    metadata={"vwap": vwap, "pnl_pct": pnl_pct},
                )

    def _calculate_vwap(self, klines: List[Dict]) -> float:
        """Calculate VWAP from klines"""
        if not klines:
            return 0

        # Use 24h of data for VWAP
        data = klines[-24:] if len(klines) >= 24 else klines

        total_pv = 0
        total_vol = 0

        for k in data:
            typical_price = (k["high"] + k["low"] + k["close"]) / 3
            pv = typical_price * k["volume"]
            total_pv += pv
            total_vol += k["volume"]

        return total_pv / total_vol if total_vol > 0 else 0

    def get_parameters(self) -> Dict:
        return {
            "vwap_threshold_pct": self.vwap_threshold_pct,
            "order_size_pct": self.order_size_pct,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
        }
