#!/usr/bin/env python3
"""
Verification script for Step-wise Drawdown Response module.

Tests:
  - Each drawdown boundary (2.9%, 3%, 4.9%, 5%, 7.9%, 8%, 9.9%, 10%, 11%)
  - Time-based escalation from moderate → severe after 2h
  - Position size multipliers
  - Stop-loss tightening factors
  - Close-all logic
"""
import os
import sys
import time
import tempfile

# Add project root to path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

# Use an isolated temp database for testing
tmpdir = tempfile.mkdtemp()
os.environ["STATE_DB_PATH"] = os.path.join(tmpdir, "test_state.db")

from src.state_db import get_state_db
from src.stepwise_drawdown import (
    get_drawdown_action,
    get_position_size_multiplier,
    get_sl_tightening,
    should_close_all,
    _load_state,
    _save_state,
    _get_level_for_drawdown,
    LEVELS,
    ESCALATION_TIMEOUT_SECONDS,
)

db = get_state_db()
passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        msg = f"  ✗ FAIL: {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)


def reset_db():
    """Clear stepwise state between tests."""
    db.kv_remove("stepwise_drawdown:state")


def test_boundary_levels():
    """Test each boundary value maps to the correct level."""
    print("\n[Test: Boundary Levels]")
    reset_db()

    cases = [
        (0.0,   "normal"),
        (1.5,   "normal"),
        (2.9,   "normal"),
        (3.0,   "mild"),
        (4.0,   "mild"),
        (4.9,   "mild"),
        (5.0,   "moderate"),
        (6.5,   "moderate"),
        (7.9,   "moderate"),
        (8.0,   "severe"),
        (9.0,   "severe"),
        (9.9,   "severe"),
        (10.0,  "critical"),
        (11.0,  "critical"),
        (15.0,  "critical"),
        (100.0, "critical"),
    ]

    for pct, expected_level in cases:
        action = get_drawdown_action(pct, db=db)
        check(
            f"drawdown={pct}% → level='{expected_level}'",
            action["level"] == expected_level,
            f"got '{action['level']}'",
        )


def test_size_multipliers():
    """Test position size multiplier at each drawdown level."""
    print("\n[Test: Size Multipliers]")
    reset_db()

    cases = [
        (0.0,  1.0),
        (2.9,  1.0),
        (3.0,  0.7),
        (4.9,  0.7),
        (5.0,  0.4),
        (7.9,  0.4),
        (8.0,  0.0),
        (9.9,  0.0),
        (10.0, 0.0),
        (11.0, 0.0),
    ]

    for pct, expected_mult in cases:
        mult = get_position_size_multiplier(pct)
        check(
            f"size_mult at {pct}% = {expected_mult}",
            abs(mult - expected_mult) < 1e-9,
            f"got {mult}",
        )


def test_sl_tightening():
    """Test stop-loss tightening factor at each drawdown level."""
    print("\n[Test: SL Tightening]")
    reset_db()

    cases = [
        (0.0,  1.0),
        (2.9,  1.0),
        (3.0,  1.0),   # mild: no tightening yet
        (4.9,  1.0),
        (5.0,  0.7),   # moderate: tighten
        (7.9,  0.7),
        (8.0,  0.5),   # severe: tightest
        (9.9,  0.5),
        (10.0, 0.5),   # critical: same as severe
        (11.0, 0.5),
    ]

    for pct, expected_sl in cases:
        sl = get_sl_tightening(pct)
        check(
            f"sl_tightening at {pct}% = {expected_sl}",
            abs(sl - expected_sl) < 1e-9,
            f"got {sl}",
        )


def test_block_and_close():
    """Test block_new_trades and close_all flags."""
    print("\n[Test: Block & Close Flags]")
    reset_db()

    # Normal: no block, no close
    action = get_drawdown_action(1.0, db=db)
    check("normal: block=False", action["block_new_trades"] is False)
    check("normal: close_all=False", action["close_all"] is False)

    # Mild: no block, no close
    action = get_drawdown_action(4.0, db=db)
    check("mild: block=False", action["block_new_trades"] is False)
    check("mild: close_all=False", action["close_all"] is False)

    # Moderate: no block, no close
    action = get_drawdown_action(6.5, db=db)
    check("moderate: block=False", action["block_new_trades"] is False)
    check("moderate: close_all=False", action["close_all"] is False)

    # Severe: block, no close
    action = get_drawdown_action(9.0, db=db)
    check("severe: block=True", action["block_new_trades"] is True)
    check("severe: close_all=False", action["close_all"] is False)

    # Critical: block and close
    action = get_drawdown_action(10.5, db=db)
    check("critical: block=True", action["block_new_trades"] is True)
    check("critical: close_all=True", action["close_all"] is True)


def test_should_close_all():
    """Test should_close_all standalone function."""
    print("\n[Test: should_close_all]")
    reset_db()

    check("should_close_all(5%) = False", should_close_all(5.0) is False)
    check("should_close_all(9.9%) = False", should_close_all(9.9) is False)
    check("should_close_all(10%) = True", should_close_all(10.0) is True)
    check("should_close_all(11%) = True", should_close_all(11.0) is True)


def test_level_transitions_log():
    """Test that level transitions update state and log."""
    print("\n[Test: Level Transitions & State]")
    reset_db()

    # Start at normal
    action = get_drawdown_action(1.0, db=db, now=1000.0)
    check("initial level = normal", action["level"] == "normal")

    state = _load_state(db)
    check("state.current_level = normal", state["current_level"] == "normal")

    # Transition to mild
    action = get_drawdown_action(4.0, db=db, now=2000.0)
    check("after 4%: level = mild", action["level"] == "mild")

    state = _load_state(db)
    check("state.current_level = mild", state["current_level"] == "mild")
    check("state.level_entry_time = 2000", state["level_entry_time"] == 2000.0)

    # Transition to moderate
    action = get_drawdown_action(6.0, db=db, now=3000.0)
    check("after 6%: level = moderate", action["level"] == "moderate")

    state = _load_state(db)
    check("state.current_level = moderate", state["current_level"] == "moderate")
    check("state.level_entry_time = 3000", state["level_entry_time"] == 3000.0)

    # Stay in moderate (same level, no transition)
    action = get_drawdown_action(6.5, db=db, now=3500.0)
    check("staying moderate: level = moderate", action["level"] == "moderate")
    check("staying moderate: escalated=False", action["escalated"] is False)

    state = _load_state(db)
    check("no transition: entry_time still 3000", state["level_entry_time"] == 3000.0)

    # Recovery back to normal
    action = get_drawdown_action(2.0, db=db, now=4000.0)
    check("recovery to 2%: level = normal", action["level"] == "normal")

    state = _load_state(db)
    check("state.current_level = normal", state["current_level"] == "normal")


def test_time_based_escalation():
    """Test that moderate zone escalates to severe after 2 hours."""
    print("\n[Test: Time-Based Escalation]")
    reset_db()

    entry_time = 1000.0

    # Enter moderate zone
    action = get_drawdown_action(6.0, db=db, now=entry_time)
    check("enter moderate: level = moderate", action["level"] == "moderate")
    check("enter moderate: escalated = False", action["escalated"] is False)

    state = _load_state(db)
    check("moderate entry_time set", state["level_entry_time"] == entry_time)

    # After 1 hour (< 2h) — no escalation
    action = get_drawdown_action(6.0, db=db, now=entry_time + 3600)
    check("after 1h: still moderate", action["level"] == "moderate")
    check("after 1h: escalated=False", action["escalated"] is False)

    # After exactly 2 hours — should escalate
    action = get_drawdown_action(
        6.0, db=db, now=entry_time + ESCALATION_TIMEOUT_SECONDS + 1
    )
    check(
        "after >2h: escalated to severe",
        action["level"] == "severe",
        f"got '{action['level']}'",
    )
    check("after >2h: escalated=True", action["escalated"] is True)
    check("after >2h: block_new_trades=True", action["block_new_trades"] is True)

    # Verify escalation flag prevents double-escalation
    state = _load_state(db)
    check("escalated flag set", state.get("escalated") is True)

    # Calling again at same level shouldn't re-escalate
    action = get_drawdown_action(
        6.0, db=db, now=entry_time + ESCALATION_TIMEOUT_SECONDS + 100
    )
    check("second call: still severe (not double-escalated)", action["level"] == "severe")


def test_escalation_only_for_moderate():
    """Test that time-based escalation only applies to moderate zone."""
    print("\n[Test: Escalation Only for Moderate]")
    reset_db()

    # Normal zone should NOT escalate with time
    action = get_drawdown_action(1.0, db=db, now=1000.0)
    action = get_drawdown_action(
        1.0, db=db, now=1000.0 + ESCALATION_TIMEOUT_SECONDS + 100
    )
    check("normal zone: no escalation", action["level"] == "normal")
    check("normal zone: escalated=False", action["escalated"] is False)

    # Mild zone should NOT escalate with time
    action = get_drawdown_action(4.0, db=db, now=1000.0)
    action = get_drawdown_action(
        4.0, db=db, now=1000.0 + ESCALATION_TIMEOUT_SECONDS + 100
    )
    check("mild zone: no escalation", action["level"] == "mild")
    check("mild zone: escalated=False", action["escalated"] is False)

    # Severe zone should NOT escalate with time
    action = get_drawdown_action(9.0, db=db, now=1000.0)
    action = get_drawdown_action(
        9.0, db=db, now=1000.0 + ESCALATION_TIMEOUT_SECONDS + 100
    )
    check("severe zone: no escalation", action["level"] == "severe")
    check("severe zone: escalated=False", action["escalated"] is False)


def test_reason_strings():
    """Test that reason strings are present and meaningful."""
    print("\n[Test: Reason Strings]")
    reset_db()

    action = get_drawdown_action(1.0, db=db)
    check("normal has reason", "normal" in action["reason"].lower() or "0-3%" in action["reason"])

    action = get_drawdown_action(4.0, db=db)
    check("mild has reason", len(action["reason"]) > 10)

    action = get_drawdown_action(6.0, db=db)
    check("moderate has reason", len(action["reason"]) > 10)

    action = get_drawdown_action(9.0, db=db)
    check("severe has reason", len(action["reason"]) > 10)

    action = get_drawdown_action(11.0, db=db)
    check("critical has reason", len(action["reason"]) > 10)


def test_state_persistence():
    """Test that state survives across get_drawdown_action calls."""
    print("\n[Test: State Persistence]")
    reset_db()

    # Set up state
    action = get_drawdown_action(5.5, db=db, now=1000.0)

    # Load raw state and verify structure
    state = _load_state(db)
    check("state has current_level", "current_level" in state)
    check("state has level_entry_time", "level_entry_time" in state)
    check("state has escalated", "escalated" in state)
    check("state persisted to kv store", db.kv_get("stepwise_drawdown:state") is not None)


def test_full_api():
    """Test the full API returns correct structure."""
    print("\n[Test: Full API Structure]")
    reset_db()

    action = get_drawdown_action(5.0, db=db)

    check("action has 'level'", "level" in action)
    check("action has 'size_multiplier'", "size_multiplier" in action)
    check("action has 'sl_tightening'", "sl_tightening" in action)
    check("action has 'block_new_trades'", "block_new_trades" in action)
    check("action has 'close_all'", "close_all" in action)
    check("action has 'reason'", "reason" in action)
    check("action has 'time_in_level'", "time_in_level" in action)
    check("action has 'escalated'", "escalated" in action)

    check("level is string", isinstance(action["level"], str))
    check("size_multiplier is float", isinstance(action["size_multiplier"], float))
    check("sl_tightening is float", isinstance(action["sl_tightening"], float))
    check("block_new_trades is bool", isinstance(action["block_new_trades"], bool))
    check("close_all is bool", isinstance(action["close_all"], bool))
    check("reason is str", isinstance(action["reason"], str))
    check("time_in_level is float", isinstance(action["time_in_level"], float))
    check("escalated is bool", isinstance(action["escalated"], bool))


if __name__ == "__main__":
    print("=" * 60)
    print("Step-wise Drawdown Response — Verification Tests")
    print("=" * 60)

    test_boundary_levels()
    test_size_multipliers()
    test_sl_tightening()
    test_block_and_close()
    test_should_close_all()
    test_level_transitions_log()
    test_time_based_escalation()
    test_escalation_only_for_moderate()
    test_reason_strings()
    test_state_persistence()
    test_full_api()

    print("\n" + "=" * 60)
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if failed == 0:
        print("ALL TESTS PASSED ✓")
    else:
        print("SOME TESTS FAILED ✗")
        sys.exit(1)
