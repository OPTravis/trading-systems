"""
Stock Risk Manager - Orchestrates all stock-specific risk checks.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from .earnings_blackout import EarningsBlackout
from .pdt_guard import PDTGuard
from .settlement_guard import SettlementGuard
from .vix_position_scale import VIXPositionScale

logger = logging.getLogger(__name__)


@dataclass
class RiskDecision:
    """Result of a pre-trade risk check."""

    approved: bool
    reason: str = ""
    position_multiplier: float = 1.0
    warnings: list = field(default_factory=list)


@dataclass
class TradeSignal:
    """Incoming trade signal for risk evaluation."""

    symbol: str
    side: str  # 'buy' or 'sell'
    quantity: float
    price: float
    market: str = "US"
    is_day_trade: bool = False
    sector: str = ""


class StockRiskManager:
    """Stock-specific risk orchestrator with PDT, earnings, settlement, VIX, and more."""

    def __init__(
        self,
        account_value: float = 50000.0,
        max_sector_concentration: float = 0.30,
        min_liquidity_volume: int = 100000,
        initial_cash: float = 0.0,
    ):
        self.account_value = account_value
        self.max_sector_concentration = max_sector_concentration
        self.min_liquidity_volume = min_liquidity_volume

        self.pdt_guard = PDTGuard()
        self.earnings_blackout = EarningsBlackout()
        self.settlement_guard = SettlementGuard()
        self.vix_scale = VIXPositionScale()

        self.sector_exposure: dict = {}  # sector -> total_value

        self.settlement_guard.set_cash(initial_cash)

    def pre_trade_check(self, signal: TradeSignal, vix: float = 20.0) -> RiskDecision:
        """Run all risk checks on a trade signal."""
        if self.account_value <= 0:
            return RiskDecision(approved=False, reason="Invalid account value")

        warnings = []
        multiplier = 1.0

        # PDT check
        if signal.is_day_trade:
            if not self.pdt_guard.can_day_trade(self.account_value):
                return RiskDecision(
                    approved=False,
                    reason="PDT rule: max 3 day trades per 5 business days for accounts < $25K",
                )

        # Earnings blackout
        if self.earnings_blackout.is_blackout(signal.symbol):
            return RiskDecision(
                approved=False,
                reason=f"Earnings blackout: {signal.symbol} has upcoming earnings",
            )

        # Settlement check for buys
        if signal.side == "buy":
            available = self.settlement_guard.get_available_cash()
            trade_cost = signal.quantity * signal.price
            if trade_cost > available:
                return RiskDecision(
                    approved=False,
                    reason=f"Insufficient settled funds: need ${trade_cost:.2f}, have ${available:.2f}",
                )

        # VIX scaling
        vix_mult = self.vix_scale.get_multiplier(vix)
        multiplier *= vix_mult
        if vix_mult < 1.0:
            warnings.append(f"VIX {vix:.1f}: position scaled to {vix_mult:.1f}x")

        # Sector concentration
        if signal.sector:
            sector_value = self.sector_exposure.get(signal.sector, 0)
            trade_value = signal.quantity * signal.price
            new_concentration = (sector_value + trade_value) / self.account_value
            if new_concentration > self.max_sector_concentration:
                return RiskDecision(
                    approved=False,
                    reason=f"Sector concentration: {signal.sector} would be {new_concentration:.1%} (max {self.max_sector_concentration:.0%})",
                )

        # Liquidity check (placeholder - would need real volume data)
        # warnings could be added here

        return RiskDecision(
            approved=True,
            position_multiplier=multiplier,
            warnings=warnings,
        )

    def update_account_value(self, value: float):
        """Update account value."""
        self.account_value = value

    def update_sector_exposure(self, sector: str, value: float):
        """Update sector exposure after a trade."""
        self.sector_exposure[sector] = self.sector_exposure.get(sector, 0.0) + value
        logger.info(
            f"Sector exposure updated: {sector} = ${self.sector_exposure[sector]:,.2f}"
        )

    def record_settled_sale(
        self,
        symbol: str,
        amount: float,
        settle_days: Optional[int] = None,
        market: str = "US",
    ):
        """Record a sale that needs to settle, delegating to SettlementGuard."""
        self.settlement_guard.record_sale(amount=amount, market=market)
