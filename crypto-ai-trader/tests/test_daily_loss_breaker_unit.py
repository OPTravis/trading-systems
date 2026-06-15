"""
Unit tests for daily_loss_breaker.py — mock-based, no network.

Covers:
  - Tier escalation: -1% → tier 1, -2% → tier 2, -3% → tier 3
  - Tier only escalates (never de-escalates within same day)
  - P1-3: PnL turns positive → tier downgrade by one level
  - UTC day reset → tier returns to 0
  - Position size multiplier and blocking logic
"""

import time
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime, timezone, timedelta

import pytest

from src import daily_loss_breaker
from src.daily_loss_breaker import (
    DailyLossBreaker,
    TIER_1_LOSS_PCT,
    TIER_2_LOSS_PCT,
    TIER_3_LOSS_PCT,
    get_daily_loss_breaker,
)


@pytest.fixture(autouse=True)
def _reset_dlb_singleton():
    """Reset module-level singleton between tests."""
    daily_loss_breaker._dlb_instance = None
    yield
    daily_loss_breaker._dlb_instance = None


def _make_dlb():
    """Create a DailyLossBreaker with mocked StateDB."""
    with patch.object(DailyLossBreaker, "_load_state", lambda self: None), \
         patch.object(DailyLossBreaker, "_save_state", lambda self: None):
        dlb = DailyLossBreaker()
        dlb._last_reset_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return dlb


# ────────────────────────────────────────────────────────────
# Tier escalation
# ────────────────────────────────────────────────────────────

class TestTierEscalation:

    def test_tier0_normal(self):
        """No loss → tier 0, normal operation."""
        dlb = _make_dlb()
        result = dlb.check_daily_loss(10000.0)
        assert result["tier"] == 0
        assert result["action"] == "none"

    def test_tier1_at_1pct_loss(self):
        """Daily loss >= 1% → tier 1."""
        dlb = _make_dlb()
        # First call sets start balance
        dlb.check_daily_loss(10000.0)
        # 1.5% loss → 9850
        result = dlb.check_daily_loss(9850.0)
        assert result["tier"] == 1
        assert result["action"] == "defensive_mode"

    def test_tier2_at_2pct_loss(self):
        """Daily loss >= 2% → tier 2."""
        dlb = _make_dlb()
        dlb.check_daily_loss(10000.0)
        # 2.5% loss → 9750
        result = dlb.check_daily_loss(9750.0)
        assert result["tier"] == 2
        assert result["action"] == "block_new_trades"

    def test_tier3_at_3pct_loss(self):
        """Daily loss >= 3% → tier 3 (close all, halt 24h)."""
        dlb = _make_dlb()
        dlb.check_daily_loss(10000.0)
        # 4% loss → 9600
        result = dlb.check_daily_loss(9600.0)
        assert result["tier"] == 3
        assert result["action"] == "close_all_and_halt"


# ────────────────────────────────────────────────────────────
# Tier only escalates
# ────────────────────────────────────────────────────────────

class TestTierOnlyEscalates:

    def test_no_deescalation_on_partial_recovery(self):
        """Once at tier 2, partial recovery (still in loss) → stays tier 2."""
        dlb = _make_dlb()
        dlb.check_daily_loss(10000.0)
        # Reach tier 2
        result = dlb.check_daily_loss(9750.0)
        assert result["tier"] == 2
        # Partial recovery to -1.5% (still in tier 1 zone, but can't downgrade)
        result = dlb.check_daily_loss(9850.0)
        assert result["tier"] == 2  # stays at 2

    def test_no_deescalation_to_tier0_from_tier1(self):
        """Tier 1 → still slightly negative → stays tier 1."""
        dlb = _make_dlb()
        dlb.check_daily_loss(10000.0)
        result = dlb.check_daily_loss(9850.0)  # -1.5%
        assert result["tier"] == 1
        # Still slightly negative: -0.5%
        result = dlb.check_daily_loss(9950.0)
        assert result["tier"] == 1  # no de-escalation


# ────────────────────────────────────────────────────────────
# P1-3: PnL turns positive → tier downgrade
# ────────────────────────────────────────────────────────────

class TestTierDowngradeOnProfit:

    def test_positive_pnl_downgrades_one_tier(self):
        """P1-3: When daily PnL > 0, tier drops by one level."""
        dlb = _make_dlb()
        dlb.check_daily_loss(10000.0)
        # Reach tier 2
        dlb.check_daily_loss(9750.0)  # -2.5%
        assert dlb._current_tier == 2
        # Turn positive: portfolio now 10100 (+1%)
        result = dlb.check_daily_loss(10100.0)
        assert result["daily_pnl_pct"] > 0
        assert dlb._current_tier == 1  # downgraded by 1

    def test_downgrade_from_tier3_to_tier2_on_profit(self):
        dlb = _make_dlb()
        dlb.check_daily_loss(10000.0)
        dlb.check_daily_loss(9600.0)  # -4% → tier 3
        assert dlb._current_tier == 3
        # Turn positive
        result = dlb.check_daily_loss(10100.0)
        assert dlb._current_tier == 2

    def test_downgrade_to_zero(self):
        """Repeated positive PnL calls can reach tier 0."""
        dlb = _make_dlb()
        dlb.check_daily_loss(10000.0)
        dlb.check_daily_loss(9850.0)  # tier 1
        assert dlb._current_tier == 1
        # Positive PnL → tier 0
        dlb.check_daily_loss(10100.0)
        assert dlb._current_tier == 0


# ────────────────────────────────────────────────────────────
# UTC day reset
# ────────────────────────────────────────────────────────────

class TestUtcDayReset:

    def test_new_day_resets_tier_to_zero(self):
        """New UTC day → tier resets to 0, start balance re-snapshots."""
        dlb = _make_dlb()
        dlb.check_daily_loss(10000.0)
        dlb.check_daily_loss(9600.0)  # tier 3
        assert dlb._current_tier == 3

        # Simulate new day
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        dlb._last_reset_date = "2020-01-01"  # force old date
        result = dlb.check_daily_loss(10000.0)
        assert dlb._current_tier == 0
        assert result["tier"] == 0

    def test_new_day_resets_halt_until(self):
        dlb = _make_dlb()
        dlb.check_daily_loss(10000.0)
        dlb.check_daily_loss(9600.0)  # tier 3 → 24h halt
        assert dlb._halt_until > 0

        # Force new day
        dlb._last_reset_date = "2020-01-01"
        dlb.check_daily_loss(10000.0)
        assert dlb._halt_until == 0.0
        assert dlb._current_tier == 0


# ────────────────────────────────────────────────────────────
# Position size multiplier & blocking
# ────────────────────────────────────────────────────────────

class TestPositionSizingAndBlocking:

    def test_tier0_multiplier_is_1(self):
        dlb = _make_dlb()
        assert dlb.get_position_size_multiplier() == 1.0

    def test_tier1_multiplier_is_0_5(self):
        dlb = _make_dlb()
        dlb._current_tier = 1
        assert dlb.get_position_size_multiplier() == 0.5

    def test_tier2_multiplier_is_0(self):
        dlb = _make_dlb()
        dlb._current_tier = 2
        assert dlb.get_position_size_multiplier() == 0.0

    def test_tier2_blocks_new_trades(self):
        dlb = _make_dlb()
        dlb._current_tier = 2
        assert dlb.should_block_new_trades() is True

    def test_tier1_does_not_block_new_trades(self):
        dlb = _make_dlb()
        dlb._current_tier = 1
        assert dlb.should_block_new_trades() is False

    def test_tier3_should_close_all(self):
        dlb = _make_dlb()
        dlb._current_tier = 3
        assert dlb.should_close_all() is True

    def test_tier2_should_not_close_all(self):
        dlb = _make_dlb()
        dlb._current_tier = 2
        assert dlb.should_close_all() is False


# ────────────────────────────────────────────────────────────
# Manual reset
# ────────────────────────────────────────────────────────────

class TestManualReset:

    def test_reset_clears_tier(self):
        dlb = _make_dlb()
        dlb._current_tier = 3
        dlb._halt_until = time.time() + 3600
        dlb._daily_start_balance = 9500.0

        dlb.reset()
        assert dlb._current_tier == 0
        assert dlb._halt_until == 0.0
        assert dlb._daily_start_balance == 0.0
