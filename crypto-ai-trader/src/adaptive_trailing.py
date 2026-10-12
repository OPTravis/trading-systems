"""Adaptive Trailing Stop module with step-wise tightening based on profit level."""

from typing import Dict, Optional


def get_state_db():
    from src.state_db import get_state_db as _get

    return _get()


class AdaptiveTrailingStop:
    """Step-wise trailing stop that tightens as profit increases."""

    MIN_PROFIT_LOCK_PCT = 0.005  # 0.5% minimum profit lock

    # WO-1044-C (2026-10-12): tightened from 6/5/3/2% — forensics showed
    # 39/92 trades giving back ≥1.5% peak profit: the old 6% trail in the
    # 1-3% profit band (where most positions live) locked nothing at all.
    # New ladder locks profit from +1% with a ~2% pullback band (ATR
    # volatility_adjustment 0.7-1.5 still applies; MIN_PROFIT_LOCK 0.5%
    # unchanged). Step names keep the legacy prefix for log continuity.
    STEPS = [
        (0.01, 0.025, "step_1_6"),  # profit 1-3%: trail 2.5% below peak
        (0.03, 0.02, "step_3_5"),  # profit 3-5%: trail 2% below peak
        (0.05, 0.02, "step_5_10"),  # profit 5-10%: trail 2% below peak
        (0.10, 0.015, "step_10_plus"),  # profit >10%: trail 1.5% below peak
    ]

    def calculate_trailing_sl(
        self,
        entry_price: float,
        current_price: float,
        highest_price: float,
        initial_sl: float,
        volatility_adjustment: float = 1.0,
    ) -> Dict:
        profit_pct = (current_price - entry_price) / entry_price
        min_sl = entry_price * (1 + self.MIN_PROFIT_LOCK_PCT)

        if profit_pct < 0.01:
            return {
                "trailing_sl": initial_sl,
                "trailing_active": False,
                "step": "no_trail",
                "locked_profit_pct": profit_pct,
            }

        trail_width = None
        step_name = None
        for threshold, base_width, sn in self.STEPS:
            if profit_pct >= threshold:
                trail_width = base_width * volatility_adjustment
                step_name = sn

        if trail_width is None:
            return {
                "trailing_sl": initial_sl,
                "trailing_active": False,
                "step": "no_trail",
                "locked_profit_pct": profit_pct,
            }

        trailing_sl = highest_price * (1 - trail_width)
        trailing_sl = max(trailing_sl, min_sl)

        return {
            "trailing_sl": trailing_sl,
            "trailing_active": True,
            "step": step_name,
            "locked_profit_pct": profit_pct,
        }

    @staticmethod
    def should_update_sl(current_sl: float, new_sl: float) -> bool:
        return new_sl > current_sl

    @staticmethod
    def get_step_description(profit_pct: float) -> str:
        if profit_pct < 0.01:
            return "No trailing stop (profit < 1%)"
        elif profit_pct < 0.03:
            return "Trailing at 2.5% below peak (profit 1-3%)"
        elif profit_pct < 0.05:
            return "Trailing at 2% below peak (profit 3-5%)"
        elif profit_pct < 0.10:
            return "Trailing at 2% below peak (profit 5-10%)"
        else:
            return "Trailing at 1.5% below peak (profit > 10%)"

    def save_state(self, symbol: str, state: Dict) -> None:
        db = get_state_db()
        db.kv_set(f"adaptive_trailing:{symbol}", state)

    def load_state(self, symbol: str) -> Optional[Dict]:
        db = get_state_db()
        return db.kv_get(f"adaptive_trailing:{symbol}")


# Module-level convenience functions
_adaptive_instance = AdaptiveTrailingStop()


def calculate_trailing_sl(
    entry_price: float,
    current_price: float,
    highest_price: float,
    initial_sl: float,
    volatility_adjustment: float = 1.0,
) -> Dict:
    """Convenience wrapper for AdaptiveTrailingStop.calculate_trailing_sl()."""
    return _adaptive_instance.calculate_trailing_sl(
        entry_price, current_price, highest_price, initial_sl, volatility_adjustment
    )


def should_update_sl(current_sl: float, new_sl: float) -> bool:
    """Convenience wrapper for AdaptiveTrailingStop.should_update_sl()."""
    return AdaptiveTrailingStop.should_update_sl(current_sl, new_sl)


def get_step_description(profit_pct: float) -> str:
    """Convenience wrapper for AdaptiveTrailingStop.get_step_description()."""
    return AdaptiveTrailingStop.get_step_description(profit_pct)
