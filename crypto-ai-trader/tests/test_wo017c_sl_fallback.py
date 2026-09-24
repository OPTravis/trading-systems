"""WO-0923-viii: guardian TP→OCO rebuild failure rescue ladder.

Incident (9/23 18:31 UNIUSDT): SL leg filled -> guardian auto rebuild ->
place_oco rejected ((400, -2010, 'The relationship of the prices ...'))
-> safety net restored 2/2 TP legs but the position was left with NO
downside protection until a manual cancel-TP2-place-SL (9/251/9.112
qty 0.78) closed the gap.

Ladder (this suite pins it):
  1. transient/rate-limit OCO errors -> backoff retry, no demote
  2. business OCO rejection -> demote: restore TPs except the last,
     plain STOP_LOSS_LIMIT over that leg's qty (base stays free — no
     -2010 double lock); tracker + audit stay in sync
  3. plain SL also fails -> restore the demoted TP too and emit an
     ERROR-grade invariant breach alert
"""

import time
import pytest

from src import protection_guardian as pg


def _biz_err(msg="The relationship of the prices for the orders is not correct."):
    return RuntimeError((400, -2010, msg, {}, None))


def _insuff_err():
    return RuntimeError((400, -2010,
                         "Account has insufficient balance for requested "
                         "action.", {}, None))


def _rate_err():
    return RuntimeError((418, -1003, "Too many requests.", {}, None))


class FakeClient:
    def __init__(self, orders=None, oco_errors=None, sl_errors=None):
        self.orders = orders or []
        self.cancelled = []
        self.oco_calls = []
        self.sl_calls = []
        self.limit_calls = []
        self.oco_errors = list(oco_errors or [])
        self.sl_errors = list(sl_errors or [])

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
        self.oco_calls.append({"symbol": symbol, "qty": quantity})
        if self.oco_errors:
            raise self.oco_errors.pop(0)
        return {"orderListId": 990001, "orders": []}

    def place_stop_loss_limit(self, symbol, quantity, price, stop_price):
        self.sl_calls.append({"symbol": symbol, "qty": quantity,
                              "price": price, "stop": stop_price})
        if self.sl_errors:
            raise self.sl_errors.pop(0)
        return {"orderId": 880001}

    def place_limit_sell(self, symbol, quantity, price):
        self.limit_calls.append({"symbol": symbol, "qty": quantity,
                                 "price": price})
        return {"orderId": 770000 + len(self.limit_calls)}

    def get_ticker_price(self, symbol):
        return None


class FakePortfolio:
    def __init__(self, positions):
        self._p = positions

    def get_all_positions(self):
        return self._p


def _pos(symbol="UNIUSDT", qty=2.8, entry=9.612, tp=9.9, sl=9.112):
    return {"symbol": symbol, "quantity": qty, "entry_price": entry,
            "take_profit": tp, "stop_loss": sl}


def _tp_leg(oid, qty, px):
    return {"symbol": "UNIUSDT", "orderId": oid, "side": "SELL",
            "type": "LIMIT_MAKER", "price": px, "origQty": qty,
            "status": "NEW", "orderListId": -1,
            "time": int(time.time() * 1000) - 3600_000}


@pytest.fixture(autouse=True)
def _no_alerts(monkeypatch):
    emitted = []
    # guardian binds `emit as emit_alert` at import time — patch the
    # bound name, not the source module attribute
    monkeypatch.setattr("src.protection_guardian.emit_alert",
                        lambda et, sym=None, d=None: emitted.append(
                            {"event_type": et, "symbol": sym,
                             "details": d or {}}) or True)
    yield emitted


@pytest.fixture()
def tracker(monkeypatch):
    saved = []
    monkeypatch.setattr("src.tp_sl_tracker.save_state",
                        lambda *a, **k: saved.append(a))
    return saved


@pytest.fixture()
def audit(monkeypatch):
    """Captures guardian audit rows.

    P6-B3 seam: the legacy fallback now writes via StateDB.audit_log()
    instead of a raw INSERT, so the stub captures that call. Row shape
    (now, action, details) matches the legacy INSERT params."""
    rows = []
    import time as _time

    class _DB:
        # no kv_get -> repairs_enabled() raises inside the funnel's try,
        # which is exactly how this suite always reached the fallback
        def audit_log(self, action, details="", **kw):
            rows.append((_time.time(), action, details))

    monkeypatch.setattr("src.state_db.get_state_db", lambda: _DB())
    return rows


class TestRescueLadder:
    def test_transient_oco_error_retried_then_succeeds(
            self, monkeypatch, tracker, audit, _no_alerts):
        sleeps = []
        monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
        c = FakeClient(orders=[_tp_leg(1, "2.8", "9.9")],
                       oco_errors=[_rate_err()])  # fail once, then ok
        s = pg.run(c, FakePortfolio([_pos()]))
        assert s["healed"] == 1
        assert len(c.oco_calls) == 2          # retried once
        assert sleeps == [0.5]                # backoff fired
        assert c.sl_calls == []               # no demote on transient path

    def test_price_relation_failure_demotes_last_tp(
            self, tracker, audit, _no_alerts):
        """UNI live-fire: 2 TP legs, OCO rejected on price relationship —
        keep TP1, demote TP2 into a plain SL over its qty."""
        c = FakeClient(
            orders=[_tp_leg(1, "2.0", "9.9"), _tp_leg(2, "0.8", "10.2")],
            oco_errors=[_biz_err(), _biz_err(), _biz_err()])
        s = pg.run(c, FakePortfolio([_pos()]))
        assert s["healed"] == 1
        # business error: exactly ONE oco attempt, no blind retry
        assert len(c.oco_calls) == 1
        # TP1 restored, TP2 demoted to SL covering 0.8
        assert [l["qty"] for l in c.limit_calls] == [pytest.approx(2.0)]
        assert len(c.sl_calls) == 1
        sl = c.sl_calls[0]
        assert sl["qty"] == pytest.approx(0.8, abs=1e-6)
        assert sl["stop"] == pytest.approx(9.112, abs=1e-3)
        assert sl["price"] == pytest.approx(9.112 * 0.995, abs=1e-3)
        # tracker: 1 kept TP + sl_order for the demoted slice
        assert tracker and tracker[0][3] and len(tracker[0][3]) == 1
        assert tracker[0][4]["stop_price"] == pytest.approx(9.112, abs=1e-3)
        # audit trail
        assert audit and audit[0][1] == "GUARDIAN_SL_DEMOTE"
        import json as _j
        assert "UNIUSDT" in _j.loads(audit[0][2])["symbol"]
        assert any(e["details"].get("mode") == "sl_demote"
                   for e in _no_alerts if e["event_type"] ==
                   "PROTECTION_HEALED")

    def test_demote_sl_also_fails_restores_all_and_errors(
            self, tracker, audit, _no_alerts):
        c = FakeClient(
            orders=[_tp_leg(1, "2.0", "9.9"), _tp_leg(2, "0.8", "10.2")],
            oco_errors=[_biz_err()],
            sl_errors=[_insuff_err()])
        s = pg.run(c, FakePortfolio([_pos()]))
        assert s["failed"] == 1
        # both TP legs restored (keep + demoted fallback)
        assert [l["qty"] for l in c.limit_calls] == [
            pytest.approx(2.0), pytest.approx(0.8)]
        breaches = [e for e in _no_alerts
                    if e["event_type"] == "PROTECTION_HEAL_FAILED"
                    and e["details"].get("mode") == "sl_rescue_failed"]
        assert breaches and breaches[0]["details"]["urgent"] is True

    def test_single_tp_leg_becomes_sl_only(
            self, tracker, audit, _no_alerts):
        """One TP leg cannot be split: the whole slice demotes to SL —
        downside protection outranks the upside exit."""
        c = FakeClient(orders=[_tp_leg(1, "2.8", "9.9")],
                       oco_errors=[_biz_err()])
        s = pg.run(c, FakePortfolio([_pos()]))
        assert s["healed"] == 1
        assert c.limit_calls == []                 # nothing re-locked
        assert c.sl_calls and c.sl_calls[0]["qty"] == pytest.approx(
            2.8, abs=1e-6)
        assert tracker and tracker[0][3] == []     # no TPs tracked
        assert tracker[0][4]["stop_price"] == pytest.approx(9.112, abs=1e-3)
