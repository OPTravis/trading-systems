"""
Test suite for Portfolio + StateDB consistency.

Checks:
1. sync_from_binance properly persists to StateDB
2. Memory DB consistency after sync
3. Debounce doesn't skip critical saves
4. cash_balance synchronization between memory and DB
"""

import os
import sys
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.state_db import StateDB, get_state_db
from src.portfolio import PortfolioManager


def _make_mock_binance_client(balances, prices):
    """Create a mock BinanceClient that returns given balances and prices."""
    client = MagicMock()
    client.get_account.return_value = {"balances": balances}
    def _get_24hr_stats(symbol):
        return {"last_price": prices.get(symbol, 0)}
    client.get_24hr_stats.side_effect = _get_24hr_stats
    return client


def _make_db(tmp_path: Path) -> StateDB:
    db_path = tmp_path / "test_state.db"
    # Reset singleton so each test gets fresh instance
    import src.state_db as sdb
    sdb._state_db_instance = None
    return StateDB(str(db_path))


def test_sync_from_binance_persistence():
    """Test 1: sync_from_binance must persist positions + cash to StateDB."""
    tmp = Path(tempfile.mkdtemp())
    db = _make_db(tmp)

    balances = [
        {"asset": "USDT", "free": "1000.0", "locked": "0"},
        {"asset": "BTC",  "free": "0.05",  "locked": "0"},
        {"asset": "ETH",  "free": "1.5",   "locked": "0"},
    ]
    prices = {"BTCUSDT": 50000.0, "ETHUSDT": 3000.0}
    client = _make_mock_binance_client(balances, prices)

    pm = PortfolioManager(binance_client=client)
    pm._db = db
    # Reset state to empty before sync
    pm.positions = {}
    pm.cash_balance = 0
    pm._db = db

    # Monkey-patch get_avg_entry_price to avoid network call
    import src.portfolio as pf
    original_get_avg = None
    if hasattr(pf, 'get_avg_entry_price'):
        original_get_avg = pf.get_avg_entry_price
    pf.get_avg_entry_price = lambda c, s, q: prices.get(s, 0)

    try:
        result = pm.sync_from_binance(client)
        assert result is True, "sync_from_binance should return True"

        # Check memory
        assert "BTCUSDT" in pm.positions, "BTC position missing in memory"
        assert "ETHUSDT" in pm.positions, "ETH position missing in memory"
        assert pm.cash_balance == 1000.0, f"cash_balance should be 1000, got {pm.cash_balance}"

        # Check DB directly
        db_positions = db.portfolio_get_all()
        assert "BTCUSDT" in db_positions, "BTC position missing in DB"
        assert "ETHUSDT" in db_positions, "ETH position missing in DB"
        db_cash = db.portfolio_get_cash_balance()
        assert db_cash == 1000.0, f"DB cash_balance should be 1000, got {db_cash}"
    finally:
        if original_get_avg:
            pf.get_avg_entry_price = original_get_avg

    print("PASS: test_sync_from_binance_persistence")


def test_memory_db_consistency_after_add():
    """Test 2: After add_position, memory and DB must match."""
    tmp = Path(tempfile.mkdtemp())
    db = _make_db(tmp)

    pm = PortfolioManager(binance_client=None)
    pm._db = db
    pm.positions = {}
    pm.cash_balance = 5000.0
    pm._last_save_time = 0  # clear debounce

    pm.add_position("SOLUSDT", quantity=10.0, entry_price=20.0, strategy="test")

    # Memory
    assert "SOLUSDT" in pm.positions
    assert pm.positions["SOLUSDT"]["quantity"] == 10.0
    assert pm.cash_balance == 5000.0 - 10.0 * 20.0  # 4800

    # DB
    db_pos = db.portfolio_get("SOLUSDT")
    assert db_pos is not None, "SOLUSDT missing in DB after add_position"
    assert db_pos["quantity"] == 10.0
    db_cash = db.portfolio_get_cash_balance()
    assert db_cash == 4800.0, f"DB cash should be 4800, got {db_cash}"

    print("PASS: test_memory_db_consistency_after_add")


def test_debounce_does_not_skip_critical_save():
    """Test 3: Debounce should not skip saves when force=True or sync."""
    tmp = Path(tempfile.mkdtemp())
    db = _make_db(tmp)

    pm = PortfolioManager(binance_client=None)
    pm._db = db
    pm.positions = {}
    pm.cash_balance = 0.0
    pm._last_save_time = time.monotonic()  # simulate recent save

    # Normal save should be debounced
    pm.cash_balance = 999.0
    pm._save_state(force=False)
    # Might be skipped; that's OK for non-critical

    # Force save must NOT be debounced
    pm._save_state(force=True)
    db_cash = db.portfolio_get_cash_balance()
    assert db_cash == 999.0, f"force=True save was debounced! cash={db_cash}"

    # sync_from_binance resets _last_save_time=0 and calls force=True
    pm._last_save_time = time.monotonic()
    pm.cash_balance = 1234.0
    pm._last_save_time = 0
    pm._save_state(force=True)
    db_cash = db.portfolio_get_cash_balance()
    assert db_cash == 1234.0, f"sync-style save was debounced! cash={db_cash}"

    print("PASS: test_debounce_does_not_skip_critical_save")


def test_cash_balance_sync():
    """Test 4: update_balance must sync cash to DB."""
    tmp = Path(tempfile.mkdtemp())
    db = _make_db(tmp)

    pm = PortfolioManager(binance_client=None)
    pm._db = db
    pm.positions = {}
    pm.cash_balance = 0.0
    pm._last_save_time = 0

    pm.update_balance(7777.0)
    assert pm.cash_balance == 7777.0
    db_cash = db.portfolio_get_cash_balance()
    assert db_cash == 7777.0, f"DB cash mismatch after update_balance: {db_cash}"

    print("PASS: test_cash_balance_sync")


def test_close_position_removes_from_db():
    """Test 5: close_position must remove from DB and update cash."""
    tmp = Path(tempfile.mkdtemp())
    db = _make_db(tmp)

    pm = PortfolioManager(binance_client=None)
    pm._db = db
    pm.positions = {}
    pm.cash_balance = 5000.0
    pm._last_save_time = 0

    pm.add_position("ADAUSDT", quantity=1000.0, entry_price=1.0, strategy="test")
    assert db.portfolio_get("ADAUSDT") is not None

    pm.close_position("ADAUSDT", close_price=1.2)
    assert "ADAUSDT" not in pm.positions
    db_pos = db.portfolio_get("ADAUSDT")
    assert db_pos is None, "ADAUSDT still in DB after close"
    # cash should be 5000 - 1000 + 1200 = 5200
    assert pm.cash_balance == 5200.0
    db_cash = db.portfolio_get_cash_balance()
    assert db_cash == 5200.0, f"DB cash after close mismatch: {db_cash}"

    print("PASS: test_close_position_removes_from_db")


if __name__ == "__main__":
    test_sync_from_binance_persistence()
    test_memory_db_consistency_after_add()
    test_debounce_does_not_skip_critical_save()
    test_cash_balance_sync()
    test_close_position_removes_from_db()
    print("\nAll consistency tests passed!")
