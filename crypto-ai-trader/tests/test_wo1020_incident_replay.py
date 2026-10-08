"""WO-1020 (10/8): permanent incident replays — the guard's point of view.

Three production incidents share one shape: a protective action executed
(or rested) OUTSIDE the 5-7% design band with no independent
re-computation. These tests pin that the invariant guard would have
caught each one at the moment the damage was still avoidable, and will
keep catching regressions in CI:

  DASH 9/24   BUY #99 0.512@64.22 (9/23 02:01) -> loss-side ladder
              #113 0.176@60.44 (-5.88%) / #117 0.168@58.18 (-9.40%) /
              #119 0.168@57.75 (-10.07%), all resting orders filled
              server-side, exit chain uninvolved. Weighted outcome
              ~-8.3% vs a 6% design line.
  PENGU #91   partial SL exit 10/1 05:31 left 429.614 with a trailed
              floor nobody enforced for 54h (max_hold 48h); settled
              -13.96% through the guardian-clamped OCO floor 10/3.
  WO-1006     guardian re-hung the SL verbatim from a stale db stop,
              -2010 price-filter reject loop; _legalize_sl_price now
              clamps to px*0.93 — legal to place, but still outside
              the band: the guard is the second net.
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
from src.portfolio_reconciler import reconcile_exchange_fills
from src.invariant_guard import run_guard, check_position

# ── DASH 9/24 ground truth (production trades, 9/23-9/24) ───────────────
DASH_ENTRY = 64.22
DASH_QTY = 0.512
DASH_FILTERS = {"stepSize": 0.001, "tickSize": 0.01, "minQty": 0.001,
                "minNotional": 5.0}

# ── PENGU #91 ground truth (WO-1019 suite, same values) ─────────────────
P_ENTRY = 0.010237
P_FLOOR = 0.009725
P_SL_QTY, P_SL_PX = 185.0, 0.00952
P_FILTERS = {"stepSize": 1.0, "tickSize": 1e-06, "minQty": 1,
             "minNotional": 1.0}


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


class IncidentClient:
    """Fake exchange for one symbol; enough surface for the guard, the
    exit chain and (PENGU) the reconciler."""

    def __init__(self, *, sym, price, orders=None, free="0", locked="0",
                 trades=None):
        self.sym, self.price = sym, price
        self.orders = list(orders or [])
        self.free, self.locked = free, locked
        self._trades = trades or []
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
        return dict(P_FILTERS) if symbol.startswith("PENGU") \
            else dict(DASH_FILTERS)

    def get_free_balance(self, asset="USDT"):
        # post-cancel pool for the base asset (legs are cancelled first)
        base = self.sym[:-4]
        if asset == base:
            return float(self.free) + float(self.locked)
        return 10_000.0

    def get_account(self):
        base = self.sym[:-4]
        return {"balances": [
            {"asset": base, "free": self.free, "locked": self.locked},
            {"asset": "USDT", "free": "100", "locked": "0"},
        ]}

    def get_my_trades(self, symbol, limit=100, from_id=None):
        return list(self._trades)


@pytest.fixture
def db():
    d = tempfile.mkdtemp()
    os.environ["STATE_DB_PATH"] = os.path.join(d, "wo1020r.db")
    os.environ["TESTING"] = "1"
    db = get_state_db()
    yield db
    os.environ["STATE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "n.db")


def _seed_buy(db, sym, qty, entry, *, held_h, stop_loss=None):
    """BUY anchor + portfolio row as executor/guardian left it."""
    now = time.time()
    t_buy = now - held_h * 3600
    db.kv_set("ledger:shadow:bootstrap_ts", now - 86400)
    db.trade_add(sym, "BUY", qty, entry)
    conn = db._get_conn()
    conn.execute(
        f"UPDATE trades SET timestamp = ? WHERE symbol = ? AND side = 'BUY'",
        (t_buy, sym))
    conn.commit()
    st = record_fill(
        {"type": "BUY", "symbol": sym, "qty": qty, "price": entry,
         "ts": t_buy, "order_id": "99", "source": "wo1020.replay"},
        observe_only=True, db=db)
    assert st["status"] == "ok", st
    row = {"quantity": qty, "entry_price": entry,
           "strategy": "technical_v2", "opened_at": _iso(t_buy)}
    if stop_loss is not None:
        row["stop_loss"] = stop_loss
    db.portfolio_set(sym, row)
    return now


def _outbox_ids(db):
    return [r["notif_id"] for r in db.notification_outbox_pending(limit=100)]


# ── a) DASH 9/24 — loss-side ladder outside the band ────────────────────

class TestDash924Replay:
    LADDER = [  # (orderId, qty, px, pct) — live resting orders
        (2907028424, 0.176, 60.44, -5.88),   # filled 9/24 (in/near band)
        (2909857409, 0.168, 58.18, -9.40),   # filled 9/24 (OFF band)
        (2909341038, 0.168, 57.75, -10.07),  # filled 9/24 (OFF band)
    ]

    def _ladder_orders(self):
        return [{"symbol": "DASHUSDT", "side": "SELL", "status": "NEW",
                 "orderId": oid, "type": "LIMIT_MAKER",
                 "price": str(px), "origQty": str(qty)}
                for oid, qty, px, _ in self.LADDER]

    def test_guard_closes_at_in_band_price_not_minus_9(self, db):
        """The moment the full ladder is resting with price still in
        band (62.0 = -3.30%), the guard sees the worst leg 57.75 below
        floor_fixed*0.97 = 58.556 and closes NOW. Live outcome: waited
        for the ladder, weighted ~-8.3% across three fills."""
        now = _seed_buy(db, "DASHUSDT", DASH_QTY, DASH_ENTRY, held_h=22.0)
        cli = IncidentClient(
            sym="DASHUSDT", price=62.0, orders=self._ladder_orders(),
            free="0", locked="0.512")
        v = check_position(
            cli, db, db.portfolio_get("DASHUSDT"),
            now=now, open_orders=cli.get_open_orders())
        assert v and v["kind"] == "sl_order_off_band"
        s = run_guard(cli, db, now=now)
        assert s["closed"] and s["closed"][0]["status"] == "ok"
        assert cli.sold == pytest.approx(0.512, abs=0.002)
        assert cli.orders == []            # ladder cancelled first
        # the close price is the in-band price, not the ladder tail
        assert v["pnl_pct"] == pytest.approx(-3.46, abs=0.1)
        assert any(i.startswith("invariant:DASHUSDT:sl_order_off_band:")
                   for i in _outbox_ids(db))

    def test_in_band_leg_alone_does_not_fire(self, db):
        """DASH #113 alone (60.44 vs floor*0.97 = 58.556) sits inside
        the tolerance — the guard must not scream at a first-tier stop."""
        now = _seed_buy(db, "DASHUSDT", DASH_QTY, DASH_ENTRY, held_h=22.0)
        cli = IncidentClient(
            sym="DASHUSDT", price=62.0,
            orders=[{"symbol": "DASHUSDT", "side": "SELL", "status": "NEW",
                     "orderId": 2907028424, "type": "STOP_LOSS_LIMIT",
                     "price": "60.44", "stopPrice": "60.44"}],
            free="0", locked="0.512")
        v = check_position(
            cli, db, db.portfolio_get("DASHUSDT"),
            now=now, open_orders=cli.get_open_orders())
        assert v is None


# ── b) PENGU #91 — 54h sit after a partial exit ─────────────────────────

class TestPengu91Replay:
    def _client(self, *, price, t_buy, t_sl):
        def f(fid, oid, qty, px, ts, buyer, comm, casset):
            return {"id": fid, "price": str(px), "qty": str(qty),
                    "quoteQty": str(qty * px), "commission": str(comm),
                    "commissionAsset": casset, "time": int(ts * 1000),
                    "isBuyer": buyer, "isMaker": False,
                    "orderId": oid, "symbol": "PENGUUSDT"}
        # live myTrades: two BUY fills (base-asset fee) + the SL leg
        trades = [
            f(1, 3585506576, 147, P_ENTRY, t_buy, True, 0.307, "PENGU"),
            f(2, 3585506576, 467, P_ENTRY, t_buy + 2, True, 0.307, "PENGU"),
            f(3, 3585506719, P_SL_QTY, P_SL_PX, t_sl, False, 0, "USDT"),
        ]
        orders = [  # the two TP legs still resting after the SL fill
            {"symbol": "PENGUUSDT", "side": "SELL", "status": "NEW",
             "orderId": 3585506710, "type": "LIMIT_MAKER"},
            {"symbol": "PENGUUSDT", "side": "SELL", "status": "NEW",
             "orderId": 3585506711, "type": "LIMIT_MAKER"},
        ]
        return IncidentClient(
            sym="PENGUUSDT", price=price, orders=orders,
            free="23.614", locked="406", trades=trades)

    def test_guard_closes_next_round_not_54h_later(self, db):
        """Full #91 timeline: BUY 614 -> SL leg fills server-side 05:31
        -> reconciler books it and trims the remainder (floor kept) ->
        price under the trailed floor. Live: 54h of starved exit steps,
        settle at -13.96%. Guard: breach + close in the SAME round."""
        now = time.time()
        t_buy, t_sl = now - 9 * 3600 - 50 * 60, now - 50 * 60
        _seed_buy(db, "PENGUUSDT", 614, P_ENTRY, held_h=9.83,
                  stop_loss=P_FLOOR)
        cli = self._client(price=0.009666, t_buy=t_buy, t_sl=t_sl)
        # the round's normal order: reconcile first (fresh books) ...
        entries = reconcile_exchange_fills(cli, db, log=logging.getLogger("t"))
        assert entries, "SL fill must book"
        pos = db.portfolio_get("PENGUUSDT")
        assert abs(float(pos["quantity"]) - 429.614) < 1.0
        assert float(pos.get("stop_loss") or 0) == P_FLOOR
        # ... then the guard re-computes on those books
        s = run_guard(cli, db, now=now)
        kinds = [b["kind"] for b in s["breaches"]]
        assert "sl_band_breach" in kinds
        assert s["closed"][0]["status"] == "ok"
        assert cli.sold >= 428                     # the whole remainder
        b = [x for x in s["breaches"] if x["kind"] == "sl_band_breach"][0]
        assert b["pnl_pct"] == pytest.approx(-5.58, abs=0.1)  # in band,
        # versus the live -13.96% settle 54h later
        assert any(i.startswith("invariant:PENGUUSDT:sl_band_breach:")
                   for i in _outbox_ids(db))

    def test_guard_works_even_when_exit_step_is_starved(self, db):
        """The fail-safe claim: the guard does not depend on the exit
        step running. Same breach, but this replay never calls
        run_exit_step at all (the pre-WO-1011 orchestrator skipped it
        on every NO_OPPORTUNITIES round) — the guard still closes."""
        now = time.time()
        t_buy = now - 49 * 3600        # ALSO past max_hold 48h (live 54h)
        _seed_buy(db, "PENGUUSDT", 614, P_ENTRY, held_h=49.0,
                  stop_loss=P_FLOOR)
        cli = self._client(price=0.009666, t_buy=t_buy,
                           t_sl=now - 3600)
        s = run_guard(cli, db, now=now)   # the ONLY protection layer run
        kinds = [b["kind"] for b in s["breaches"]]
        assert kinds == ["max_hold"]     # max_hold checked first (worst)
        assert s["closed"][0]["status"] == "ok" and cli.sold >= 428


# ── c) WO-1006 — clamped re-list outside the band ───────────────────────

class TestWo1006ClampView:
    def test_price_through_floor_closes_even_before_clamp_fills(self, db):
        """WO-1006 shape: db stop 0.009725 above market 0.009687. The
        guardian's legal clamp (px*0.93 = 0.009008, -12%) would only
        fill at -12%; the guard sees price under the trailed floor NOW
        and closes at -5.4%."""
        now = _seed_buy(db, "PENGUUSDT", 429, P_ENTRY, held_h=9.0,
                        stop_loss=P_FLOOR)
        cli = IncidentClient(
            sym="PENGUUSDT", price=0.009687,
            orders=[{"symbol": "PENGUUSDT", "side": "SELL",
                     "status": "NEW", "orderId": 77,
                     "type": "STOP_LOSS_LIMIT",
                     "price": "0.009008", "stopPrice": "0.009008"}],
            free="0", locked="429")
        s = run_guard(cli, db, now=now)
        assert [b["kind"] for b in s["breaches"]] == ["sl_band_breach"]
        assert s["closed"][0]["status"] == "ok" and cli.sold == 429
        b = s["breaches"][0]
        assert b["pnl_pct"] == pytest.approx(-5.38, abs=0.1)

    def test_price_back_in_band_clamped_leg_still_reported(self, db):
        """Bounce variant: price back above the floor (0.00975), the
        clamped leg 0.009008 still resting — waiting for it = -12% by
        construction. Off-band leg -> close at the in-band price."""
        now = _seed_buy(db, "PENGUUSDT", 429, P_ENTRY, held_h=9.0,
                        stop_loss=P_FLOOR)
        cli = IncidentClient(
            sym="PENGUUSDT", price=0.00975,
            orders=[{"symbol": "PENGUUSDT", "side": "SELL",
                     "status": "NEW", "orderId": 77,
                     "type": "STOP_LOSS_LIMIT",
                     "price": "0.009008", "stopPrice": "0.009008"}],
            free="0", locked="429")
        s = run_guard(cli, db, now=now)
        assert [b["kind"] for b in s["breaches"]] == ["sl_order_off_band"]
        assert cli.sold == 429
        b = s["breaches"][0]
        assert b["pnl_pct"] == pytest.approx(-4.76, abs=0.1)
