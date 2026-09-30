"""WO-0931: strategy exit signal consumption (P1).

Impact recap: 6/6 strategies return SELL from their position branch but
nothing consumed them; exits relied purely on resting OCO orders. QNT
9/30 (+8% TP crossed, gave back to +5.2%) and the coming 0G hold expiry
(10/1 19:52, trades anchor 1790682754) forced the fix.

P1: hold_expiry / take_profit / stop_loss auto-exit; momentum reversal
notify-only; kv kill-switch; per-symbol cooldown; OCO-aware cancel-then-
market-sell with safety-net re-SL; booking reuses the WO-0928
event-anchored reconciler (orderId idempotent).
"""

import datetime
import sqlite3
import time

import pytest

from src.state_db import get_state_db
from src.exit_check import (
    evaluate_exits, execute_exit, run_exit_step, scan_exit_triggers,
    _last_buy_ts, _band_position,
)


class ExitFakeClient:
    """Full-surface fake for the exit chain (evaluate + execute)."""

    def __init__(self, *, price=100.0, klines=None, open_orders=None,
                 cancel_fail=False, sell_fail=False, balances=None,
                 trades_by_symbol=None):
        self.price = price
        self._klines = klines
        self._open_orders = open_orders or []
        self.cancel_fail = cancel_fail
        self.sell_fail = sell_fail
        self._balances = balances or {}
        self._trades = trades_by_symbol or {}
        self.sell_calls = []
        self.cancel_calls = []
        self.emergency_sl_calls = []

    def get_ticker_price(self, symbol):
        return self.price

    def get_klines(self, symbol, interval="1h", limit=40):
        return self._klines

    def get_open_orders(self, symbol=None):
        return self._open_orders

    def cancel_order(self, symbol, order_id):
        self.cancel_calls.append(order_id)
        if self.cancel_fail:
            raise Exception("cancel rejected (OCO leg busy)")
        return {"orderId": order_id, "status": "CANCELED"}

    def place_market_sell(self, symbol, quantity):
        self.sell_calls.append((symbol, quantity))
        if self.sell_fail:
            return None
        return {"orderId": 990001, "executedQty": str(quantity),
                "status": "FILLED"}

    def place_stop_loss_market(self, symbol, quantity, stop_price):
        self.emergency_sl_calls.append((symbol, quantity, stop_price))
        return {"orderId": 990002}

    def get_symbol_filters(self, symbol):
        return {"stepSize": 0.001, "minNotional": 5.0}

    def get_free_balance(self, asset="USDT"):
        return float(self._balances.get(asset, 10_000.0))

    # WO-0928 reconciler surface (used by execute_exit post-sell booking)
    def get_account(self):
        return {"balances": [
            {"asset": a, "free": str(v), "locked": "0"}
            for a, v in self._balances.items()
        ]}

    def get_my_trades(self, symbol, limit=100, from_id=None):
        return self._trades.get(symbol, [])


def _fill(symbol, oid, qty, price, ts, is_buyer=False):
    return {"id": oid * 10, "price": str(price), "qty": str(qty),
            "quoteQty": str(round(qty * price, 8)), "commission": "0",
            "commissionAsset": symbol[:-4], "time": int(ts * 1000),
            "isBuyer": is_buyer, "isMaker": False, "orderId": oid,
            "symbol": symbol}


def _seed_buy(db, symbol, qty, price, ts):
    db.trade_add(symbol, "BUY", qty, price)
    conn = db._get_conn()
    conn.execute(
        "UPDATE trades SET timestamp = ? WHERE symbol = ? AND side = 'BUY'",
        (ts, symbol))
    conn.commit()
    from src.ledger import record_fill
    st = record_fill(
        {"type": "BUY", "symbol": symbol, "qty": qty, "price": price,
         "ts": ts, "order_id": "111", "source": "test.seed"},
        observe_only=True, db=db)
    assert st["status"] == "ok", st


@pytest.fixture
def db():
    d = get_state_db()
    d.kv_set("ledger:shadow:bootstrap_ts", time.time() - 86400)
    yield d


def _pos(symbol="XUSDT", qty=10.0, entry=100.0, opened_at=None):
    return {"symbol": symbol, "quantity": qty, "entry_price": entry,
            "opened_at": opened_at or "2026-09-30T10:30:07"}


NOW = 1790800000.0  # fixed clock


# ---------- evaluate ----------

def test_hold_expiry_anchors_on_trades_not_refreshed_opened_at(db):
    # opened_at refreshed by a sync rebuild 1h ago, real BUY 49h ago —
    # the refreshed (later) opened_at must NOT push the hold clock forward
    refreshed = datetime.datetime.fromtimestamp(
        NOW - 3600, tz=datetime.timezone.utc).isoformat()
    db.trade_add("XUSDT", "BUY", 10, 100.0)
    conn = db._get_conn()
    conn.execute("UPDATE trades SET timestamp = ? WHERE symbol = ? "
                 "AND side = 'BUY'", (NOW - 49 * 3600, "XUSDT"))
    conn.commit()
    assert _last_buy_ts(db, "XUSDT") == NOW - 49 * 3600
    cli = ExitFakeClient(price=100.0)
    dec = evaluate_exits(cli, db, holdings=[_pos(opened_at=refreshed)],
                         now=NOW)
    assert len(dec) == 1 and dec[0]["kind"] == "hold_expiry"
    assert dec[0]["auto"] is True and dec[0]["sell_pct"] == 100


def test_take_profit_and_stop_loss_trigger_auto(db):
    cli = ExitFakeClient(price=109.0)  # +9% >= 8 default
    dec = evaluate_exits(cli, db, holdings=[_pos()], now=NOW)
    assert [d["kind"] for d in dec] == ["take_profit"]
    assert dec[0]["auto"] is True

    cli2 = ExitFakeClient(price=93.0)  # -7% <= -6 default
    dec2 = evaluate_exits(cli2, db, holdings=[_pos()], now=NOW)
    assert [d["kind"] for d in dec2] == ["stop_loss"]


def test_momentum_reversal_evaluates_as_auto_since_p2(db):
    # closes drift around 100, last close 99 → band position < 0.15.
    # P1 shipped this notify-only; P2 (Leo 2026-09-30 11:18, observation
    # week waived) promoted it to the same auto tier as hold/TP/SL.
    klines = [{"close": 100.0 + (0.2 if i % 2 else -0.2)} for i in range(19)]
    klines.append({"close": 99.0})
    cli = ExitFakeClient(price=104.0, klines=klines)  # +4% (no TP), pnl>0
    dec = evaluate_exits(cli, db, holdings=[_pos()], now=NOW)
    assert [d["kind"] for d in dec] == ["momentum_reversal"]
    assert dec[0]["auto"] is True and dec[0]["sell_pct"] == 100  # deep tier


def test_no_trigger_on_quiet_position(db):
    klines = [{"close": 100.0}] * 20
    cli = ExitFakeClient(price=102.0, klines=klines)  # +2%, mid band
    assert evaluate_exits(cli, db, holdings=[_pos()], now=NOW) == []


def test_kv_param_override_tp(db):
    db.kv_set("exit:XUSDT:tp_pct", 1.5)
    cli = ExitFakeClient(price=101.6)  # +1.6% >= 1.5 override
    dec = evaluate_exits(cli, db, holdings=[_pos()], now=NOW)
    assert [d["kind"] for d in dec] == ["take_profit"]


# ---------- execute guards ----------

def test_execute_cooldown_blocks_repeat(db):
    db.kv_set("exit:XUSDT:last_exit_ts", NOW - 60)  # 1 min ago < 600
    d = {"symbol": "XUSDT", "kind": "take_profit", "qty": 10,
         "entry_price": 100, "price": 109, "pnl_pct": 9.0,
         "held_hours": 5, "sell_pct": 100, "reason": "tp", "ts": NOW}
    cli = ExitFakeClient()
    out = execute_exit(cli, db, d, now=NOW)
    assert out["status"] == "cooldown" and cli.sell_calls == []


def test_cancel_failure_aborts_keeps_oco_and_notifies(db):
    oco_leg = {"symbol": "XUSDT", "side": "SELL", "status": "NEW",
               "orderId": 555}
    cli = ExitFakeClient(open_orders=[oco_leg], cancel_fail=True)
    d = {"symbol": "XUSDT", "kind": "hold_expiry", "qty": 10,
         "entry_price": 100, "price": 100, "pnl_pct": 0.0,
         "held_hours": 49, "sell_pct": 100, "reason": "hold", "ts": NOW}
    out = execute_exit(cli, db, d, now=NOW)
    assert out["status"] == "aborted"
    assert cli.sell_calls == []  # never sold while an OCO leg survived
    pend = db.notification_outbox_pending(limit=10)
    assert any("aborted" in (p.get("title") or "") for p in pend)


def test_market_sell_failure_lists_emergency_sl(db):
    cli = ExitFakeClient(open_orders=[
        {"symbol": "XUSDT", "side": "SELL", "status": "NEW", "orderId": 556}],
        sell_fail=True)
    d = {"symbol": "XUSDT", "kind": "stop_loss", "qty": 10,
         "entry_price": 100, "price": 93, "pnl_pct": -7.0,
         "held_hours": 3, "sell_pct": 100, "reason": "sl", "ts": NOW}
    out = execute_exit(cli, db, d, now=NOW)
    assert out["status"] == "failed"
    assert len(cli.cancel_calls) == 1  # OCO was cancelled...
    assert len(cli.emergency_sl_calls) == 1  # ...so a -3% net was re-listed
    pend = db.notification_outbox_pending(limit=10)
    assert any("FAILED" in (p.get("title") or "") for p in pend)


def test_full_chain_sell_books_via_wo0928_reconciler(db):
    # seed a real BUY lifecycle so the reconciler can compute the gap.
    # Real clock (not the fixed NOW, which sits in the future): the WO-0928
    # lifecycle anchor rejects any SELL fill predating the BUY row.
    _seed_buy(db, "XUSDT", 10.0, 100.0, time.time() - 7200)
    sell_oid = 880077
    cli = ExitFakeClient(
        price=109.0,
        balances={"X": 10.0, "USDT": 1100.0},
        open_orders=[{"symbol": "XUSDT", "side": "SELL", "status": "NEW",
                      "orderId": 557}],
        trades_by_symbol={"XUSDT": [
            _fill("XUSDT", sell_oid, 10.0, 108.9, time.time())]},
    )
    d = {"symbol": "XUSDT", "kind": "take_profit", "qty": 10,
         "entry_price": 100, "price": 109, "pnl_pct": 9.0,
         "held_hours": 2, "sell_pct": 100, "reason": "tp", "ts": NOW}
    out = execute_exit(cli, db, d, now=NOW)
    assert out["status"] == "ok" and len(cli.sell_calls) == 1
    # the market sell got booked through the event-anchored reconciler
    evs = db._get_conn().execute(
        "SELECT type, order_id FROM ledger_events WHERE symbol='XUSDT' "
        "ORDER BY id").fetchall()
    sells = [e for e in evs if e["type"] == "SELL"]
    assert sells and sells[-1]["order_id"] == str(sell_oid)
    # kv cooldown stamped + notice queued
    assert float(db.kv_get("exit:XUSDT:last_exit_ts")) == NOW
    assert any("Auto exit" in (p.get("title") or "")
               for p in db.notification_outbox_pending(limit=10))


# ---------- mode gates ----------

def test_mode_off_skips_everything(db):
    db.kv_set("exit:mode", "off")
    cli = ExitFakeClient(price=109.0)
    assert run_exit_step(cli, db, now=NOW) == []
    assert cli.sell_calls == []


def _seed_portfolio_row(db, symbol="XUSDT", qty=10.0, entry=100.0):
    db._get_conn().execute(
        "INSERT OR REPLACE INTO portfolio "
        "(symbol, quantity, entry_price, strategy, opened_at, updated_at) "
        "VALUES (?, ?, ?, 'bollinger', ?, ?)",
        (symbol, qty, entry,
         datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
         datetime.datetime.now(tz=datetime.timezone.utc).isoformat()))
    db._get_conn().commit()


def test_mode_notify_notifies_but_never_sells(db):
    db.kv_set("exit:mode", "notify")
    _seed_portfolio_row(db)
    db.trade_add("XUSDT", "BUY", 10, 100.0)
    conn = db._get_conn()
    conn.execute("UPDATE trades SET timestamp = ? WHERE symbol = ? "
                 "AND side = 'BUY'", (NOW - 49 * 3600, "XUSDT"))
    conn.commit()
    cli = ExitFakeClient(price=109.0)
    res = run_exit_step(cli, db, now=NOW)
    assert all(r["status"] == "notified" for r in res)
    assert cli.sell_calls == [] and cli.cancel_calls == []


def test_auto_mode_executes_deterministic_kind(db):
    _seed_portfolio_row(db)
    db.trade_add("XUSDT", "BUY", 10, 100.0)
    conn = db._get_conn()
    conn.execute("UPDATE trades SET timestamp = ? WHERE symbol = ? "
                 "AND side = 'BUY'", (NOW - 49 * 3600, "XUSDT"))
    conn.commit()
    cli = ExitFakeClient(price=100.0,
                          balances={"X": 10.0, "USDT": 1100.0})  # hold, not tp
    res = run_exit_step(cli, db, now=NOW)
    assert res and res[0]["status"] == "ok"
    assert len(cli.sell_calls) == 1


# ---------- event_tick read-only trigger ----------

def test_scan_exit_triggers_with_raw_ro_conn(tmp_path):
    p = str(tmp_path / "ro.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE portfolio (symbol TEXT, quantity REAL, "
              "entry_price REAL, opened_at TEXT)")
    c.execute("CREATE TABLE trades (symbol TEXT, side TEXT, qty REAL, "
              "price REAL, pnl REAL, timestamp REAL, client_order_id TEXT)")
    c.execute("INSERT INTO portfolio VALUES ('XUSDT', 10, 100.0, ?)",
              ("2026-09-30T10:30:07",))
    c.execute("INSERT INTO trades VALUES ('XUSDT','BUY',10,100,0,?,NULL)",
              (NOW - 49 * 3600,))
    c.commit()
    c.close()
    ro = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    hit = scan_exit_triggers(ro, ["XUSDT"], {"XUSDT": 100.0}, now=NOW)
    ro.close()
    assert hit and "hold_expiry" in hit
    # quiet: price fine, hold young
    ro = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    hit2 = scan_exit_triggers(ro, ["XUSDT"], {"XUSDT": 101.0},
                              now=NOW - 48 * 3600 + 3600)
    ro.close()
    assert hit2 is None


# ---------- fail-open in the orchestrator step ----------

def test_step_exit_positions_fail_open(db):
    from src.scan_orchestrator import _step_exit_positions

    class ExplodingClient(ExitFakeClient):
        def get_ticker_price(self, symbol):
            raise Exception("proxy dead")

    # must not raise even with a broken exchange surface
    _step_exit_positions({"client": ExplodingClient()})


def test_band_position_math():
    klines = [{"close": 100.0 + (0.2 if i % 2 else -0.2)} for i in range(19)]
    klines.append({"close": 99.0})
    bp = _band_position(type("C", (), {"get_klines": staticmethod(
        lambda s, interval="1h", limit=40: klines)})(), "XUSDT")
    assert bp is not None and bp < 0.15


# ==================== P2 (Leo 2026-09-30 11:18): momentum auto + trailing ====

def _klines_with_close(last):
    # 19 alternating 99.8/100.2 closes (sd=0.2, band 99.6..100.4) + `last`
    ks = [{"close": 99.8 if i % 2 == 0 else 100.2} for i in range(19)]
    ks.append({"close": last})
    return ks


def _seed_for_momentum(db):
    _seed_portfolio_row(db)
    db.trade_add("XUSDT", "BUY", 10, 100.0)
    conn = db._get_conn()
    conn.execute("UPDATE trades SET timestamp = ? WHERE symbol = ? "
                 "AND side = 'BUY'", (time.time() - 3600, "XUSDT"))
    conn.commit()


def test_p2_momentum_deep_tier_sells_100_pct(db):
    _seed_for_momentum(db)
    # last close 99.0 -> band_position < 0.15 -> full exit
    cli = ExitFakeClient(price=104.0, klines=_klines_with_close(99.0),
                         balances={"X": 10.0, "USDT": 1100.0})
    res = run_exit_step(cli, db, now=time.time())
    assert res and res[0]["kind"] == "momentum_reversal"
    assert res[0]["status"] == "ok"
    assert cli.sell_calls and cli.sell_calls[0][1] == 10.0  # full qty


def test_p2_momentum_mid_tier_sells_50_and_relists_breakeven(db):
    _seed_for_momentum(db)
    # last close 99.75 -> band_position ~0.19 -> 50% tier
    cli = ExitFakeClient(price=104.0, klines=_klines_with_close(99.75),
                         balances={"X": 10.0, "USDT": 1100.0})
    res = run_exit_step(cli, db, now=time.time())
    assert res and res[0]["kind"] == "momentum_reversal"
    assert res[0]["status"] == "ok"
    assert cli.sell_calls and cli.sell_calls[0][1] == 5.0  # 50% of 10
    # remainder protected: one breakeven SL re-list, qty 5.0 @ entry*0.995
    assert len(cli.emergency_sl_calls) == 1
    sym, rq, rpx = cli.emergency_sl_calls[0]
    assert rq == 5.0 and rpx == pytest.approx(99.5, abs=1e-6)
    # notice mentions the protection
    assert any("protected" in (p.get("body") or "")
               for p in db.notification_outbox_pending(limit=10))


def test_p2_partial_relist_failure_flags_critical(db):
    class NoRelistClient(ExitFakeClient):
        def place_stop_loss_market(self, symbol, quantity, stop_price):
            self.emergency_sl_calls.append((symbol, quantity, stop_price))
            return None  # exchange refused the re-list

    _seed_for_momentum(db)
    cli = NoRelistClient(price=104.0, klines=_klines_with_close(99.75),
                         balances={"X": 10.0, "USDT": 1100.0})
    res = run_exit_step(cli, db, now=time.time())
    assert res[0]["status"] == "ok"  # the sell itself succeeded
    assert any("CRITICAL" in (p.get("body") or "")
               for p in db.notification_outbox_pending(limit=10))


def test_p2_priority_hold_beats_momentum(db):
    # BUY 49h ago (hold) AND a deep reversal band -> only hold fires (elif)
    _seed_portfolio_row(db)
    db.trade_add("XUSDT", "BUY", 10, 100.0)
    conn = db._get_conn()
    conn.execute("UPDATE trades SET timestamp = ? WHERE symbol = ? "
                 "AND side = 'BUY'", (NOW - 49 * 3600, "XUSDT"))
    conn.commit()
    cli = ExitFakeClient(price=104.0, klines=_klines_with_close(99.0))
    dec = evaluate_exits(cli, db, holdings=[_pos()], now=NOW)
    assert [d["kind"] for d in dec] == ["hold_expiry"]


def test_p2_mode_notify_still_gates_momentum(db):
    db.kv_set("exit:mode", "notify")
    _seed_for_momentum(db)
    cli = ExitFakeClient(price=104.0, klines=_klines_with_close(99.0),
                         balances={"X": 10.0, "USDT": 1100.0})
    res = run_exit_step(cli, db, now=time.time())
    assert res and res[0]["status"] == "notified"
    assert cli.sell_calls == []  # kill-switch beats the P2 promotion


def test_p2_trailing_step_calls_impl_with_skip_and_fail_open(monkeypatch):
    import src.cmd_trailing_check as ctc
    import src.scan_orchestrator as so

    seen = {}

    def fake_impl(skip_legacy_recon=False):
        seen["skip"] = skip_legacy_recon
        raise RuntimeError("boom inside trailing")

    monkeypatch.setattr(ctc, "cmd_trailing_check", fake_impl)
    # the step imports the symbol at call time from the module — patch there
    monkeypatch.setattr(
        "src.cmd_trailing_check.cmd_trailing_check", fake_impl)
    so._step_trailing_check({})  # must not raise (fail-open)
    assert seen["skip"] is True


def test_p2_trailing_mounted_after_defense_in_main_chain():
    src = open("/root/trading-systems/crypto-ai-trader/src/scan_orchestrator.py").read()
    chain = ("_step_reconcile_portfolio(ctx)\n"
             "        _step_exit_positions(ctx)\n"
             "        _step_defense_sweep(ctx)\n"
             "        _step_trailing_check(ctx)\n"
             "        _step_ledger_shadow_diff(ctx)")
    assert chain in src  # order: reconcile -> exit -> defense -> trailing
