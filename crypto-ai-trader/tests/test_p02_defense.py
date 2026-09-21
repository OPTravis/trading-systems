"""P0-2 defense tests: circuit tiers / stuck-order monitor / KV preflight.

Work order 2026-09-21 — three defense items. Test philosophy follows
test_dust_reaper.py: FakeDB / FakePortfolio / FakeClient doubles, no real
API, report-vs-act behaviour distinction, SPOT ONLY semantics.
"""

import json
import logging
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src import circuit_tiers as ct  # noqa: E402
from src import stuck_order_monitor as som  # noqa: E402
from src import kv_preflight as kpf  # noqa: E402


# ── shared doubles ────────────────────────────────────────────────────────────

class FakeDB:
    def __init__(self, kv=None, portfolio_rows=None, kv_ages=None,
                 portfolio_age=None):
        self.kv = kv or {}
        self.kv_ages = kv_ages or {}
        self.portfolio_rows = portfolio_rows or []
        self.portfolio_age = portfolio_age

    def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    def kv_set(self, key, value):
        self.kv[key] = value
        self.kv_ages.pop(key, None)

    def portfolio_get_cash_balance(self):
        cb = self.kv.get("cash_balance")
        return float(cb) if cb is not None else 0.0

    class _Row(dict):
        """sqlite3.Row-style: r["col"] access."""

        def __getitem__(self, k):
            return dict.__getitem__(self, k)

    class _Result(list):
        def fetchone(self):
            return self[0] if self else None

        def fetchall(self):
            return list(self)

    class _Conn:
        def __init__(self, outer):
            self.outer = outer

        def execute(self, sql, params=()):
            if sql.startswith("SELECT updated_at FROM kv"):
                key = params[0]
                age = self.outer.kv_ages.get(key)
                if key not in self.outer.kv:
                    return FakeDB._Result()
                ts = time.time() - age if age is not None else time.time()
                return FakeDB._Result([FakeDB._Row(updated_at=ts)])
            if "FROM portfolio" in sql:
                now = time.time()
                ts = (now - self.outer.portfolio_age
                      if self.outer.portfolio_age is not None else now)
                return FakeDB._Result([
                    FakeDB._Row(symbol=r[0], qty=r[1], avg_price=r[2],
                                cash_balance=r[3], updated_at=ts)
                    for r in self.outer.portfolio_rows])
            raise AssertionError("unexpected sql: %s" % sql)

    def _get_conn(self):
        return FakeDB._Conn(self)


def _bind_conn(db):
    return db


class FakePortfolio:
    def __init__(self, positions, db=None):
        self.positions = positions
        self._db = db
        self.closed = []

    def get_all_positions(self):
        return self.positions

    def close_position(self, symbol, close_price=None, exit_reason=None,
                       client_order_id=None):
        self.closed.append({"symbol": symbol, "price": close_price,
                            "reason": exit_reason, "cid": client_order_id})
        return {"success": True}


class FakeClient:
    def __init__(self, prices, fail_symbols=()):
        self.prices = prices
        self.fail_symbols = set(fail_symbols)
        self.orders_placed = []

    def get_ticker_price(self, symbol):
        if symbol in self.fail_symbols:
            raise RuntimeError("ticker down for %s" % symbol)
        return self.prices.get(symbol)

    def place_order(self, symbol, side, order_type, quantity):
        self.orders_placed.append({"symbol": symbol, "side": side,
                                   "type": order_type, "qty": quantity})
        price = self.prices.get(symbol, 1.0)
        return {"orderId": 900000 + len(self.orders_placed),
                "clientOrderId": "ct-%d" % len(self.orders_placed),
                "fills": [{"qty": quantity, "price": price}]}


@pytest.fixture(autouse=True)
def _no_live_alerts(monkeypatch):
    emitted = []
    monkeypatch.setattr("src.live_alerts.emit",
                        lambda et, sym=None, d=None: emitted.append(
                            {"event_type": et, "symbol": sym,
                             "details": d}) or True)
    yield emitted


# ── circuit tiers ─────────────────────────────────────────────────────────────

def _anchored_db(mode=None, regime=None, day_start_equity=100.0):
    """StateDB double with today's day-anchor preset (production sets it
    on the first evaluation of the UTC day; tests fast-forward)."""
    kv = {
        ct.KV_STATE: {
            "day": time.strftime("%Y-%m-%d", time.gmtime()),
            "day_start_equity": day_start_equity,
            "tier": 0,
        },
    }
    if mode:
        kv[ct.KV_MODE] = mode
    if regime:
        kv["hmm_regime"] = json.dumps({"regime": regime})
    return _bind_conn(FakeDB(kv=kv))


def test_t1_trip_on_6pct_and_blocks_entries(_no_live_alerts):
    db = _anchored_db()
    portfolio = FakePortfolio({"BTCUSDT": {"qty": 100}}, db=db)
    client = FakeClient({"BTCUSDT": 0.94})  # -6% from 100 anchor
    res = ct.evaluate_and_act(client, portfolio)
    assert res["tier"] == 1 and res["action"] == "STOP_NEW"
    assert client.orders_placed == []  # T1 never sells
    assert any(e["event_type"] == "CIRCUIT_TIER"
               and e["details"]["event"] == "trip" for e in _no_live_alerts)
    assert ct.entry_blocked(db) is not None


def test_t1_no_trip_below_threshold(_no_live_alerts):
    db = _anchored_db()
    portfolio = FakePortfolio({"BTCUSDT": {"qty": 100}}, db=db)
    client = FakeClient({"BTCUSDT": 0.97})  # -3%: below -6% tier1
    res = ct.evaluate_and_act(client, portfolio)
    assert res["tier"] == 0
    assert ct.entry_blocked(db) is None


def test_t2_deleverage_report_mode_keeps_positions(_no_live_alerts):
    db = _anchored_db(mode="report")
    portfolio = FakePortfolio({"BTCUSDT": {"qty": 80},
                               "ETHUSDT": {"qty": 20}}, db=db)
    # equity = 80*0.88 + 20*0.85 = 87.4 vs anchor 100 → -12.6% → tier2
    client = FakeClient({"BTCUSDT": 0.88, "ETHUSDT": 0.85})
    res = ct.evaluate_and_act(client, portfolio)
    assert res["tier"] == 2
    assert res["action"] == "DELEVERAGE_REPORTED"
    assert client.orders_placed == []  # report mode: no sells
    assert portfolio.closed == []
    assert any(e["details"].get("event") == "deleverage_needed_report_mode"
               for e in _no_live_alerts)


def test_t2_deleverage_act_mode_compresses_exposure(_no_live_alerts):
    db = _anchored_db(mode="act", day_start_equity=111.2)
    portfolio = FakePortfolio({"BTCUSDT": {"qty": 80},
                               "ETHUSDT": {"qty": 40}}, db=db)
    client = FakeClient({"BTCUSDT": 0.5, "ETHUSDT": 1.0})
    # equity = 0 cash + 40 + 40 = 80; dd = (80-111.2)/111.2 = -28% → T3!
    # Instead: equity must land between -10% and -15%: anchor 111.2, want
    # equity ≈ 100 → add cash 20 via kv cash_balance? FakePortfolio cash
    # comes from db.portfolio_get_cash_balance → set cash_balance kv.
    db.kv["cash_balance"] = 20.0
    # equity = 20 + 40 + 40 = 100, dd = (100-111.2)/111.2 = -10.07% → tier2
    res = ct.evaluate_and_act(client, portfolio)
    assert res["tier"] == 2
    # target exposure = 40% * 100 = 40; open = 80 → must sell ~40 notional
    assert client.orders_placed, "act mode must sell down to target"
    assert all(o["side"] == "SELL" for o in client.orders_placed)
    assert len(portfolio.closed) == len(client.orders_placed)
    # total sold notional must land within tolerance of the 40 target
    sold_notional = sum(o["qty"] * 0.5 if o["symbol"] == "BTCUSDT"
                        else o["qty"] * 1.0 for o in client.orders_placed)
    assert sold_notional == pytest.approx(40.0, abs=1.0)


def test_t3_report_mode_alerts_and_latches(_no_live_alerts):
    db = _anchored_db(mode="report")
    portfolio = FakePortfolio({"BTCUSDT": {"qty": 100}}, db=db)
    client = FakeClient({"BTCUSDT": 0.80})  # -20% ≤ -15%
    res = ct.evaluate_and_act(client, portfolio)
    assert res["tier"] == 3 and res["action"] == "LIQUIDATE_REPORTED"
    assert client.orders_placed == [] and portfolio.closed == []
    # recovery alone does not release tier 3
    client2 = FakeClient({"BTCUSDT": 1.5})
    res2 = ct.evaluate_and_act(client2, portfolio)
    assert res2["tier"] == 3
    # only manual reset clears it
    res3 = ct.manual_reset(db, note="human verified")
    assert res3["tier"] == 0
    assert ct.entry_blocked(db) is None


def test_t1_auto_release_requires_all_three_conditions(_no_live_alerts):
    db = _anchored_db()  # regime absent → not extreme
    portfolio = FakePortfolio({"BTCUSDT": {"qty": 100}}, db=db)
    ct.evaluate_and_act(FakeClient({"BTCUSDT": 0.93}), portfolio)  # trip T1
    assert ct.entry_blocked(db) is not None

    # price recovered + cooldown NOT elapsed → still blocked
    ct.evaluate_and_act(FakeClient({"BTCUSDT": 0.99}), portfolio)
    assert ct.entry_blocked(db) is not None

    # force cooldown elapsed in state
    state = db.kv_get(ct.KV_STATE)
    state["tripped_at"] = time.time() - 5 * 3600
    db.kv_set(ct.KV_STATE, state)
    res = ct.evaluate_and_act(FakeClient({"BTCUSDT": 0.99}), portfolio)
    assert res["action"] == "RELEASE" and res["tier"] == 0
    assert ct.entry_blocked(db) is None

    # extreme regime blocks release even with recovery + cooldown
    db2 = _anchored_db(regime="bear_trend")
    portfolio2 = FakePortfolio({"BTCUSDT": {"qty": 100}}, db=db2)
    ct.evaluate_and_act(FakeClient({"BTCUSDT": 0.93}), portfolio2)
    state2 = db2.kv_get(ct.KV_STATE)
    state2["tripped_at"] = time.time() - 5 * 3600
    db2.kv_set(ct.KV_STATE, state2)
    res2 = ct.evaluate_and_act(FakeClient({"BTCUSDT": 0.99}), portfolio2)
    assert res2["tier"] == 1, "extreme regime must hold the tier"


def test_missing_mark_skips_round_fail_open(_no_live_alerts):
    db = _anchored_db()
    portfolio = FakePortfolio({"BTCUSDT": {"qty": 100},
                               "ETHUSDT": {"qty": 10}}, db=db)
    client = FakeClient({"BTCUSDT": 0.80}, fail_symbols=("ETHUSDT",))
    res = ct.evaluate_and_act(client, portfolio)
    assert res["action"] == "SKIP_NO_MARKS"
    assert client.orders_placed == []


# ── stuck order monitor ───────────────────────────────────────────────────────

class OrderClient:
    def __init__(self, orders):
        self.orders = orders
        self.cancelled = []

    def get_open_orders(self, symbol=None):
        return self.orders

    def cancel_order(self, symbol, order_id):
        self.cancelled.append((symbol, order_id))
        return {"orderId": order_id, "status": "CANCELED"}


def test_stuck_order_detected_and_cancelled(_no_live_alerts):
    old = (time.time() - 20 * 60) * 1000
    orders = [{"symbol": "ZECUSDT", "orderId": 1, "status": "NEW",
               "type": "LIMIT", "time": old, "price": "50",
               "origQty": "2", "executedQty": "0"}]
    client = OrderClient(orders)
    res = som.run(client)
    assert res["stuck"] == 1 and res["cancelled"] == 1
    assert client.cancelled == [("ZECUSDT", 1)]
    assert any(e["event_type"] == "STUCK_ORDER" for e in _no_live_alerts)


def test_fresh_order_not_cancelled(_no_live_alerts):
    fresh = (time.time() - 5 * 60) * 1000
    orders = [{"symbol": "ZECUSDT", "orderId": 2, "status": "NEW",
               "type": "LIMIT", "time": fresh}]
    client = OrderClient(orders)
    res = som.run(client)
    assert res["stuck"] == 0 and res["cancelled"] == 0
    assert client.cancelled == []


def test_protective_orders_never_cancelled(_no_live_alerts):
    old = (time.time() - 2 * 3600) * 1000
    orders = [
        {"symbol": "FILUSDT", "orderId": 10, "status": "NEW",
         "type": "STOP_LOSS_LIMIT", "time": old, "listId": 555},
        {"symbol": "FILUSDT", "orderId": 11, "status": "NEW",
         "type": "TAKE_PROFIT_LIMIT", "time": old, "listId": 555},
        {"symbol": "BTCUSDT", "orderId": 12, "status": "NEW",
         "type": "LIMIT", "time": old, "contingencyType": "OCO"},
    ]
    client = OrderClient(orders)
    res = som.run(client)
    assert res["checked"] == 3
    assert res["protective_skipped"] == 3
    assert res["stuck"] == 0 and client.cancelled == []


def test_partial_fill_stalled_counts_stuck(_no_live_alerts):
    old = (time.time() - 18 * 60) * 1000
    orders = [{"symbol": "INJUSDT", "orderId": 20,
               "status": "PARTIALLY_FILLED", "type": "LIMIT",
               "time": old, "executedQty": "1", "origQty": "5"}]
    client = OrderClient(orders)
    res = som.run(client)
    assert res["stuck"] == 1 and res["cancelled"] == 1


def test_list_failure_fail_open(_no_live_alerts):
    class BrokenClient:
        def get_open_orders(self, symbol=None):
            raise RuntimeError("api down")

    res = som.run(BrokenClient())
    assert res == {"checked": 0, "protective_skipped": 0, "stuck": 0,
                   "cancelled": 0, "cancel_failed": 0}


# ── KV preflight ──────────────────────────────────────────────────────────────

def test_preflight_pass_fresh_state(_no_live_alerts):
    db = _bind_conn(FakeDB(
        kv={"cash_balance": "123.45", "hmm_regime": '{"regime":"bull"}'},
        portfolio_rows=[("BTCUSDT", 0.1, 50000, 123.45)],
    ))
    res = kpf.run(db)
    assert res["ok"] is True and res["skip_new_entries"] is False
    assert all(c["status"] == "PASS" for c in res["checks"])


def test_preflight_stale_kv_fails(_no_live_alerts):
    db = _bind_conn(FakeDB(
        kv={"cash_balance": "100", "hmm_regime": '{}'},
        kv_ages={"cash_balance": 3 * 3600},  # 3h old > 2h
        portfolio_rows=[("BTCUSDT", 0.1, 50000, 100)],
    ))
    res = kpf.run(db)
    assert res["ok"] is False and res["skip_new_entries"] is True
    fails = [c for c in res["checks"] if c["status"] == "FAIL"]
    assert any(c["item"] == "kv:cash_balance" and "stale" in c["reason"]
               for c in fails)
    assert any(e["event_type"] == "KV_PREFLIGHT_FAIL"
               for e in _no_live_alerts)


def test_preflight_missing_kv_is_cold_start_pass(_no_live_alerts):
    """Missing keys are legal cold start — entry path falls back to live
    exchange balances. Only stale/corrupt state may block."""
    db = _bind_conn(FakeDB(kv={}, portfolio_rows=[]))
    res = kpf.run(db)
    assert res["ok"] is True and res["skip_new_entries"] is False
    assert all(c["status"] == "PASS" for c in res["checks"])


def test_preflight_opaque_db_does_not_block(_no_live_alerts):
    """A db whose reads don't reflect writes (mocked handle) cannot be
    evaluated — degraded checker must not freeze trading."""
    class OpaqueDB:
        def kv_get(self, key, default=None):
            return "<mock>"

        def kv_set(self, key, value):
            pass

    res = kpf.run(OpaqueDB())
    assert res["ok"] is True
    assert res.get("note") == "OPAQUE_DB"


def test_preflight_corrupt_json_fails(_no_live_alerts):
    db = _bind_conn(FakeDB(
        kv={"cash_balance": "{not json", "hmm_regime": "{}"},
        portfolio_rows=[("BTCUSDT", 1, 1, 0)],
    ))
    res = kpf.run(db)
    assert res["ok"] is False
    assert any("unparseable" in c["reason"]
               for c in res["checks"] if c["status"] == "FAIL")


def test_preflight_stale_portfolio_fails(_no_live_alerts):
    db = _bind_conn(FakeDB(
        kv={"cash_balance": "50", "hmm_regime": "{}"},
        portfolio_rows=[("BTCUSDT", 0.1, 50000, 50)],
        portfolio_age=4 * 3600,
    ))
    res = kpf.run(db)
    assert res["ok"] is False
    assert any(c["item"] == "portfolio_table" and "stale" in c["reason"]
               for c in res["checks"] if c["status"] == "FAIL")


def test_preflight_none_db_blind_not_blocking(_no_live_alerts):
    res = kpf.run(None)
    assert res["ok"] is True
    assert res.get("note") == "NO_DB_OPINION"


def test_tier_escalation_t1_to_t2_same_day(_no_live_alerts):
    """Depth increase must escalate across tiers even while latched."""
    db = _anchored_db()
    portfolio = FakePortfolio({"BTCUSDT": {"qty": 100}}, db=db)
    # -7% → trip tier 1
    res1 = ct.evaluate_and_act(FakeClient({"BTCUSDT": 0.93}), portfolio)
    assert res1["tier"] == 1
    # deepen to -13% → escalate to tier 2 (no release in between)
    res2 = ct.evaluate_and_act(FakeClient({"BTCUSDT": 0.87}), portfolio)
    assert res2["tier"] == 2 and res2["prev_tier"] == 1
    assert res2["action"] in ("DELEVERAGE", "DELEVERAGE_REPORTED")


# ── acceptance addendum: alert visibility + throttling/escalation ─────────────

def test_opaque_db_emits_visibility_alert(_no_live_alerts):
    class OpaqueDB:
        def kv_get(self, key, default=None):
            return "<mock>"

        def kv_set(self, key, value):
            pass

    kpf.run(OpaqueDB())
    assert any(e["event_type"] == "KV_PREFLIGHT_OPAQUE"
               for e in _no_live_alerts)
    # opaque must NOT emit a blocking FAIL alert
    assert not any(e["event_type"] == "KV_PREFLIGHT_FAIL"
                   for e in _no_live_alerts)


def test_fail_alert_throttled_to_hourly(_no_live_alerts):
    db = _bind_conn(FakeDB(
        kv={"cash_balance": "50", "hmm_regime": "{}"},
        portfolio_rows=[("BTCUSDT", 0.1, 50000, 50)],
        kv_ages={"cash_balance": 3 * 3600},  # stale → FAIL
    ))
    kpf.run(db)   # first failure → immediate alert
    kpf.run(db)   # second failure seconds later → throttled, no new alert
    fails = [e for e in _no_live_alerts
             if e["event_type"] == "KV_PREFLIGHT_FAIL"]
    assert len(fails) == 1


def test_continuous_fail_escalates_after_2h(_no_live_alerts):
    db = _bind_conn(FakeDB(
        kv={"cash_balance": "50", "hmm_regime": "{}"},
        portfolio_rows=[("BTCUSDT", 0.1, 50000, 50)],
        kv_ages={"cash_balance": 3 * 3600},
    ))
    kpf.run(db)  # trip
    # fast-forward: first failure 3h ago, last alert just now → throttle
    # window open (>=1h), escalation boundary crossed
    st = db.kv_get("kv_preflight:fail_state")
    st["first_ts"] = time.time() - 3 * 3600
    db.kv_set("kv_preflight:fail_state", st)
    kpf.run(db)
    assert any(e["event_type"] == "KV_PREFLIGHT_ESCALATED"
               for e in _no_live_alerts)


def test_healthy_round_clears_fail_streak(_no_live_alerts):
    db = _bind_conn(FakeDB(
        kv={"cash_balance": "50", "hmm_regime": "{}"},
        portfolio_rows=[("BTCUSDT", 0.1, 50000, 50)],
        kv_ages={"cash_balance": 3 * 3600},
    ))
    kpf.run(db)  # trip + state saved
    assert db.kv_get("kv_preflight:fail_state")
    # heal: fresh cash_balance
    db2 = _bind_conn(FakeDB(
        kv={"cash_balance": "50", "hmm_regime": "{}"},
        portfolio_rows=[("BTCUSDT", 0.1, 50000, 50)],
        kv_ages={"cash_balance": 3 * 3600},
    ))
    db2.kv = {"cash_balance": "50", "hmm_regime": "{}",
              "kv_preflight:fail_state":
                  db.kv_get("kv_preflight:fail_state")}
    db2.kv_ages = {}  # everything fresh
    res = kpf.run(db2)
    assert res["ok"] is True
    assert db2.kv_get("kv_preflight:fail_state") == {}
