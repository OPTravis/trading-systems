"""
Unit tests for portfolio_state.py (StateMixin) — mock-based, no network.

Covers:
  - P1-5: sync_from_binance build-then-swap pattern
    - Normal sync: build new state → atomic swap → save
    - Mid-build failure: old state restored (rollback)
    - No partial positions exposed during build
  - _save_state and _load_state_from_db basic flow
"""

import time
from datetime import datetime
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.portfolio_state import StateMixin


# ────────────────────────────────────────────────────────────
# Test harness: create a concrete class using the mixin
# ────────────────────────────────────────────────────────────

class TestPortfolio(StateMixin):
    """Concrete portfolio for testing the mixin."""

    def __init__(self):
        self.DUST_THRESHOLD_USD = 5.0
        self._db = MagicMock()
        self.config = {
            "stop_loss": {"default_pct": 5.0},
            "take_profit": {"default_pct": 6.0},
        }
        self.positions = {}
        self.cash_balance = 0.0
        self._last_save_time = 0.0
        self._save_debounce_sec = 0.0  # disable debounce for tests

    def add_position(self, symbol="", quantity=0, entry_price=0,
                     strategy="", deduct_cash=False,
                     _skip_validation=False, _from_sync=False):
        """Simplified add_position for testing."""
        self.positions[symbol] = {
            "symbol": symbol,
            "quantity": quantity,
            "entry_price": entry_price,
            "current_price": entry_price,
            "strategy": strategy,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "stop_loss": entry_price * 0.95,
            "take_profit": entry_price * 1.06,
            "trailing_stop_pct": 1.5,
            "highest_price": entry_price,
        }


@pytest.fixture
def portfolio():
    """Fresh portfolio with mocked DB."""
    p = TestPortfolio()
    p._db.portfolio_get_all.return_value = {}
    p._db.portfolio_get_cash_balance.return_value = 0.0
    return p


@pytest.fixture
def mock_binance():
    """Mock Binance client returning controlled account data."""
    b = MagicMock()
    return b


# ────────────────────────────────────────────────────────────
# P1-5: sync_from_binance — normal sync (build-then-swap)
# ────────────────────────────────────────────────────────────

class TestSyncNormal:

    def test_sync_success_replaces_positions(self, portfolio, mock_binance):
        """Normal sync: builds new positions and replaces old ones."""
        # Pre-existing state
        portfolio.positions = {"OLDUSDT": {"symbol": "OLDUSDT", "quantity": 1.0}}
        portfolio.cash_balance = 500.0

        mock_binance.get_account.return_value = {
            "balances": [
                {"asset": "USDT", "free": "1000", "locked": "0"},
                {"asset": "BTC", "free": "0.1", "locked": "0"},
            ]
        }
        mock_binance.get_24hr_stats.return_value = {"last_price": "40000"}
        mock_binance.get_ticker_price.return_value = 40000.0

        # DB: no existing entries
        portfolio._db.portfolio_get.return_value = None

        result = portfolio.sync_from_binance(mock_binance)

        assert result is True
        assert "BTCUSDT" in portfolio.positions
        assert "OLDUSDT" not in portfolio.positions  # old replaced
        assert portfolio.cash_balance == 1000.0  # USDT from Binance

    def test_sync_sets_cash_to_usdt_balance(self, portfolio, mock_binance):
        portfolio.cash_balance = 0.0
        mock_binance.get_account.return_value = {
            "balances": [
                {"asset": "USDT", "free": "2500.50", "locked": "100"},
            ]
        }

        result = portfolio.sync_from_binance(mock_binance)

        assert result is True
        assert portfolio.cash_balance == 2600.50  # free + locked

    def test_sync_skips_dust_positions(self, portfolio, mock_binance):
        """Assets worth < DUST_THRESHOLD_USD should be skipped."""
        mock_binance.get_account.return_value = {
            "balances": [
                {"asset": "USDT", "free": "1000", "locked": "0"},
                {"asset": "DOGE", "free": "10", "locked": "0"},  # 10 * 0.1 = $1 < $5
            ]
        }
        mock_binance.get_24hr_stats.return_value = {"last_price": "0.1"}
        mock_binance.get_ticker_price.return_value = 0.1

        portfolio._db.portfolio_get.return_value = None

        result = portfolio.sync_from_binance(mock_binance)

        assert result is True
        assert "DOGEUSDT" not in portfolio.positions  # dust filtered
        assert len(portfolio.positions) == 0

    def test_sync_skips_stablecoins(self, portfolio, mock_binance):
        """USDC, BUSD etc. should not be treated as positions."""
        mock_binance.get_account.return_value = {
            "balances": [
                {"asset": "USDT", "free": "1000", "locked": "0"},
                {"asset": "USDC", "free": "500", "locked": "0"},
                {"asset": "BUSD", "free": "200", "locked": "0"},
            ]
        }

        result = portfolio.sync_from_binance(mock_binance)

        assert result is True
        assert len(portfolio.positions) == 0  # no non-stablecoin positions


# ────────────────────────────────────────────────────────────
# P1-5: sync_from_binance — failure rollback
# ────────────────────────────────────────────────────────────

class TestSyncFailureRollback:

    def test_api_failure_preserves_old_state(self, portfolio, mock_binance):
        """When get_account() fails, old state should be preserved."""
        old_positions = {"BTCUSDT": {"symbol": "BTCUSDT", "quantity": 0.5}}
        portfolio.positions = dict(old_positions)
        portfolio.cash_balance = 800.0

        mock_binance.get_account.side_effect = ConnectionError("API down")

        result = portfolio.sync_from_binance(mock_binance)

        assert result is False
        assert portfolio.positions == old_positions  # unchanged
        assert portfolio.cash_balance == 800.0  # unchanged

    def test_mid_build_failure_rollbacks(self, portfolio, mock_binance):
        """P1-5: Exception after position build (during _save_state) → old state restored.

        The sync_from_binance method uses a try/except around the entire build+save
        block. If _save_state fails after building new positions, the outer except
        catches it and restores old_positions/old_cash.
        """
        old_positions = {"BTCUSDT": {"symbol": "BTCUSDT", "quantity": 0.5}}
        portfolio.positions = dict(old_positions)
        portfolio.cash_balance = 800.0

        mock_binance.get_account.return_value = {
            "balances": [
                {"asset": "USDT", "free": "1000", "locked": "0"},
                {"asset": "BTC", "free": "0.1", "locked": "0"},
            ]
        }
        mock_binance.get_24hr_stats.return_value = {"last_price": "40000"}
        portfolio._db.portfolio_get.return_value = None

        # Force _save_state to fail on first call (build phase) but succeed
        # on second call (rollback) — the rollback path also calls _save_state
        with patch.object(portfolio, "_save_state",
                          side_effect=[RuntimeError("DB write error"), None]):
            result = portfolio.sync_from_binance(mock_binance)

        assert result is False
        # P1-5: Old state should be restored, not partially overwritten
        assert portfolio.positions == old_positions
        assert portfolio.cash_balance == 800.0

    def test_zero_balance_with_positions_skips_sync(self, portfolio, mock_binance):
        """When Binance returns zero total assets but local has positions,
        sync is skipped to prevent destroying local state."""
        portfolio.positions = {"BTCUSDT": {"symbol": "BTCUSDT", "quantity": 0.5}}

        mock_binance.get_account.return_value = {
            "balances": [
                {"asset": "USDT", "free": "0", "locked": "0"},
            ]
        }

        result = portfolio.sync_from_binance(mock_binance)

        assert result is False
        assert "BTCUSDT" in portfolio.positions  # preserved


# ────────────────────────────────────────────────────────────
# P1-5: No partial positions during build
# ────────────────────────────────────────────────────────────

class TestNoPartialPositions:

    def test_build_then_swap_no_intermediate_exposure(self, portfolio, mock_binance):
        """During sync, self.positions should be the temp dict until fully built.

        The build-then-swap pattern means we should never see old + new mixed.
        """
        portfolio.positions = {"OLDUSDT": {"symbol": "OLDUSDT", "quantity": 1.0}}

        mock_binance.get_account.return_value = {
            "balances": [
                {"asset": "USDT", "free": "1000", "locked": "0"},
                {"asset": "BTC", "free": "0.1", "locked": "0"},
                {"asset": "ETH", "free": "1.0", "locked": "0"},
            ]
        }
        # Different prices per asset
        def mock_stats(symbol):
            prices = {"BTCUSDT": "40000", "ETHUSDT": "2000"}
            return {"last_price": prices.get(symbol, "1")}

        mock_binance.get_24hr_stats.side_effect = mock_stats
        mock_binance.get_ticker_price.return_value = 40000.0
        portfolio._db.portfolio_get.return_value = None

        # Capture positions state during build
        snapshots = []
        original_add_position = portfolio.add_position

        def tracking_add_position(*args, **kwargs):
            # During build, self.positions should NOT contain old positions
            snapshots.append(dict(portfolio.positions))
            return original_add_position(*args, **kwargs)

        with patch.object(portfolio, "add_position", side_effect=tracking_add_position):
            result = portfolio.sync_from_binance(mock_binance)

        assert result is True

        # After sync, new positions present and old gone
        assert "BTCUSDT" in portfolio.positions
        assert "ETHUSDT" in portfolio.positions
        assert "OLDUSDT" not in portfolio.positions

        # During build, OLD positions should not have been visible alongside new ones
        for snap in snapshots:
            # No snapshot should have both OLD and new positions
            assert not ("OLDUSDT" in snap and "BTCUSDT" in snap), \
                "P1-5 VIOLATION: old and new positions visible simultaneously during build!"


# ────────────────────────────────────────────────────────────
# None client
# ────────────────────────────────────────────────────────────

class TestSyncNoneClient:

    def test_none_client_returns_false(self, portfolio):
        result = portfolio.sync_from_binance(None)
        assert result is False


# ────────────────────────────────────────────────────────────
# _save_state basic flow
# ────────────────────────────────────────────────────────────

class TestSaveState:

    def test_save_state_persists_positions(self, portfolio):
        portfolio.positions = {
            "BTCUSDT": {"quantity": 0.1, "entry_price": 40000, "strategy": "test",
                        "stop_loss": 38000, "take_profit": 42000, "created_at": "2025-01-01"},
        }
        portfolio.cash_balance = 1000.0
        portfolio._db.portfolio_get_all.return_value = {"BTCUSDT": {}}

        portfolio._save_state(force=True)

        portfolio._db.portfolio_set_cash_balance.assert_called_once_with(1000.0)
        portfolio._db.portfolio_set.assert_called()

    def test_save_state_removes_closed_positions(self, portfolio):
        """Positions in DB but not in memory should be removed."""
        portfolio.positions = {}  # no positions in memory
        portfolio.cash_balance = 1000.0
        portfolio._db.portfolio_get_all.return_value = {"GONEUSDT": {}, "ALSOGONEUSDT": {}}

        portfolio._save_state(force=True)

        portfolio._db.portfolio_remove.assert_any_call("GONEUSDT")
        portfolio._db.portfolio_remove.assert_any_call("ALSOGONEUSDT")
