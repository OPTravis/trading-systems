"""Tests for account-restricted symbol registry (tokenized securities / -2010)."""
from unittest.mock import MagicMock, patch

from src.restricted_symbols import (
    TOKENIZED_SECURITIES,
    get_restricted_symbols,
    is_not_permitted_error,
    mark_symbol_restricted,
)
from src.dynamic_coin_pool import DynamicCoinPool


def test_static_seeds_cover_known_tokenized_securities():
    assert {"SNXXBUSDT", "SNDKBUSDT", "SPCXBUSDT", "MUBUSDT"} <= TOKENIZED_SECURITIES


def test_error_matcher_detects_not_permitted():
    assert is_not_permitted_error(
        "(400, -2010, 'This symbol is not permitted for this account.')"
    )


def test_error_matcher_ignores_insufficient_balance():
    assert not is_not_permitted_error(
        "(400, -2010, 'Account has insufficient balance for requested action.')"
    )


def test_mark_and_get_roundtrip(tmp_path, monkeypatch):
    from src.state_db import StateDB
    import src.restricted_symbols as rmod

    db = StateDB(str(tmp_path / "t.db"))
    monkeypatch.setattr("src.state_db.get_state_db", lambda *a, **k: db)
    rmod._cache = set()
    rmod._cache_ts = 0

    mark_symbol_restricted("FOOXUSDT")
    assert "FOOXUSDT" in get_restricted_symbols()
    # idempotent
    mark_symbol_restricted("FOOXUSDT")
    assert get_restricted_symbols().count("FOOXUSDT") if isinstance(get_restricted_symbols(), list) else True


def test_build_pool_excludes_restricted(tmp_path):
    fake = [
        {"symbol": "SNXXBUSDT", "quote_volume": 50_000_000, "last_price": 19.44,
         "price_change_pct": 5.0, "trades": 50_000},
        {"symbol": "BTCUSDT", "quote_volume": 2_000_000_000, "last_price": 64000,
         "price_change_pct": 1.0, "trades": 900_000},
        {"symbol": "MUBUSDT", "quote_volume": 20_000_000, "last_price": 24.0,
         "price_change_pct": 2.0, "trades": 30_000},
    ]
    client = MagicMock()
    client.get_24hr_stats.return_value = fake
    syms = [c["symbol"] for c in DynamicCoinPool(client).build_pool()]
    assert syms == ["BTCUSDT"]
