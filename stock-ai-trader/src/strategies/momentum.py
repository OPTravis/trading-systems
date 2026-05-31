"""
Momentum Strategy - Relative strength + breakout momentum.

Identifies stocks with strong relative performance and breakout patterns.
Holding period: 10-30 days.

Entry: 12-1 month relative strength top 20% + 20-day high breakout
Exit:  Relative strength drops below median OR trailing stop
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pandas as pd

from .base_strategy import BaseStrategy, Position, Signal, SignalAction

logger = logging.getLogger(__name__)

DEFAULT_PARAMS = {
    "rs_lookback_long": 252,  # 12 months for relative strength
    "rs_lookback_short": 21,  # 1 month (subtract recent for 12-1 month RS)
    "rs_top_pct": 0.20,  # Top 20% for entry
    "rs_median_pct": 0.50,  # Below median for exit
    "breakout_period": 20,  # 20-day high for breakout
    "volume_surge_mult": 1.5,  # Volume must be 1.5x average on breakout
    "volume_avg_period": 20,  # Volume moving average period
    "atr_period": 14,
    "trailing_stop_atr_mult": 2.5,  # Trailing stop in ATR units
    "min_holding_days": 10,
    "max_holding_days": 30,
}


class MomentumStrategy(BaseStrategy):
    """
    Momentum strategy using relative strength ranking + breakout.

    Logic:
    - Rank universe by 12-1 month relative strength
    - Buy top 20% stocks that break 20-day high with volume surge
    - Sell when relative strength drops below median or trailing stop hit
    """

    def __init__(self, params: Optional[dict] = None) -> None:
        merged = {**DEFAULT_PARAMS, **(params or {})}
        super().__init__(name="Momentum", params=merged)
        self._rs_scores: dict[str, float] = {}  # symbol -> RS percentile

    def generate_signals(
        self,
        universe: dict[str, pd.DataFrame],
    ) -> list[Signal]:
        """
        Generate momentum signals for the entire universe.

        Process:
        1. Calculate 12-1 month relative strength for all symbols
        2. Rank and select top 20%
        3. Check for 20-day breakout with volume confirmation
        """
        signals: list[Signal] = []
        p = self._params
        now = datetime.now()

        if len(universe) < 5:
            logger.warning("Universe too small for meaningful momentum ranking")
            return signals

        # Step 1: Calculate relative strength for all symbols
        rs_scores = self._calculate_relative_strength(universe)
        if not rs_scores:
            return signals

        # Step 2: Rank and get cutoffs
        sorted_rs = sorted(rs_scores.items(), key=lambda x: x[1], reverse=True)
        top_n = max(1, int(len(sorted_rs) * p["rs_top_pct"]))
        median_idx = len(sorted_rs) // 2

        top_symbols = {s for s, _ in sorted_rs[:top_n]}
        median_rs = sorted_rs[median_idx][1] if median_idx < len(sorted_rs) else 0

        # Store scores for exit logic
        self._rs_scores = dict(sorted_rs)

        # Step 3: Generate buy signals for top stocks with breakout
        for symbol in top_symbols:
            if symbol not in universe:
                continue
            df = universe[symbol]
            if len(df) < p["rs_lookback_long"]:
                continue

            signal = self._check_breakout(symbol, df, rs_scores[symbol], now)
            if signal:
                signals.append(signal)

        # Step 4: Generate sell signals for positions dropping below median
        for symbol, position in self._positions.items():
            if symbol in self._rs_scores:
                if self._rs_scores[symbol] < median_rs:
                    current_price = position.metadata.get(
                        "current_price", position.entry_price
                    )
                    signals.append(
                        Signal(
                            symbol=symbol,
                            action=SignalAction.SELL,
                            strategy=self.name,
                            timestamp=now,
                            strength=0.7,
                            price=current_price,
                            metadata={
                                "reason": "rs_below_median",
                                "rs_rank": self._rs_scores[symbol],
                                "median_rs": median_rs,
                            },
                        )
                    )

        signals.sort(key=lambda s: s.strength, reverse=True)
        return signals

    def _calculate_relative_strength(
        self,
        universe: dict[str, pd.DataFrame],
    ) -> dict[str, float]:
        """
        Calculate 12-1 month relative strength for each symbol.

        RS = (price / price_12mo_ago) / (price_1mo_ago / price_12mo_ago)
           = price / price_1mo_ago  (simplified: recent 1-month return)

        More precisely: Total return from 12 months ago to 1 month ago
        (excluding the most recent month to avoid mean reversion).
        """
        p = self._params
        rs_scores = {}

        for symbol, df in universe.items():
            close = df["close"]
            if len(close) < p["rs_lookback_long"]:
                continue

            try:
                # 12-month lookback price (approximately 252 trading days ago)
                price_12mo = close.iloc[-p["rs_lookback_long"]]
                # 1-month lookback price (21 trading days ago)
                price_1mo = close.iloc[-p["rs_lookback_short"]]

                if price_12mo <= 0:
                    continue

                # 12-1 month relative strength (skip last month)
                rs = (price_1mo / price_12mo - 1) * 100
                rs_scores[symbol] = rs
            except (IndexError, ZeroDivisionError):
                continue

        return rs_scores

    def _check_breakout(
        self,
        symbol: str,
        df: pd.DataFrame,
        rs_score: float,
        now: datetime,
    ) -> Optional[Signal]:
        """Check if symbol has a breakout with volume confirmation."""
        p = self._params
        close = df["close"]
        high = df["high"]
        volume = df["volume"]

        current_price = close.iloc[-1]
        high.iloc[-1]

        # 20-day high (excluding today)
        high_n = high.iloc[-(p["breakout_period"] + 1) : -1].max()

        # Breakout: today's close above the N-day high
        if current_price <= high_n:
            return None

        # Volume confirmation
        avg_volume = volume.iloc[-p["volume_avg_period"] :].mean()
        current_volume = volume.iloc[-1]
        if avg_volume <= 0 or current_volume < avg_volume * p["volume_surge_mult"]:
            return None

        # Calculate ATR for trailing stop
        atr_val = self.atr(high, df["low"], close, p["atr_period"])
        current_atr = atr_val.iloc[-1]
        stop_loss = current_price - (current_atr * p["trailing_stop_atr_mult"])

        # Signal strength based on RS percentile and breakout magnitude
        breakout_pct = (current_price - high_n) / high_n * 100
        strength = max(0.0, min(1.0, 0.5 + breakout_pct / 5 + rs_score / 200))

        return Signal(
            symbol=symbol,
            action=SignalAction.BUY,
            strategy=self.name,
            timestamp=now,
            strength=strength,
            price=current_price,
            stop_loss=stop_loss,
            metadata={
                "rs_score": rs_score,
                "breakout_above": high_n,
                "breakout_pct": breakout_pct,
                "volume_ratio": current_volume / avg_volume,
                "atr": current_atr,
            },
        )

    def should_enter(self, signal: Signal) -> bool:
        """
        Validate momentum entry:
        - Must be BUY signal
        - No existing position
        - Minimum strength
        - Must be in top RS tier
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
        - Max holding period exceeded
        - Trailing stop hit
        - Relative strength below median (checked in generate_signals)

        NOTE: This method does NOT mutate position. Call update_trailing_stop()
        separately before checking should_exit().
        """
        p = self._params

        days_held = (datetime.now() - position.entry_date).days

        # Force exit at max holding
        if days_held >= p["max_holding_days"]:
            return True

        # Trailing stop check (read-only)
        current_price = position.metadata.get("current_price")
        if current_price and position.stop_loss:
            if current_price <= position.stop_loss:
                return True

        # Min holding period
        if days_held < p["min_holding_days"]:
            return False

        # RS below median is checked via generate_signals
        return False

    def update_trailing_stop(
        self, position: Position, current_price: float, params: dict | None = None
    ) -> None:
        """
        Update trailing stop (ratchet up, never down).
        Call this before should_exit() to keep the stop-loss current.

        Args:
            position: The open position to update.
            current_price: Current market price.
            params: Optional params override (uses self._params if None).
        """
        p = params or self._params
        atr = position.metadata.get("atr")

        # Guard: ATR missing or zero — skip update
        if atr is None or atr == 0:
            return

        new_stop = current_price - (atr * p["trailing_stop_atr_mult"])
        if position.stop_loss is None or new_stop > position.stop_loss:
            position.stop_loss = new_stop
