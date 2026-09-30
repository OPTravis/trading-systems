"""WO-1004: exit-engine config-pollution postmortem fixes.

1) run_exit_step kill-switch observability (mode=off must warn + show
   what it holds back; notify-mode held-back auto kinds warn too)
2) _step_config_guard safety-switch fingerprint drift alert
3) scan_exit_triggers accepts raw sqlite conn AND StateDB
4) get_my_trades -1121 invalid symbol → warning + [] (both clients)
"""
import logging
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import src.exit_check as ec
from src.exit_check import run_exit_step, scan_exit_triggers
from src.state_db import get_state_db


@pytest.fixture
def db():
    d = get_state_db()
    d.kv_set("ledger:shadow:bootstrap_ts", time.time() - 86400)
    yield d


class _Client:
    """Stub client: one hold-expired position, prices fixed."""
    def __init__(self, price=90.0):
        self.price = price
        self.sell_calls = []

    def get_ticker_price(self, symbol):
        return self.price

    def get_klines(self, symbol, interval="1h", limit=500, **kw):
        # flat closes → band_position ~0.5, momentum quiet
        return [{"close": 100.0} for _ in range(30)]

    def get_open_orders(self, symbol):
        return []

    def cancel_order(self, symbol, order_id):
        return True

    def create_order(self, **kw):
        self.sell_calls.append(kw)
        return {"orderId": 1, "status": "FILLED", "fills": []}

    def place_market_sell(self, symbol, quantity):
        self.sell_calls.append({"symbol": symbol, "quantity": quantity})
        return {"orderId": 1, "status": "FILLED",
                "executedQty": quantity, "cummulativeQuoteQty": quantity * self.price}

    def get_free_balance(self, asset):
        return 10.0

    def get_symbol_filters(self, symbol):
        return {"stepSize": 0.001, "minNotional": 5}

    def place_stop_loss_market(self, symbol, quantity, stop_price):
        return {"orderId": 2, "status": "NEW"}


def _seed_hold_expired(db, sym="AAAUSDT"):
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO portfolio (symbol, quantity, entry_price, opened_at)"
        " VALUES (?, 10, 100, datetime('now', '-3 days'))", (sym,))
    conn.commit()
    db.trade_add(sym, "BUY", 10, 100.0)       # BUY anchor for hold clock
    conn.execute("UPDATE trades SET timestamp = ? WHERE symbol = ? "
                 "AND side = 'BUY'", (time.time() - 3600 * 72, sym))
    conn.commit()
    db.kv_set("exit:AAAUSDT:tp_pct", "99")   # keep tp/sl out of the way
    db.kv_set("exit:AAAUSDT:sl_pct", "99")


# ── (1) kill-switch observability ───────────────────────────────────

def test_mode_off_warns_and_reports_held_back(db, caplog):
    db.kv_set("exit:mode", "off")
    _seed_hold_expired(db)
    with caplog.at_level(logging.WARNING):
        res = run_exit_step(_Client(price=95.0), db, now=time.time())
    assert res == []
    warns = [r.message for r in caplog.records
             if r.levelno >= logging.WARNING]
    assert any("KILL-SWITCH" in m and "off" in m for m in warns)
    assert any("would-exit BLOCKED" in m and "AAAUSDT" in m for m in warns)


def test_mode_off_zero_candidates_still_warns(db, caplog):
    db.kv_set("exit:mode", "off")   # no positions at all
    with caplog.at_level(logging.WARNING):
        run_exit_step(_Client(), db, now=time.time())
    assert any("KILL-SWITCH" in r.message for r in caplog.records
               if r.levelno >= logging.WARNING)
    assert not any("would-exit" in r.message for r in caplog.records)


def test_mode_off_probe_failure_still_warns(db, caplog, monkeypatch):
    db.kv_set("exit:mode", "off")

    def boom(*a, **kw):
        raise RuntimeError("ticker down")

    monkeypatch.setattr(ec, "evaluate_exits", boom)
    with caplog.at_level(logging.WARNING):
        run_exit_step(_Client(), db, now=time.time())
    msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("KILL-SWITCH" in m for m in msgs)
    assert any("probe failed" in m for m in msgs)


def test_mode_notify_auto_kind_warns_and_notifies(db, caplog):
    db.kv_set("exit:mode", "notify")
    _seed_hold_expired(db)
    cli = _Client(price=95.0)
    with caplog.at_level(logging.WARNING):
        res = run_exit_step(cli, db, now=time.time())
    assert cli.sell_calls == []            # notify never executes
    assert res and res[0]["status"] == "notified"   # and still notifies
    assert any("held back" in r.message for r in caplog.records
               if r.levelno >= logging.WARNING)


def test_mode_auto_executes_normally(db, caplog):
    _seed_hold_expired(db)
    cli = _Client(price=95.0)
    res = run_exit_step(cli, db, now=time.time())
    assert cli.sell_calls, "auto mode must still execute"
    assert not any("KILL-SWITCH" in r.message for r in caplog.records
                   if r.levelno >= logging.WARNING)


# ── (2) config guard fingerprint ────────────────────────────────────

def test_config_guard_alerts_on_drift(db, caplog):
    import src.scan_orchestrator as so
    db.kv_set("exit:mode", "auto")
    so._step_config_guard(ctx={})          # round 1: fingerprint baseline
    with caplog.at_level(logging.WARNING):
        db.kv_set("exit:mode", "off")      # silent pollution happens here
        so._step_config_guard(ctx={})      # round 2: must scream
    warns = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("SAFETY-SWITCH DRIFT" in m and "exit:mode" in m
               and "off" in m for m in warns)
    # outbox got the drift notification
    rows = db._get_conn().execute(
        "SELECT COUNT(*) FROM notification_outbox "
        "WHERE msg_type='config_drift'"
    ).fetchone()
    assert rows[0] >= 1
    # steady state: no drift, no new alerts
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        so._step_config_guard(ctx={})
    assert not any("DRIFT" in r.message for r in caplog.records)


def test_config_guard_fail_open(db):
    import src.scan_orchestrator as so
    # db with no kv table at all — must not raise
    class _Broken:
        def kv_get(self, k):
            raise sqlite3.OperationalError("no table")

        def kv_set(self, k, v):
            raise sqlite3.OperationalError("no table")

    import src.state_db as sd
    orig = sd.get_state_db
    try:
        sd.get_state_db = lambda: _Broken()
        so._step_config_guard(ctx={})   # must swallow + warn, not raise
    finally:
        sd.get_state_db = orig


# ── (3) scan_exit_triggers call shapes ──────────────────────────────

def test_scan_exit_triggers_raw_conn_and_statedb(db):
    _seed_hold_expired(db)
    prices = {"AAAUSDT": 130.0}          # +30% → tp trigger
    holdings = ["AAAUSDT"]
    raw = db._get_conn()
    trig_raw = scan_exit_triggers(raw, holdings, prices)
    trig_sdb = scan_exit_triggers(db, holdings, prices)
    assert trig_raw and "hold_expiry" in trig_raw  # hold outranks tp
    assert trig_sdb == trig_raw          # StateDB shape now equivalent


def test_scan_exit_triggers_bad_object_warns(caplog):
    with caplog.at_level(logging.WARNING):
        out = scan_exit_triggers(object(), [], {})
    assert out is None
    assert any("neither a sqlite connection" in r.message
               for r in caplog.records if r.levelno >= logging.WARNING)


# ── (4) -1121 benign downgrade ──────────────────────────────────────

def test_ccxt_invalid_symbol_downgraded(caplog):
    import src.ccxt_client as cc

    class FakeEx:
        def fetch_my_trades(self, *a, **kw):
            raise Exception("(400, -1121, 'Invalid symbol.', ...)")

    cli = cc.BinanceClient.__new__(cc.BinanceClient)
    cli.exchange = FakeEx()
    with caplog.at_level(logging.WARNING):
        fills = cli.get_my_trades("XUSDT")
    assert fills == []
    recs = caplog.records
    assert any(r.levelno == logging.WARNING and "invalid symbol" in r.message
               for r in recs)
    assert not any(r.levelno == logging.ERROR for r in recs)


def test_ccxt_other_errors_stay_error(caplog):
    import src.ccxt_client as cc

    class FakeEx:
        def fetch_my_trades(self, *a, **kw):
            raise Exception("(500, -1000, 'server boom')")

    cli = cc.BinanceClient.__new__(cc.BinanceClient)
    cli.exchange = FakeEx()
    with caplog.at_level(logging.INFO):
        cli.get_my_trades("BTCUSDT")
    assert any(r.levelno == logging.ERROR for r in caplog.records)
