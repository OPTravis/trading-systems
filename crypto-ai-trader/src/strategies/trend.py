"""
Trend Following Strategy
MA crossover + volume confirmation
"""

import logging
from typing import Dict, List, Optional
from .base import BaseStrategy, StrategySignal, SignalType

logger = logging.getLogger(__name__)


class TrendStrategy(BaseStrategy):
    """Trend following with MA crossover"""
    
    def __init__(self, params: Dict):
        super().__init__("Trend Following", params)
        self.fast_ma = params.get("fast_ma", 9)
        self.slow_ma = params.get("slow_ma", 21)
        # Validate
        if self.fast_ma < 2 or self.slow_ma < 2:
            logger.warning(f"Trend MA periods must be >= 2, got fast={self.fast_ma} slow={self.slow_ma}, using defaults 9/21")
            self.fast_ma, self.slow_ma = 9, 21
        if self.fast_ma >= self.slow_ma:
            logger.warning(f"Trend fast_ma ({self.fast_ma}) must be < slow_ma ({self.slow_ma}), using defaults 9/21")
            self.fast_ma, self.slow_ma = 9, 21
        self.volume_threshold = params.get("volume_threshold", 1.5)
        self.stop_loss_pct = params.get("stop_loss_pct", 2.0)
        self.take_profit_pct = params.get("take_profit_pct", 6.0)
    
    def analyze(
        self,
        symbol: str,
        klines: List[Dict],
        position: Optional[Dict] = None,
        idx: int = None
    ) -> StrategySignal:
        """Generate trend following signals"""
        if idx is not None:
            klines = klines[:idx]
        if not klines or len(klines) < self.slow_ma + 10:
            return StrategySignal(
                signal=SignalType.WAIT,
                confidence=0,
                reason="Insufficient data",
                metadata={}
            )
        
        # Calculate MAs
        prices = self._recent_prices(klines, 200)
        fast_ma_val = self._sma(prices, self.fast_ma)
        slow_ma_val = self._sma(prices, self.slow_ma)
        
        # Previous MAs for crossover detection
        prev_fast_ma = self._sma(prices[:-1], self.fast_ma)
        prev_slow_ma = self._sma(prices[:-1], self.slow_ma)
        
        # Volume analysis
        volumes = [k["volume"] for k in klines[-20:]]
        avg_volume = sum(volumes) / len(volumes)
        current_volume = volumes[-1]
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1
        
        # Crossover detection
        golden_cross = prev_fast_ma <= prev_slow_ma and fast_ma_val > slow_ma_val
        death_cross = prev_fast_ma >= prev_slow_ma and fast_ma_val < slow_ma_val
        
        current_price = self._current_price(klines)
        current_qty = position.get("total", 0) if position else 0
        
        # Position check
        entry_price = position.get("entry_price", current_price) if position else current_price
        pnl_pct = ((current_price - entry_price) / entry_price) * 100 if current_qty > 0 else 0
        
        if current_qty == 0:
            # No position - look for entry
            if golden_cross and volume_ratio >= self.volume_threshold:
                return StrategySignal(
                    signal=SignalType.BUY,
                    confidence=85,
                    reason="Golden cross with volume confirmation",
                    metadata={
                        "fast_ma": fast_ma_val,
                        "slow_ma": slow_ma_val,
                        "volume_ratio": volume_ratio,
                        "stop_loss": current_price * (1 - self.stop_loss_pct / 100)
                    }
                )
            elif fast_ma_val > slow_ma_val and volume_ratio >= 1.2:
                return StrategySignal(
                    signal=SignalType.BUY,
                    confidence=65,
                    reason="Uptrend with moderate volume",
                    metadata={
                        "fast_ma": fast_ma_val,
                        "slow_ma": slow_ma_val,
                        "volume_ratio": volume_ratio
                    }
                )
            else:
                return StrategySignal(
                    signal=SignalType.WAIT,
                    confidence=40,
                    reason="No trend confirmation",
                    metadata={
                        "fast_ma": fast_ma_val,
                        "slow_ma": slow_ma_val
                    }
                )
        else:
            # Have position - check exit
            if death_cross:
                return StrategySignal(
                    signal=SignalType.SELL,
                    confidence=80,
                    reason="Death cross - exit trend",
                    metadata={
                        "pnl_pct": pnl_pct
                    }
                )
            elif pnl_pct >= self.take_profit_pct:
                return StrategySignal(
                    signal=SignalType.SELL,
                    confidence=75,
                    reason=f"Take profit target reached ({pnl_pct:.1f}%)",
                    metadata={
                        "pnl_pct": pnl_pct,
                        "sell_pct": 50  # Partial sell
                    }
                )
            elif pnl_pct <= -self.stop_loss_pct:
                return StrategySignal(
                    signal=SignalType.SELL,
                    confidence=90,
                    reason=f"Stop loss triggered ({pnl_pct:.1f}%)",
                    metadata={
                        "pnl_pct": pnl_pct
                    }
                )
            else:
                return StrategySignal(
                    signal=SignalType.HOLD,
                    confidence=60,
                    reason=f"Trend intact, PnL: {pnl_pct:.1f}%",
                    metadata={
                        "pnl_pct": pnl_pct,
                        "fast_ma": fast_ma_val,
                        "slow_ma": slow_ma_val
                    }
                )
    
    def _sma(self, prices: List[float], period: int) -> float:
        """Simple Moving Average"""
        if len(prices) < period:
            return prices[-1] if prices else 0
        return sum(prices[-period:]) / period
    
    def get_parameters(self) -> Dict:
        return {
            "fast_ma": self.fast_ma,
            "slow_ma": self.slow_ma,
            "volume_threshold": self.volume_threshold,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct
        }
