"""
3-Tier Daily Loss Circuit Breaker — separate from the main CircuitBreaker.

Monitors daily P&L percentage and escalates through tiers:
  Tier 0: Normal (daily P&L > -1%)
  Tier 1: Defensive mode (daily loss >= 1%) → position sizes × 0.5
  Tier 2: Exits only (daily loss >= 2%) → block new trades
  Tier 3: Full halt (daily loss >= 3%) → close ALL positions, halt 24h

Auto-resets at start of new UTC day.

Usage:
    from src.daily_loss_breaker import DailyLossBreaker
    dlb = DailyLossBreaker()

    result = dlb.check_daily_loss(portfolio_value=9900.0)
    if dlb.should_block_new_trades():
        return
    multiplier = dlb.get_position_size_multiplier()
"""
import logging
import time
from datetime import datetime, timezone
from typing import Dict

logger = logging.getLogger(__name__)

# ── Tier thresholds (daily loss percentage) ──
TIER_1_LOSS_PCT = 1.0   # ≥ 1% loss → defensive mode
TIER_2_LOSS_PCT = 2.0   # ≥ 2% loss → block new trades
TIER_3_LOSS_PCT = 3.0   # ≥ 3% loss → close all, halt 24h

# ── Persistence key ──
STATE_KEY = "daily_loss_breaker:state"


class DailyLossBreaker:
    """3-tier daily loss circuit breaker with StateDB persistence."""

    def __init__(self):
        self._daily_start_balance: float = 0.0
        self._current_tier: int = 0
        self._last_reset_date: str = ""
        self._trip_history: list = []
        self._halt_until: float = 0.0  # epoch when 24h halt expires (tier 3)
        self._load_state()

    # ── Persistence ──

    def _load_state(self):
        """Load state from StateDB kv store."""
        try:
            from src.state_db import get_state_db
            db = get_state_db()
            state = db.kv_get(STATE_KEY, {})
            if state:
                self._daily_start_balance = state.get("daily_start_balance", 0.0)
                self._current_tier = state.get("current_tier", 0)
                self._last_reset_date = state.get("last_reset_date", "")
                self._trip_history = state.get("trip_history", [])
                self._halt_until = state.get("halt_until", 0.0)
        except Exception as e:
            logger.warning(f"DailyLossBreaker: failed to load state: {e}")

    def _save_state(self):
        """Persist state to StateDB kv store."""
        try:
            from src.state_db import get_state_db
            db = get_state_db()
            db.kv_set(STATE_KEY, {
                "daily_start_balance": self._daily_start_balance,
                "current_tier": self._current_tier,
                "last_reset_date": self._last_reset_date,
                "trip_history": self._trip_history[-50:],  # keep last 50 entries
                "halt_until": self._halt_until,
            })
        except Exception as e:
            logger.warning(f"DailyLossBreaker: failed to save state: {e}")

    # ── Auto-reset ──

    def _get_today_utc(self) -> str:
        """Return today's date string in YYYY-MM-DD (UTC)."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _auto_reset_if_new_day(self):
        """Auto-reset tier state at start of new UTC day."""
        today = self._get_today_utc()
        if self._last_reset_date != today:
            logger.info(
                f"DailyLossBreaker: new day detected "
                f"({self._last_reset_date} → {today}), auto-resetting"
            )
            self._current_tier = 0
            self._daily_start_balance = 0.0
            self._halt_until = 0.0
            self._last_reset_date = today
            self._save_state()

    # ── Public API ──

    def check_daily_loss(self, portfolio_value: float) -> Dict:
        """Check current daily P&L and return tier/action info.

        Args:
            portfolio_value: Current total portfolio value in USDT.

        Returns:
            Dict with keys: tier, action, daily_pnl_pct, reason
        """
        self._auto_reset_if_new_day()

        today = self._get_today_utc()

        # Snapshot starting balance on first check of the day
        if self._daily_start_balance == 0.0:
            self._daily_start_balance = portfolio_value
            self._last_reset_date = today
            logger.info(
                f"DailyLossBreaker: daily start balance set to ${portfolio_value:.2f}"
            )
            self._save_state()

        # Calculate daily P&L percentage
        if self._daily_start_balance > 0:
            daily_pnl_pct = (
                (portfolio_value - self._daily_start_balance)
                / self._daily_start_balance
                * 100.0
            )
        else:
            daily_pnl_pct = 0.0

        # Determine tier (tier is escalation-only within a day, never de-escalates)
        new_tier = 0
        reason = "Normal operation"

        if daily_pnl_pct <= -TIER_3_LOSS_PCT:
            new_tier = 3
            reason = (
                f"Daily loss {daily_pnl_pct:.2f}% >= {TIER_3_LOSS_PCT}% — "
                f"HALT: close all positions, trading halted 24h"
            )
        elif daily_pnl_pct <= -TIER_2_LOSS_PCT:
            new_tier = 2
            reason = (
                f"Daily loss {daily_pnl_pct:.2f}% >= {TIER_2_LOSS_PCT}% — "
                f"BLOCK new trades, allow exits only"
            )
        elif daily_pnl_pct <= -TIER_1_LOSS_PCT:
            new_tier = 1
            reason = (
                f"Daily loss {daily_pnl_pct:.2f}% >= {TIER_1_LOSS_PCT}% — "
                f"DEFENSIVE: position sizes reduced 50%"
            )

        # Tier escalates (never de-escalates within same day)
        if new_tier > self._current_tier:
            old_tier = self._current_tier
            self._current_tier = new_tier
            self._trip_history.append({
                "date": today,
                "time": time.time(),
                "from_tier": old_tier,
                "to_tier": new_tier,
                "daily_pnl_pct": round(daily_pnl_pct, 4),
                "portfolio_value": round(portfolio_value, 2),
            })

            # Tier 3: set 24h halt
            if new_tier == 3:
                self._halt_until = time.time() + 86400  # 24 hours
                logger.critical(
                    f"DailyLossBreaker: TIER 3 TRIPPED — "
                    f"halting ALL trading for 24h. Loss: {daily_pnl_pct:.2f}%"
                )

            self._save_state()
            logger.warning(
                f"DailyLossBreaker: tier escalated {old_tier} → {new_tier}. "
                f"{reason}"
            )

        # Check if 24h halt has expired
        if self._current_tier == 3 and time.time() >= self._halt_until:
            logger.info("DailyLossBreaker: 24h tier-3 halt expired, resetting to tier 0")
            self._current_tier = 0
            self._halt_until = 0.0
            self._save_state()

        # Build action string
        actions = {
            0: "none",
            1: "defensive_mode",
            2: "block_new_trades",
            3: "close_all_and_halt",
        }

        return {
            "tier": self._current_tier,
            "action": actions.get(self._current_tier, "none"),
            "daily_pnl_pct": round(daily_pnl_pct, 4),
            "reason": reason if self._current_tier > 0 else "Normal operation",
        }

    def get_position_size_multiplier(self) -> float:
        """Return position size multiplier based on current tier.

        Returns:
            1.0 at tier 0 (normal)
            0.5 at tier 1 (defensive)
            0.0 at tier 2+ (no new positions)
        """
        if self._current_tier == 0:
            return 1.0
        elif self._current_tier == 1:
            return 0.5
        else:
            return 0.0

    def should_block_new_trades(self) -> bool:
        """Return True if new trades should be blocked (tier 2+)."""
        return self._current_tier >= 2

    def should_close_all(self) -> bool:
        """Return True if all positions should be closed (tier 3)."""
        return self._current_tier == 3

    def reset(self):
        """Manually reset the daily loss breaker."""
        self._daily_start_balance = 0.0
        self._current_tier = 0
        self._halt_until = 0.0
        self._last_reset_date = self._get_today_utc()
        self._save_state()
        logger.info("DailyLossBreaker: manually reset")

    def get_status(self) -> Dict:
        """Return current status for monitoring."""
        return {
            "current_tier": self._current_tier,
            "daily_start_balance": self._daily_start_balance,
            "last_reset_date": self._last_reset_date,
            "halt_until": self._halt_until,
            "trip_count": len(self._trip_history),
            "trip_history": self._trip_history[-10:],  # last 10 trips
            "position_multiplier": self.get_position_size_multiplier(),
            "blocked": self.should_block_new_trades(),
            "close_all": self.should_close_all(),
        }


# ── Singleton ──
_dlb_instance = None


def get_daily_loss_breaker() -> DailyLossBreaker:
    """Get singleton DailyLossBreaker instance."""
    global _dlb_instance
    if _dlb_instance is None:
        _dlb_instance = DailyLossBreaker()
    return _dlb_instance
