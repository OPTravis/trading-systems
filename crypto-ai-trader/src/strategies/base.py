"""
Base Strategy Class
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from dataclasses import dataclass
from enum import Enum


class SignalType(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    WAIT = "WAIT"


@dataclass
class StrategySignal:
    signal: SignalType
    confidence: float  # 0-100
    reason: str
    metadata: Dict


class BaseStrategy(ABC):
    """Base class for all trading strategies"""
    
    def __init__(self, name: str, params: Dict):
        self.name = name
        self.params = params
    
    @abstractmethod
    def analyze(
        self,
        symbol: str,
        klines: List[Dict],
        position: Optional[Dict] = None,
        idx: int = None
    ) -> StrategySignal:
        """Analyze and generate signal. When idx is provided, use klines[:idx] instead of all klines."""
        pass
    
    @abstractmethod
    def get_parameters(self) -> Dict:
        """Return strategy parameters"""
        pass
    
    def analyze_safe(
        self,
        symbol: str,
        klines: List[Dict],
        position: Optional[Dict] = None,
        idx: int = None
    ) -> StrategySignal:
        """Wrapper around analyze() with exception handling.
        
        FIX A19/A20: All strategy calls should go through this method
        to prevent unhandled exceptions from crashing the trading loop.
        """
        try:
            return self.analyze(symbol, klines, position, idx)
        except Exception as e:
            logger.error(f"Strategy {self.name} analyze() failed for {symbol}: {e}")
            return StrategySignal(
                signal=SignalType.WAIT,
                confidence=0,
                reason=f"Strategy error: {str(e)[:50]}",
                metadata={"error": str(e), "strategy": self.name}
            )

    def validate(self) -> bool:
        """Validate strategy parameters"""
        return True

    def _current_price(self, klines: List[Dict]) -> float:
        """Get current price from klines"""
        return klines[-1]["close"] if klines else 0
    
    def _recent_prices(self, klines: List[Dict], n: int = 20) -> List[float]:
        """Get n most recent prices"""
        return [k["close"] for k in klines[-n:]] if klines else []
    
    def _atr(self, klines: List[Dict], period: int = 14) -> float:
        """Calculate ATR"""
        if len(klines) < period + 1:
            return 0
        
        trs = []
        for i in range(1, len(klines)):
            high = klines[i]["high"]
            low = klines[i]["low"]
            prev_close = klines[i-1]["close"]
            
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
            trs.append(tr)
        
        return sum(trs[-period:]) / period if trs else 0
