"""Restricted symbols registry (account-level trading restrictions).

Binance lists some symbols (e.g. tokenized stocks/ETFs such as SNXXB, SNDKB,
SPCXB, MUB) that exist on the exchange with status=TRADING but are NOT
permitted for our account (compliance/eligibility opt-in required). Placing
orders on them fails with -2010 "This symbol is not permitted for this account".

This module maintains a persistent, self-learning registry of such symbols in
StateDB (kv table, key="restricted_symbols"), so:
  * DynamicCoinPool excludes them before scoring (no wasted slots / fake signals)
  * ccxt_client auto-registers any symbol rejected with "not permitted"

They are securities-backed products whose price behaviour (market hours gaps,
equity beta) is incompatible with our crypto momentum scoring anyway, so we
also ship a static seed list of known tokenized-security symbols.
"""

import json
import logging
import time
from typing import List, Set

logger = logging.getLogger(__name__)

KV_KEY = "restricted_symbols"
_CACHE_TTL = 60  # seconds

_cache: Set[str] = set()
_cache_ts: float = 0.0

# Static seed: known tokenized stocks/ETFs on Binance spot (cannot be traded
# by this account; securities-backed, not crypto-native).
TOKENIZED_SECURITIES = {
    "SNXXBUSDT",  # tokenized equity index
    "SNDKBUSDT",  # tokenized equity
    "SPCXBUSDT",  # tokenized S&P 500 tracker
    "MUBUSDT",    # tokenized muni-bond ETF
}


def get_restricted_symbols() -> Set[str]:
    """Return the union of static seeds and dynamically-learned symbols."""
    global _cache, _cache_ts
    now = time.time()
    if _cache and now - _cache_ts < _CACHE_TTL:
        return _cache | TOKENIZED_SECURITIES
    learned: Set[str] = set()
    try:
        from src.state_db import get_state_db

        raw = get_state_db().kv_get(KV_KEY, default=[])
        if isinstance(raw, str):
            raw = json.loads(raw)
        if isinstance(raw, list):
            learned = {str(s).upper() for s in raw if s}
    except Exception as e:  # DB unavailable -> fall back to static seeds only
        logger.warning("restricted_symbols: kv read failed (%s)", e)
    _cache = learned
    _cache_ts = now
    return learned | TOKENIZED_SECURITIES


def mark_symbol_restricted(symbol: str, reason: str = "") -> None:
    """Persist a symbol as restricted for this account (idempotent)."""
    global _cache, _cache_ts
    symbol = str(symbol).upper().strip()
    if not symbol:
        return
    try:
        from src.state_db import get_state_db

        db = get_state_db()
        raw = db.kv_get(KV_KEY, default=[])
        if isinstance(raw, str):
            raw = json.loads(raw)
        current = {str(s).upper() for s in (raw or []) if s}
        if symbol in current:
            return
        current.add(symbol)
        db.kv_set(KV_KEY, sorted(current))
        _cache = current
        _cache_ts = time.time()
        logger.warning(
            "restricted_symbols: %s added to account-restricted registry%s",
            symbol,
            f" ({reason})" if reason else "",
        )
    except Exception as e:
        logger.error("restricted_symbols: failed to persist %s (%s)", symbol, e)


def is_restricted(symbol: str) -> bool:
    return str(symbol).upper().strip() in get_restricted_symbols()


def is_not_permitted_error(err_text: str) -> bool:
    """Detect Binance -2010 'symbol not permitted for this account' errors."""
    t = str(err_text).lower()
    return "not permitted" in t or ("-2010" in t and "permitted" in t)
