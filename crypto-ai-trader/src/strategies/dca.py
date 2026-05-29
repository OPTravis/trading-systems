"""
Dollar Cost Averaging (DCA) Strategy
Periodic buys to accumulate on dips
"""

import logging
from typing import Dict, List, Optional
from .base import BaseStrategy, StrategySignal, SignalType

logger = logging.getLogger(__name__)


class DCAStrategy(BaseStrategy):
    """DCA - Buy periodically and on dips"""
    
    def __init__(self, params: Dict):
        super().__init__("DCA", params)
        self.interval_hours = params.get("interval_hours", 24)
        self.order_size_pct = params.get("order_size_pct", 5)
        self.dip_threshold_pct = params.get("dip_threshold_pct", -5.0)
        self.max_dca_rounds = params.get("max_dca_rounds", 5)
        # Validate
        if self.interval_hours <= 0:
            logger.warning(f"DCA interval_hours must be positive, got {self.interval_hours}, using default 24")
            self.interval_hours = 24
        if self.max_dca_rounds < 1:
            logger.warning(f"DCA max_dca_rounds must be >= 1, got {self.max_dca_rounds}, using default 5")
            self.max_dca_rounds = 5
    
    def analyze(
        self,
        symbol: str,
        klines: List[Dict],
        position: Optional[Dict] = None,
        idx: int = None
    ) -> StrategySignal:
        """Generate DCA signals"""
        if idx is not None:
            klines = klines[:idx]
        if not klines or len(klines) < 50:
            return StrategySignal(
                signal=SignalType.WAIT,
                confidence=0,
                reason="Insufficient data",
                metadata={}
            )
        
        current_price = self._current_price(klines)
        prices = self._recent_prices(klines, 100)
        
        # Calculate DCA price (average of recent prices)
        dca_price = sum(prices) / len(prices)
        
        # Price deviation from DCA
        deviation_pct = ((current_price - dca_price) / dca_price) * 100
        
        # Current position
        current_qty = position.get("total", 0) if position else 0
        current_dca_round = getattr(self, "_dca_round", 0)
        
        # Check if in downtrend
        recent_change = ((prices[-1] - prices[-20]) / prices[-20]) * 100 if len(prices) >= 20 else 0
        
        if current_qty == 0:
            # No position - reset DCA round and look for entry
            self._dca_round = 0
            if deviation_pct <= self.dip_threshold_pct:
                return StrategySignal(
                    signal=SignalType.BUY,
                    confidence=80,
                    reason=f"Price {deviation_pct:.1f}% below DCA - accumulate",
                    metadata={
                        "deviation_pct": deviation_pct,
                        "dca_price": dca_price,
                        "order_size_pct": self.order_size_pct
                    }
                )
            elif recent_change < -3:
                return StrategySignal(
                    signal=SignalType.BUY,
                    confidence=60,
                    reason=f"Downtrend detected - start DCA",
                    metadata={
                        "recent_change_pct": recent_change,
                        "order_size_pct": self.order_size_pct
                    }
                )
            else:
                return StrategySignal(
                    signal=SignalType.WAIT,
                    confidence=40,
                    reason="No significant dip yet",
                    metadata={
                        "deviation_pct": deviation_pct
                    }
                )
        else:
            # Have position - check for additional DCA
            if deviation_pct <= self.dip_threshold_pct * 0.5 and current_dca_round < self.max_dca_rounds:
                # FIX: Persist dca_round to strategy instance so it survives across calls
                self._dca_round = current_dca_round + 1
                return StrategySignal(
                    signal=SignalType.BUY,
                    confidence=75,
                    reason=f"Additional DCA - price {deviation_pct:.1f}% below DCA",
                    metadata={
                        "dca_round": self._dca_round,
                        "deviation_pct": deviation_pct,
                        "order_size_pct": self.order_size_pct
                    }
                )
            elif deviation_pct > -self.dip_threshold_pct and current_dca_round > 0:
                # Profit - consider taking some
                return StrategySignal(
                    signal=SignalType.SELL,
                    confidence=60,
                    reason=f"Profitable DCA - take partial profits",
                    metadata={
                        "sell_pct": 25,
                        "deviation_pct": deviation_pct
                    }
                )
            else:
                return StrategySignal(
                    signal=SignalType.HOLD,
                    confidence=50,
                    reason="Hold DCA position",
                    metadata={
                        "dca_round": current_dca_round,
                        "deviation_pct": deviation_pct
                    }
                )
    
    def get_parameters(self) -> Dict:
        return {
            "interval_hours": self.interval_hours,
            "order_size_pct": self.order_size_pct,
            "dip_threshold_pct": self.dip_threshold_pct,
            "max_dca_rounds": self.max_dca_rounds
        }
