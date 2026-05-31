"""
RSI Mean Reversion Strategy
Buy oversold, sell overbought
"""

import logging
from typing import Dict, List, Optional

from .base import BaseStrategy, SignalType, StrategySignal

logger = logging.getLogger(__name__)


class RSIStrategy(BaseStrategy):
    """RSI mean reversion"""

    def __init__(self, params: Dict):
        super().__init__("RSI Mean Reversion", params)
        self.rsi_oversold = params.get("rsi_oversold", 30)
        self.rsi_overbought = params.get("rsi_overbought", 70)
        # Validate
        if self.rsi_oversold >= self.rsi_overbought:
            logger.warning(
                f"Invalid RSI params: oversold={self.rsi_oversold} >= overbought={self.rsi_overbought}, using defaults"
            )
            self.rsi_oversold, self.rsi_overbought = 30, 70
        if not (0 <= self.rsi_oversold <= 100 and 0 <= self.rsi_overbought <= 100):
            logger.warning("RSI thresholds out of 0-100 range, using defaults")
            self.rsi_oversold, self.rsi_overbought = 30, 70
        self.stop_loss_pct = params.get("stop_loss_pct", 2.0)
        self.take_profit_pct = params.get("take_profit_pct", 4.0)

    def analyze(
        self,
        symbol: str,
        klines: List[Dict],
        position: Optional[Dict] = None,
        idx: Optional[int] = None,
    ) -> StrategySignal:
        """Generate RSI signals"""
        if idx is not None:
            klines = klines[:idx]
        if not klines or len(klines) < 20:
            return StrategySignal(
                signal=SignalType.WAIT,
                confidence=0,
                reason="Insufficient data",
                metadata={},
            )

        # Calculate RSI
        rsi = self._calculate_rsi(klines)
        current_price = self._current_price(klines)
        current_qty = position.get("total", 0) if position else 0

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
            if rsi < self.rsi_oversold:
                return StrategySignal(
                    signal=SignalType.BUY,
                    confidence=80,
                    reason=f"RSI oversold ({rsi:.1f}) - buy signal",
                    metadata={
                        "rsi": rsi,
                        "stop_loss": current_price * (1 - self.stop_loss_pct / 100),
                        "take_profit": current_price * (1 + self.take_profit_pct / 100),
                    },
                )
            elif rsi < 40:
                return StrategySignal(
                    signal=SignalType.BUY,
                    confidence=50,
                    reason=f"RSI neutral-low ({rsi:.1f}) - potential entry",
                    metadata={"rsi": rsi},
                )
            else:
                return StrategySignal(
                    signal=SignalType.WAIT,
                    confidence=40,
                    reason=f"RSI not oversold ({rsi:.1f})",
                    metadata={"rsi": rsi},
                )
        else:
            # Have position
            if rsi > self.rsi_overbought:
                return StrategySignal(
                    signal=SignalType.SELL,
                    confidence=80,
                    reason=f"RSI overbought ({rsi:.1f}) - take profit",
                    metadata={"rsi": rsi, "pnl_pct": pnl_pct},
                )
            elif pnl_pct >= self.take_profit_pct:
                return StrategySignal(
                    signal=SignalType.SELL,
                    confidence=75,
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
                    reason=f"RSI {rsi:.1f}, PnL: {pnl_pct:.1f}%",
                    metadata={"rsi": rsi, "pnl_pct": pnl_pct},
                )

    def _calculate_rsi(self, klines: List[Dict], period: int = 14) -> float:
        """Calculate RSI using Wilder's smoothing (EMA method)"""
        if len(klines) < period + 1:
            return 50.0

        prices = [k["close"] for k in klines]

        deltas = []
        for i in range(1, len(prices)):
            deltas.append(prices[i] - prices[i - 1])

        # First average: simple moving average
        gains = [d if d > 0 else 0 for d in deltas[:period]]
        losses = [-d if d < 0 else 0 for d in deltas[:period]]

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        # Wilder smoothing: EMA with alpha = 1/period
        for d in deltas[period:]:
            gain = d if d > 0 else 0
            loss = -d if d < 0 else 0
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def get_parameters(self) -> Dict:
        return {
            "rsi_oversold": self.rsi_oversold,
            "rsi_overbought": self.rsi_overbought,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
        }
