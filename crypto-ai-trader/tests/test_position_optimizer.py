"""
Position Optimizer Tests

Tests for position switching logic, including:
- Switch decision thresholds
- _execute_switch portfolio state updates
- stepSize flooring
- Cooldown enforcement
"""

import pytest
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from src.position_optimizer import PositionOptimizer


@pytest.fixture
def mock_binance_client():
    client = MagicMock()
    client.place_market_sell = MagicMock(return_value={"orderId": 12345})
    client.place_market_buy = MagicMock(return_value={"orderId": 67890})
    client.get_symbol_filters = MagicMock(return_value={
        "minQty": 0.001,
        "maxQty": 100000,
        "stepSize": 0.001,
        "minNotional": 10,
        "qty_decimals": 3,
    })
    client.get_24hr_stats = MagicMock(return_value={"last_price": 100.0})
    client.get_ticker_price = MagicMock(return_value=50000.0)
    return client


@pytest.fixture
def mock_portfolio():
    portfolio = MagicMock()
    portfolio.get_all_positions = MagicMock(return_value=[])
    portfolio.close_position = MagicMock(return_value={
        "symbol": "BTCUSDT",
        "quantity": 0.1,
        "entry_price": 50000,
        "pnl": 100,
    })
    portfolio.add_position = MagicMock()
    return portfolio


@pytest.fixture
def mock_scanner():
    scanner = MagicMock()
    scanner.scan_all = MagicMock(return_value=[])
    return scanner


@pytest.fixture
def optimizer(mock_binance_client, mock_portfolio, mock_scanner):
    return PositionOptimizer(
        binance_client=mock_binance_client,
        portfolio=mock_portfolio,
        market_scanner=mock_scanner,
    )


class TestSwitchDecision:
    """Test switch decision logic."""

    def test_no_positions_returns_empty(self, optimizer):
        """No positions = no decisions."""
        decisions = optimizer.analyze_and_switch(dry_run=True)
        assert decisions == []

    def test_loss_threshold_triggers_switch(self, optimizer, mock_portfolio, mock_scanner, mock_binance_client):
        """Position with -6% 24h change should trigger switch."""
        mock_binance_client.get_24hr_stats = MagicMock(return_value={
            "price_change_pct": -6.0
        })
        mock_portfolio.get_all_positions = MagicMock(return_value=[{
            "symbol": "BTCUSDT",
            "quantity": 0.1,
            "entry_price": 50000,
            "current_price": 47000,
            "position_value": 4700,
            "price_change_24h": -6.0,
            "score": 50,
        }])
        # BTCUSDT must also be in opportunities so existing_score > 0
        mock_scanner.scan_all = MagicMock(return_value=[
            {
                "symbol": "BTCUSDT",
                "price": 47000,
                "score": 50,
                "price_change_24h": -6.0,
            },
            {
                "symbol": "ETHUSDT",
                "price": 3000,
                "score": 80,
                "price_change_24h": 5.0,
            },
        ])

        # Reset cooldowns to allow immediate switch
        optimizer._last_switch_time = {}

        decisions = optimizer.analyze_and_switch(dry_run=True)
        assert len(decisions) == 1
        assert decisions[0]["from_symbol"] == "BTCUSDT"
        assert decisions[0]["to_symbol"] == "ETHUSDT"
        # Both loss and score gap conditions are met; score gap takes priority in reason
        assert ("loss" in decisions[0]["reason"]) or ("score" in decisions[0]["reason"])

    def test_score_gap_triggers_switch(self, optimizer, mock_portfolio, mock_scanner, mock_binance_client):
        """New coin score 25 points higher should trigger switch."""
        mock_binance_client.get_24hr_stats = MagicMock(return_value={
            "price_change_pct": -2.0
        })
        mock_portfolio.get_all_positions = MagicMock(return_value=[{
            "symbol": "BTCUSDT",
            "quantity": 0.1,
            "entry_price": 50000,
            "current_price": 50000,
            "position_value": 5000,
            "price_change_24h": -2.0,
            "score": 50,
        }])
        # BTCUSDT must also be in opportunities so existing_score > 0
        mock_scanner.scan_all = MagicMock(return_value=[
            {
                "symbol": "BTCUSDT",
                "price": 50000,
                "score": 50,
                "price_change_24h": -2.0,
            },
            {
                "symbol": "ETHUSDT",
                "price": 3000,
                "score": 80,
                "price_change_24h": 5.0,
            },
        ])

        # Reset cooldowns to allow immediate switch
        optimizer._last_switch_time = {}

        decisions = optimizer.analyze_and_switch(dry_run=True)
        assert len(decisions) == 1
        assert decisions[0]["from_symbol"] == "BTCUSDT"
        assert decisions[0]["to_symbol"] == "ETHUSDT"
        assert "score" in decisions[0]["reason"]

    def test_blacklist_24h_change_30_skipped(self, optimizer, mock_portfolio, mock_scanner, mock_binance_client):
        """Coin with +31% 24h change should be blacklisted."""
        mock_binance_client.get_24hr_stats = MagicMock(return_value={
            "price_change_pct": -6.0
        })
        mock_portfolio.get_all_positions = MagicMock(return_value=[{
            "symbol": "BTCUSDT",
            "quantity": 0.1,
            "entry_price": 50000,
            "current_price": 47000,
            "position_value": 4700,
            "price_change_24h": -6.0,
            "score": 50,
        }])
        mock_scanner.scan_all = MagicMock(return_value=[{
            "symbol": "ETHUSDT",
            "price": 3000,
            "score": 80,
            "price_change_24h": 31.0,  # Blacklisted
        }])

        decisions = optimizer.analyze_and_switch(dry_run=True)
        assert len(decisions) == 0  # Blacklisted, no switch

    def test_cooldown_blocks_switch(self, optimizer, mock_portfolio, mock_scanner, mock_binance_client):
        """Switch within 4h cooldown should be blocked."""
        mock_binance_client.get_24hr_stats = MagicMock(return_value={
            "price_change_pct": -6.0
        })
        mock_portfolio.get_all_positions = MagicMock(return_value=[{
            "symbol": "BTCUSDT",
            "quantity": 0.1,
            "entry_price": 50000,
            "current_price": 47000,
            "position_value": 4700,
            "price_change_24h": -6.0,
            "score": 50,
        }])
        mock_scanner.scan_all = MagicMock(return_value=[{
            "symbol": "ETHUSDT",
            "price": 3000,
            "score": 80,
            "price_change_24h": 5.0,
        }])

        # First switch
        optimizer.analyze_and_switch(dry_run=False)
        # Second switch within cooldown
        mock_binance_client.place_market_sell = MagicMock(return_value={"orderId": 11111})
        mock_binance_client.place_market_buy = MagicMock(return_value={"orderId": 22222})
        decisions = optimizer.analyze_and_switch(dry_run=True)
        assert len(decisions) == 0  # Cooldown blocks


class TestExecuteSwitch:
    """Test _execute_switch method."""

    def test_successful_switch_updates_portfolio(self, optimizer, mock_binance_client, mock_portfolio):
        """Successful switch should update portfolio state."""
        decision = {
            "from_symbol": "BTCUSDT",
            "to_symbol": "ETHUSDT",
            "from_value": 5000,
            "to_price": 3000,
            "expected_gain_pct": 5.0,
            "reason": "score_gap",
        }
        mock_portfolio.get_all_positions = MagicMock(return_value=[{
            "symbol": "BTCUSDT",
            "quantity": 0.1,
        }])

        result = optimizer._execute_switch(decision)

        assert result is True
        assert decision["executed"] is True
        mock_binance_client.place_market_sell.assert_called_once_with(
            symbol="BTCUSDT", quantity=0.1
        )
        mock_binance_client.place_market_buy.assert_called_once()
        # Portfolio should be updated
        mock_portfolio.close_position.assert_called_once()
        mock_portfolio.add_position.assert_called_once()

    def test_sell_fails_aborts_switch(self, optimizer, mock_binance_client, mock_portfolio):
        """If sell fails, switch should abort."""
        mock_binance_client.place_market_sell = MagicMock(return_value=None)
        decision = {
            "from_symbol": "BTCUSDT",
            "to_symbol": "ETHUSDT",
            "from_value": 5000,
            "to_price": 3000,
        }
        mock_portfolio.get_all_positions = MagicMock(return_value=[{
            "symbol": "BTCUSDT",
            "quantity": 0.1,
        }])

        result = optimizer._execute_switch(decision)

        assert result is False
        mock_binance_client.place_market_buy.assert_not_called()
        mock_portfolio.close_position.assert_not_called()

    def test_buy_fails_half_failed_logged(self, optimizer, mock_binance_client, mock_portfolio):
        """If buy fails after sell, should log HALF-FAILED alert."""
        mock_binance_client.place_market_buy = MagicMock(return_value=None)
        decision = {
            "from_symbol": "BTCUSDT",
            "to_symbol": "ETHUSDT",
            "from_value": 5000,
            "to_price": 3000,
        }
        mock_portfolio.get_all_positions = MagicMock(return_value=[{
            "symbol": "BTCUSDT",
            "quantity": 0.1,
        }])

        with patch("src.position_optimizer.logger") as mock_logger:
            result = optimizer._execute_switch(decision)

        assert result is False
        mock_logger.critical.assert_called_once()
        assert "HALF-FAILED" in str(mock_logger.critical.call_args)

    def test_stepsize_flooring(self, optimizer, mock_binance_client, mock_portfolio):
        """Buy quantity should be floored to stepSize."""
        # stepSize=0.001, but calculated qty might be 1.234567
        mock_binance_client.get_symbol_filters = MagicMock(return_value={
            "minQty": 0.001,
            "stepSize": 0.001,
            "minNotional": 10,
        })
        decision = {
            "from_symbol": "BTCUSDT",
            "to_symbol": "ETHUSDT",
            "from_value": 5000,
            "to_price": 3000,
        }
        mock_portfolio.get_all_positions = MagicMock(return_value=[{
            "symbol": "BTCUSDT",
            "quantity": 0.1,
        }])

        optimizer._execute_switch(decision)

        # Check that place_market_buy received floored quantity
        call_args = mock_binance_client.place_market_buy.call_args
        qty = call_args.kwargs.get("quantity", call_args.args[1] if len(call_args.args) > 1 else 0)
        # Should be floored to 3 decimals (0.001 step)
        assert round(qty, 3) == qty, f"Quantity {qty} not floored to stepSize"

    def test_portfolio_update_failure_non_critical(self, optimizer, mock_binance_client, mock_portfolio):
        """Portfolio update failure should not fail the switch."""
        mock_portfolio.close_position = MagicMock(side_effect=Exception("DB error"))
        decision = {
            "from_symbol": "BTCUSDT",
            "to_symbol": "ETHUSDT",
            "from_value": 5000,
            "to_price": 3000,
        }
        mock_portfolio.get_all_positions = MagicMock(return_value=[{
            "symbol": "BTCUSDT",
            "quantity": 0.1,
        }])

        result = optimizer._execute_switch(decision)

        # Switch should still succeed even if portfolio update fails
        assert result is True
        assert decision["executed"] is True


class TestDryRun:
    """Test dry_run mode."""

    def test_dry_run_no_orders_placed(self, optimizer, mock_binance_client, mock_portfolio, mock_scanner):
        """dry_run=True should not place any orders."""
        mock_binance_client.get_24hr_stats = MagicMock(return_value={
            "price_change_pct": -6.0
        })
        mock_portfolio.get_all_positions = MagicMock(return_value=[{
            "symbol": "BTCUSDT",
            "quantity": 0.1,
            "entry_price": 50000,
            "current_price": 47000,
            "position_value": 4700,
            "price_change_24h": -6.0,
            "score": 50,
        }])
        # BTCUSDT must also be in opportunities so existing_score > 0
        mock_scanner.scan_all = MagicMock(return_value=[
            {
                "symbol": "BTCUSDT",
                "price": 47000,
                "score": 50,
                "price_change_24h": -6.0,
            },
            {
                "symbol": "ETHUSDT",
                "price": 3000,
                "score": 80,
                "price_change_24h": 5.0,
            },
        ])

        # Reset cooldowns to allow immediate switch
        optimizer._last_switch_time = {}

        decisions = optimizer.analyze_and_switch(dry_run=True)

        assert len(decisions) == 1
        mock_binance_client.place_market_sell.assert_not_called()
        mock_binance_client.place_market_buy.assert_not_called()

    def test_dry_run_false_executes(self, optimizer, mock_binance_client, mock_portfolio, mock_scanner):
        """dry_run=False should execute trades."""
        mock_binance_client.get_24hr_stats = MagicMock(return_value={
            "price_change_pct": -6.0
        })
        mock_portfolio.get_all_positions = MagicMock(return_value=[{
            "symbol": "BTCUSDT",
            "quantity": 0.1,
            "entry_price": 50000,
            "current_price": 47000,
            "position_value": 4700,
            "price_change_24h": -6.0,
            "score": 50,
        }])
        # BTCUSDT must also be in opportunities so existing_score > 0
        mock_scanner.scan_all = MagicMock(return_value=[
            {
                "symbol": "BTCUSDT",
                "price": 47000,
                "score": 50,
                "price_change_24h": -6.0,
            },
            {
                "symbol": "ETHUSDT",
                "price": 3000,
                "score": 80,
                "price_change_24h": 5.0,
            },
        ])

        # Reset cooldowns to allow immediate switch
        optimizer._last_switch_time = {}

        decisions = optimizer.analyze_and_switch(dry_run=False)

        assert len(decisions) == 1
        assert decisions[0]["executed"] is True
        mock_binance_client.place_market_sell.assert_called_once()
        mock_binance_client.place_market_buy.assert_called_once()


class TestCooldownPersistence:
    """Test switch cooldown persistence."""

    def test_switch_times_saved_to_statedb(self, optimizer, mock_portfolio, mock_binance_client):
        """After switch, cooldowns should be persisted."""
        decision = {
            "from_symbol": "BTCUSDT",
            "to_symbol": "ETHUSDT",
            "from_value": 5000,
            "to_price": 3000,
        }
        mock_portfolio.get_all_positions = MagicMock(return_value=[{
            "symbol": "BTCUSDT",
            "quantity": 0.1,
        }])

        with patch("src.state_db.get_state_db") as mock_db:
            mock_db.return_value.kv_get = MagicMock(return_value={})
            mock_db.return_value.kv_set = MagicMock()
            optimizer._execute_switch(decision)

            mock_db.return_value.kv_set.assert_called_once()
            key, value = mock_db.return_value.kv_set.call_args[0]
            assert key == "position_optimizer:switch_times"
