"""
TP/SL Order Tracker — tracks placed TP/SL orders so trailing-check can detect fills.

Stores per-symbol state in state.db kv table:
  tp_sl_tracker:{SYMBOL} = {
    entry_price: float,
    total_qty: float,
    tp_orders: [
      {order_id, price, qty, tier, pct, side: "LIMIT"},
      ...
    ],
    sl_order: {order_id, price, qty, stop_price} or None,
    tp_filled: [bool, ...],   # which TPs have been detected as filled
    sl_moved_after_tp: int,   # 0=none, 1=moved after TP1, 2=moved after TP2
    created_at, updated_at
  }
"""

import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_PREFIX = "tp_sl_tracker"


def _key(symbol: str) -> str:
    return f"{_PREFIX}:{symbol}"


def save_state(
    symbol: str,
    entry_price: float,
    total_qty: float,
    tp_orders: list,
    sl_order: Optional[dict],
) -> None:
    """Save TP/SL tracking state after placing orders at entry."""
    from src.state_db import get_state_db

    state = {
        "entry_price": entry_price,
        "total_qty": total_qty,
        "tp_orders": tp_orders,
        "sl_order": sl_order,
        "tp_filled": [False] * len(tp_orders),
        "sl_moved_after_tp": 0,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    db = get_state_db()
    db.kv_set(_key(symbol), state)  # kv_set does json.dumps internally
    logger.info(
        "TP/SL tracker saved %s: entry=%.6f %d TPs SL=%s",
        symbol, entry_price, len(tp_orders), "yes" if sl_order else "no",
    )


def get_state(symbol: str) -> Optional[dict]:
    """Get TP/SL tracking state for a symbol."""
    from src.state_db import get_state_db

    db = get_state_db()
    raw = db.kv_get(_key(symbol), None)
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    # Fallback: handle legacy double-encoded values
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def update_state(symbol: str, **kwargs) -> Optional[dict]:
    """Update specific fields in TP/SL state."""
    from src.state_db import get_state_db

    state = get_state(symbol)
    if state is None:
        logger.warning("Cannot update TP/SL state for %s: not found", symbol)
        return None
    state.update(kwargs)
    state["updated_at"] = time.time()
    db = get_state_db()
    db.kv_set(_key(symbol), state)  # kv_set does json.dumps internally
    return state


def remove_state(symbol: str) -> None:
    """Remove TP/SL tracking state (position closed)."""
    from src.state_db import get_state_db

    db = get_state_db()
    db.kv_remove(_key(symbol))


def get_all_tracked() -> dict:
    """Get all tracked symbols and their states. Returns {symbol: state_dict}."""
    from src.state_db import get_state_db

    db = get_state_db()
    try:
        conn = db._get_conn()
        rows = conn.execute(
            f"SELECT key, value FROM kv WHERE key LIKE '{_PREFIX}:%'"
        ).fetchall()
        result = {}
        for row in rows:
            sym = row["key"].replace(f"{_PREFIX}:", "")
            try:
                parsed = json.loads(row["value"])
                # Handle legacy double-encoded values
                if isinstance(parsed, str):
                    parsed = json.loads(parsed)
                result[sym] = parsed
            except (json.JSONDecodeError, TypeError):
                continue
        return result
    except Exception as e:
        logger.warning("Failed to get all tracked TP/SL states: %s", e)
        return {}
