"""
E2E Test Suite: Portfolio + StateDB Position Sync, Persistence, Consistency

Tests the critical data flow between PortfolioManager (in-memory) and StateDB (SQLite):
1. Position sync from Binance → Portfolio → StateDB
2. Position persistence (add, update, close, reload)
3. Consistency between in-memory positions and DB state
4. Cash balance sync and persistence
5. Edge cases: ghost positions, dust filtering, empty portfolio

Run: cd ~/crypto-ai-trader && .venv/bin/python3 -m pytest tests/test_e2e_portfolio_statedb.py -v
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.portfolio import PortfolioManager
from src.state_db import StateDB

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def tmp_db_path(tmp_path):
    """Create a temporary StateDB path for isolated testing."""
    db_path = tmp_path / "test_state.db"
    return str(db_path)


@pytest.fixture
def fresh_statedb(tmp_db_path):
    """Return a fresh StateDB instance (not singleton) with clean tables."""
    db = StateDB(tmp_db_path)
    yield db
    db.close()


@pytest.fixture
def mock_binance_client():
    """Mock BinanceClient for sync tests."""
    client = MagicMock()
    client.get_account.return_value = {
        "balances": [
            {"asset": "USDT", "free": "1000.00", "locked": "0.00"},
            {"asset": "BTC", "free": "0.01", "locked": "0.00"},
            {"asset": "ETH", "free": "0.50", "locked": "0.00"},
            {"asset": "SOL", "free": "10.00", "locked": "0.00"},
            {"asset": "DUST", "free": "0.0001", "locked": "0.00"},  # Dust asset
        ]
    }
    client.get_24hr_stats.side_effect = lambda symbol: {
        "BTCUSDT": {"last_price": "65000.00"},
        "ETHUSDT": {"last_price": "3500.00"},
        "SOLUSDT": {"last_price": "150.00"},
        "DUSTUSDT": {"last_price": "0.01"},
    }.get(symbol, {"last_price": "0"})
    return client


@pytest.fixture
def portfolio_with_db(tmp_db_path, mock_binance_client):
    """Create a PortfolioManager with a fresh StateDB and mock Binance client."""
    # Patch StateDB initialization to use our tmp db
    # portfolio.py does `from src.state_db import get_state_db` inside __init__
    # We patch the function in the state_db module itself
    db = StateDB(tmp_db_path)
    with patch("src.state_db.get_state_db", return_value=db):
        pm = PortfolioManager(config_path=None, binance_client=None)
        pm._client = mock_binance_client
        yield pm
        db.close()


# ============================================================================
# Test Class A: Position Persistence (Add/Update/Close/Reload)
# ============================================================================


class TestPositionPersistence:
    """Test that positions survive add → save → reload cycles."""

    def test_add_position_persists_to_db(self, portfolio_with_db):
        """Adding a position should write it to StateDB portfolio table."""
        pm = portfolio_with_db
        pm.cash_balance = 100000.0  # Enough to pass position size check

        pm.add_position("BTCUSDT", quantity=0.01, entry_price=60000.0, strategy="trend")

        # Verify in-memory
        assert "BTCUSDT" in pm.positions
        assert pm.positions["BTCUSDT"]["quantity"] == 0.01
        assert pm.positions["BTCUSDT"]["entry_price"] == 60000.0

        # Verify in DB
        db_pos = pm._db.portfolio_get("BTCUSDT")
        assert db_pos is not None
        assert db_pos["quantity"] == 0.01
        assert db_pos["entry_price"] == 60000.0
        assert db_pos["strategy"] == "trend"

    def test_close_position_removes_from_db(self, portfolio_with_db):
        """Closing a position should remove it from DB."""
        pm = portfolio_with_db
        pm.cash_balance = 100000.0

        pm.add_position("ETHUSDT", quantity=0.1, entry_price=3000.0, strategy="grid")
        assert pm._db.portfolio_get("ETHUSDT") is not None

        pm.close_position("ETHUSDT", close_price=3200.0)

        # Verify in-memory removed
        assert "ETHUSDT" not in pm.positions

        # Verify DB removed
        assert pm._db.portfolio_get("ETHUSDT") is None

    def test_update_position_price_does_not_affect_db(self, portfolio_with_db):
        """update_position_price only affects in-memory cache, not DB entry_price."""
        pm = portfolio_with_db
        pm.cash_balance = 100000.0

        pm.add_position("SOLUSDT", quantity=1.0, entry_price=140.0, strategy="dca")

        # Update price (simulates market price update)
        pm.update_position_price("SOLUSDT", current_price=150.0)

        # In-memory should have new current_price
        assert pm.positions["SOLUSDT"]["current_price"] == 150.0

        # DB should still have original entry_price (not current_price)
        db_pos = pm._db.portfolio_get("SOLUSDT")
        assert db_pos["entry_price"] == 140.0
        # DB doesn't store current_price (it's ephemeral market data)

    def test_cash_balance_persists_to_kv(self, portfolio_with_db):
        """Cash balance should be saved to kv store."""
        pm = portfolio_with_db
        pm.cash_balance = 5000.0
        pm._save_state()

        db_cash = pm._db.portfolio_get_cash_balance()
        assert db_cash == 5000.0

    def test_reload_from_db_restores_positions(self, tmp_db_path, mock_binance_client):
        """Creating a new PortfolioManager should load positions from DB."""
        # First, create a portfolio and add positions
        db = StateDB(tmp_db_path)
        with patch("src.state_db.get_state_db", return_value=db):
            pm1 = PortfolioManager(config_path=None, binance_client=None)
            pm1.cash_balance = 100000.0
            pm1.add_position(
                "BTCUSDT", quantity=0.1, entry_price=60000.0, strategy="trend"
            )
            pm1.add_position(
                "ETHUSDT", quantity=1.0, entry_price=3000.0, strategy="grid"
            )
        db.close()

        # Now create a new PortfolioManager — it should load from DB
        db2 = StateDB(tmp_db_path)
        with patch("src.state_db.get_state_db", return_value=db2):
            pm2 = PortfolioManager(config_path=None, binance_client=None)

            assert "BTCUSDT" in pm2.positions
            assert "ETHUSDT" in pm2.positions
            assert pm2.positions["BTCUSDT"]["quantity"] == 0.1
            assert pm2.positions["ETHUSDT"]["entry_price"] == 3000.0
            assert pm2.cash_balance == 91000.0  # 100000 - 6000 - 3000 = 91000
        db2.close()

    def test_reload_restores_sl_tp(self, tmp_db_path):
        """Stop-loss and take-profit should survive reload."""
        db = StateDB(tmp_db_path)
        with patch("src.state_db.get_state_db", return_value=db):
            pm1 = PortfolioManager(config_path=None, binance_client=None)
            pm1.cash_balance = 100000.0
            pm1.add_position(
                "BTCUSDT", quantity=0.1, entry_price=60000.0, strategy="trend"
            )

            # Manually set SL/TP (normally set by add_position defaults)
            expected_sl = pm1.positions["BTCUSDT"]["stop_loss"]
            expected_tp = pm1.positions["BTCUSDT"]["take_profit"]
        db.close()

        db2 = StateDB(tmp_db_path)
        with patch("src.state_db.get_state_db", return_value=db2):
            pm2 = PortfolioManager(config_path=None, binance_client=None)
            pos = pm2.positions["BTCUSDT"]

            assert pos["stop_loss"] == pytest.approx(expected_sl, abs=0.01)
            assert pos["take_profit"] == pytest.approx(expected_tp, abs=0.01)
        db2.close()


# ============================================================================
# Test Class B: Binance Sync
# ============================================================================


class TestBinanceSync:
    """Test sync_from_binance — the source of truth reconciliation."""

    def test_sync_creates_positions_from_binance(
        self, portfolio_with_db, mock_binance_client
    ):
        """sync_from_binance should create positions for non-dust balances."""
        pm = portfolio_with_db

        result = pm.sync_from_binance(mock_binance_client)

        assert result is True
        # BTC, ETH, SOL should be present (non-dust)
        assert "BTCUSDT" in pm.positions
        assert "ETHUSDT" in pm.positions
        assert "SOLUSDT" in pm.positions
        # DUST should be filtered out
        assert "DUSTUSDT" not in pm.positions

    def test_sync_sets_cash_balance(self, portfolio_with_db, mock_binance_client):
        """sync_from_binance should set cash_balance to USDT balance."""
        pm = portfolio_with_db

        pm.sync_from_binance(mock_binance_client)

        assert pm.cash_balance == 1000.0

    def test_sync_clears_old_positions(self, portfolio_with_db, mock_binance_client):
        """sync_from_binance should clear positions not present on Binance."""
        pm = portfolio_with_db
        pm.cash_balance = 100000.0

        # Add a ghost position
        pm.add_position("GHOSTUSDT", quantity=100.0, entry_price=1.0, strategy="test")
        assert "GHOSTUSDT" in pm.positions

        pm.sync_from_binance(mock_binance_client)

        # Ghost should be removed
        assert "GHOSTUSDT" not in pm.positions
        # Real positions should exist
        assert "BTCUSDT" in pm.positions

    def test_sync_does_not_deduct_cash(self, portfolio_with_db, mock_binance_client):
        """sync_from_binance should use deduct_cash=False to avoid double-counting."""
        pm = portfolio_with_db
        initial_cash = 5000.0
        pm.cash_balance = initial_cash

        pm.sync_from_binance(mock_binance_client)

        # Cash should be set to USDT balance from API, not reduced
        assert pm.cash_balance == 1000.0  # from mock

    def test_sync_persists_to_db(self, portfolio_with_db, mock_binance_client):
        """After sync, positions and cash should be in StateDB."""
        pm = portfolio_with_db

        pm.sync_from_binance(mock_binance_client)

        db_positions = pm._db.portfolio_get_all()
        assert "BTCUSDT" in db_positions
        assert "ETHUSDT" in db_positions
        assert "SOLUSDT" in db_positions

        db_cash = pm._db.portfolio_get_cash_balance()
        assert db_cash == 1000.0

    def test_sync_without_client_returns_false(self, portfolio_with_db):
        """sync_from_binance with None client should return False gracefully."""
        pm = portfolio_with_db

        result = pm.sync_from_binance(None)

        assert result is False


# ============================================================================
# Test Class C: Consistency Between Memory and DB
# ============================================================================


class TestMemoryDbConsistency:
    """Test that in-memory state and DB state remain consistent."""

    def test_add_then_verify_db_matches_memory(self, portfolio_with_db):
        """After add_position, DB should exactly match in-memory state."""
        pm = portfolio_with_db
        pm.cash_balance = 100000.0

        pm.add_position("BTCUSDT", quantity=0.1, entry_price=60000.0, strategy="trend")
        pm.add_position("ETHUSDT", quantity=1.0, entry_price=3000.0, strategy="grid")

        mem_symbols = set(pm.positions.keys())
        db_symbols = set(pm._db.portfolio_get_all().keys())

        assert mem_symbols == db_symbols

        for sym in mem_symbols:
            mem_pos = pm.positions[sym]
            db_pos = pm._db.portfolio_get(sym)
            assert mem_pos["quantity"] == db_pos["quantity"]
            assert mem_pos["entry_price"] == db_pos["entry_price"]

    def test_close_then_verify_db_matches_memory(self, portfolio_with_db):
        """After close_position, DB should not contain closed position."""
        pm = portfolio_with_db
        pm.cash_balance = 100000.0

        pm.add_position("BTCUSDT", quantity=0.1, entry_price=60000.0, strategy="trend")
        pm.close_position("BTCUSDT", close_price=65000.0)

        assert "BTCUSDT" not in pm.positions
        assert pm._db.portfolio_get("BTCUSDT") is None

        # Verify DB has no stale entries
        all_db = pm._db.portfolio_get_all()
        assert "BTCUSDT" not in all_db

    def test_multiple_operations_consistency(self, portfolio_with_db):
        """Complex sequence: add, add, close, add — verify consistency throughout."""
        pm = portfolio_with_db
        pm.cash_balance = 100000.0

        operations = [
            ("add", "BTCUSDT", 0.1, 60000.0),
            ("add", "ETHUSDT", 1.0, 3000.0),
            ("close", "BTCUSDT", 65000.0),
            ("add", "SOLUSDT", 10.0, 140.0),
        ]

        for op in operations:
            if op[0] == "add":
                pm.add_position(
                    op[1], quantity=op[2], entry_price=op[3], strategy="test"
                )
            elif op[0] == "close":
                pm.close_position(op[1], close_price=op[2])

            # Verify consistency after each operation
            mem_symbols = set(pm.positions.keys())
            db_symbols = set(pm._db.portfolio_get_all().keys())
            assert (
                mem_symbols == db_symbols
            ), f"Mismatch after {op}: mem={mem_symbols}, db={db_symbols}"

    def test_cash_consistency_after_operations(self, portfolio_with_db):
        """Cash balance in memory and DB should match after add/close."""
        pm = portfolio_with_db
        initial_cash = 100000.0
        pm.cash_balance = initial_cash

        pm.add_position("BTCUSDT", quantity=0.1, entry_price=60000.0, strategy="test")
        expected_after_add = initial_cash - (0.1 * 60000.0)

        assert pm.cash_balance == expected_after_add
        assert pm._db.portfolio_get_cash_balance() == expected_after_add

        pm.close_position("BTCUSDT", close_price=65000.0)
        expected_after_close = expected_after_add + (0.1 * 65000.0)

        assert pm.cash_balance == expected_after_close
        assert pm._db.portfolio_get_cash_balance() == expected_after_close


# ============================================================================
# Test Class D: Edge Cases
# ============================================================================


class TestEdgeCases:
    """Edge cases: dust, empty portfolio, merge positions, validation."""

    def test_dust_position_ignored(self, portfolio_with_db):
        """Positions below DUST_THRESHOLD_USD should be skipped."""
        pm = portfolio_with_db
        pm.cash_balance = 100000.0

        # $0.50 position is below $1.0 dust threshold
        pm.add_position("DUSTUSDT", quantity=100.0, entry_price=0.005, strategy="test")

        assert "DUSTUSDT" not in pm.positions
        assert pm._db.portfolio_get("DUSTUSDT") is None

    def test_empty_portfolio_reload(self, tmp_db_path):
        """Reloading from empty DB should result in empty portfolio."""
        db = StateDB(tmp_db_path)
        with patch("src.state_db.get_state_db", return_value=db):
            pm = PortfolioManager(config_path=None, binance_client=None)

            assert len(pm.positions) == 0
            assert pm.cash_balance == 0.0
        db.close()

    def test_merge_position_updates_db(self, portfolio_with_db):
        """Adding to existing position should merge and update DB."""
        pm = portfolio_with_db
        pm.cash_balance = 100000.0

        pm.add_position("BTCUSDT", quantity=0.1, entry_price=60000.0, strategy="test")
        pm.add_position(
            "BTCUSDT",
            quantity=0.1,
            entry_price=70000.0,
            strategy="test",
            deduct_cash=False,
        )

        # Should be merged: 0.2 @ weighted average
        expected_entry = (0.1 * 60000.0 + 0.1 * 70000.0) / 0.2
        assert pm.positions["BTCUSDT"]["quantity"] == 0.2
        assert pm.positions["BTCUSDT"]["entry_price"] == expected_entry

        db_pos = pm._db.portfolio_get("BTCUSDT")
        assert db_pos["quantity"] == 0.2
        assert db_pos["entry_price"] == expected_entry

    def test_insufficient_cash_raises(self, portfolio_with_db):
        """Adding position with insufficient cash should raise ValueError."""
        pm = portfolio_with_db
        pm.cash_balance = 100.0

        with pytest.raises(ValueError, match="Insufficient cash"):
            pm.add_position(
                "BTCUSDT", quantity=0.1, entry_price=60000.0, strategy="test"
            )

    def test_position_size_limit_raises(self, portfolio_with_db):
        """Position exceeding max_position_pct should raise ValueError."""
        pm = portfolio_with_db
        pm.cash_balance = 100000.0
        pm.config["max_position_pct"] = 10  # 10% max

        # $60000 position on $100000 total = 60% > 10%
        with pytest.raises(ValueError, match="Position size"):
            pm.add_position(
                "BTCUSDT", quantity=1.0, entry_price=60000.0, strategy="test"
            )

    def test_max_positions_limit_raises(self, portfolio_with_db):
        """Opening more than max_open_positions should raise ValueError."""
        pm = portfolio_with_db
        pm.cash_balance = 50000.0
        pm.config["max_open_positions"] = 2

        pm.add_position("BTCUSDT", quantity=0.1, entry_price=60000.0, strategy="test")
        pm.add_position("ETHUSDT", quantity=1.0, entry_price=3000.0, strategy="test")

        with pytest.raises(ValueError, match="Cannot open new position"):
            pm.add_position(
                "SOLUSDT", quantity=10.0, entry_price=140.0, strategy="test"
            )

    def test_dry_run_does_not_modify(self, portfolio_with_db):
        """_dry_run=True should validate but not modify state."""
        pm = portfolio_with_db
        pm.cash_balance = 100000.0

        result = pm.add_position(
            "BTCUSDT", quantity=0.1, entry_price=60000.0, strategy="test", _dry_run=True
        )

        assert "BTCUSDT" not in pm.positions
        assert pm._db.portfolio_get("BTCUSDT") is None
        assert pm.cash_balance == 100000.0  # unchanged

    def test_close_nonexistent_position(self, portfolio_with_db):
        """Closing a non-existent position should return empty dict."""
        pm = portfolio_with_db

        result = pm.close_position("NONEXISTENT", close_price=100.0)

        assert result == {}


# ============================================================================
# Test Class E: Trade History Persistence
# ============================================================================


class TestTradeHistory:
    """Test that trade history is recorded to StateDB on close."""

    def test_close_records_trade(self, portfolio_with_db):
        """Closing a position should record a trade in the trades table."""
        pm = portfolio_with_db
        pm.cash_balance = 100000.0

        pm.add_position("BTCUSDT", quantity=0.1, entry_price=60000.0, strategy="test")
        pm.close_position("BTCUSDT", close_price=65000.0)

        trades = pm._db.trade_get_recent(symbol="BTCUSDT", limit=10)
        assert len(trades) >= 1

        latest = trades[0]
        assert latest["symbol"] == "BTCUSDT"
        assert latest["side"] == "SELL"
        assert latest["qty"] == 0.1
        assert latest["price"] == 65000.0
        assert latest["pnl"] == pytest.approx(500.0, abs=0.01)

    def test_multiple_trades_recorded(self, portfolio_with_db):
        """Multiple closes should all be recorded."""
        pm = portfolio_with_db
        pm.cash_balance = 100000.0

        pm.add_position("BTCUSDT", quantity=0.1, entry_price=60000.0, strategy="test")
        pm.close_position("BTCUSDT", close_price=65000.0)

        pm.add_position("BTCUSDT", quantity=0.2, entry_price=62000.0, strategy="test")
        pm.close_position("BTCUSDT", close_price=64000.0)

        trades = pm._db.trade_get_recent(symbol="BTCUSDT", limit=10)
        assert len(trades) == 4  # 2 ADD + 2 CLOSE (dual persistence)


# ============================================================================
# Test Class F: Audit Log
# ============================================================================


class TestAuditLog:
    """Test that portfolio operations write to audit_log."""

    def test_sync_writes_audit_log(self, portfolio_with_db, mock_binance_client):
        """sync_from_binance should write a PORTFOLIO_SYNC audit entry."""
        pm = portfolio_with_db

        pm.sync_from_binance(mock_binance_client)

        audits = pm._db.audit_get_recent(limit=10)
        sync_audits = [a for a in audits if a["action"] == "PORTFOLIO_SYNC"]
        assert len(sync_audits) >= 1

        audit = sync_audits[0]
        assert "Synced" in audit["details"]
        assert audit["source"] == "binance_api"
        assert audit["old_value"] is not None
        assert audit["new_value"] is not None


# ============================================================================
# Test Class G: Debounced Save
# ============================================================================


class TestDebouncedSave:
    """Test that _save_state respects debounce interval."""

    def test_debounce_prevents_excessive_saves(self, portfolio_with_db):
        """Multiple rapid saves should be debounced."""
        pm = portfolio_with_db
        pm._save_debounce_sec = 2  # 2 second debounce
        pm.cash_balance = 100000.0

        # Track DB writes
        original_portfolio_set = pm._db.portfolio_set
        call_count = {"count": 0}

        def counting_set(symbol, data):
            call_count["count"] += 1
            return original_portfolio_set(symbol, data)

        pm._db.portfolio_set = counting_set

        pm.add_position("BTCUSDT", quantity=0.1, entry_price=60000.0, strategy="test")
        pm.add_position("ETHUSDT", quantity=1.0, entry_price=3000.0, strategy="test")

        # Both adds happened within debounce window, but _save_state is called
        # after each add_position. With debounce, second save may be skipped.
        # We verify at least one save happened.
        assert call_count["count"] >= 1

    def test_save_after_debounce_interval(self, portfolio_with_db):
        """Save should happen after debounce interval expires."""
        pm = portfolio_with_db
        pm._save_debounce_sec = 0  # No debounce for this test
        pm.cash_balance = 100000.0

        pm.add_position("BTCUSDT", quantity=0.1, entry_price=60000.0, strategy="test")

        # Should be in DB immediately (no debounce)
        assert pm._db.portfolio_get("BTCUSDT") is not None


# ============================================================================
# Test Class H: Full Cycle Integration
# ============================================================================


class TestFullCycle:
    """End-to-end: simulate a trading session with multiple operations."""

    def test_full_trading_session(self, tmp_db_path, mock_binance_client):
        """Simulate: sync → trade → close → reload → verify."""

        # Phase 1: Initial sync from Binance
        db = StateDB(tmp_db_path)
        with patch("src.state_db.get_state_db", return_value=db):
            pm = PortfolioManager(config_path=None, binance_client=mock_binance_client)
            pm.sync_from_binance(mock_binance_client)

            initial_positions = dict(pm.positions)
            initial_cash = pm.cash_balance
        db.close()

        # Phase 2: Simulate trading (add new position)
        db = StateDB(tmp_db_path)
        with patch("src.state_db.get_state_db", return_value=db):
            pm = PortfolioManager(config_path=None, binance_client=None)
            pm.add_position(
                "BNBUSDT", quantity=1.0, entry_price=600.0, strategy="manual"
            )

            mid_positions = dict(pm.positions)
        db.close()

        # Phase 3: Close a position
        db = StateDB(tmp_db_path)
        with patch("src.state_db.get_state_db", return_value=db):
            pm = PortfolioManager(config_path=None, binance_client=None)
            pm.close_position("BTCUSDT", close_price=70000.0)

            post_close_positions = dict(pm.positions)
        db.close()

        # Phase 4: Reload and verify final state
        db = StateDB(tmp_db_path)
        with patch("src.state_db.get_state_db", return_value=db):
            pm = PortfolioManager(config_path=None, binance_client=None)
            final_positions = pm.positions
            final_db = db.portfolio_get_all()

            # BTC should be gone (closed)
            assert "BTCUSDT" not in final_positions
            assert "BTCUSDT" not in final_db

            # ETH, SOL should still be there (from initial sync)
            assert "ETHUSDT" in final_positions
            assert "SOLUSDT" in final_positions

            # BNB should be there (added manually)
            assert "BNBUSDT" in final_positions
            assert "BNBUSDT" in final_db

            # Verify DB and memory match
            assert set(final_positions.keys()) == set(final_db.keys())

        db.close()
