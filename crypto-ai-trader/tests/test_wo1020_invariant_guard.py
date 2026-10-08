"""WO-1020 (10/8): the execution invariant guard — unit + wiring tests.

The guard re-computes three hard invariants per open position per round
(SL band incl. resting stop legs, max_hold, qty drift vs exchange) and
is the layer that would have caught both DASH 9/24 (resting ladder
-9.4/-10.07% outside a 5-7% design band) and PENGU #91 (54h hold vs
48h max, settle at -13.96%). Covered here:
  - each violation kind fires with the right disposition (exit vs
    alert-only) and the right race guards (exit-cooldown stamp)
  - OFF_BAND_TOL tolerates the executor's initial -7% stop vs the 6%
    band but still catches -10% ladders
  - human kill-switch (exit:mode=off/notify) is honoured: alert-only
  - dust pre-flight passes through: never hard-sell a below-tradable
    remainder
  - fail-safe wiring: all three orchestrator branches run the guard
    BEFORE the exit step, exactly once per round
"""
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.state_db import get_state_db
from src.ledger import record_fill
from src.invariant_guard import (
    check_position,
    run_guard,
    MAX_HOLD_GRACE_H,
    OFF_BAND_TOL,
)

ENTRY = 0.010237
TRAILING_FLOOR = 0.009725      # -5% trailed floor (live PENGU value)
FLOOR_FIXED = ENTRY * 0.94     # -6% design line (EXIT_DEFAULTS sl_pct)
FILTERS = {"stepSize": 1.0, "tickSize": 1e-06, "minQty": 1,
           "minNotional": 1.0}


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def _stop_leg(sym, stop_price, oid=91001):
    return {"symbol": sym, "side": "SELL", "status": "NEW",
            "orderId": oid, "type": "STOP_LOSS_LIMIT",
            "price": str(stop_price), "stopPrice": str(stop_price)}


class GuardClient:
    """Fake exchange for the guard + full execute_exit chain."""

    def __init__(self, *, price, orders=None, free="23.614",
                 locked="406", asset="PENGU", sym="PENGUUSDT"):
        self.price = price
        self.orders = list(orders or [])
        self.free, self.locked, self.asset, self.sym = free, locked, asset, sym
        self.sold = None
        self.emergency = None

    def get_ticker_price(self, symbol):
        return self.price

    def get_klines(self, symbol, interval="1h", limit=40):
        return None

    def get_open_orders(self, symbol=None):
        return [o for o in self.orders
                if symbol is None or o["symbol"] == symbol]

    def cancel_order(self, symbol, order_id):
        self.orders = [o for o in self.orders if o["orderId"] != order_id]
        return {"orderId": order_id, "status": "CANCELED"}

    def place_market_sell(self, symbol, quantity):
        self.sold = quantity
        return {"orderId": 990001, "executedQty": str(quantity),
                "status": "FILLED"}

    def place_stop_loss_market(self, symbol, quantity, stop_price):
        self.emergency = (symbol, quantity, stop_price)
        return {"orderId": 990002}

    def get_symbol_filters(self, symbol):
        return dict(FILTERS)

    def get_free_balance(self, asset="USDT"):
        # execute_exit cancels every SELL leg before selling, so free is
        # the post-cancel pool (free+locked) for the base asset.
        if asset == self.asset:
            return float(self.free) + float(self.locked)
        return 10_000.0

    def get_account(self):
        return {"balances": [
            {"asset": self.asset, "free": self.free, "locked": self.locked},
            {"asset": "USDT", "free": "100", "locked": "0"},
        ]}

    def get_my_trades(self, symbol, limit=100, from_id=None):
        return []


@pytest.fixture
def db():
    d = tempfile.mkdtemp()
    os.environ["STATE_DB_PATH"] = os.path.join(d, "wo1020.db")
    os.environ["TESTING"] = "1"
    db = get_state_db()
    yield db
    os.environ["STATE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "n.db")


def _seed(db, *, qty=429, stop_loss=TRAILING_FLOOR, held_h=9.0,
          sym="PENGUUSDT"):
    """Portfolio row + the BUY trade anchor, as the executor/guardian
    would have left it after a partial exit."""
    now = time.time()
    t_buy = now - held_h * 3600
    db.kv_set("ledger:shadow:bootstrap_ts", now - 86400)
    db.trade_add(sym, "BUY", 614, ENTRY)
    conn = db._get_conn()
    conn.execute(
        f"UPDATE trades SET timestamp = ? WHERE symbol = ? AND side = 'BUY'",
        (t_buy, sym))
    conn.commit()
    st = record_fill(
        {"type": "BUY", "symbol": sym, "qty": 614, "price": ENTRY,
         "ts": t_buy, "order_id": "3585506576", "source": "wo1020.seed"},
        observe_only=True, db=db)
    assert st["status"] == "ok", st
    row = {"quantity": qty, "entry_price": ENTRY,
           "strategy": "technical_v2", "opened_at": _iso(t_buy)}
    if stop_loss is not None:
        row["stop_loss"] = stop_loss
    db.portfolio_set(sym, row)
    return now


def _outbox_ids(db):
    return [r["notif_id"] for r in db.notification_outbox_pending(limit=100)]


# ── (a1) price through the breach line — the PENGU shape ───────────────

class TestSlBandBreach:
    def test_breach_fires_and_closes(self, db):
        """Price under BOTH floors, no exit attempt inside the cooldown
        window -> violation + protective close (mode auto default)."""
        now = _seed(db)
        cli = GuardClient(price=0.0095)   # under trail 0.009725 & fixed
        v = check_position(cli, db, db.portfolio_get("PENGUUSDT"), now=now)
        assert v and v["kind"] == "sl_band_breach" and v["action"] == "exit"

        s = run_guard(cli, db, now=now)
        assert s["mode"] == "auto"
        assert [b["kind"] for b in s["breaches"]] == ["sl_band_breach"]
        assert cli.sold == 429
        assert cli.emergency is None
        assert s["closed"] and s["closed"][0]["status"] == "ok"
        # exit chain stamped the cooldown -> next round will not re-fire
        assert float(db.kv_get("exit:PENGUUSDT:last_exit_ts") or 0) == now
        assert any(i.startswith("invariant:PENGUUSDT:sl_band_breach:")
                   for i in _outbox_ids(db))

    def test_exit_recent_race_guard(self, db):
        """The exit chain sold inside the 600s window (stamp present):
        the guard must stand down, not fight a working chain."""
        now = _seed(db)
        db.kv_set("exit:PENGUUSDT:last_exit_ts", now - 100)
        cli = GuardClient(price=0.0095)
        v = check_position(cli, db, db.portfolio_get("PENGUUSDT"), now=now)
        assert v is None
        s = run_guard(cli, db, now=now)
        assert s["breaches"] == [] and cli.sold is None

    def test_price_above_floors_no_violation(self, db):
        now = _seed(db)
        cli = GuardClient(price=0.0100)
        v = check_position(cli, db, db.portfolio_get("PENGUUSDT"), now=now)
        assert v is None


# ── (a2) resting stop leg outside the band — the DASH shape ────────────

class TestSlOrderOffBand:
    def test_dash_tail_ladder_fires(self, db):
        """Price in band but the stop leg sits at -10% (DASH #117/#119
        settled -9.4/-10.07%) -> close now at the in-band price."""
        now = _seed(db, stop_loss=None)
        cli = GuardClient(
            price=0.00983,                     # -3.96%, in band
            orders=[_stop_leg("PENGUUSDT", ENTRY * 0.8996)])  # ~-10%
        v = check_position(cli, db, db.portfolio_get("PENGUUSDT"),
                           now=now, open_orders=cli.get_open_orders())
        assert v and v["kind"] == "sl_order_off_band" and v["action"] == "exit"
        s = run_guard(cli, db, now=now)
        assert cli.sold == 429
        assert s["closed"][0]["status"] == "ok"

    def test_executor_initial_stop_tolerated(self, db):
        """The executor's initial -7% stop (0.9285*entry) against the 6%
        line must NOT fire — OFF_BAND_TOL exists exactly for this."""
        now = _seed(db, stop_loss=None)
        cli = GuardClient(
            price=0.00983,
            orders=[_stop_leg("PENGUUSDT", ENTRY * 0.9285)])  # -7.15%
        v = check_position(cli, db, db.portfolio_get("PENGUUSDT"),
                           now=now, open_orders=cli.get_open_orders())
        assert v is None

    def test_tp_only_no_stop_leg_no_fire(self, db):
        """TP-only book, price in band: naked-side detection is the
        guardian's job, not the guard's (order-side invariant only)."""
        now = _seed(db, stop_loss=None)
        cli = GuardClient(
            price=0.00983,
            orders=[{"symbol": "PENGUUSDT", "side": "SELL",
                     "status": "NEW", "orderId": 8, "type": "LIMIT_MAKER",
                     "price": "0.0112"}])
        v = check_position(cli, db, db.portfolio_get("PENGUUSDT"),
                           now=now, open_orders=cli.get_open_orders())
        assert v is None

    def test_guardian_clamped_leg_fires(self, db):
        """Guardian clamp from inside the band (px*0.93 while px is
        only -4%) -> the -12%-ish leg is a worse outcome waiting to
        happen; the guard closes at the in-band price instead."""
        now = _seed(db, stop_loss=None)
        cli = GuardClient(
            price=0.00983,
            orders=[_stop_leg("PENGUUSDT", 0.00983 * 0.93)])
        v = check_position(cli, db, db.portfolio_get("PENGUUSDT"),
                           now=now, open_orders=cli.get_open_orders())
        assert v and v["kind"] == "sl_order_off_band"


# ── (b) max_hold ────────────────────────────────────────────────────────

class TestMaxHold:
    def test_overdue_fires(self, db):
        """PENGU #91: 49h held vs 48h max, no exit attempt -> the guard
        closes what the starved exit step never consumed."""
        now = _seed(db, held_h=49.0)
        cli = GuardClient(price=0.0100)        # in band, no stop legs
        v = check_position(cli, db, db.portfolio_get("PENGUUSDT"), now=now)
        assert v and v["kind"] == "max_hold" and v["action"] == "exit"
        s = run_guard(cli, db, now=now)
        assert cli.sold == 429

    def test_grace_window_no_fire(self, db):
        """48.2h < 48h + 0.5h grace: the normal exit step gets the first
        shot every round; the guard is the backstop, not the trigger."""
        now = _seed(db, held_h=48.2)
        cli = GuardClient(price=0.0100)
        v = check_position(cli, db, db.portfolio_get("PENGUUSDT"), now=now)
        assert v is None


# ── (c) qty drift — alert only ──────────────────────────────────────────

class TestQtyDrift:
    def test_drift_alerts_but_never_sells(self, db):
        """Portfolio says 429, exchange says 350: the reconciler owns
        the books; the guard only escalates. Selling on an unconfirmed
        qty is the one action that could make it worse."""
        now = _seed(db)
        cli = GuardClient(price=0.0100, free="0", locked="350")
        v = check_position(cli, db, db.portfolio_get("PENGUUSDT"),
                           now=now, ex_qty=350.0)
        assert v and v["kind"] == "qty_drift" and v["action"] == "alert_only"
        s = run_guard(cli, db, now=now)
        assert cli.sold is None and s["closed"] == []
        assert any(i.startswith("invariant:PENGUUSDT:qty_drift:")
                   for i in _outbox_ids(db))

    def test_within_tolerance_no_fire(self, db):
        """2% of exchange qty (8.5 on 429.614) is reconciler territory."""
        now = _seed(db)
        cli = GuardClient(price=0.0100, free="1.614", locked="420")
        v = check_position(cli, db, db.portfolio_get("PENGUUSDT"),
                           now=now, ex_qty=421.614)
        assert v is None


# ── human kill-switch + dust pass-through ───────────────────────────────

class TestModeGateAndDust:
    def test_mode_off_alert_only(self, db):
        """exit:mode=off is a human decision: the breach is reported at
        ERROR level but never auto-closed (kill-switch contract)."""
        now = _seed(db)
        db.kv_set("exit:mode", "off")
        cli = GuardClient(price=0.0095)
        s = run_guard(cli, db, now=now)
        assert s["mode"] == "off" and s["breaches"]
        assert cli.sold is None and s["closed"] == []
        assert any(i.startswith("invariant:PENGUUSDT:sl_band_breach:")
                   for i in _outbox_ids(db))

    def test_mode_notify_alert_only(self, db):
        now = _seed(db)
        db.kv_set("exit:mode", "notify")
        cli = GuardClient(price=0.0095)
        s = run_guard(cli, db, now=now)
        assert s["mode"] == "notify" and cli.sold is None

    def test_dust_remainder_not_hard_sold(self, db):
        """WO-1009-③ pass-through: a below-minNotional remainder cannot
        market-sell; execute_exit aborts BEFORE cancelling legs — the
        guard must not strip the floor protecting it."""
        now = _seed(db, qty=0.5)
        cli = GuardClient(price=0.0095, free="0.5", locked="0")
        s = run_guard(cli, db, now=now)
        assert s["breaches"]                      # breach still reported
        assert cli.sold is None                   # but nothing sold
        assert cli.orders == []                   # (client had none; the
        # abort path in execute_exit is covered by its own suite — here
        # we pin that the guard's disposition degrades to alert-stands
        # when the protective close cannot clear dust)
        assert any(i.startswith("invariant:PENGUUSDT:") for i in _outbox_ids(db))


# ── fail-safe wiring in the orchestrator ────────────────────────────────

class TestFailSafeWiring:
    SRC = Path("src/scan_orchestrator.py").read_text()

    def test_guard_step_defined_fail_open(self):
        assert "def _step_invariant_guard(ctx=None):" in self.SRC
        body = self.SRC.split("def _step_invariant_guard(ctx=None):")[1]
        body = body.split("def cmd_cron_scan")[0]
        assert 'logger.warning("invariant guard step failed (non-fatal)"' in body

    def test_main_chain_runs_guard_before_exit_step(self):
        """The guard must precede the exit step in the main chain — it
        fixes what the exit step would then re-verify on clean books."""
        assert ("_step_config_guard(ctx)\n"
                "        _step_invariant_guard(ctx)\n"
                "        _step_exit_positions(ctx)" in self.SRC)

    def test_starved_branch_runs_guard(self):
        """WO-1011 lesson: the starved/exception branch must not starve
        the guard either (the PENGU 54h sit happened exactly here)."""
        branch = self.SRC.split(
            "WO-1020 (10/8): starved rounds owe the invariant guard too")[1]
        i_guard = branch.index("_step_invariant_guard(ctx)")
        i_exit = branch.index("_step_exit_positions(ctx)")
        assert i_guard < i_exit

    def test_no_opportunity_branch_runs_guard(self):
        branch = self.SRC.split(
            "WO-1020 (10/8): no-opportunity rounds owe the invariant")[1]
        i_guard = branch.index("_step_invariant_guard(ctx)")
        i_exit = branch.index("_step_exit_positions(ctx)")
        assert i_guard < i_exit

    def test_guard_runs_exactly_three_times(self):
        """Once per round: three call sites (main + two early-return
        branches), never in the finally block (no double-trigger)."""
        assert self.SRC.count("_step_invariant_guard(ctx)") == 3
        # the finally block (WO-0930b protection exit) is the LAST
        # "finally:" in the file; it runs reconcile+defense only.
        finally_block = self.SRC.rsplit("finally:", 1)[1]
        assert "_step_invariant_guard" not in finally_block
        assert "_step_exit_positions" not in finally_block

    def test_guard_touches_no_forbidden_module(self):
        """Hard constraint: the guard step must not import into
        guardian / evolver / reconciler write paths."""
        body = self.SRC.split("def _step_invariant_guard(ctx=None):")[1]
        body = body.split("def cmd_cron_scan")[0]
        for forbidden in ("protection_guardian", "strategy_evolver",
                          "portfolio_reconciler"):
            assert forbidden not in body, forbidden
