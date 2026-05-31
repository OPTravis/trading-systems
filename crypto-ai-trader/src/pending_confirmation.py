"""
Pending Confirmation Manager
Stores the state of pending trading confirmations so that even after
session resets, the system can match user confirmations to pending requests.
"""

import json
import logging
import os
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)

PENDING_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", ".pending_confirmation.json"
)

# TTL: pending confirmations expire after this many hours
DEFAULT_TTL_HOURS = 4


def save_pending(data: Dict) -> bool:
    """Save pending confirmation to file.

    Expected data keys:
        symbol, price, strategy, score, signals,
        stop_loss_pct, tp_levels, stop_price, max_hold_hours
    """
    try:
        os.makedirs(os.path.dirname(PENDING_FILE), exist_ok=True)
        data["saved_at"] = datetime.now().isoformat()
        data["ttl_hours"] = DEFAULT_TTL_HOURS
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved pending confirmation: {data.get('symbol')}")
        return True
    except Exception as e:
        logger.error(f"Failed to save pending confirmation: {e}")
        return False


def load_pending() -> Optional[Dict]:
    """Load pending confirmation if exists and not expired."""
    if not os.path.exists(PENDING_FILE):
        return None

    try:
        with open(PENDING_FILE, encoding="utf-8") as f:
            data = json.load(f)

        saved_at = datetime.fromisoformat(data["saved_at"])
        age_hours = (datetime.now() - saved_at).total_seconds() / 3600
        ttl = data.get("ttl_hours", DEFAULT_TTL_HOURS)

        if age_hours > ttl:
            os.remove(PENDING_FILE)
            logger.info(
                f"Pending confirmation expired (age: {age_hours:.1f}h > {ttl}h)"
            )
            return None

        return data
    except Exception as e:
        logger.error(f"Failed to load pending confirmation: {e}")
        return None


def clear_pending(symbol: Optional[str] = None) -> bool:
    """Clear pending confirmation. If symbol is None, clear all."""
    if not os.path.exists(PENDING_FILE):
        return True

    try:
        if symbol is None:
            os.remove(PENDING_FILE)
            logger.info("Cleared pending confirmation (all)")
            return True

        with open(PENDING_FILE, encoding="utf-8") as f:
            data = json.load(f)

        if data.get("symbol") == symbol:
            os.remove(PENDING_FILE)
            logger.info(f"Cleared pending confirmation: {symbol}")
            return True
        else:
            logger.info(
                f"Pending is for {data.get('symbol')}, not {symbol} — not clearing"
            )
            return False
    except Exception as e:
        logger.error(f"Failed to clear pending confirmation: {e}")
        return False


def check_confirmation(user_input: str) -> tuple[bool, Optional[Dict], str]:
    """
    Check if user input matches a pending confirmation.

    Returns:
        (is_confirmation, pending_data, symbol_from_input)

    Parses inputs like:
        "YES NOMUSDT" -> (True, pending_data, "NOMUSDT")
        "YES BTCUSDT" -> (True, pending_data, "BTCUSDT")
        "NOMUSDT"     -> (True if symbol matches, pending_data, "NOMUSDT")
        "yes"         -> (True if pending exists, pending_data, None)
        other         -> (False, None, None)
    """
    user_input = user_input.strip().upper()

    # Load pending
    pending = load_pending()

    # Parse YES SYMBOL pattern
    symbol_from_input = None
    is_yes = False

    if user_input.startswith("YES "):
        is_yes = True
        symbol_from_input = user_input[4:].strip()
    elif user_input == "YES":
        is_yes = True
    elif user_input.endswith("USDT"):
        symbol_from_input = user_input

    if not is_yes and symbol_from_input is None:
        return False, None, ""

    if pending is None:
        # No pending, but user said YES something
        if symbol_from_input:
            return False, None, symbol_from_input
        return False, None, ""

    # Check if symbol matches (case insensitive)
    pending_symbol = pending.get("symbol", "").upper()

    if symbol_from_input and symbol_from_input != pending_symbol:
        # User said YES WRONGSYMBOL when pending is something else
        return False, pending, symbol_from_input

    # Match!
    return True, pending, pending_symbol


if __name__ == "__main__":
    # Test
    test_data = {
        "symbol": "NOMUSDT",
        "price": 0.009350,
        "strategy": "vwap",
        "score": 100,
        "signals": ["MACD Bullish", "Above VWAP"],
        "stop_loss_pct": 2.0,
        "tp_levels": [
            {"pct": 2.0, "size_pct": 33},
            {"pct": 3.0, "size_pct": 33},
            {"pct": 5.0, "size_pct": 34},
        ],
        "stop_price": 0.009163,
        "max_hold_hours": 24,
    }

    print("=== Testing Pending Confirmation Manager ===")
    save_pending(test_data)
    loaded = load_pending()
    print(f"Loaded: {loaded['symbol'] if loaded else None}")

    is_conf, pending, sym = check_confirmation("YES NOMUSDT")
    print(f"'YES NOMUSDT' -> is_conf={is_conf}, symbol={sym}")

    is_conf, pending, sym = check_confirmation("YES BTCUSDT")
    print(f"'YES BTCUSDT' -> is_conf={is_conf}, symbol={sym}")

    clear_pending()
    print(f"After clear: {load_pending()}")
