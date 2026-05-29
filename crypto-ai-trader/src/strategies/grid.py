"""
Grid Trading Strategy
Buy low, sell high within a price range
"""

from typing import Dict, List, Optional
from .base import BaseStrategy, StrategySignal, SignalType


class GridStrategy(BaseStrategy):
    """Grid trading - place orders at regular price intervals"""
    
    def __init__(self, params: Dict):
        super().__init__("Grid Trading", params)
        self.grid_levels = params.get("grid_levels", 10)
        self.price_range_pct = params.get("price_range_pct", 5.0)
        self.stop_loss_pct = params.get("stop_loss_pct", 3.0)
    
    def analyze(
        self,
        symbol: str,
        klines: List[Dict],
        position: Optional[Dict] = None,
        idx: int = None
    ) -> StrategySignal:
        """Generate grid trading signals"""
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
        prices = self._recent_prices(klines, 50)
        
        # Calculate grid boundaries
        high_price = max(prices)
        low_price = min(prices)
        
        # Current price position within range
        range_size = high_price - low_price
        if range_size == 0:
            return StrategySignal(
                signal=SignalType.HOLD,
                confidence=50,
                reason="No price range established",
                metadata={}
            )
        
        price_position = (current_price - low_price) / range_size  # 0 to 1
        
        # Grid level
        grid_level = int(price_position * self.grid_levels)
        
        # Determine action
        if position and position.get("total", 0) > 0:
            # Have position - check if near top of range
            if price_position > 0.9:  # Near top
                return StrategySignal(
                    signal=SignalType.SELL,
                    confidence=80,
                    reason=f"Price at top of range (level {grid_level}/{self.grid_levels})",
                    metadata={
                        "grid_level": grid_level,
                        "price_position": price_position,
                        "suggested_sell_pct": 50
                    }
                )
            elif price_position < 0.1:  # Near bottom
                return StrategySignal(
                    signal=SignalType.BUY,
                    confidence=70,
                    reason=f"Price at bottom of range (level {grid_level}/{self.grid_levels})",
                    metadata={
                        "grid_level": grid_level,
                        "price_position": price_position,
                        "suggested_buy_pct": 25
                    }
                )
            else:
                return StrategySignal(
                    signal=SignalType.HOLD,
                    confidence=60,
                    reason=f"Price mid-range (level {grid_level}/{self.grid_levels})",
                    metadata={
                        "grid_level": grid_level,
                        "price_position": price_position
                    }
                )
        else:
            # No position
            if price_position < 0.2:  # Near bottom - potential buy
                return StrategySignal(
                    signal=SignalType.BUY,
                    confidence=70,
                    reason=f"Price at bottom of range - start grid buy",
                    metadata={
                        "grid_level": grid_level,
                        "price_position": price_position,
                        "entry_price": current_price
                    }
                )
            else:
                return StrategySignal(
                    signal=SignalType.WAIT,
                    confidence=50,
                    reason=f"Waiting for better entry at lower price",
                    metadata={
                        "grid_level": grid_level,
                        "price_position": price_position
                    }
                )
    
    def get_parameters(self) -> Dict:
        return {
            "grid_levels": self.grid_levels,
            "price_range_pct": self.price_range_pct,
            "stop_loss_pct": self.stop_loss_pct
        }
