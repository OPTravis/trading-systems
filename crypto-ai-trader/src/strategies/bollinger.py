"""
Bollinger Band Breakout Strategy
"""

import logging
from typing import Dict, List, Optional
from .base import BaseStrategy, StrategySignal, SignalType

logger = logging.getLogger(__name__)


class BollingerStrategy(BaseStrategy):
    """Bollinger Band breakout strategy"""
    
    def __init__(self, params: Dict):
        super().__init__("Bollinger Breakout", params)
        self.period = params.get("period", 20)
        self.std_dev = params.get("std_dev", 2.0)
        # Validate
        if self.period < 2:
            logger.warning(f"Bollinger period must be >= 2, got {self.period}, using default 20")
            self.period = 20
        if self.std_dev <= 0:
            logger.warning(f"Bollinger std_dev must be > 0, got {self.std_dev}, using default 2.0")
            self.std_dev = 2.0
        self.volume_threshold = params.get("volume_threshold", 1.5)
        self.stop_loss_pct = params.get("stop_loss_pct", 2.5)
        self.take_profit_pct = params.get("take_profit_pct", 5.0)
    
    def analyze(
        self,
        symbol: str,
        klines: List[Dict],
        position: Optional[Dict] = None,
        idx: int = None
    ) -> StrategySignal:
        """Generate Bollinger breakout signals"""
        if idx is not None:
            klines = klines[:idx]
        if not klines or len(klines) < self.period + 10:
            return StrategySignal(
                signal=SignalType.WAIT,
                confidence=0,
                reason="Insufficient data",
                metadata={}
            )
        
        # Calculate Bollinger Bands
        bb = self._bollinger_bands(klines)
        
        current_price = self._current_price(klines)
        current_qty = position.get("total", 0) if position else 0
        
        # Volume check
        volumes = [k["volume"] for k in klines[-20:]]
        avg_volume = sum(volumes) / len(volumes) if volumes else 1
        current_volume = volumes[-1] if volumes else 1
        volume_ratio = current_volume / avg_volume
        
        # Band position
        bandwidth = bb["upper"] - bb["lower"]
        band_position = (current_price - bb["lower"]) / bandwidth if bandwidth > 0 else 0.5
        
        # Entry price for PnL
        entry_price = position.get("entry_price", current_price) if position else current_price
        pnl_pct = ((current_price - entry_price) / entry_price) * 100 if current_qty > 0 else 0
        
        if current_qty == 0:
            # No position - look for breakout
            if current_price > bb["upper"] and volume_ratio >= self.volume_threshold:
                return StrategySignal(
                    signal=SignalType.BUY,
                    confidence=75,
                    reason=f"Breakout above upper band with volume",
                    metadata={
                        "bb_upper": bb["upper"],
                        "bb_middle": bb["middle"],
                        "bb_lower": bb["lower"],
                        "volume_ratio": volume_ratio,
                        "stop_loss": bb["middle"]  # Stop at middle band
                    }
                )
            elif band_position < 0.1:  # Near lower band
                return StrategySignal(
                    signal=SignalType.BUY,
                    confidence=65,
                    reason="Near lower Bollinger Band - oversold",
                    metadata={
                        "bb_lower": bb["lower"],
                        "band_position": band_position
                    }
                )
            else:
                return StrategySignal(
                    signal=SignalType.WAIT,
                    confidence=40,
                    reason="No clear signal",
                    metadata={
                        "band_position": band_position
                    }
                )
        else:
            # Have position
            if current_price < bb["middle"] and pnl_pct > 0:
                return StrategySignal(
                    signal=SignalType.SELL,
                    confidence=70,
                    reason="Price fell back below middle band",
                    metadata={
                        "pnl_pct": pnl_pct,
                        "sell_pct": 50
                    }
                )
            elif pnl_pct >= self.take_profit_pct:
                return StrategySignal(
                    signal=SignalType.SELL,
                    confidence=80,
                    reason=f"Take profit ({pnl_pct:.1f}%)",
                    metadata={
                        "pnl_pct": pnl_pct,
                        "sell_pct": 100
                    }
                )
            elif pnl_pct <= -self.stop_loss_pct:
                return StrategySignal(
                    signal=SignalType.SELL,
                    confidence=90,
                    reason=f"Stop loss ({pnl_pct:.1f}%)",
                    metadata={"pnl_pct": pnl_pct}
                )
            else:
                return StrategySignal(
                    signal=SignalType.HOLD,
                    confidence=60,
                    reason=f"Band position OK, PnL: {pnl_pct:.1f}%",
                    metadata={
                        "pnl_pct": pnl_pct,
                        "band_position": band_position
                    }
                )
    
    def _bollinger_bands(self, klines: List[Dict]) -> Dict:
        """Calculate Bollinger Bands"""
        if len(klines) < self.period:
            return {"upper": 0, "middle": 0, "lower": 0}
        
        prices = [k["close"] for k in klines[-self.period:]]
        
        # Simple average
        middle = sum(prices) / len(prices)
        
        # Sample standard deviation (Bessel's correction)
        if len(prices) < 2:
            return {"upper": middle, "middle": middle, "lower": middle}
        
        variance = sum((p - middle) ** 2 for p in prices) / (len(prices) - 1)
        std = variance ** 0.5
        
        upper = middle + (self.std_dev * std)
        lower = middle - (self.std_dev * std)
        
        return {"upper": upper, "middle": middle, "lower": lower}
    
    def get_parameters(self) -> Dict:
        return {
            "period": self.period,
            "std_dev": self.std_dev,
            "volume_threshold": self.volume_threshold,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct
        }
