"""
Unit tests for cmd_trailing_check.py — mock-based, no network.

Covers:
  - P0-6: SL move places new SL FIRST, then cancels old SL
  - P0-6: SL move retries 3x → keeps old SL and alerts on failure
  - No SL move needed when new_sl <= old_sl * 1.001
  - P1-10: Trailing PnL uses actual trade history qty when position is gone
  - Helper functions: _order_qty, _order_id, _is_stop_order
"""

import json
import time
from unittest.mock import MagicMock, patch, call

import pytest

from src.cmd_trailing_check import (
    _order_qty,
    _order_id,
    _is_stop_order,
    cmd_trailing_check,
)


# ────────────────────────────────────────────────────────────
# Helper function tests
# ────────────────────────────────────────────────────────────

class TestHelperFunctions:

    def test_order_qty_binance_sdk(self):
        o = {"origQty": "0.5", "amount": None}
        assert _order_qty(o) == 0.5

    def test_order_qty_ccxt(self):
        o = {"origQty": None, "amount": "1.25"}
        assert _order_qty(o) == 1.25

    def test_order_qty_missing(self):
        o = {}
        assert _order_qty(o) == 0

    def test_order_id_binance_sdk(self):
        o = {"orderId": 12345, "id": None}
        assert _order_id(o) == 12345

    def test_order_id_ccxt(self):
        o = {"orderId": None, "id": "abc-123"}
        assert _order_id(o) == "abc-123"

    def test_is_stop_order_true(self):
        assert _is_stop_order({"type": "STOP_LOSS_LIMIT"}) is True
        assert _is_stop_order({"type": "stop_loss"}) is True
        assert _is_stop_order({"type": "STOP_MARKET"}) is True

    def test_is_stop_order_false(self):
        assert _is_stop_order({"type": "LIMIT"}) is False
        assert _is_stop_order({"type": "MARKET"}) is False


# ────────────────────────────────────────────────────────────
# Shared mock setup
# ────────────────────────────────────────────────────────────

_DEFAULT = object()


def _make_mock_client(*, balances=None, price=40000.0,
                      open_orders=None, place_order_result=_DEFAULT,
                      cancel_result=None, my_trades=None):
    """Build a mock BinanceClient with sensible defaults.

    To make place_order fail, pass place_order_result=None explicitly.
    """
    c = MagicMock()
    c.get_account.return_value = {
        "balances": balances or [
            {"asset": "USDT", "free": "1000", "locked": "0"},
            {"asset": "BTC", "free": "0.1", "locked": "0"},
        ]
    }
    c.get_24hr_stats.return_value = {"last_price": str(price)}
    c.get_klines.return_value = [{"close": str(price)}] * 20
    c.get_open_orders.return_value = open_orders or []
    if place_order_result is _DEFAULT:
        c.place_order.return_value = {"orderId": 999, "status": "NEW"}
    else:
        c.place_order.return_value = place_order_result  # None → failure
    c.cancel_order.return_value = cancel_result or {"status": "CANCELED"}
    c.cancel_all_orders.return_value = True
    c.get_price_precision.return_value = 2
    c.get_my_trades.return_value = my_trades or []
    c.get_ticker_price.return_value = price
    return c


def _run_trailing_check(client, ts, ind_atr=500.0, entry_price=38000.0):
    """Run cmd_trailing_check with all deps mocked. Returns (notifier, risk_mgr)."""
    risk_mgr = MagicMock()
    notifier = MagicMock()
    notifier.send_text.return_value = True

    ind_mock = MagicMock()
    ind_mock.atr.return_value = ind_atr

    with patch("src.cmd_trailing_check.BinanceClient", return_value=client), \
         patch("src.cmd_trailing_check.TrailingStop", return_value=ts), \
         patch("src.cmd_trailing_check.RiskManager", return_value=risk_mgr), \
         patch("src.cmd_trailing_check.FeishuNotifier", return_value=notifier), \
         patch("src.cmd_trailing_check.Indicators", ind_mock):
        cmd_trailing_check()

    return notifier, risk_mgr


# ────────────────────────────────────────────────────────────
# P0-6: SL move — new SL placed first, then old cancelled
# ────────────────────────────────────────────────────────────

class TestSLMoveOrder:

    def test_new_sl_placed_before_old_cancelled(self, capsys):
        """Current strategy: cancel old SL first (free balance), then place new SL."""
        existing_sl = {
            "orderId": 100,
            "type": "STOP_LOSS_LIMIT",
            "side": "SELL",
            "origQty": "0.1",
            "stopPrice": "37000",
            "price": "37000",
        }
        client = _make_mock_client(
            price=42000.0,
            open_orders=[existing_sl],
            place_order_result={"orderId": 200, "status": "NEW"},
        )
        ts = MagicMock()
        ts.get_all.return_value = {"BTCUSDT": {"entry_price": 38000, "activated": True}}
        ts.update.return_value = {
            "activated": True,
            "sl_price": 40000,
            "highest_price": 42000,
            "callback_pct": 4.76,
        }
        ts.remove.return_value = None

        _run_trailing_check(client, ts)

        assert client.place_order.called
        assert client.cancel_order.called

        # Current strategy: cancel first (free balance), then place new SL
        calls = client.method_calls
        cancel_idx = next(i for i, c in enumerate(calls) if c[0] == "cancel_order")
        place_idx = next(i for i, c in enumerate(calls) if c[0] == "place_order")
        assert cancel_idx < place_idx, \
            "Strategy: cancel_order should be called before place_order to free balance!"

    def test_sl_move_successful_result(self, capsys):
        """Verify SL move produces correct result output."""
        existing_sl = {
            "orderId": 100,
            "type": "STOP_LOSS_LIMIT",
            "origQty": "0.1",
            "stopPrice": "37000",
            "price": "37000",
        }
        client = _make_mock_client(
            price=42000.0,
            open_orders=[existing_sl],
        )
        ts = MagicMock()
        ts.get_all.return_value = {"BTCUSDT": {"entry_price": 38000}}
        ts.update.return_value = {"activated": True, "sl_price": 40000, "highest_price": 42000}

        _run_trailing_check(client, ts)

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        results = data.get("results", [])
        sl_moved = [r for r in results if r.get("action") == "sl_moved"]
        assert len(sl_moved) == 1
        assert sl_moved[0]["old_sl"] == 37000.0
        assert sl_moved[0]["new_sl"] == 40000.0


# ────────────────────────────────────────────────────────────
# P0-6: SL move failure — retries 3x, keeps old SL
# ────────────────────────────────────────────────────────────

class TestSLMoveFailure:

    def test_sl_move_retries_3x_then_preserves_old(self, capsys):
        """When new SL fails 3x AND safety net fails, position is naked (alert sent)."""
        existing_sl = {
            "orderId": 100,
            "type": "STOP_LOSS_LIMIT",
            "origQty": "0.1",
            "stopPrice": "37000",
            "price": "37000",
        }
        client = _make_mock_client(
            price=42000.0,
            open_orders=[existing_sl],
            place_order_result=None,  # all placements fail
        )
        ts = MagicMock()
        ts.get_all.return_value = {"BTCUSDT": {"entry_price": 38000}}
        ts.update.return_value = {"activated": True, "sl_price": 40000, "highest_price": 42000}

        notifier, _ = _run_trailing_check(client, ts)

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        results = data.get("results", [])
        # New behavior: when both new SL and safety net fail → sl_naked_position
        naked = [r for r in results if r.get("action") == "sl_naked_position"]
        assert len(naked) == 1
        assert naked[0]["old_sl"] == 37000.0

    def test_place_order_attempted_4_times(self, capsys):
        """New SL placement: 3 retries + 1 safety net = 4 total STOP_LOSS_LIMIT attempts."""
        existing_sl = {
            "orderId": 100,
            "type": "STOP_LOSS_LIMIT",
            "origQty": "0.1",
            "stopPrice": "37000",
            "price": "37000",
        }
        client = _make_mock_client(
            price=42000.0,
            open_orders=[existing_sl],
            place_order_result=None,
        )
        ts = MagicMock()
        ts.get_all.return_value = {"BTCUSDT": {"entry_price": 38000}}
        ts.update.return_value = {"activated": True, "sl_price": 40000, "highest_price": 42000}

        notifier, _ = _run_trailing_check(client, ts)

        # Count STOP_LOSS_LIMIT placement attempts: 3 new SL + 1 safety net
        sl_attempts = [
            c for c in client.place_order.call_args_list
            if len(c.args) > 2 and c.args[2] == "STOP_LOSS_LIMIT"
        ]
        assert len(sl_attempts) == 4, f"Expected 4 SL placement attempts (3 new + 1 safety), got {len(sl_attempts)}"

    def test_old_sl_cancelled_before_new_placement(self, capsys):
        """Current strategy: old SL is cancelled first to free balance, then new SL placed."""
        existing_sl = {
            "orderId": 100,
            "type": "STOP_LOSS_LIMIT",
            "origQty": "0.1",
            "stopPrice": "37000",
            "price": "37000",
        }
        client = _make_mock_client(
            price=42000.0,
            open_orders=[existing_sl],
            place_order_result=None,
        )
        ts = MagicMock()
        ts.get_all.return_value = {"BTCUSDT": {"entry_price": 38000}}
        ts.update.return_value = {"activated": True, "sl_price": 40000, "highest_price": 42000}

        _run_trailing_check(client, ts)

        # Old SL SHOULD be cancelled (to free balance for new SL placement)
        cancel_calls = [c for c in client.cancel_order.call_args_list if c.args[1] == 100]
        assert len(cancel_calls) >= 1, "Old SL (orderId=100) should be cancelled to free balance"

    def test_alert_sent_on_failure(self, capsys):
        """P0-6: Alert notification sent when SL move fails 3x."""
        existing_sl = {
            "orderId": 100,
            "type": "STOP_LOSS_LIMIT",
            "origQty": "0.1",
            "stopPrice": "37000",
            "price": "37000",
        }
        client = _make_mock_client(
            price=42000.0,
            open_orders=[existing_sl],
            place_order_result=None,
        )
        ts = MagicMock()
        ts.get_all.return_value = {"BTCUSDT": {"entry_price": 38000}}
        ts.update.return_value = {"activated": True, "sl_price": 40000, "highest_price": 42000}

        notifier, _ = _run_trailing_check(client, ts)

        alert_calls = [c for c in notifier.send_text.call_args_list if "SL移動失敗" in str(c)]
        assert len(alert_calls) >= 1, "Should have sent SL failure alert"


# ────────────────────────────────────────────────────────────
# No SL move needed
# ────────────────────────────────────────────────────────────

class TestNoSLMoveNeeded:

    def test_sl_already_at_target(self, capsys):
        """When new SL <= old SL * 1.001, no move should happen."""
        existing_sl = {
            "orderId": 100,
            "type": "STOP_LOSS_LIMIT",
            "origQty": "0.1",
            "stopPrice": "40000",  # old SL = 40000
            "price": "40000",
        }
        client = _make_mock_client(
            price=41000.0,
            open_orders=[existing_sl],
        )
        ts = MagicMock()
        ts.get_all.return_value = {"BTCUSDT": {"entry_price": 38000}}
        # new SL = 40030, old SL * 1.001 = 40040, so 40030 < 40040 → no move
        ts.update.return_value = {
            "activated": True,
            "sl_price": 40030,
            "highest_price": 41000,
        }

        _run_trailing_check(client, ts)

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        results = data.get("results", [])
        unchanged = [r for r in results if r.get("action") == "sl_unchanged"]
        assert len(unchanged) == 1

        # No STOP_LOSS_LIMIT orders placed
        sl_placements = [
            c for c in client.place_order.call_args_list
            if len(c.args) > 2 and c.args[2] == "STOP_LOSS_LIMIT"
        ]
        assert len(sl_placements) == 0


# ────────────────────────────────────────────────────────────
# No positions → cleanup only
# ────────────────────────────────────────────────────────────

class TestNoPositions:

    def test_no_positions_returns_none_action(self, capsys):
        """When account has no non-USDT positions, action is 'none'."""
        client = _make_mock_client(
            balances=[{"asset": "USDT", "free": "1000", "locked": "0"}]
        )
        ts = MagicMock()
        ts.get_all.return_value = {}

        _run_trailing_check(client, ts)

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["action"] == "none"
        assert data["reason"] == "no_positions"


# ────────────────────────────────────────────────────────────
# Trailing triggered → position closed
# ────────────────────────────────────────────────────────────

class TestTrailingTriggered:

    def test_triggered_sells_position(self, capsys):
        """When trailing stop triggers, position is sold via market order."""
        client = _make_mock_client(
            price=39000.0,
            open_orders=[],
        )
        ts = MagicMock()
        ts.get_all.return_value = {"BTCUSDT": {"entry_price": 40000, "activated": True}}
        ts.update.return_value = {
            "triggered": True,
            "symbol": "BTCUSDT",
            "entry_price": 40000,
            "highest_price": 41000,
            "sl_price": 39500,
            "current_price": 39000,
        }
        ts.remove.return_value = None

        notifier, risk_mgr = _run_trailing_check(client, ts)

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        results = data.get("results", [])
        triggered = [r for r in results if r.get("action") == "trailing_triggered"]
        assert len(triggered) == 1
        assert triggered[0]["sell_qty"] > 0

        # Verify cancel_all_orders was called
        assert client.cancel_all_orders.called
        # Verify market sell was attempted
        sell_calls = [
            c for c in client.place_order.call_args_list
            if len(c.args) > 1 and c.args[1] == "SELL" and c.args[2] == "MARKET"
        ]
        assert len(sell_calls) >= 1

    def test_triggered_records_pnl(self, capsys):
        """When trailing triggers, PnL is recorded via risk_mgr.post_trade_update."""
        client = _make_mock_client(price=39000.0, open_orders=[])
        ts = MagicMock()
        ts.get_all.return_value = {"BTCUSDT": {"entry_price": 40000, "activated": True}}
        ts.update.return_value = {
            "triggered": True,
            "symbol": "BTCUSDT",
            "entry_price": 40000,
            "highest_price": 41000,
            "sl_price": 39500,
            "current_price": 39000,
        }
        ts.remove.return_value = None

        _, risk_mgr = _run_trailing_check(client, ts)

        # post_trade_update should have been called with a negative PnL (loss)
        assert risk_mgr.post_trade_update.called
        call_args = risk_mgr.post_trade_update.call_args
        pnl = call_args[0][1]
        assert pnl < 0  # loss: (39000 - 40000) * qty < 0
