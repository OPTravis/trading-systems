"""
Tests for PortfolioManager — positions, cash, P&L, NAV, settlement.
"""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.portfolio import (
    CashAccount,
    PortfolioManager,
    Position,
)

# ── Position Dataclass ────────────────────────────────────────────────


class TestPosition:
    def test_market_value(self):
        pos = Position(
            symbol="AAPL", quantity=100, entry_price=150.0, current_price=155.0
        )
        assert pos.market_value == 15_500.0

    def test_cost_basis(self):
        pos = Position(
            symbol="AAPL", quantity=100, entry_price=150.0, current_price=155.0
        )
        assert pos.cost_basis == 15_000.0

    def test_unrealized_pnl_pct(self):
        pos = Position(
            symbol="AAPL", quantity=100, entry_price=100.0, current_price=110.0
        )
        assert pos.unrealized_pnl_pct == pytest.approx(10.0)

    def test_unrealized_pnl_pct_zero_entry(self):
        pos = Position(
            symbol="AAPL", quantity=100, entry_price=0.0, current_price=110.0
        )
        assert pos.unrealized_pnl_pct == 0.0

    def test_to_dict(self):
        pos = Position(
            symbol="AAPL", quantity=100, entry_price=150.0, current_price=155.0
        )
        d = pos.to_dict()
        assert d["symbol"] == "AAPL"
        assert d["quantity"] == 100
        assert d["market_value"] == 15_500.0
        assert "unrealized_pnl_pct" in d


# ── CashAccount ───────────────────────────────────────────────────────


class TestCashAccount:
    def test_initial_balance(self):
        cash = CashAccount(currency="USD", total_cash=10_000.0)
        assert cash.available() == 10_000.0
        assert cash.unsettled_amount() == 0.0

    def test_record_sell_adds_to_total(self):
        cash = CashAccount(currency="USD", total_cash=10_000.0)
        cash.record_sell(5_000.0, market="US")
        assert cash.total_cash == 15_000.0
        # Funds unsettled until T+1
        assert cash.available() < 15_000.0

    def test_record_buy_deducts_cash(self):
        cash = CashAccount(currency="USD", total_cash=10_000.0)
        cash.record_buy(3_000.0, market="US")
        assert cash.total_cash == 7_000.0

    def test_settlement_t1_us(self):
        """US stocks settle T+1."""
        today = date(2026, 1, 5)  # Monday
        cash = CashAccount(currency="USD", total_cash=10_000.0)
        cash.record_sell(5_000.0, market="US", trade_date=today)

        # Before settlement, funds are unsettled
        assert cash.unsettled_amount(today) == 5_000.0

        # After T+1 (next day), funds settle
        tomorrow = today + timedelta(days=1)
        assert cash.unsettled_amount(tomorrow) == 0.0

    def test_settlement_t2_hk(self):
        """HK stocks settle T+2."""
        today = date(2026, 1, 5)
        cash = CashAccount(currency="HKD", total_cash=100_000.0)
        cash.record_sell(50_000.0, market="HK", trade_date=today)

        # Still unsettled after 1 day
        assert cash.unsettled_amount(today + timedelta(days=1)) == 50_000.0

        # Settled after 2 days
        assert cash.unsettled_amount(today + timedelta(days=2)) == 0.0

    def test_available_never_negative(self):
        cash = CashAccount(currency="USD", total_cash=0.0)
        assert cash.available() == 0.0


# ── PortfolioManager ──────────────────────────────────────────────────


class TestPortfolioManager:
    @pytest.fixture
    def pm(self):
        """Create a PortfolioManager with no DB and funded USD account."""
        p = PortfolioManager(db=None)
        p._cash["USD"].total_cash = 1_000_000.0
        return p

    def test_initial_state(self, pm):
        assert pm.position_count == 0
        assert pm.get_all_positions() == []
        assert pm.get_nav() == 1_000_000.0  # Initial cash

    def test_add_position(self, pm):
        pos = pm.add_position("AAPL", quantity=100, price=150.0)
        assert pos.symbol == "AAPL"
        assert pos.quantity == 100
        assert pos.entry_price == 150.0
        assert pm.position_count == 1

    def test_add_position_deducts_cash(self, pm):
        before = pm.get_cash_balance("USD")
        pm.add_position("AAPL", quantity=100, price=150.0)
        assert pm.get_cash_balance("USD") == before - 15_000.0

    def test_add_position_insufficient_cash(self, pm):
        with pytest.raises(ValueError, match="Insufficient"):
            pm.add_position("AAPL", quantity=10000, price=150.0)

    def test_add_merge_position(self, pm):
        pm.add_position("AAPL", quantity=100, price=150.0)
        pm.add_position("AAPL", quantity=100, price=160.0)
        assert pm.position_count == 1
        pos = pm.get_position("AAPL")
        assert pos.quantity == 200
        # Weighted average entry
        assert pos.entry_price == pytest.approx(155.0)

    def test_reduce_position(self, pm):
        pm.add_position("AAPL", quantity=100, price=150.0)
        result = pm.reduce_position("AAPL", quantity=50, price=160.0)
        assert result["quantity_sold"] == 50
        assert result["pnl"] == pytest.approx(500.0)  # (160-150) * 50
        assert pm.get_position("AAPL").quantity == 50

    def test_reduce_position_full(self, pm):
        pm.add_position("AAPL", quantity=100, price=150.0)
        pm.reduce_position("AAPL", quantity=100, price=160.0)
        assert pm.position_count == 0

    def test_reduce_nonexistent(self, pm):
        with pytest.raises(ValueError, match="No position"):
            pm.reduce_position("AAPL", quantity=50, price=160.0)

    def test_reduce_overfill(self, pm):
        pm.add_position("AAPL", quantity=100, price=150.0)
        with pytest.raises(ValueError, match="Cannot sell"):
            pm.reduce_position("AAPL", quantity=200, price=160.0)

    def test_close_position(self, pm):
        pm.add_position("AAPL", quantity=100, price=150.0)
        result = pm.close_position("AAPL", price=160.0)
        assert result["pnl"] == pytest.approx(1_000.0)
        assert pm.position_count == 0

    def test_update_price(self, pm):
        pm.add_position("AAPL", quantity=100, price=150.0)
        pm.update_price("AAPL", 160.0)
        assert pm.get_position("AAPL").current_price == 160.0

    def test_update_price_tracks_highest(self, pm):
        pm.add_position("AAPL", quantity=100, price=150.0)
        pm.update_price("AAPL", 160.0)
        pm.update_price("AAPL", 155.0)
        assert pm.get_position("AAPL").highest_price == 160.0

    def test_update_prices_batch(self, pm):
        pm.add_position("AAPL", quantity=100, price=150.0)
        pm.add_position("MSFT", quantity=50, price=380.0)
        pm.update_prices({"AAPL": 160.0, "MSFT": 390.0})
        assert pm.get_position("AAPL").current_price == 160.0
        assert pm.get_position("MSFT").current_price == 390.0

    def test_update_nonexistent_symbol(self, pm):
        # Should not raise
        pm.update_price("FAKE", 100.0)

    def test_get_position_none(self, pm):
        assert pm.get_position("FAKE") is None

    def test_get_position_dicts(self, pm):
        pm.add_position("AAPL", quantity=100, price=150.0)
        dicts = pm.get_position_dicts()
        assert len(dicts) == 1
        assert dicts[0]["symbol"] == "AAPL"

    # ── P&L ────────────────────────────────────────────────────────

    def test_unrealized_pnl(self, pm):
        pm.add_position("AAPL", quantity=100, price=150.0)
        pm.update_price("AAPL", 160.0)
        # unrealized_pnl is a stored field, not computed from price
        # Set it explicitly to simulate broker sync
        pm.get_position("AAPL").unrealized_pnl = 1_000.0
        assert pm.get_unrealized_pnl() == pytest.approx(1_000.0)

    def test_realized_pnl(self, pm):
        pm.add_position("AAPL", quantity=100, price=150.0)
        pm.close_position("AAPL", price=160.0)
        assert pm.get_realized_pnl() == pytest.approx(1_000.0)

    def test_total_pnl(self, pm):
        pm.add_position("AAPL", quantity=100, price=150.0)
        pm.add_position("MSFT", quantity=50, price=380.0)
        # Set unrealized PnL explicitly (simulates broker sync)
        pm.get_position("AAPL").unrealized_pnl = 1_000.0
        pm.get_position("MSFT").unrealized_pnl = -500.0
        # Unrealized: +1000 - 500 = +500
        assert pm.get_total_pnl() == pytest.approx(500.0)

    def test_realized_trades(self, pm):
        pm.add_position("AAPL", quantity=100, price=150.0)
        pm.close_position("AAPL", price=160.0)
        trades = pm.get_realized_trades()
        assert len(trades) >= 2  # BUY + SELL

    # ── NAV & Exposure ─────────────────────────────────────────────

    @patch("src.portfolio._get_fx_to_usd", return_value=1.0)
    def test_get_nav(self, mock_fx, pm):
        pm.add_position("AAPL", quantity=100, price=150.0)
        pm.update_price("AAPL", 160.0)
        nav = pm.get_nav()
        # Cash (1M - 15k) + market value (16k) = 1,001,000
        assert nav == pytest.approx(1_001_000.0)

    @patch("src.portfolio._get_fx_to_usd", return_value=1.0)
    def test_exposure_pct(self, mock_fx, pm):
        pm.add_position("AAPL", quantity=100, price=150.0)
        pm.update_price("AAPL", 150.0)
        exposure = pm.get_exposure_pct()
        # Market value 15k / NAV 1M = 1.5%
        assert exposure == pytest.approx(1.5)

    def test_exposure_pct_zero_nav(self, pm):
        # No positions, no cash
        pm._cash = {}
        assert pm.get_exposure_pct() == 0.0

    @patch("src.portfolio._get_fx_to_usd", return_value=1.0)
    def test_sector_exposure(self, mock_fx, pm):
        pm.add_position("AAPL", quantity=100, price=150.0, sector="Technology")
        pm.add_position("JPM", quantity=50, price=200.0, sector="Finance")
        pm.update_price("AAPL", 150.0)
        pm.update_price("JPM", 200.0)
        sectors = pm.get_sector_exposure()
        assert "Technology" in sectors
        assert "Finance" in sectors
        assert sectors["Technology"] > 0
        assert sectors["Finance"] > 0

    # ── Summary ────────────────────────────────────────────────────

    @patch("src.portfolio._get_fx_to_usd", return_value=1.0)
    def test_get_summary(self, mock_fx, pm):
        pm.add_position("AAPL", quantity=100, price=150.0)
        summary = pm.get_summary()
        assert "nav" in summary
        assert "positions_count" in summary
        assert "market_value" in summary
        assert "cash" in summary
        assert summary["positions_count"] == 1

    # ── Multi-Currency ─────────────────────────────────────────────

    def test_multiple_currencies(self, pm):
        # Default has USD, HKD, CNY
        assert pm.get_cash_balance("USD") == 1_000_000.0
        assert pm.get_cash_balance("HKD") == 0.0
        assert pm.get_cash_balance("CNY") == 0.0

    def test_custom_currency_created(self, pm):
        pm._get_cash("EUR")
        assert "EUR" in pm._cash

    # ── Broker Sync ────────────────────────────────────────────────

    def test_sync_from_broker_success(self, pm):
        broker = MagicMock()
        account = MagicMock()
        account.currency = "USD"
        account.total_cash = 50_000.0
        broker.get_account.return_value = account

        contract = MagicMock()
        contract.symbol = "AAPL"
        contract.exchange = "SMART"
        contract.currency = "USD"

        pos = MagicMock()
        pos.contract = contract
        pos.quantity = 100
        pos.avg_cost = 150.0
        pos.market_value = 15_000.0
        pos.unrealized_pnl = 0.0

        broker.get_portfolio.return_value = [pos]

        result = pm.sync_from_broker(broker)
        assert result is True
        assert pm.position_count == 1
        assert pm.get_position("AAPL").quantity == 100

    def test_sync_from_broker_failure(self, pm):
        broker = MagicMock()
        broker.get_account.side_effect = Exception("Connection lost")
        result = pm.sync_from_broker(broker)
        assert result is False

    def test_sync_clears_old_positions(self, pm):
        # Add a local position
        pm.add_position("MSFT", quantity=50, price=380.0)

        broker = MagicMock()
        account = MagicMock()
        account.currency = "USD"
        account.total_cash = 50_000.0
        broker.get_account.return_value = account

        contract = MagicMock()
        contract.symbol = "AAPL"
        contract.exchange = "SMART"
        contract.currency = "USD"

        pos = MagicMock()
        pos.contract = contract
        pos.quantity = 100
        pos.avg_cost = 150.0
        pos.market_value = 15_000.0
        pos.unrealized_pnl = 0.0

        broker.get_portfolio.return_value = [pos]
        pm.sync_from_broker(broker)

        # MSFT should be gone, AAPL should exist
        assert pm.get_position("MSFT") is None
        assert pm.get_position("AAPL") is not None

    def test_sync_hk_market(self, pm):
        broker = MagicMock()
        account = MagicMock()
        account.currency = "HKD"
        account.total_cash = 500_000.0
        broker.get_account.return_value = account

        contract = MagicMock()
        contract.symbol = "0700"
        contract.exchange = "SEHK"
        contract.currency = "HKD"

        pos = MagicMock()
        pos.contract = contract
        pos.quantity = 100
        pos.avg_cost = 400.0
        pos.market_value = 40_000.0
        pos.unrealized_pnl = 0.0

        broker.get_portfolio.return_value = [pos]
        pm.sync_from_broker(broker)

        assert pm.get_position("0700").market == "HK"

    # ── Edge Cases ─────────────────────────────────────────────────

    def test_position_count(self, pm):
        assert pm.position_count == 0
        pm.add_position("AAPL", quantity=10, price=150.0)
        assert pm.position_count == 1
        pm.add_position("MSFT", quantity=10, price=380.0)
        assert pm.position_count == 2
        pm.close_position("AAPL", price=160.0)
        assert pm.position_count == 1
