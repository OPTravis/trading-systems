"""
Unit tests for trade_executor.py — mock-based, no network.

Covers:
  - _check_price_deviation: normal pass + fail-closed anomaly (P0-1)
  - _check_duplicate_order: normal pass + fail-closed dup detection (P0-1)
  - count_active_positions: normal count + error returns -1 (P1-8)
  - _send_execution_notification: success + notification call verification
  - _record_trade_portfolio: success + DB exception handling
  - execute_auto_trade pre-check chain: circuit breaker / daily loss / SIGTERM (P2-6)
  - Position sizing boundaries: zero balance / insufficient / Kelly fallback
"""

import time
from unittest.mock import MagicMock, patch, call

import pytest

from src import trade_executor


# ────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────

@pytest.fixture
def mock_client():
    """Generic mock exchange client for helper-function tests."""
    c = MagicMock()
    return c


@pytest.fixture(autouse=True)
def _reset_sigterm():
    """Reset SIGTERM shutdown flag between tests."""
    trade_executor._shutting_down = False
    yield
    trade_executor._shutting_down = False


def _make_klines(closes):
    """Build kline dicts with the given close prices."""
    return [{"close": str(p)} for p in closes]


# ────────────────────────────────────────────────────────────
# _check_price_deviation
# ────────────────────────────────────────────────────────────

class TestCheckPriceDeviation:
    """P0-1: Price anomaly detection — fail-closed on errors."""

    def test_normal_price_passes(self, mock_client):
        closes = [100.0 + i * 0.1 for i in range(14)]
        mock_client.get_klines.return_value = _make_klines(closes)
        price = 100.5  # well within 3σ
        assert trade_executor._check_price_deviation(mock_client, "BTCUSDT", price) is True

    def test_anomalous_price_blocked(self, mock_client):
        closes = [100.0, 100.1, 99.9] * 4 + [100.0, 100.0]
        mock_client.get_klines.return_value = _make_klines(closes)
        price = 500.0  # massive deviation
        assert trade_executor._check_price_deviation(mock_client, "BTCUSDT", price) is False

    def test_insufficient_klines_passes(self, mock_client):
        """Fewer than 14 klines → not enough data → pass through."""
        mock_client.get_klines.return_value = _make_klines([100.0, 101.0])
        assert trade_executor._check_price_deviation(mock_client, "BTCUSDT", 999.0) is True

    def test_flat_price_passes(self, mock_client):
        """std == 0 → no deviation to check → pass."""
        mock_client.get_klines.return_value = _make_klines([100.0] * 14)
        assert trade_executor._check_price_deviation(mock_client, "BTCUSDT", 100.0) is True

    def test_api_exception_fail_closed(self, mock_client):
        """P0-1: Exception during check → block trade (fail-closed)."""
        mock_client.get_klines.side_effect = ConnectionError("API down")
        assert trade_executor._check_price_deviation(mock_client, "BTCUSDT", 100.0) is False


# ────────────────────────────────────────────────────────────
# _check_duplicate_order
# ────────────────────────────────────────────────────────────

class TestCheckDuplicateOrder:
    """P0-1: Duplicate order detection — fail-closed on errors."""

    def test_no_duplicate_passes(self, mock_client):
        mock_client.get_open_orders.return_value = []
        assert trade_executor._check_duplicate_order(mock_client, "BTCUSDT") is True

    def test_sell_order_only_passes(self, mock_client):
        mock_client.get_open_orders.return_value = [
            {"side": "SELL", "orderId": 1},
        ]
        assert trade_executor._check_duplicate_order(mock_client, "BTCUSDT") is True

    def test_duplicate_buy_blocked(self, mock_client):
        mock_client.get_open_orders.return_value = [
            {"side": "BUY", "orderId": 42},
        ]
        assert trade_executor._check_duplicate_order(mock_client, "BTCUSDT") is False

    def test_api_exception_fail_closed(self, mock_client):
        mock_client.get_open_orders.side_effect = TimeoutError("timeout")
        assert trade_executor._check_duplicate_order(mock_client, "BTCUSDT") is False


# ────────────────────────────────────────────────────────────
# count_active_positions
# ────────────────────────────────────────────────────────────

class TestCountActivePositions:
    """P1-8: Active position counter — returns -1 on error (fail-closed)."""

    def test_normal_count(self, mock_client):
        mock_client.get_account.return_value = {
            "balances": [
                {"asset": "USDT", "free": "1000", "locked": "0"},
                {"asset": "BTC", "free": "0.5", "locked": "0"},
                {"asset": "ETH", "free": "2.0", "locked": "0"},
            ]
        }
        mock_client.get_24hr_stats.return_value = [
            {"symbol": "BTCUSDT", "last_price": "40000"},
            {"symbol": "ETHUSDT", "last_price": "2000"},
        ]
        assert trade_executor.count_active_positions(mock_client) == 2

    def test_dust_filtered(self, mock_client):
        """Positions worth < $5 should not be counted."""
        mock_client.get_account.return_value = {
            "balances": [
                {"asset": "USDT", "free": "1000", "locked": "0"},
                {"asset": "DOGE", "free": "10", "locked": "0"},  # $1 < $5
            ]
        }
        mock_client.get_24hr_stats.return_value = [
            {"symbol": "DOGEUSDT", "last_price": "0.1"},
        ]
        assert trade_executor.count_active_positions(mock_client) == 0

    def test_ntrn_excluded(self, mock_client):
        mock_client.get_account.return_value = {
            "balances": [
                {"asset": "USDT", "free": "1000", "locked": "0"},
                {"asset": "NTRN", "free": "100", "locked": "0"},
            ]
        }
        mock_client.get_24hr_stats.return_value = []
        assert trade_executor.count_active_positions(mock_client) == 0

    def test_account_fetch_error_returns_minus1(self, mock_client):
        """P1-8: Account fetch failure → return -1 (not 0)."""
        mock_client.get_account.side_effect = ConnectionError("API error")
        assert trade_executor.count_active_positions(mock_client) == -1

    def test_no_price_skips(self, mock_client):
        """Asset with no available price → conservatively skipped."""
        mock_client.get_account.return_value = {
            "balances": [
                {"asset": "USDT", "free": "1000", "locked": "0"},
                {"asset": "NEWCOIN", "free": "5", "locked": "0"},
            ]
        }
        mock_client.get_24hr_stats.return_value = []
        assert trade_executor.count_active_positions(mock_client) == 0


# ────────────────────────────────────────────────────────────
# _send_execution_notification
# ────────────────────────────────────────────────────────────

class TestSendExecutionNotification:
    """Notification sending — verify correct call patterns."""

    def test_normal_send(self):
        notifier = MagicMock()
        notifier.send_text.return_value = True
        trade_executor._send_execution_notification(
            notifier, "BTCUSDT", "dip_buy", "HIGH", 85,
            0.15, 1000.0, 150.0, {"win_rate": 0.6, "confidence": "high"},
            0.004, 40000.0, "MACD cross", 1, 5, ["BUY: 0.004 @ $40000"],
        )
        notifier.send_text.assert_called_once()
        # Verify the message contains key info
        sent_text = notifier.send_text.call_args[0][0]
        assert "BTCUSDT" in sent_text
        assert "dip_buy".upper() in sent_text

    def test_notification_includes_all_order_results(self):
        notifier = MagicMock()
        trade_executor._send_execution_notification(
            notifier, "ETHUSDT", "breakout", "MEDIUM", 70,
            0.10, 2000.0, 200.0, {"win_rate": 0.5, "confidence": "medium"},
            0.1, 2000.0, "EMA cross", 2, 5,
            ["SL: 0.1 @ $1900", "TP1: 0.04 @ $2100"],
        )
        sent_text = notifier.send_text.call_args[0][0]
        assert "SL: 0.1 @ $1900" in sent_text
        assert "TP1: 0.04 @ $2100" in sent_text


# ────────────────────────────────────────────────────────────
# _record_trade_portfolio
# ────────────────────────────────────────────────────────────

class TestRecordTradePortfolio:
    """Portfolio tracking — DB exception should not crash."""

    def test_normal_record(self):
        client = MagicMock()
        client.get_free_balance.return_value = 950.0

        with patch("src.trade_executor.PortfolioManager") as MockPM:
            pm_instance = MagicMock()
            MockPM.return_value = pm_instance
            pm_instance.positions = {}

            trade_executor._record_trade_portfolio(
                client, "BTCUSDT", 0.004, 40000.0, "dip_buy",
                1000.0, 150.0, 0.001,
                0.15, None, 1.0,
            )
            pm_instance.update_balance.assert_called_once_with(950.0)
            pm_instance.add_position.assert_called_once()

    def test_balance_fetch_fallback_on_error(self):
        """When get_free_balance fails, falls back to computed balance."""
        client = MagicMock()
        client.get_free_balance.side_effect = Exception("API error")

        with patch("src.trade_executor.PortfolioManager") as MockPM:
            pm_instance = MagicMock()
            MockPM.return_value = pm_instance
            pm_instance.positions = {}

            trade_executor._record_trade_portfolio(
                client, "BTCUSDT", 0.004, 40000.0, "dip_buy",
                1000.0, 150.0, 0.001,
                0.15, None, 1.0,
            )
            # Should have called update_balance with computed fallback value
            pm_instance.update_balance.assert_called_once()
            fallback = pm_instance.update_balance.call_args[0][0]
            assert fallback < 1000.0  # should be less than initial balance after investment

    def test_portfolio_exception_does_not_raise(self):
        """PortfolioManager failure should be caught internally."""
        client = MagicMock()
        client.get_free_balance.return_value = 950.0

        with patch("src.trade_executor.PortfolioManager") as MockPM:
            MockPM.side_effect = RuntimeError("Cannot init PM")
            # Should not raise
            trade_executor._record_trade_portfolio(
                client, "BTCUSDT", 0.004, 40000.0, "dip_buy",
                1000.0, 150.0, 0.001,
                0.15, None, 1.0,
            )


# ────────────────────────────────────────────────────────────
# execute_auto_trade — pre-check chain
# ────────────────────────────────────────────────────────────

class TestExecuteAutoTradePreChecks:
    """Test the safety gates that run before any order placement."""

    def _setup_base_mocks(self):
        """Return a dict of common mocks for execute_auto_trade tests."""
        client = MagicMock()
        client.get_free_balance.return_value = 1000.0
        client.get_account.return_value = {
            "balances": [{"asset": "USDT", "free": "1000", "locked": "0"}]
        }
        return client

    def _default_trade_args(self, **overrides):
        """Default args for execute_auto_trade."""
        args = dict(
            symbol="BTCUSDT",
            price=40000.0,
            strategy="dip_buy",
            stop_loss_pct=5.0,
            tp_levels=[{"pct": 5.0, "size_pct": 50}],
            stop_price=38000.0,
            max_hold=24,
            signals={},
            reason="test",
            score=80,
        )
        args.update(overrides)
        return args

    def test_sigterm_blocks_trade(self):
        """P2-6: SIGTERM flag prevents new trades."""
        trade_executor._shutting_down = True
        result = trade_executor.execute_auto_trade(**self._default_trade_args())
        assert result["success"] is False
        assert "shutdown" in result["reason"]

    def test_zero_stop_loss_blocked(self):
        """Invalid stop_loss_pct <= 0 → block trade."""
        result = trade_executor.execute_auto_trade(
            **self._default_trade_args(stop_loss_pct=0)
        )
        assert result["success"] is False
        assert "Invalid stop_loss_pct" in result["reason"]

    def test_insufficient_balance(self):
        """Balance < $10 → reject."""
        client = self._setup_base_mocks()
        client.get_free_balance.return_value = 5.0
        with patch("src.trade_executor.get_trading_client", return_value=client), \
             patch("src.trade_executor.FeishuNotifier"):
            result = trade_executor.execute_auto_trade(**self._default_trade_args())
        assert result["success"] is False
        assert "Insufficient USDT" in result["error"]

    def test_circuit_breaker_tripped(self):
        """Circuit breaker tripped → block trade."""
        client = self._setup_base_mocks()
        cb_mock = MagicMock()
        cb_mock.is_tripped.return_value = True
        with patch("src.trade_executor.get_trading_client", return_value=client), \
             patch("src.trade_executor.FeishuNotifier"), \
             patch("src.circuit_breaker.CircuitBreaker", return_value=cb_mock):
            result = trade_executor.execute_auto_trade(**self._default_trade_args())
        assert result["success"] is False
        assert "Circuit breaker tripped" in result["error"]

    def test_circuit_breaker_check_failure_blocks(self):
        """Circuit breaker check failure → block trade (fail-closed)."""
        client = self._setup_base_mocks()
        with patch("src.trade_executor.get_trading_client", return_value=client), \
             patch("src.trade_executor.FeishuNotifier"), \
             patch("src.circuit_breaker.CircuitBreaker", side_effect=Exception("DB error")):
            result = trade_executor.execute_auto_trade(**self._default_trade_args())
        assert result["success"] is False
        assert "Circuit breaker check failed" in result["error"]

    def test_daily_loss_tier3_halt(self):
        """Daily loss tier 3 (close all) → block trade."""
        client = self._setup_base_mocks()
        cb_mock = MagicMock()
        cb_mock.is_tripped.return_value = False
        dlb_mock = MagicMock()
        dlb_mock.should_close_all.return_value = True
        dlb_mock.should_block_new_trades.return_value = True
        dlb_mock.check_daily_loss.return_value = {"tier": 3, "action": "close_all_and_halt"}

        with patch("src.trade_executor.get_trading_client", return_value=client), \
             patch("src.trade_executor.FeishuNotifier"), \
             patch("src.circuit_breaker.CircuitBreaker", return_value=cb_mock), \
             patch("src.daily_loss_breaker.get_daily_loss_breaker", return_value=dlb_mock):
            result = trade_executor.execute_auto_trade(**self._default_trade_args())
        assert result["success"] is False
        assert "close all" in result["error"]

    def test_daily_loss_tier2_blocks_new_trades(self):
        """Daily loss tier 2 → block new trades."""
        client = self._setup_base_mocks()
        cb_mock = MagicMock()
        cb_mock.is_tripped.return_value = False
        dlb_mock = MagicMock()
        dlb_mock.should_close_all.return_value = False
        dlb_mock.should_block_new_trades.return_value = True
        dlb_mock.check_daily_loss.return_value = {"tier": 2, "action": "block_new_trades"}

        with patch("src.trade_executor.get_trading_client", return_value=client), \
             patch("src.trade_executor.FeishuNotifier"), \
             patch("src.circuit_breaker.CircuitBreaker", return_value=cb_mock), \
             patch("src.daily_loss_breaker.get_daily_loss_breaker", return_value=dlb_mock):
            result = trade_executor.execute_auto_trade(**self._default_trade_args())
        assert result["success"] is False
        assert "new trades blocked" in result["error"]

    def test_stepwise_drawdown_blocks(self):
        """Severe stepwise drawdown → block new trades."""
        client = self._setup_base_mocks()
        cb_mock = MagicMock()
        cb_mock.is_tripped.return_value = False
        dlb_mock = MagicMock()
        dlb_mock.should_close_all.return_value = False
        dlb_mock.should_block_new_trades.return_value = False
        dlb_mock.get_position_size_multiplier.return_value = 1.0
        dlb_mock.check_daily_loss.return_value = {"tier": 0, "action": "none"}

        dd_breaker_mock = MagicMock()
        dd_breaker_mock.check_drawdown.return_value = {"drawdown_pct": 9.0, "tripped": False}

        sd_action = {
            "level": "severe", "size_multiplier": 0.0,
            "block_new_trades": True, "reason": "Severe drawdown",
        }

        with patch("src.trade_executor.get_trading_client", return_value=client), \
             patch("src.trade_executor.FeishuNotifier"), \
             patch("src.circuit_breaker.CircuitBreaker", return_value=cb_mock), \
             patch("src.daily_loss_breaker.get_daily_loss_breaker", return_value=dlb_mock), \
             patch("src.drawdown_breaker.DrawdownBreaker", return_value=dd_breaker_mock), \
             patch("src.stepwise_drawdown.get_drawdown_action", return_value=sd_action):
            result = trade_executor.execute_auto_trade(**self._default_trade_args())
        assert result["success"] is False
        assert "Stepwise drawdown" in result["error"]

    def test_count_positions_failure_blocks(self):
        """P1-8: count_active_positions returning -1 → block trade."""
        client = self._setup_base_mocks()
        cb_mock = MagicMock()
        cb_mock.is_tripped.return_value = False
        dlb_mock = MagicMock()
        dlb_mock.should_close_all.return_value = False
        dlb_mock.should_block_new_trades.return_value = False
        dlb_mock.get_position_size_multiplier.return_value = 1.0
        dlb_mock.check_daily_loss.return_value = {"tier": 0, "action": "none"}

        dd_breaker_mock = MagicMock()
        dd_breaker_mock.check_drawdown.return_value = {"drawdown_pct": 0.0, "tripped": False}

        sd_action = {"level": "normal", "size_multiplier": 1.0,
                     "block_new_trades": False, "reason": "normal"}

        with patch("src.trade_executor.get_trading_client", return_value=client), \
             patch("src.trade_executor.FeishuNotifier"), \
             patch("src.circuit_breaker.CircuitBreaker", return_value=cb_mock), \
             patch("src.daily_loss_breaker.get_daily_loss_breaker", return_value=dlb_mock), \
             patch("src.drawdown_breaker.DrawdownBreaker", return_value=dd_breaker_mock), \
             patch("src.stepwise_drawdown.get_drawdown_action", return_value=sd_action), \
             patch("src.trade_executor.count_active_positions", return_value=-1):
            result = trade_executor.execute_auto_trade(**self._default_trade_args())
        assert result["success"] is False
        assert "count_active_positions failed" in result["error"]

    def test_score_too_low(self):
        """Score < 60 → skip trade."""
        client = self._setup_base_mocks()
        cb_mock = MagicMock()
        cb_mock.is_tripped.return_value = False
        dlb_mock = MagicMock()
        dlb_mock.should_close_all.return_value = False
        dlb_mock.should_block_new_trades.return_value = False
        dlb_mock.get_position_size_multiplier.return_value = 1.0
        dlb_mock.check_daily_loss.return_value = {"tier": 0, "action": "none"}

        dd_breaker_mock = MagicMock()
        dd_breaker_mock.check_drawdown.return_value = {"drawdown_pct": 0.0, "tripped": False}

        sd_action = {"level": "normal", "size_multiplier": 1.0,
                     "block_new_trades": False, "reason": "normal"}

        with patch("src.trade_executor.get_trading_client", return_value=client), \
             patch("src.trade_executor.FeishuNotifier"), \
             patch("src.circuit_breaker.CircuitBreaker", return_value=cb_mock), \
             patch("src.daily_loss_breaker.get_daily_loss_breaker", return_value=dlb_mock), \
             patch("src.drawdown_breaker.DrawdownBreaker", return_value=dd_breaker_mock), \
             patch("src.stepwise_drawdown.get_drawdown_action", return_value=sd_action), \
             patch("src.trade_executor.count_active_positions", return_value=2):
            result = trade_executor.execute_auto_trade(
                **self._default_trade_args(score=55)
            )
        assert result["success"] is False
        assert "Score too low" in result["error"]

    def test_max_positions_reached(self):
        """Max positions reached → reject."""
        client = self._setup_base_mocks()
        cb_mock = MagicMock()
        cb_mock.is_tripped.return_value = False
        dlb_mock = MagicMock()
        dlb_mock.should_close_all.return_value = False
        dlb_mock.should_block_new_trades.return_value = False
        dlb_mock.get_position_size_multiplier.return_value = 1.0
        dlb_mock.check_daily_loss.return_value = {"tier": 0, "action": "none"}

        dd_breaker_mock = MagicMock()
        dd_breaker_mock.check_drawdown.return_value = {"drawdown_pct": 0.0, "tripped": False}

        sd_action = {"level": "normal", "size_multiplier": 1.0,
                     "block_new_trades": False, "reason": "normal"}

        with patch("src.trade_executor.get_trading_client", return_value=client), \
             patch("src.trade_executor.FeishuNotifier"), \
             patch("src.circuit_breaker.CircuitBreaker", return_value=cb_mock), \
             patch("src.daily_loss_breaker.get_daily_loss_breaker", return_value=dlb_mock), \
             patch("src.drawdown_breaker.DrawdownBreaker", return_value=dd_breaker_mock), \
             patch("src.stepwise_drawdown.get_drawdown_action", return_value=sd_action), \
             patch("src.trade_executor.count_active_positions", return_value=5):
            result = trade_executor.execute_auto_trade(**self._default_trade_args())
        assert result["success"] is False
        assert "Max positions" in result["error"]


# ────────────────────────────────────────────────────────────
# get_position_tier
# ────────────────────────────────────────────────────────────

class TestGetPositionTier:

    def test_high_tier(self):
        pct, label = trade_executor.get_position_tier(95)
        assert pct == 0.50
        assert label == "HIGH"

    def test_medium_high_tier(self):
        pct, label = trade_executor.get_position_tier(80)
        assert pct == 0.30
        assert label == "MEDIUM-HIGH"

    def test_medium_tier(self):
        pct, label = trade_executor.get_position_tier(70)
        assert pct == 0.20
        assert label == "MEDIUM"

    def test_cautious_tier(self):
        pct, label = trade_executor.get_position_tier(62)
        assert pct == 0.15
        assert label == "CAUTIOUS"

    def test_skip_tier(self):
        pct, label = trade_executor.get_position_tier(50)
        assert pct == 0.0
        assert label == "SKIP"


# ────────────────────────────────────────────────────────────
# Tiered SL preservation bug (2026-07-05)
# When tiered TP fails after SL was placed, exception handler
# must NOT cancel the SL, and must carry it over to fallback.
# ────────────────────────────────────────────────────────────

class TestTieredSLPreservation:
    """Bug fix: tiered exception handler was cancelling successfully-placed SL
    and using wrong key ('id' instead of 'orderId') for cancel.

    Note: With TP-first ordering (Binance spot balance locking fix),
    TPs are placed before SL. These tests use positions > _min_notional*6
    to ensure tiered path is used.
    """

    def test_sl_preserved_when_tp_fails(self):
        """When TPs fail after partial placement, residue must be cancelled
        and fallback path must handle it."""
        from src.trade_executor import _place_sl_tp_orders

        client = MagicMock()
        notifier = MagicMock()

        # TP1 succeeds, TP2 fails
        client.place_order.side_effect = [
            {"orderId": 20001, "status": "NEW"},  # TP1 success
            None,  # TP2 fails (returns None)
        ]
        client.get_open_orders.return_value = [
            {"orderId": 20001, "type": "LIMIT", "price": "95.0", "side": "SELL"},
        ]
        client.get_price_precision.return_value = 2

        result = _place_sl_tp_orders(
            client, notifier, "TESTUSDT",
            executed_qty=0.5,
            price=89.62,
            p_prec=2,
            stop_loss_pct=8.0,
            tp_levels=[{"pct": 6, "size_pct": 40}, {"pct": 9, "size_pct": 40}, {"pct": 12, "size_pct": 20}],
            _step_size=0.001,
            _qty_decimals=3,
            _min_notional=5.0,
            strategy_size_multiplier=1.0,
        )

        # The TP1 residue should have been cancelled in fallback
        client.cancel_order.assert_called()
        call_args = client.cancel_order.call_args
        assert call_args[0][1] == 20001

    def test_cancel_uses_correct_key(self):
        """Cancel must use 'orderId' not 'id' (Binance SDK field)."""
        from src.trade_executor import _place_sl_tp_orders

        client = MagicMock()
        notifier = MagicMock()

        # All TPs fail, then residue needs cancelling
        client.place_order.return_value = None  # everything fails
        client.get_open_orders.return_value = [
            {"orderId": 99999, "type": "LIMIT", "price": "95.0", "side": "SELL"},
        ]
        client.get_price_precision.return_value = 2

        _place_sl_tp_orders(
            client, notifier, "TESTUSDT",
            executed_qty=0.5,
            price=89.62,
            p_prec=2,
            stop_loss_pct=8.0,
            tp_levels=[{"pct": 6, "size_pct": 40}, {"pct": 9, "size_pct": 40}, {"pct": 12, "size_pct": 20}],
            _step_size=0.001,
            _qty_decimals=3,
            _min_notional=5.0,
            strategy_size_multiplier=1.0,
        )

        # cancel_order must have been called with orderId=99999, not None
        client.cancel_order.assert_called()
        call_args = client.cancel_order.call_args
        assert call_args[0][1] == 99999 or call_args[1].get("order_id") == 99999


# ────────────────────────────────────────────────────────────
# _place_sl_tp_orders — SL-only fallback (bug#11, 81a5e46)
# ────────────────────────────────────────────────────────────
class TestSlOnlyPercentPriceFallback:
    """Small-position path: STOP_LOSS_LIMIT rejected by PERCENT_PRICE_BY_SIDE
    (ZEC-style wide SL on high-vol symbols) must degrade to STOP_LOSS
    (market-on-trigger, no limit price) instead of leaving the position
    naked / force-closing it."""

    # Small position: 0.04 × $581.29 ≈ $23.25 < 6 × $5 min notional
    PARAMS = dict(
        symbol="ZECUSDT",
        executed_qty=0.04,
        price=581.29,
        p_prec=2,
        stop_loss_pct=5.0,
        tp_levels=[{"pct": 10.0}],
        _step_size=0.001,
        _qty_decimals=3,
        _min_notional=5.0,
        strategy_size_multiplier=1.0,
    )

    def _run(self, client, notifier):
        with patch.object(trade_executor.time, "sleep"):
            return trade_executor._place_sl_tp_orders(
                client, notifier, **self.PARAMS
            )

    def test_sl_limit_success_never_uses_fallback(self):
        client, notifier = MagicMock(), MagicMock()
        client.get_klines.return_value = []
        client.place_order.return_value = {"orderId": 111}
        out = self._run(client, notifier)
        assert any(r.startswith("SL-only") for r in out["results"])
        assert out["sl_placed_qty"] == 0.04
        assert client.place_order.call_count == 1
        assert client.place_order.call_args.args[2] == "STOP_LOSS_LIMIT"

    def test_percent_price_rejection_falls_back_to_market_stop(self):
        client, notifier = MagicMock(), MagicMock()
        client.get_klines.return_value = []
        calls = []

        def place(symbol, side, otype, qty, **kw):
            calls.append((otype, kw))
            if otype == "STOP_LOSS_LIMIT":
                raise Exception("-1013 PERCENT_PRICE_BY_SIDE filter")
            return {"orderId": 222}

        client.place_order.side_effect = place
        out = self._run(client, notifier)
        # 2 rejected STOP_LOSS_LIMIT attempts + 1 STOP_LOSS fallback
        assert sum(1 for t, _ in calls if t == "STOP_LOSS_LIMIT") == 2
        assert calls[-1][0] == "STOP_LOSS"
        # market-on-trigger: stop price preserved, NO limit price kwarg
        kw = calls[-1][1]
        assert "price" not in kw
        assert kw["stop_price"] == round(581.29 * 0.95, 2)
        assert any("SL-fallback (market trigger)" in r for r in out["results"])
        assert out["sl_placed_qty"] == 0.04

    def test_all_paths_failed_urgent_alert(self):
        client, notifier = MagicMock(), MagicMock()
        client.get_klines.return_value = []
        client.place_order.side_effect = Exception("-1013 filter")
        out = self._run(client, notifier)
        assert any("SL: FAILED" in r for r in out["results"])
        assert out["sl_placed_qty"] == 0.0
        # two alerts expected: "URGENT: SL failed" + naked-position escalation
        msgs = [c.args[0] for c in notifier.send_text.call_args_list]
        assert any("URGENT" in m for m in msgs)
        assert any("裸露" in m for m in msgs)
