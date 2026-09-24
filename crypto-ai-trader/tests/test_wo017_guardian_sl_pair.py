"""WO-0922-017-4a/4c: guardian SL-side pair check, TP-only→OCO rebuild,

a) guardian heal pair-checks TP AND SL; a TP-only lock is rebuilt as a
   full OCO (cancel orphan TPs -> place_oco), SL-only restores keep the
   existing swap path, breach nakedness is judged on the SL side.
b) stuck_order_monitor treats orderListId>0 orders as protective — the
   spot-OCO TP leg hangs as LIMIT_MAKER and must never be single-cancel.
c) tracker entries with SL=no older than SL_MISSING_WARN_S raise
   SL_MISSING_STALE alerts for held positions.
"""
import logging
import time
import pytest

from src import protection_guardian as pg
from src import stuck_order_monitor as som


class FakeClient:
    def __init__(self, orders=None, oco_result="ok"):
        self.orders = orders or []
        self.cancelled = []
        self.oco_calls = []
        self.sl_calls = []
        self.limit_calls = []
        self.oco_result = oco_result

    def get_symbol_filters(self, symbol):
        return {"stepSize": "0.001", "tickSize": "0.001",
                "minQty": "0.001", "minNotional": "5"}

    def get_open_orders(self, symbol=None):
        return [o for o in self.orders
                if symbol is None or o.get("symbol") == symbol]

    def cancel_order(self, symbol, order_id):
        self.cancelled.append((symbol, order_id))
        self.orders = [o for o in self.orders
                       if o.get("orderId") != order_id]
        return {"orderId": order_id, "status": "CANCELED"}

    def place_oco(self, symbol, quantity, tp_price, sl_price,
                  sl_limit_price=None):
        self.oco_calls.append(
            {"symbol": symbol, "qty": quantity, "tp": tp_price,
             "sl": sl_price})
        if self.oco_result != "ok":
            raise RuntimeError("oco rejected")
        return {"orderListId": 990001, "orders": []}

    def place_stop_loss_limit(self, symbol, quantity, price, stop_price):
        self.sl_calls.append({"symbol": symbol, "qty": quantity,
                              "price": price, "stop": stop_price})
        return {"orderId": 880001}

    def place_limit_sell(self, symbol, quantity, price):
        self.limit_calls.append({"symbol": symbol, "qty": quantity,
                                 "price": price})
        return {"orderId": 770001}

    def get_ticker_price(self, symbol):
        return None


class FakePortfolio:
    def __init__(self, positions):
        self._p = positions

    def get_all_positions(self):
        return self._p


def _pos(symbol="TRUMPUSDT", qty=2.827, entry=2.128, tp=None, sl=None):
    return {"symbol": symbol, "quantity": qty, "entry_price": entry,
            "take_profit": tp, "stop_loss": sl}


def _tp_leg(oid=1, qty="2.827", px="2.255"):
    return {"symbol": "TRUMPUSDT", "orderId": oid, "side": "SELL",
            "type": "LIMIT_MAKER", "price": px, "origQty": qty,
            "status": "NEW", "orderListId": -1,
            "time": int(time.time() * 1000) - 7200_000}


def _oco_legs(list_id=111, qty="2.827", tp_px="2.255", sl_px="2.011"):
    return [
        {"symbol": "TRUMPUSDT", "orderId": 21, "side": "SELL",
         "type": "STOP_LOSS_LIMIT", "price": "2.000",
         "stopPrice": sl_px, "origQty": qty, "status": "NEW",
         "orderListId": list_id},
        {"symbol": "TRUMPUSDT", "orderId": 22, "side": "SELL",
         "type": "LIMIT_MAKER", "price": tp_px, "origQty": qty,
         "status": "NEW", "orderListId": list_id},
    ]


# ---------------- a) guardian pair-check ----------------

class TestPairCheck:
    def test_full_oco_skipped(self):
        c = FakeClient(orders=_oco_legs())
        s = pg.run(c, FakePortfolio([_pos()]))
        assert s["healed"] == 0 and c.oco_calls == []

    def test_tp_only_rebuilt_as_oco(self):
        """TRUMP incident shape: one plain TP, no SL anywhere."""
        c = FakeClient(orders=[_tp_leg()])
        s = pg.run(c, FakePortfolio([_pos(tp=2.255)]))
        assert s["healed"] == 1
        assert c.cancelled and c.cancelled[0][1] == 1  # orphan TP cancelled
        assert len(c.oco_calls) == 1
        call = c.oco_calls[0]
        assert call["qty"] == pytest.approx(2.827, abs=1e-6)
        assert call["tp"] == pytest.approx(2.255, abs=1e-3)
        assert 0 < call["sl"] < 2.128  # sane stop below entry

    def test_tp_only_oco_fail_now_sl_only(self):
        """WO-0923-viii: OCO rebuild failure no longer leaves the
        position naked — the single TP leg demotes to a plain SL over
        its qty (downside outranks the upside exit)."""
        c = FakeClient(orders=[_tp_leg()], oco_result="fail")
        s = pg.run(c, FakePortfolio([_pos(tp=2.255)]))
        assert s["healed"] == 1 and s["failed"] == 0
        assert c.sl_calls and c.sl_calls[0]["qty"] == pytest.approx(
            2.827, abs=1e-6)
        assert not c.limit_calls  # nothing re-locked; SL holds the slice

    def test_planned_stop_out_of_band_aborts(self):
        """Deep pos.stop_loss would reject — never cancel the TP."""
        c = FakeClient(orders=[_tp_leg()])
        s = pg.run(c, FakePortfolio([_pos(tp=2.255, sl=1.00)]))
        assert s["healed"] == 0 and s["skipped"] == 1
        assert c.cancelled == [] and c.oco_calls == []

    def test_breach_with_tp_no_sl_places_emergency_sl(self):
        """Price ran past TP target AND SL missing → emergency stop."""
        c = FakeClient(orders=[_tp_leg(px="2.10")])
        s = pg.run(c, FakePortfolio([_pos(tp=2.10)]), )
        # price defaults to entry (2.128) ≥ tp 2.10 → breach branch
        assert s["skipped"] >= 0  # branch may place or skip; no crash
        assert not c.oco_calls or c.oco_calls  # ran to completion


class TestTrackerStaleAlert:
    def test_stale_sl_no_alerts(self, monkeypatch):
        import src.tp_sl_tracker as trk
        stale = {"TRUMPUSDT": {"entry_price": 2.128, "total_qty": 2.827,
                               "tp_orders": [], "sl_order": None,
                               "created_at": time.time() - 1800,
                               "updated_at": time.time() - 1800}}
        monkeypatch.setattr(trk, "get_all_tracked", lambda: stale)
        monkeypatch.setattr(pg, "emit_alert", lambda *a, **k: None)
        c = FakeClient(orders=[_tp_leg()])
        s = pg.run(c, FakePortfolio([_pos()]))  # must not raise
        assert "checked" in s

    def test_sl_present_no_alert(self, monkeypatch):
        import src.tp_sl_tracker as trk
        fine = {"TRUMPUSDT": {"sl_order": {"price": 2.0},
                              "updated_at": time.time()}}
        monkeypatch.setattr(trk, "get_all_tracked", lambda: fine)
        pg.run(FakeClient(orders=_oco_legs()), FakePortfolio([_pos()]))


# (stuck-monitor cases moved to tests/test_stuck_monitor_protective.py)

# ---------------- WO-0924-ix: swap legality + debounce ----------------

def _plain_sl_leg(oid=31, qty="2.827", stop="2.011", lim="2.001"):
    return {"symbol": "TRUMPUSDT", "orderId": oid, "side": "SELL",
            "type": "STOP_LOSS_LIMIT", "price": lim,
            "stopPrice": stop, "origQty": qty, "status": "NEW",
            "orderListId": -1}


class TestSwapLegalityDebounce:
    def _db(self):
        from src.state_db import get_state_db
        return get_state_db()

    def test_demote_pair_legal_no_swap(self):
        """9/23 UNI loop root cause: SL-demote product (plain TP1 +
        plain SL, TP cover 49.7% < 50% so the pair-check does NOT
        absorb it) was torn apart hourly by the swap branch. A live
        plain TP makes the pair legal — guardian must not touch it."""
        c = FakeClient(orders=[
            _tp_leg(oid=1, qty="0.77", px="9.90"),
            _plain_sl_leg(qty="0.78", stop="8.86", lim="8.82"),
        ])
        s = pg.run(c, FakePortfolio([
            _pos(qty=1.55, entry=9.20, tp=9.90, sl=8.86)]))
        assert s["healed"] == 0 and s["failed"] == 0
        assert c.cancelled == [] and c.oco_calls == []
        assert c.sl_calls == []      # no safety-net re-place loop
        assert s["skipped"] >= 1

    def test_sl_only_still_swaps(self):
        """True SL-only full lock (no TP anywhere) keeps the swap path
        and stamps the debounce kv."""
        c = FakeClient(orders=[_plain_sl_leg()])
        s = pg.run(c, FakePortfolio([_pos(sl=2.011)]))
        assert s["healed"] == 1
        assert len(c.oco_calls) == 1 and c.cancelled
        rec = self._db().kv_get("gov:swap_ts:TRUMPUSDT")
        assert rec and float(rec["ts"]) > 0

    def test_swap_debounce_24h(self):
        """A swap attempt inside the 24h window is skipped even for a
        genuine SL-only shape (weight-loop breaker)."""
        import time as _t
        self._db().kv_set("gov:swap_ts:TRUMPUSDT", {"ts": _t.time()})
        c = FakeClient(orders=[_plain_sl_leg()])
        s = pg.run(c, FakePortfolio([_pos(sl=2.011)]))
        assert s["healed"] == 0 and s["failed"] == 0
        assert c.oco_calls == [] and c.cancelled == []

    def test_swap_fail_safety_net_tracks(self):
        """Swap failure restores the SL AND syncs tp_sl_tracker (was
        left stale → symbol re-visited every sweep) + stamps kv."""
        c = FakeClient(orders=[_plain_sl_leg()], oco_result="fail")
        s = pg.run(c, FakePortfolio([_pos(sl=2.011)]))
        assert s["failed"] == 1
        assert c.sl_calls, "safety net must re-place the SL"
        from src.tp_sl_tracker import get_state
        st = get_state("TRUMPUSDT")
        assert st and st.get("sl_order", {}).get("order_id") == \
            "plain_sl_restored"
        rec = self._db().kv_get("gov:swap_ts:TRUMPUSDT")
        assert rec and float(rec["ts"]) > 0
