"""
Position Sizer — Reference position sizing for analytical purposes only.

This module provides position sizing reference calculations for opportunity
assessment and research. No trade execution functionality remains.

HybridPositionSizer combines three sizing methods:
  - Kelly Criterion: Optimal bet size based on win rate and payoff ratio
  - CVaR (Conditional Value at Risk): Tail-risk-aware sizing
  - Volatility Targeting: Normalize to target portfolio volatility

All sizing output is FOR REFERENCE ONLY — not for automatic execution.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)


# ─── Order result model (kept for backtest/analysis compatibility) ────────────


@dataclass
class ExecutionResult:
    """Result of a hypothetical order (for backtesting and reference)."""

    success: bool
    symbol: str
    side: str
    order_type: str
    requested_qty: float
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    commission: float = 0.0
    order_id: Optional[int] = None
    exchange: str = ""
    error: str = ""
    retry_count: int = 0
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "requested_qty": self.requested_qty,
            "filled_qty": self.filled_qty,
            "avg_fill_price": self.avg_fill_price,
            "commission": self.commission,
            "order_id": self.order_id,
            "exchange": self.exchange,
            "error": self.error,
            "retry_count": self.retry_count,
            "timestamp": self.timestamp,
        }


# ─── Position Sizer ─────────────────────────────────────────────────────────


class HybridPositionSizer:
    """
    Hybrid position sizer: Kelly × CVaR × Vol Target.

    Combines three sizing methods for reference position sizing:
    1. Kelly Criterion: Optimal bet size based on win rate and payoff ratio
    2. CVaR (Conditional Value at Risk): Tail-risk-aware sizing
    3. Volatility Targeting: Normalize to target portfolio volatility

    Final size = min(Kelly, CVaR, VolTarget) × regime_multiplier × vix_multiplier

    FOR REFERENCE ONLY — not for automatic execution.
    """

    TARGET_VOL = 0.15  # 15% annualized portfolio volatility target
    MAX_POSITION_PCT = 0.20  # Max 20% in single position
    MIN_POSITION_PCT = 0.01  # Min 1%

    def __init__(
        self,
        win_rate: float = 0.55,
        payoff_ratio: float = 2.0,
        cvar_confidence: float = 0.95,
        cvar_max_loss: float = 0.05,
    ):
        self.win_rate = win_rate
        self.payoff_ratio = payoff_ratio
        self.cvar_confidence = cvar_confidence
        self.cvar_max_loss = cvar_max_loss

    def calculate(
        self,
        symbol: str,
        portfolio: Optional[Dict] = None,
        current_vol: Optional[float] = None,
        regime_multiplier: float = 1.0,
        vix_multiplier: float = 1.0,
    ) -> float:
        """Calculate position size as fraction of portfolio.

        Compatible API with VolTargetSizer.calculate() so scan_orchestrator
        can use either sizer interchangeably.

        Args:
            symbol: Stock symbol.
            portfolio: Portfolio context dict with 'total_value' and 'n_positions'.
            current_vol: Current realized volatility (annualized).
            regime_multiplier: Regime-based sizing adjustment.
            vix_multiplier: VIX-based sizing adjustment.

        Returns:
            Position size as fraction of portfolio (0.0 to MAX_POSITION_PCT).
        """
        portfolio = portfolio or {}
        nav = portfolio.get("total_value", 100_000.0)
        n_positions = portfolio.get("n_positions", 10)
        stock_vol = current_vol or 0.25

        result = self.size_position(
            symbol=symbol,
            nav=nav,
            stock_vol=stock_vol,
            n_positions=n_positions,
            regime_multiplier=regime_multiplier,
            vix_multiplier=vix_multiplier,
        )
        return result["position_pct"]

    def size_position(
        self,
        symbol: str,
        nav: float,
        stock_vol: float = 0.25,
        n_positions: int = 10,
        regime_multiplier: float = 1.0,
        vix_multiplier: float = 1.0,
    ) -> Dict:
        """
        Calculate position size using hybrid approach.

        Args:
            symbol: Stock symbol.
            nav: Net Asset Value (total portfolio value).
            stock_vol: Annualized volatility of the stock.
            n_positions: Current number of open positions.
            regime_multiplier: Regime-based sizing adjustment.
            vix_multiplier: VIX-based sizing adjustment.

        Returns:
            Dict with position_size (fraction), position_usd, method breakdown.
        """

        # 1. Kelly Criterion (Half-Kelly for safety)
        kelly_frac = self._kelly_fraction()

        # 2. CVaR-based sizing
        cvar_frac = self._cvar_fraction(stock_vol)

        # 3. Volatility Target sizing
        vol_frac = self._vol_target_fraction(stock_vol, n_positions)

        # Take the minimum (most conservative)
        raw_frac = min(kelly_frac, cvar_frac, vol_frac)

        # Apply multipliers
        adjusted_frac = raw_frac * regime_multiplier * vix_multiplier

        # Clamp to limits
        final_frac = max(
            self.MIN_POSITION_PCT, min(self.MAX_POSITION_PCT, adjusted_frac)
        )

        position_usd = nav * final_frac

        logger.info(
            "PositionSizer %s: Kelly=%.2f%% CVaR=%.2f%% Vol=%.2f%% → raw=%.2f%% "
            "× regime=%.2f × vix=%.2f → final=%.2f%% ($%.0f) [REFERENCE ONLY]",
            symbol,
            kelly_frac * 100,
            cvar_frac * 100,
            vol_frac * 100,
            raw_frac * 100,
            regime_multiplier,
            vix_multiplier,
            final_frac * 100,
            position_usd,
        )

        return {
            "position_pct": final_frac,
            "position_usd": position_usd,
            "kelly_pct": kelly_frac,
            "cvar_pct": cvar_frac,
            "vol_target_pct": vol_frac,
            "regime_multiplier": regime_multiplier,
            "vix_multiplier": vix_multiplier,
        }

    def _kelly_fraction(self) -> float:
        """Half-Kelly criterion: f* = (p*b - q) / b, halved for safety."""
        p = self.win_rate
        q = 1 - p
        b = self.payoff_ratio
        if b <= 0:
            return 0.0
        full_kelly = (p * b - q) / b
        half_kelly = full_kelly / 2
        return max(0.0, min(0.25, half_kelly))  # Cap at 25%

    def _cvar_fraction(self, stock_vol: float) -> float:
        """CVaR-based sizing: limit expected tail loss to cvar_max_loss of NAV."""
        if stock_vol <= 0:
            return 0.0
        # Simplified: CVaR ≈ vol × z_score for 95% confidence
        z_95 = 1.645
        cvar_per_unit = stock_vol * z_95
        if cvar_per_unit <= 0:
            return 0.0
        return min(0.25, self.cvar_max_loss / cvar_per_unit)

    def _vol_target_fraction(self, stock_vol: float, n_positions: int) -> float:
        """Volatility-target sizing: size = target_vol / (stock_vol × sqrt(n))."""
        import math

        if stock_vol <= 0:
            return 0.0
        n = max(1, n_positions)
        return min(0.25, (self.TARGET_VOL / stock_vol) / math.sqrt(n))
