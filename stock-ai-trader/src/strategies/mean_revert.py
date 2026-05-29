"""
Mean Reversion Strategy - RSI + Bollinger Bands mean reversion.

Buys oversold conditions and sells when price reverts to mean.
Holding period: 3-10 days.

Entry: RSI < 30 AND price near Bollinger Band lower band
Exit:  RSI > 70 OR price at BB middle band
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import numpy as np
import pandas as pd

from .base_strategy import BaseStrategy, Position, Signal, SignalAction

logger = logging.getLogger(__name__)

DEFAULT_PARAMS = {
    "rsi_period": 14,          # RSI calculation period
    "rsi_oversold": 30,        # RSI buy threshold
    "rsi_overbought": 70,      # RSI sell threshold
    "bb_period": 20,           # Bollinger Band period
    "bb_std": 2.0,             # Bollinger Band standard deviations
    "bb_lower_entry_pct": 0.1, # Price must be within 10% of lower band
    "min_holding_days": 3,
    "max_holding_days": 10,
    "atr_period": 14,
    "atr_stop_multiplier": 1.5,
}


class MeanRevertStrategy(BaseStrategy):
    """
    Mean reversion strategy using RSI + Bollinger Bands.

    Logic:
    - Buy when RSI < 30 AND price near BB lower band
    - Sell when RSI > 70 OR price reaches BB middle band
    - Stop-loss at entry - ATR * multiplier
    """

    def __init__(self, params: Optional[dict] = None) -> None:
        merged = {**DEFAULT_PARAMS, **(params or {})}
        super().__init__(name="MeanReversion", params=merged)

    def generate_signals(
        self,
        universe: dict[str, pd.DataFrame],
    ) -> list[Signal]:
        signals = []
        p = self._params
        now = datetime.now()

        for symbol, df in universe.items():
            if len(df) < max(p["bb_period"], p["rsi_period"]) + 5:
                continue

            try:
                signal = self._analyze(symbol, df, now)
                if signal:
                    signals.append(signal)
            except Exception as e:
                logger.warning("MeanRevertStrategy error on %s: %s", symbol, e)

        signals.sort(key=lambda s: s.strength, reverse=True)
        return signals

    def _analyze(
        self,
        symbol: str,
        df: pd.DataFrame,
        now: datetime,
    ) -> Optional[Signal]:
        """Analyze a single symbol for mean reversion signals."""
        p = self._params
        close = df["close"]
        high = df["high"]
        low = df["low"]

        # Calculate indicators
        rsi_val = self.rsi(close, p["rsi_period"])
        bb_upper, bb_middle, bb_lower = self.bollinger_bands(
            close, p["bb_period"], p["bb_std"]
        )
        atr_val = self.atr(high, low, close, p["atr_period"])

        current_rsi = rsi_val.iloc[-1]
        current_price = close.iloc[-1]
        current_lower = bb_lower.iloc[-1]
        current_middle = bb_middle.iloc[-1]
        current_upper = bb_upper.iloc[-1]
        current_atr = atr_val.iloc[-1]

        # Check if price is near lower Bollinger Band
        bb_width = current_upper - current_lower
        if bb_width <= 0:
            return None
        distance_to_lower = (current_price - current_lower) / bb_width

        # BUY: RSI oversold + price near BB lower band
        is_oversold = current_rsi <= p["rsi_oversold"]
        near_lower_band = distance_to_lower <= p["bb_lower_entry_pct"]

        if is_oversold and near_lower_band:
            # Strength: deeper into oversold = stronger signal
            rsi_strength = max(0, (p["rsi_oversold"] - current_rsi) / p["rsi_oversold"])
            bb_strength = max(0, 1 - distance_to_lower / p["bb_lower_entry_pct"])
            strength = min(1.0, (rsi_strength + bb_strength) / 2)

            stop_loss = current_price - (current_atr * p["atr_stop_multiplier"])

            return Signal(
                symbol=symbol,
                action=SignalAction.BUY,
                strategy=self.name,
                timestamp=now,
                strength=strength,
                price=current_price,
                stop_loss=stop_loss,
                take_profit=current_middle,  # Target: BB middle
                metadata={
                    "rsi": current_rsi,
                    "bb_lower": current_lower,
                    "bb_middle": current_middle,
                    "bb_upper": current_upper,
                    "atr": current_atr,
                },
            )

        # SELL: RSI overbought or price at BB middle (for existing positions)
        if self.has_position(symbol):
            is_overbought = current_rsi >= p["rsi_overbought"]
            at_middle = current_price >= current_middle

            if is_overbought or at_middle:
                reason = "rsi_overbought" if is_overbought else "bb_middle_reached"
                return Signal(
                    symbol=symbol,
                    action=SignalAction.SELL,
                    strategy=self.name,
                    timestamp=now,
                    strength=0.7 if is_overbought else 0.5,
                    price=current_price,
                    metadata={
                        "reason": reason,
                        "rsi": current_rsi,
                        "bb_middle": current_middle,
                    },
                )

        return None

    def should_enter(self, signal: Signal) -> bool:
        """
        Validate entry:
        - Must be BUY signal
        - No existing position
        - Minimum strength
        """
        if signal.action != SignalAction.BUY:
            return False
        if self.has_position(signal.symbol):
            return False
        if signal.strength < 0.4:
            return False
        return True

    def should_exit(self, position: Position) -> bool:
        """
        Exit conditions:
        - Max holding period reached
        - Stop-loss hit
        - Take-profit reached (BB middle)
        """
        p = self._params

        days_held = (datetime.now() - position.entry_date).days

        # Force exit at max holding
        if days_held >= p["max_holding_days"]:
            return True

        # Don't exit too early (unless stop-loss)
        if days_held < p["min_holding_days"]:
            current_price = position.metadata.get("current_price")
            if current_price and position.stop_loss:
                return current_price <= position.stop_loss
            return False

        # Take-profit check
        current_price = position.metadata.get("current_price")
        if current_price and position.take_profit:
            if current_price >= position.take_profit:
                return True

        # Stop-loss check
        if current_price and position.stop_loss:
            if current_price <= position.stop_loss:
                return True

        return False
