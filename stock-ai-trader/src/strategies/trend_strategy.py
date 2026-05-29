"""
Trend Strategy - Trend following with MA crossover + ADX filter.

Uses fast/slow moving average crossover with ADX > 25 to confirm trend strength.
Holding period: 5-30 days.

Entry: Fast MA crosses above Slow MA AND ADX > 25
Exit:  Fast MA crosses below Slow MA OR stop-loss hit
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from .base_strategy import BaseStrategy, Position, Signal, SignalAction

logger = logging.getLogger(__name__)

DEFAULT_PARAMS = {
    "fast_period": 10,       # Fast MA period
    "slow_period": 30,       # Slow MA period
    "adx_period": 14,        # ADX calculation period
    "adx_threshold": 25,     # Minimum ADX for trend confirmation
    "atr_period": 14,        # ATR period for stop-loss
    "atr_stop_multiplier": 2.0,  # Stop-loss = ATR * multiplier
    "min_holding_days": 5,
    "max_holding_days": 30,
}


class TrendStrategy(BaseStrategy):
    """
    Trend-following strategy using MA crossover with ADX filter.

    Logic:
    - Buy when fast MA > slow MA (crossover) AND ADX > threshold
    - Sell when fast MA < slow MA (crossover down)
    - Stop-loss at entry - ATR * multiplier
    """

    def __init__(self, params: Optional[dict] = None) -> None:
        merged = {**DEFAULT_PARAMS, **(params or {})}
        super().__init__(name="TrendFollowing", params=merged)

    def generate_signals(
        self,
        universe: dict[str, pd.DataFrame],
    ) -> list[Signal]:
        signals = []
        p = self._params
        now = datetime.now()

        for symbol, df in universe.items():
            if len(df) < p["slow_period"] + 5:
                continue

            try:
                signal = self._analyze(symbol, df, now)
                if signal:
                    signals.append(signal)
            except Exception as e:
                logger.warning("TrendStrategy error on %s: %s", symbol, e)

        # Sort by signal strength descending
        signals.sort(key=lambda s: s.strength, reverse=True)
        return signals

    def _analyze(
        self,
        symbol: str,
        df: pd.DataFrame,
        now: datetime,
    ) -> Optional[Signal]:
        """Analyze a single symbol for trend signals."""
        p = self._params
        close = df["close"]
        high = df["high"]
        low = df["low"]

        # Calculate indicators
        fast_ma = self.ema(close, p["fast_period"])
        slow_ma = self.ema(close, p["slow_period"])
        adx_val = self.adx(high, low, close, p["adx_period"])
        atr_val = self.atr(high, low, close, p["atr_period"])

        current_fast = fast_ma.iloc[-1]
        current_slow = slow_ma.iloc[-1]
        prev_fast = fast_ma.iloc[-2]
        prev_slow = slow_ma.iloc[-2]
        current_adx = adx_val.iloc[-1]
        current_atr = atr_val.iloc[-1]
        current_price = close.iloc[-1]

        # BUY: Fast MA crosses above Slow MA + ADX > threshold
        crossover_up = prev_fast <= prev_slow and current_fast > current_slow
        strong_trend = current_adx >= p["adx_threshold"]

        if crossover_up and strong_trend:
            # Signal strength based on ADX level
            strength = min(1.0, current_adx / 50.0)
            stop_loss = current_price - (current_atr * p["atr_stop_multiplier"])

            return Signal(
                symbol=symbol,
                action=SignalAction.BUY,
                strategy=self.name,
                timestamp=now,
                strength=strength,
                price=current_price,
                stop_loss=stop_loss,
                metadata={
                    "fast_ma": current_fast,
                    "slow_ma": current_slow,
                    "adx": current_adx,
                    "atr": current_atr,
                },
            )

        # SELL: Fast MA crosses below Slow MA (if we have a position)
        crossover_down = prev_fast >= prev_slow and current_fast < current_slow

        if crossover_down and self.has_position(symbol):
            return Signal(
                symbol=symbol,
                action=SignalAction.SELL,
                strategy=self.name,
                timestamp=now,
                strength=0.8,
                price=current_price,
                metadata={
                    "reason": "ma_crossover_down",
                    "fast_ma": current_fast,
                    "slow_ma": current_slow,
                },
            )

        return None

    def should_enter(self, signal: Signal) -> bool:
        """
        Validate entry conditions:
        - Must be a BUY signal
        - Must not already have a position in this symbol
        - Strength must exceed minimum threshold
        """
        if signal.action != SignalAction.BUY:
            return False
        if self.has_position(signal.symbol):
            return False
        if signal.strength < 0.5:
            return False
        return True

    def should_exit(self, position: Position) -> bool:
        """
        Exit conditions:
        - Holding period exceeded max days
        - Stop-loss hit (current price below stop)
        - MA crossover down (checked via generate_signals)
        """
        p = self._params

        # Time-based exit
        days_held = (datetime.now() - position.entry_date).days
        if days_held >= p["max_holding_days"]:
            return True

        # Don't exit too early
        if days_held < p["min_holding_days"]:
            return False

        # Stop-loss check
        current_price = position.metadata.get("current_price")
        if current_price and position.stop_loss:
            if current_price <= position.stop_loss:
                return True

        return False
