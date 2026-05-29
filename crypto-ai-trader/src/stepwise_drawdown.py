"""
Step-wise Drawdown Response - Progressive risk reduction based on drawdown from high watermark.

Unlike DrawdownBreaker which has a single 10% hard stop, this module provides
graduated responses at multiple drawdown thresholds:

  0-3%  : normal (no action)
  3-5%  : reduce new position sizes by 30%
  5-8%  : reduce new position sizes by 60%, increase SL tightness
  8-10% : block new trades, only allow exits
  >10%  : close all positions (same as DrawdownBreaker)

Time-based escalation: if in 5-8% zone for >2h, automatically escalate to next level.

STORAGE: StateDB kv store (key: 'stepwise_drawdown:state').
"""
import json
import logging
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Drawdown level definitions
LEVELS = {
    "normal": {
        "min_pct": 0.0,
        "max_pct": 3.0,
        "size_multiplier": 1.0,
        "sl_tightening": 1.0,
        "block_new_trades": False,
        "close_all": False,
        "reason": "Drawdown within normal range (0-3%)",
    },
    "mild": {
        "min_pct": 3.0,
        "max_pct": 5.0,
        "size_multiplier": 0.7,
        "sl_tightening": 1.0,
        "block_new_trades": False,
        "close_all": False,
        "reason": "Mild drawdown detected (3-5%): reducing position sizes by 30%",
    },
    "moderate": {
        "min_pct": 5.0,
        "max_pct": 8.0,
        "size_multiplier": 0.4,
        "sl_tightening": 0.7,
        "block_new_trades": False,
        "close_all": False,
        "reason": "Moderate drawdown (5-8%): reducing sizes by 60%, tightening stops",
    },
    "severe": {
        "min_pct": 8.0,
        "max_pct": 10.0,
        "size_multiplier": 0.0,
        "sl_tightening": 0.5,
        "block_new_trades": True,
        "close_all": False,
        "reason": "Severe drawdown (8-10%): blocking new trades, exits only",
    },
    "critical": {
        "min_pct": 10.0,
        "max_pct": float("inf"),
        "size_multiplier": 0.0,
        "sl_tightening": 0.5,
        "block_new_trades": True,
        "close_all": True,
        "reason": "Critical drawdown (>10%): closing all positions immediately",
    },
}

# Ordered level names for transition tracking
LEVEL_ORDER = ["normal", "mild", "moderate", "severe", "critical"]

# Time-based escalation: 2 hours in moderate zone triggers escalation
ESCALATION_TIMEOUT_SECONDS = 2 * 60 * 60  # 7200 seconds

STATE_KEY = "stepwise_drawdown:state"


def _get_level_for_drawdown(drawdown_pct: float) -> str:
    """Determine the drawdown level name for a given drawdown percentage."""
    if drawdown_pct < 3.0:
        return "normal"
    elif drawdown_pct < 5.0:
        return "mild"
    elif drawdown_pct < 8.0:
        return "moderate"
    elif drawdown_pct < 10.0:
        return "severe"
    else:
        return "critical"


def _load_state(db) -> Dict:
    """Load state from StateDB kv store."""
    default = {
        "current_level": "normal",
        "level_entry_time": time.time(),
        "time_in_current_level": 0.0,
        "escalated": False,
    }
    try:
        stored = db.kv_get(STATE_KEY)
        if stored and isinstance(stored, dict):
            # Merge with defaults for forward compatibility
            for k, v in default.items():
                if k not in stored:
                    stored[k] = v
            return stored
    except Exception as e:
        logger.error("StepwiseDrawdown: failed to load state from kv: %s", e)
    return default


def _save_state(db, state: Dict):
    """Persist state to StateDB kv store."""
    try:
        db.kv_set(STATE_KEY, state)
    except Exception as e:
        logger.error("StepwiseDrawdown: failed to save state: %s", e)


def _check_time_escalation(state: Dict, current_level: str, now: float) -> Optional[str]:
    """Check if we should escalate due to time in moderate zone.

    Returns the escalated level name, or None if no escalation needed.
    Only applies to the 'moderate' level (5-8%).

    Args:
        state: Current persisted state dict.
        current_level: Current detected drawdown level name.
        now: Current timestamp (passed from caller for testability).
    """
    if current_level != "moderate":
        return None

    time_in_level = now - state.get("level_entry_time", now)

    if time_in_level > ESCALATION_TIMEOUT_SECONDS and not state.get("escalated", False):
        return "severe"
    return None


def get_drawdown_action(drawdown_pct: float, db=None, now: float = None) -> Dict:
    """Get the action to take based on current drawdown percentage.

    Args:
        drawdown_pct: Current drawdown from high watermark (as percentage, e.g. 5.3 for 5.3%).
        db: StateDB instance (optional, will get singleton if None).
        now: Current timestamp (optional, for testing).

    Returns:
        Dict with keys:
            level: str - Current level name
            size_multiplier: float - Position size multiplier (0.0 - 1.0)
            sl_tightening: float - Stop-loss tightening factor (0.5 - 1.0)
            block_new_trades: bool - Whether new trades are blocked
            close_all: bool - Whether all positions should be closed
            reason: str - Human-readable reason
            time_in_level: float - Seconds spent in current level
            escalated: bool - Whether this was a time-based escalation
    """
    if db is None:
        from src.state_db import get_state_db
        db = get_state_db()

    if now is None:
        now = time.time()

    state = _load_state(db)
    detected_level = _get_level_for_drawdown(drawdown_pct)
    level_config = LEVELS[detected_level]

    # Check time-based escalation
    escalated = False
    escalated_level = _check_time_escalation(state, detected_level, now)
    if escalated_level is not None:
        detected_level = escalated_level
        level_config = LEVELS[detected_level]
        escalated = True
        logger.warning(
            "StepwiseDrawdown: TIME ESCALATION from moderate → %s "
            "(spent >2h in moderate zone)",
            detected_level,
        )

    # If the state was previously escalated, don't downgrade back to
    # a lower level — stay at least at the escalated level until
    # the drawdown drops below the original escalated-from zone (< 5%).
    previous_level = state.get("current_level", "normal")
    if state.get("escalated", False) and not escalated:
        # "moderate" is the zone from which escalation originates
        if detected_level in ("moderate", "severe", "critical"):
            # Still in escalated territory — hold the escalated level
            prev_idx = LEVEL_ORDER.index(previous_level) if previous_level in LEVEL_ORDER else 0
            det_idx = LEVEL_ORDER.index(detected_level) if detected_level in LEVEL_ORDER else 0
            if det_idx < prev_idx:
                detected_level = previous_level
                level_config = LEVELS[detected_level]
        else:
            # Drawdown dropped below moderate zone — allow recovery
            state["escalated"] = False
            _save_state(db, state)

    # Log level transitions
    if detected_level != previous_level:
        logger.warning(
            "StepwiseDrawdown: LEVEL TRANSITION %s → %s "
            "(drawdown=%.1f%%, escalating=%s)",
            previous_level,
            detected_level,
            drawdown_pct,
            escalated,
        )
        state["current_level"] = detected_level
        state["level_entry_time"] = now
        state["escalated"] = escalated
        _save_state(db, state)

    # Calculate time in current level
    time_in_level = now - state.get("level_entry_time", now)

    return {
        "level": detected_level,
        "size_multiplier": level_config["size_multiplier"],
        "sl_tightening": level_config["sl_tightening"],
        "block_new_trades": level_config["block_new_trades"],
        "close_all": level_config["close_all"],
        "reason": level_config["reason"],
        "time_in_level": time_in_level,
        "escalated": escalated,
    }


def get_position_size_multiplier(drawdown_pct: float) -> float:
    """Get position size multiplier for given drawdown (no state persistence).

    Args:
        drawdown_pct: Current drawdown percentage.

    Returns:
        Float multiplier between 0.0 and 1.0.
    """
    level = _get_level_for_drawdown(drawdown_pct)
    return LEVELS[level]["size_multiplier"]


def get_sl_tightening(drawdown_pct: float) -> float:
    """Get stop-loss tightening factor for given drawdown (no state persistence).

    Args:
        drawdown_pct: Current drawdown percentage.

    Returns:
        Float factor: 1.0 normally, 0.7 at 5-8%, 0.5 at 8-10%.
    """
    level = _get_level_for_drawdown(drawdown_pct)
    return LEVELS[level]["sl_tightening"]


def should_close_all(drawdown_pct: float) -> bool:
    """Check if all positions should be closed at given drawdown.

    Args:
        drawdown_pct: Current drawdown percentage.

    Returns:
        True if drawdown >= 10%.
    """
    return drawdown_pct >= 10.0
