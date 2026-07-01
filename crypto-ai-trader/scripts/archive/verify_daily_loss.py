#!/usr/bin/env python3
"""
Verification script for the 3-Tier Daily Loss Circuit Breaker.

Tests:
  1. Tier 0 (no loss) → normal operation
  2. Tier 1 (1% loss) → position multiplier 0.5
  3. Tier 2 (2% loss) → block new trades
  4. Tier 3 (3% loss) → close all + halt
  5. Auto-reset on new UTC day
  6. Tier escalation (never de-escalates within day)
  7. get_status() returns correct structure
"""
import os
import sys
import tempfile
import time

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Use an isolated test database
_test_db = tempfile.mktemp(suffix=".db")
os.environ["STATE_DB_PATH"] = _test_db

from src.state_db import get_state_db
from src.daily_loss_breaker import DailyLossBreaker, get_daily_loss_breaker


def reset_state_db():
    """Reset the singleton and point to a fresh temp DB."""
    import src.state_db as sdb
    sdb._state_db_instance = None
    global _test_db
    _test_db = tempfile.mktemp(suffix=".db")
    os.environ["STATE_DB_PATH"] = _test_db
    return get_state_db()


def reset_breaker():
    """Get a fresh breaker instance."""
    import src.daily_loss_breaker as dlb_mod
    dlb_mod._dlb_instance = None
    return get_daily_loss_breaker()


passed = 0
failed = 0


def assert_eq(label, got, expected):
    global passed, failed
    if got == expected:
        print(f"  ✅ {label}: {got}")
        passed += 1
    else:
        print(f"  ❌ {label}: got {got!r}, expected {expected!r}")
        failed += 1


def assert_true(label, value):
    global passed, failed
    if value:
        print(f"  ✅ {label}: True")
        passed += 1
    else:
        print(f"  ❌ {label}: expected True, got {value!r}")
        failed += 1


def assert_false(label, value):
    global passed, failed
    if not value:
        print(f"  ✅ {label}: False")
        passed += 1
    else:
        print(f"  ❌ {label}: expected False, got {value!r}")
        failed += 1


# ═══════════════════════════════════════════
# Test 1: Tier 0 — no loss → normal
# ═══════════════════════════════════════════
print("\n── Test 1: Tier 0 (no loss) → normal ──")
reset_state_db()
dlb = reset_breaker()

result = dlb.check_daily_loss(portfolio_value=10000.0)
assert_eq("tier", result["tier"], 0)
assert_eq("action", result["action"], "none")
assert_eq("daily_pnl_pct", result["daily_pnl_pct"], 0.0)
assert_eq("reason", result["reason"], "Normal operation")
assert_eq("position_multiplier", dlb.get_position_size_multiplier(), 1.0)
assert_false("should_block_new_trades", dlb.should_block_new_trades())
assert_false("should_close_all", dlb.should_close_all())


# ═══════════════════════════════════════════
# Test 2: Tier 1 — 1% loss → multiplier 0.5
# ═══════════════════════════════════════════
print("\n── Test 2: Tier 1 (1% loss) → multiplier 0.5 ──")
reset_state_db()
dlb = reset_breaker()

# Set starting balance
result = dlb.check_daily_loss(portfolio_value=10000.0)
assert_eq("initial tier", result["tier"], 0)

# Now simulate 1.5% loss
result = dlb.check_daily_loss(portfolio_value=9850.0)
assert_eq("tier", result["tier"], 1)
assert_eq("action", result["action"], "defensive_mode")
assert_eq("position_multiplier", dlb.get_position_size_multiplier(), 0.5)
assert_false("should_block_new_trades", dlb.should_block_new_trades())
assert_false("should_close_all", dlb.should_close_all())
# Verify loss percentage is approximately -1.5%
assert_true("daily_pnl_pct approx -1.5%", abs(result["daily_pnl_pct"] - (-1.5)) < 0.01)


# ═══════════════════════════════════════════
# Test 3: Tier 2 — 2% loss → block trades
# ═══════════════════════════════════════════
print("\n── Test 3: Tier 2 (2% loss) → block trades ──")
reset_state_db()
dlb = reset_breaker()

result = dlb.check_daily_loss(portfolio_value=10000.0)  # set baseline
result = dlb.check_daily_loss(portfolio_value=9800.0)   # -2%
assert_eq("tier", result["tier"], 2)
assert_eq("action", result["action"], "block_new_trades")
assert_eq("position_multiplier", dlb.get_position_size_multiplier(), 0.0)
assert_true("should_block_new_trades", dlb.should_block_new_trades())
assert_false("should_close_all", dlb.should_close_all())


# ═══════════════════════════════════════════
# Test 4: Tier 3 — 3% loss → close all
# ═══════════════════════════════════════════
print("\n── Test 4: Tier 3 (3% loss) → close all ──")
reset_state_db()
dlb = reset_breaker()

result = dlb.check_daily_loss(portfolio_value=10000.0)  # set baseline
result = dlb.check_daily_loss(portfolio_value=9700.0)   # -3%
assert_eq("tier", result["tier"], 3)
assert_eq("action", result["action"], "close_all_and_halt")
assert_eq("position_multiplier", dlb.get_position_size_multiplier(), 0.0)
assert_true("should_block_new_trades", dlb.should_block_new_trades())
assert_true("should_close_all", dlb.should_close_all())

# Verify halt_until is set (within reasonable range)
status = dlb.get_status()
assert_true("halt_until set", status["halt_until"] > time.time())


# ═══════════════════════════════════════════
# Test 5: Auto-reset on new UTC day
# ═══════════════════════════════════════════
print("\n── Test 5: Auto-reset on new UTC day ──")
reset_state_db()
dlb = reset_breaker()

result = dlb.check_daily_loss(portfolio_value=10000.0)  # set baseline
result = dlb.check_daily_loss(portfolio_value=9700.0)   # -3% → tier 3
assert_eq("before reset tier", result["tier"], 3)

# Simulate new day by tampering _last_reset_date
dlb._last_reset_date = "2000-01-01"  # force different day
dlb._save_state()

# Reload to simulate fresh instance
dlb2 = get_daily_loss_breaker()
result2 = dlb2.check_daily_loss(portfolio_value=9700.0)

# After auto-reset, start_balance should be re-set to current portfolio
assert_eq("after reset tier", result2["tier"], 0)
assert_eq("after reset multiplier", dlb2.get_position_size_multiplier(), 1.0)
assert_false("after reset block", dlb2.should_block_new_trades())
assert_false("after reset close_all", dlb2.should_close_all())


# ═══════════════════════════════════════════
# Test 6: Tier escalation (never de-escalates within same day)
# ═══════════════════════════════════════════
print("\n── Test 6: Tier escalation (no de-escalation) ──")
reset_state_db()
dlb = reset_breaker()

dlb.check_daily_loss(portfolio_value=10000.0)    # baseline
dlb.check_daily_loss(portfolio_value=9900.0)     # -1% → tier 1
assert_eq("step 1 tier", dlb.get_status()["current_tier"], 1)

# Price recovers but tier should NOT de-escalate
dlb.check_daily_loss(portfolio_value=9990.0)     # -0.1% (recovered)
assert_eq("step 2 tier (should stay 1)", dlb.get_status()["current_tier"], 1)
assert_eq("step 2 multiplier", dlb.get_position_size_multiplier(), 0.5)


# ═══════════════════════════════════════════
# Test 7: get_status() returns correct structure
# ═══════════════════════════════════════════
print("\n── Test 7: get_status() structure ──")
reset_state_db()
dlb = reset_breaker()

dlb.check_daily_loss(portfolio_value=10000.0)
status = dlb.get_status()
expected_keys = {
    "current_tier", "daily_start_balance", "last_reset_date",
    "halt_until", "trip_count", "trip_history",
    "position_multiplier", "blocked", "close_all",
}
assert_eq("status keys", set(status.keys()), expected_keys)
assert_eq("current_tier type", type(status["current_tier"]).__name__, "int")
assert_eq("trip_history type", type(status["trip_history"]).__name__, "list")


# ═══════════════════════════════════════════
# Test 8: Manual reset
# ═══════════════════════════════════════════
print("\n── Test 8: Manual reset ──")
reset_state_db()
dlb = reset_breaker()

dlb.check_daily_loss(portfolio_value=10000.0)
dlb.check_daily_loss(portfolio_value=9600.0)  # -4% → tier 3
assert_eq("before reset tier", dlb.get_status()["current_tier"], 3)

dlb.reset()
assert_eq("after reset tier", dlb.get_status()["current_tier"], 0)
assert_eq("after reset multiplier", dlb.get_position_size_multiplier(), 1.0)


# ═══════════════════════════════════════════
# Test 9: State persistence across instances
# ═══════════════════════════════════════════
print("\n── Test 9: State persistence across instances ──")
reset_state_db()
dlb = reset_breaker()

dlb.check_daily_loss(portfolio_value=10000.0)
dlb.check_daily_loss(portfolio_value=9800.0)  # -2% → tier 2

# Create new instance (simulates process restart)
dlb_new = DailyLossBreaker()
assert_eq("restored tier", dlb_new.get_status()["current_tier"], 2)
assert_true("restored should_block", dlb_new.should_block_new_trades())


# ═══════════════════════════════════════════
# Test 10: trip_history recording
# ═══════════════════════════════════════════
print("\n── Test 10: trip_history recording ──")
reset_state_db()
dlb = reset_breaker()

dlb.check_daily_loss(portfolio_value=10000.0)
dlb.check_daily_loss(portfolio_value=9800.0)  # tier 0 → 2
dlb.check_daily_loss(portfolio_value=9700.0)  # tier 2 → 3

status = dlb.get_status()
assert_eq("trip count", status["trip_count"], 2)
assert_eq("trip 1 from_tier", status["trip_history"][0]["from_tier"], 0)
assert_eq("trip 1 to_tier", status["trip_history"][0]["to_tier"], 2)
assert_eq("trip 2 from_tier", status["trip_history"][1]["from_tier"], 2)
assert_eq("trip 2 to_tier", status["trip_history"][1]["to_tier"], 3)


# ═══════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════
print(f"\n{'═' * 50}")
print(f"Results: {passed} passed, {failed} failed")
if failed == 0:
    print("🎉 All tests passed!")
else:
    print("❌ Some tests failed")
    sys.exit(1)

# Cleanup
try:
    os.unlink(_test_db)
except OSError:
    pass
