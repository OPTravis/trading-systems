"""WO-1043: OCO linked-kill awareness across cancel-first protection flows.

10/11 11:12 NEAR incident: cancelling an OCO SL leg silently terminated the
whole group (TP leg died with it); the follow-up TP-leg cancel returned
-2011; cmd_trailing_check's abort branch then claimed "old SL preserved" on
a fully naked position and skipped the safety net — 58 minutes with zero
protection, saved only by a profitable momentum_reversal exit.

Covers:
- A. Trailing SL move is OCO-aware (one cancel per group, -2011 = dead,
       abort only after a verified recheck, full two-sided restore as ONE
       OCO — WO-0921: plain TP + plain SL double-lock and deadlock).
- B. exit_check._cancel_sell_legs: group dedupe + -2011 = dead.
- C. SL-coverage Case 1 never cannibalizes an OCO TP leg (linked-kill would
       destroy the SL protection it is trying to create).
"""
import json
from unittest.mock import MagicMock, patch

import pytest


# ── shared e2e harness (mirrors tests/test_e2e_trailing_check.py) ──────────

def _make_bc(balances=None):
    bc = MagicMock()
    if balances is None:
        balances = [{"asset": "USDT", "free": "500", "locked": "0"}]
    bc.get_account.return_value = {"balances": balances}
    bc.get_24hr_stats.return_value = {"last_price": "100.0"}
    bc.get_klines.return_value = [
        {"open": 100 + i, "high": 100 + i + 1, "low": 100 + i - 1,
         "close": 100 + i, "volume": 1000}
        for i in range(20)
    ]
    bc.get_price_precision.return_value = 2
    bc.get_open_orders.return_value = []
    bc.cancel_all_orders.return_value = []
    bc.cancel_order.return_value = {"status": "CANCELED"}
    bc.place_order.return_value = {"symbol": "TREEUSDT", "orderId": 999, "status": "NEW"}
    bc.place_oco.return_value = {"orderListId": 777, "status": "NEW"}
    return bc


def _make_ts_active():
    return MagicMock(
        update=MagicMock(return_value={
            "activated": True, "sl_price": 105.0, "highest_price": 120.0,
        }),
        get_all=MagicMock(return_value={"TREE": {"entry_price": 100.0}}),
    )


def _run_trailing(bc, ts):
    rm = MagicMock()
    notifier = MagicMock()
    with patch("src.cmd_trailing_check.BinanceClient", return_value=bc), patch(
        "src.cmd_trailing_check.TrailingStop", return_value=ts
    ), patch(
        "src.cmd_trailing_check.get_risk_manager", return_value=rm
    ), patch(
        "src.cmd_trailing_check.FeishuNotifier", return_value=notifier
    ), patch(
        "src.indicators.Indicators.atr", return_value=5.0
    ), patch(
        "src.entry_price.get_avg_entry_price", return_value=None
    ):
        from src.cmd_trailing_check import cmd_trailing_check
        cmd_trailing_check()
    return notifier


def _oco_sl_leg(order_id=100, list_id=55, qty="500", stop="100.0"):
    return {
        "symbol": "TREEUSDT", "type": "STOP_LOSS_LIMIT", "side": "SELL",
        "orderId": order_id, "orderListId": list_id, "status": "NEW",
        "origQty": qty, "price": stop, "stopPrice": stop,
    }


def _oco_tp_leg(order_id=101, list_id=55, qty="500", px="120.0"):
    return {
        "symbol": "TREEUSDT", "type": "LIMIT", "side": "SELL",
        "orderId": order_id, "orderListId": list_id, "status": "NEW",
        "origQty": qty, "price": px, "stopPrice": "0",
    }


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("src.cmd_trailing_check.time.sleep", lambda s: None)


# ── A. trailing SL move: OCO-aware ─────────────────────────────────────────

class TestSlMoveOcoAware:
    def _bc_with_oco(self):
        bc = _make_bc([
            {"asset": "USDT", "free": "500", "locked": "0"},
            {"asset": "TREE", "free": "0", "locked": "500"},
        ])
        bc.get_open_orders.return_value = [
            _oco_sl_leg(100, 55, "500", "100.0"),
            _oco_tp_leg(101, 55, "300", "120.0"),
            _oco_tp_leg(103, 55, "200", "125.0"),
        ]
        return bc

    def _results(self, capsys):
        data = json.loads(capsys.readouterr().out)
        return data.get("results", [])

    def test_a1_happy_path_restores_full_oco(self, capsys):
        """One cancel per OCO group; re-place = single OCO keeping the group's
        TP prices (weighted) with the new tighter SL — TP side never lost."""
        bc = self._bc_with_oco()
        _run_trailing(bc, _make_ts_active())
        res = self._results(capsys)
        moved = next(r for r in res if r.get("action") == "sl_moved")
        assert moved["restore"] == "oco"
        assert moved["tp_restored"] == pytest.approx(122.0)  # (120·300+125·200)/500
        # exactly ONE cancel — the SL leg; the TP sibling dies with the group
        cancel_ids = [c.args[1] for c in bc.cancel_order.call_args_list]
        assert cancel_ids == [100]
        bc.place_oco.assert_called_once()
        args = bc.place_oco.call_args.args
        assert args[0] == "TREEUSDT"
        assert args[1] == pytest.approx(500.0)
        assert args[2] == pytest.approx(122.0)
        assert args[3] == pytest.approx(105.0)
        # never the old plain-SL-only restore
        sell_calls = [c for c in bc.place_order.call_args_list
                      if len(c.args) > 1 and c.args[1] == "SELL"]
        assert sell_calls == []

    def test_a2_minus_2011_on_leg_is_group_dead(self, capsys):
        """-2011 (unknown order) = the leg is already dead (linked-kill or
        filled) — treated as cancelled, move proceeds, no abort."""
        bc = self._bc_with_oco()
        bc.cancel_order.side_effect = Exception("-2011 Unknown order sent.")
        _run_trailing(bc, _make_ts_active())
        res = self._results(capsys)
        assert any(r.get("action") == "sl_moved" for r in res)
        assert not any(r.get("action") == "sl_cancel_failed" for r in res)
        bc.place_oco.assert_called_once()

    def test_a3_abort_only_when_stop_verifiably_live(self, capsys):
        """Cancel fails AND recheck shows a live stop → abort is correct and
        the message reflects the verified state."""
        bc = self._bc_with_oco()
        bc.cancel_order.side_effect = Exception("-1013 filter failure")
        _run_trailing(bc, _make_ts_active())
        res = self._results(capsys)
        failed = next(r for r in res if r.get("action") == "sl_cancel_failed")
        assert "verified" in failed["msg"]
        bc.place_oco.assert_not_called()

    def test_a4_cancel_failed_but_group_dead_runs_recovery(self, capsys):
        """The 10/11 NEAR shape: cancel errors, yet the group is already
        dead — recovery re-place runs instead of aborting on a naked
        position."""
        bc = self._bc_with_oco()
        bc.cancel_order.side_effect = Exception("-1013 filter failure")
        calls = {"n": 0}

        def _orders(sym):
            calls["n"] += 1
            if calls["n"] <= 1:
                return bc.get_open_orders.return_value
            return []  # recheck: everything gone (linked-kill happened)

        bc.get_open_orders.side_effect = _orders
        notifier = _run_trailing(bc, _make_ts_active())
        res = self._results(capsys)
        assert not any(r.get("action") == "sl_cancel_failed" for r in res)
        assert any(r.get("action") == "sl_moved" for r in res)
        bc.place_oco.assert_called_once()
        # the user-facing alert must never claim "old SL preserved"
        msgs = [str(c.args[0]) for c in notifier.send_text.call_args_list]
        assert not any("舊SL保留" in m or "old SL preserved" in m for m in msgs)

    def test_a5_plain_sl_keeps_plain_behavior(self, capsys):
        """No OCO anywhere → cancel + plain STOP_LOSS_LIMIT, unchanged."""
        bc = _make_bc([
            {"asset": "USDT", "free": "500", "locked": "0"},
            {"asset": "TREE", "free": "0", "locked": "500"},
        ])
        bc.get_open_orders.return_value = [{
            "symbol": "TREEUSDT", "type": "STOP_LOSS_LIMIT", "side": "SELL",
            "orderId": 100, "orderListId": -1, "status": "NEW",
            "origQty": "500", "price": "100.0", "stopPrice": "100.0",
        }]
        _run_trailing(bc, _make_ts_active())
        res = self._results(capsys)
        moved = next(r for r in res if r.get("action") == "sl_moved")
        assert moved["restore"] == "plain_sl"
        bc.place_oco.assert_not_called()
        sell_calls = [c for c in bc.place_order.call_args_list
                      if len(c.args) > 1 and c.args[1] == "SELL"]
        assert len(sell_calls) >= 1

    def test_a6_safety_net_restores_full_shape(self, capsys):
        """New OCO fails 3x → safety net re-places the OLD shape (old TP +
        old SL as one OCO); OCO restore failing degrades to plain old SL —
        never a naked position."""
        bc = self._bc_with_oco()
        bc.place_oco.return_value = None  # every OCO attempt fails
        _run_trailing(bc, _make_ts_active())
        res = self._results(capsys)
        assert any(r.get("action") == "sl_move_reverted" for r in res)
        # safety net OCO at the old prices (tp 122 / sl 100), then plain SL
        safety_oco = [c for c in bc.place_oco.call_args_list
                      if c.args[3] == pytest.approx(100.0)]
        assert safety_oco, "safety net must try the OCO at the old stop first"
        plain = [c for c in bc.place_order.call_args_list
                 if len(c.args) > 3 and c.args[2] == "STOP_LOSS_LIMIT"]
        assert plain, "plain-SL fallback must run when OCO restore fails"


# ── B. exit_check._cancel_sell_legs: group dedupe + -2011 = dead ──────────

class FakeExitClient:
    def __init__(self, orders):
        self.orders = orders
        self.cancelled = []

    def get_open_orders(self, sym):
        return self.orders

    def cancel_order(self, sym, oid):
        self.cancelled.append(oid)
        return {"orderId": oid}


class TestExitCancelSellLegs:
    def _legs(self):
        return [
            {"symbol": "X", "side": "SELL", "status": "NEW", "orderId": 1,
             "orderListId": 7, "type": "STOP_LOSS_LIMIT"},
            {"symbol": "X", "side": "SELL", "status": "NEW", "orderId": 2,
             "orderListId": 7, "type": "LIMIT"},
            {"symbol": "X", "side": "SELL", "status": "NEW", "orderId": 3,
             "orderListId": -1, "type": "LIMIT"},
        ]

    def test_b1_group_cancelled_once(self, monkeypatch):
        monkeypatch.setattr("src.exit_check.time.sleep", lambda s: None)
        from src.exit_check import _cancel_sell_legs
        c = FakeExitClient(self._legs())
        out = _cancel_sell_legs(c, "X")
        assert out["ok"] is True
        assert out["cancelled"] == 2          # one per group + the plain leg
        assert c.cancelled == [1, 3]          # sibling 2 skipped (linked-kill)

    def test_b2_minus_2011_counts_as_dead(self, monkeypatch):
        monkeypatch.setattr("src.exit_check.time.sleep", lambda s: None)
        from src.exit_check import _cancel_sell_legs
        c = FakeExitClient(self._legs())

        def _cancel(sym, oid):
            if oid == 3:
                raise Exception("-2011 Unknown order sent.")
            c.cancelled.append(oid)
            return {"orderId": oid}

        c.cancel_order = _cancel
        out = _cancel_sell_legs(c, "X")
        assert out["ok"] is True              # never abort a market exit
        assert c.cancelled == [1]             # 3 was dead (-2011), not a failure

    def test_b3_genuine_failure_still_aborts(self, monkeypatch):
        monkeypatch.setattr("src.exit_check.time.sleep", lambda s: None)
        from src.exit_check import _cancel_sell_legs
        c = FakeExitClient(self._legs())

        def _cancel(sym, oid):
            raise Exception("-1013 PERCENT_PRICE")

        c.cancel_order = _cancel
        out = _cancel_sell_legs(c, "X")
        assert out["ok"] is False
        assert "cancel failed" in out["aborted"]


# ── C. SL-coverage Case 1: OCO TP legs are not cannibalized ────────────────

class TestCase1OcoTpProtection:
    def test_c1_oco_tp_leg_never_cancelled_for_sl_room(self, capsys):
        """Uncovered slice exists and all TPs are OCO members → cancelling
        the lowest TP would linked-kill its SL sibling (destroying the very
        protection Case 1 exists to create) → branch must skip instead."""
        bc = _make_bc([
            {"asset": "USDT", "free": "500", "locked": "0"},
            {"asset": "TREE", "free": "0", "locked": "600"},
        ])
        bc.get_open_orders.return_value = [
            _oco_sl_leg(100, 55, "500", "95.0"),
            _oco_tp_leg(101, 55, "500", "120.0"),
        ]
        ts = MagicMock()
        ts.update.return_value = {"activated": False}   # not yet trailing
        ts.get_all.return_value = {}
        _run_trailing(bc, ts)
        data = json.loads(capsys.readouterr().out)
        assert data.get("action") in ("none",) or "results" in data
        cancel_ids = [c.args[1] for c in bc.cancel_order.call_args_list]
        assert 101 not in cancel_ids    # the OCO TP leg survives
        # no plain SL was placed on the OCO-locked balance either
        sells = [c for c in bc.place_order.call_args_list
                 if len(c.args) > 1 and c.args[1] == "SELL"]
        assert sells == []
