"""P0-1 dust_reaper tests (設計 v1.1 §四, report-only first 24h).

Verdict matrix / watch transitions / report-vs-auto / re-entry constraint /
watch cap WATCH_REPORT / filter-failure skip (6979798 no-guess lesson).
"""

import json
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src import dust_reaper as dr  # noqa: E402


class FakeDB:
    def __init__(self, kv=None):
        self.kv = kv or {}
        self.audits = []

    def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    def kv_set(self, key, value):
        self.kv[key] = value

    def audit_log(self, action, details="", old_value="", new_value="",
                  source="system"):
        self.audits.append({"action": action, "details": details})


class FakePortfolio:
    def __init__(self, positions, db=None):
        self.positions = positions
        self._db = db
        self.closed = []

    def close_position(self, symbol, close_price=None, exit_reason=None,
                       client_order_id=None):
        self.closed.append(
            {"symbol": symbol, "price": close_price,
             "reason": exit_reason, "cid": client_order_id}
        )
        return {"success": True}


class FakeClient:
    def __init__(self, prices, filters, open_orders=None, fail_symbols=()):
        self.prices = prices
        self.filters = filters
        self.open_orders = open_orders or {}
        self.fail_symbols = set(fail_symbols)
        self.orders_placed = []

    def get_ticker_price(self, symbol):
        if symbol in self.fail_symbols:
            raise RuntimeError("api down")
        return self.prices.get(symbol, 0.0)

    def get_symbol_filters(self, symbol):
        return self.filters.get(symbol, {})

    def get_open_orders(self, symbol):
        return self.open_orders.get(symbol, [])

    def place_order(self, symbol, side, order_type, quantity):
        self.orders_placed.append(
            {"symbol": symbol, "side": side, "type": order_type,
             "qty": quantity}
        )
        return {
            "orderId": 900001,
            "clientOrderId": "dust_test_1",
            "fills": [{"qty": quantity, "price": self.prices[symbol],
                       "commission": 0.01}],
        }


def _filters(min_qty, min_notional, apply_min_to_market=False):
    return {
        "minQty": min_qty,
        "minNotional": min_notional,
        "applyMinToMarket": apply_min_to_market,
        "qty_decimals": 4,
    }


@pytest.fixture(autouse=True)
def _tmp_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(dr, "JSONL_PATH", str(tmp_path / "dust_reaper.jsonl"))
    # neutralize live_alerts
    import types
    fake_alerts = types.SimpleNamespace(emit=lambda *a, **k: True)
    monkeypatch.setitem(sys.modules, "src.live_alerts", fake_alerts)
    yield


# --- verdict matrix ---------------------------------------------------------

def test_protected_position_skipped():
    # protected (has open order) and above healthy line → untouched
    db = FakeDB()
    pm = FakePortfolio({"SUIUSDT": {"quantity": 10}}, db)
    cli = FakeClient(
        {"SUIUSDT": 2.0},
        {"SUIUSDT": _filters(1, 5.0, True)},
        open_orders={"SUIUSDT": [{"orderId": 1}]},
    )
    s = dr.run(cli, pm)
    assert s["protected"] == 1
    assert s["liquidate_candidates"] == 0
    assert s["watch"] == 0
    assert not cli.orders_placed


def test_unprotectable_sellable_is_candidate_report_mode():
    # notional 10 < 5*1.05? no — use tighter: notional 5.2 < 5.25 → candidate
    db = FakeDB()  # default mode = report
    pm = FakePortfolio({"SUIUSDT": {"quantity": 2.6}}, db)
    cli = FakeClient({"SUIUSDT": 2.0}, {"SUIUSDT": _filters(1, 5.0, True)})
    s = dr.run(cli, pm)
    assert s["liquidate_candidates"] == 1
    assert s["liquidated"] == 0          # report mode never executes
    assert not cli.orders_placed
    assert not pm.closed
    # jsonl verdict row written
    rows = [json.loads(l) for l in open(dr.JSONL_PATH)]
    assert rows[0]["verdict"] == "LIQUIDATE_CANDIDATE"
    assert rows[0]["mode"] == "report"


def test_auto_mode_liquidates_and_books_everything():
    db = FakeDB({dr.KV_MODE: "auto"})
    pm = FakePortfolio({"SUIUSDT": {"quantity": 2.6}}, db)
    cli = FakeClient({"SUIUSDT": 2.0}, {"SUIUSDT": _filters(1, 5.0, True)})
    s = dr.run(cli, pm)
    assert s["liquidated"] == 1
    assert len(cli.orders_placed) == 1
    o = cli.orders_placed[0]
    assert o["side"] == "SELL" and o["type"] == "MARKET"
    # bookkeeping chain
    assert pm.closed and pm.closed[0]["reason"] == "dust_reaper"
    assert any(a["action"] == "DUST_REAPER_LIQUIDATED" for a in db.audits)
    block = db.kv[dr.KV_REENTRY]["SUIUSDT"]
    assert block["until"] > time.time()
    assert block["exit_price"] == pytest.approx(2.0)


def test_notional_above_healthy_line_left_alone():
    # 11 >= 5*1.05 → healthy (can place protections), even if unprotectED
    db = FakeDB({dr.KV_MODE: "auto"})
    pm = FakePortfolio({"AAAUSDT": {"quantity": 5.5}}, db)
    cli = FakeClient({"AAAUSDT": 2.0}, {"AAAUSDT": _filters(1, 5.0, True)})
    s = dr.run(cli, pm)
    assert s["liquidate_candidates"] == 0
    assert not cli.orders_placed


def test_unsellable_goes_to_watch_legacy_filter():
    # notional 1.2 < minQty floor 2 (legacy MIN_NOTIONAL: floor=minQty)
    db = FakeDB()
    pm = FakePortfolio({"BBBUSDT": {"quantity": 0.6}}, db)
    cli = FakeClient({"BBBUSDT": 2.0}, {"BBBUSDT": _filters(2.0, 5.0, False)})
    s = dr.run(cli, pm)
    assert s["watch"] == 1
    assert s["liquidate_candidates"] == 0
    assert dr.KV_WATCH in db.kv and "BBBUSDT" in db.kv[dr.KV_WATCH]


def test_unsellable_goes_to_watch_apply_min_to_market():
    # NOTIONAL applyMinToMarket=true: floor=5, notional 3.6 → watch
    db = FakeDB()
    pm = FakePortfolio({"CCCUSDT": {"quantity": 1.8}}, db)
    cli = FakeClient({"CCCUSDT": 2.0}, {"CCCUSDT": _filters(1, 5.0, True)})
    s = dr.run(cli, pm)
    assert s["watch"] == 1 and s["liquidate_candidates"] == 0


def test_watch_upgrade_when_price_recovers():
    # round 1: below sell floor → watch. round 2: price rises above floor
    # (but still < healthy line) → upgraded to LIQUIDATE_CANDIDATE
    db = FakeDB()
    pm = FakePortfolio({"DDDUSDT": {"quantity": 2.0}}, db)
    cli = FakeClient({"DDDUSDT": 1.0}, {"DDDUSDT": _filters(1, 5.0, True)})
    s1 = dr.run(cli, pm)
    assert s1["watch"] == 1 and s1["liquidate_candidates"] == 0
    cli.prices["DDDUSDT"] = 2.6   # notional 5.2 → sellable, < 5.25
    s2 = dr.run(cli, pm)
    assert s2["liquidate_candidates"] == 1
    assert s2["watch_upgraded"] == 1
    assert "DDDUSDT" not in db.kv.get(dr.KV_WATCH, {})


def test_watch_removed_when_healthy_again():
    db = FakeDB()
    db.kv[dr.KV_WATCH] = {"EEEUSDT": {"since": 1, "notional": 3}}
    pm = FakePortfolio({"EEEUSDT": {"quantity": 6.0}}, db)
    cli = FakeClient({"EEEUSDT": 2.0}, {"EEEUSDT": _filters(1, 5.0, True)})
    s = dr.run(cli, pm)  # notional 12 >= 5.25 → healthy
    assert s["watch_removed"] == 1
    assert "EEEUSDT" not in db.kv.get(dr.KV_WATCH, {})


def test_filter_failure_skips_without_guessing():
    # empty filters (read failed) → skip, counted — never guess a floor
    db = FakeDB({dr.KV_MODE: "auto"})
    pm = FakePortfolio({"FFFUSDT": {"quantity": 1.0}}, db)
    cli = FakeClient({"FFFUSDT": 2.0}, {"FFFUSDT": {}})
    s = dr.run(cli, pm)
    assert s["filter_failures"] == 1
    assert s["liquidate_candidates"] == 0
    assert not cli.orders_placed


def test_api_failure_counted_not_raised():
    db = FakeDB()
    pm = FakePortfolio({"GGGUSDT": {"quantity": 1.0}}, db)
    cli = FakeClient({"GGGUSDT": 2.0}, {"GGGUSDT": _filters(1, 5.0)},
                     fail_symbols=("GGGUSDT",))
    s = dr.run(cli, pm)   # must not raise
    assert s["api_failures"] >= 1


def test_watch_cap_emits_watch_report_throttled():
    db = FakeDB()
    positions = {
        f"W{i:02d}USDT": {"quantity": 0.1} for i in range(dr.WATCH_MAX_POSITIONS + 3)
    }
    for i in range(dr.WATCH_MAX_POSITIONS + 3):
        pass
    pm = FakePortfolio(positions, db)
    cli = FakeClient(
        {f"W{i:02d}USDT": 1.0 for i in range(dr.WATCH_MAX_POSITIONS + 3)},
        {f"W{i:02d}USDT": _filters(1.0, 5.0, True) for i in range(dr.WATCH_MAX_POSITIONS + 3)},
    )
    s = dr.run(cli, pm)
    assert s["watch"] == dr.WATCH_MAX_POSITIONS + 3
    assert db.kv.get(dr.KV_WATCH_REPORT_TS)  # throttle stamp written


# --- re-entry constraint -----------------------------------------------------

def test_reentry_blocked_during_cooldown():
    db = FakeDB()
    db.kv[dr.KV_REENTRY] = {
        "SUIUSDT": {"until": time.time() + 3600, "exit_price": 2.0}}
    assert dr.reentry_allowed(db, "SUIUSDT", ref_price=2.5) is False


def test_reentry_price_path_allowed():
    db = FakeDB()
    db.kv[dr.KV_REENTRY] = {
        "SUIUSDT": {"until": time.time() + 3600, "exit_price": 2.0}}
    assert dr.reentry_allowed(db, "SUIUSDT", ref_price=2.0) is True  # <= 2.04


def test_reentry_unblocked_after_cooldown():
    db = FakeDB()
    db.kv[dr.KV_REENTRY] = {
        "SUIUSDT": {"until": time.time() - 1, "exit_price": 2.0}}
    assert dr.reentry_allowed(db, "SUIUSDT", ref_price=9.9) is True
    assert "SUIUSDT" not in db.kv[dr.KV_REENTRY]


def test_reentry_unknown_symbol_allowed():
    assert dr.reentry_allowed(FakeDB(), "NEWUSDT") is True
