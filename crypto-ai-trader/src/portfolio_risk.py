"""
Portfolio risk management — mixin for PortfolioManager.
"""

import logging
from datetime import datetime
from typing import Dict

logger = logging.getLogger(__name__)


class RiskMixin:
    """Risk management methods for PortfolioManager."""

    def _check_daily_reset(self):
        """Reset daily tracking if date changed."""
        today = datetime.now().date()
        if self._daily_start_date != today:
            self._daily_start_value = self.cash_balance + self.get_total_exposure()
            self._daily_start_date = today

    def validate_leverage(self, leverage: float) -> bool:
        """Validate leverage within limits"""
        return 1 <= leverage <= self.config.get("max_leverage", 1)

    def check_risk_limits(self) -> Dict:
        """Check if within risk limits - returns warnings AND positions to close"""
        self._check_daily_reset()

        total_value = self.cash_balance + self.get_total_exposure()
        exposure = self.get_total_exposure()
        exposure_pct = (exposure / total_value * 100) if total_value > 0 else 0

        warnings = []
        positions_to_close = []

        # Check daily loss limit
        if self._daily_start_value and self._daily_start_value > 0:
            daily_pnl = total_value - self._daily_start_value
            daily_loss_pct = (daily_pnl / self._daily_start_value * 100)
            if daily_loss_pct < -self.config.get("max_daily_loss_pct", 100):
                warnings.append(f"Daily loss {daily_loss_pct:.1f}% exceeds max {self.config['max_daily_loss_pct']}%")
                positions_to_close.extend([pos["symbol"] for pos in self.positions.values()])

        if exposure_pct > self.config.get("max_total_exposure_pct", 100):
            warnings.append(f"Total exposure {exposure_pct:.1f}% exceeds limit")

        for pos in self.positions.values():
            pos_value = pos["current_price"] * pos["quantity"]
            pos_pct = (pos_value / total_value * 100) if total_value > 0 else 0
            if pos_pct > self.config.get("max_position_pct", 100):
                warnings.append(f"Position {pos['symbol']} at {pos_pct:.1f}% exceeds limit")

        # Check stop losses
        for pos in self.positions.values():
            if pos["current_price"] <= pos.get("stop_loss", 0):
                warnings.append(f"Stop loss triggered for {pos['symbol']}")
                positions_to_close.append(pos["symbol"])
            elif pos.get("trailing_stop") and pos["current_price"] <= pos["trailing_stop"]:
                warnings.append(f"Trailing stop triggered for {pos['symbol']}")
                positions_to_close.append(pos["symbol"])

        # Check max hold time
        max_hold_hours = self.config.get("max_hold_hours")
        if max_hold_hours:
            now = datetime.now()
            for pos in self.positions.values():
                created = pos.get("created_at")
                if created:
                    try:
                        created_dt = datetime.fromisoformat(created)
                        hours_held = (now - created_dt).total_seconds() / 3600
                        if hours_held > max_hold_hours:
                            warnings.append(f"Max hold time exceeded for {pos['symbol']} ({hours_held:.1f}h > {max_hold_hours}h)")
                            positions_to_close.append(pos["symbol"])
                    except (ValueError, TypeError):
                        logger.debug("Could not parse created_at timestamp for %s: %s", pos.get("symbol"), created, exc_info=True)

        return {
            "total_value": total_value,
            "cash": self.cash_balance,
            "exposure": exposure,
            "exposure_pct": exposure_pct,
            "warnings": warnings,
            "positions_to_close": list(set(positions_to_close)),
            "ok": len(warnings) == 0
        }

    def suggest_rebalance(self) -> Dict:
        """Suggest portfolio rebalancing"""
        total_value = self.cash_balance + self.get_total_exposure()
        target_exposure = total_value * (self.config["max_total_exposure_pct"] / 100)
        current_exposure = self.get_total_exposure()

        return {
            "total_value": total_value,
            "cash": self.cash_balance,
            "current_exposure": current_exposure,
            "target_exposure": target_exposure,
            "rebalance_needed": abs(target_exposure - current_exposure) > total_value * 0.05,
            "action": "buy" if current_exposure < target_exposure else "sell"
        }
