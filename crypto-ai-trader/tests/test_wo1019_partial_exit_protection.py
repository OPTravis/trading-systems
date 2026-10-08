"""WO-1019 (10/8): protections on the remainder after a partial exit.

Live case PENGUUSDT #91 (binance myTrades ground truth):
  09-30 20:41 UTC  BUY 614 @ 0.010237 (orderId 3585506576, two fills);
                   executor resting orders: TP/TP (tiered) + SL 185
                   (the 30% stop cover) @ 0.00952
  10-01 05:31 UTC  SL leg filled server-side: SELL 185 @ 0.00952
                   (-6.99%) — a partial exit; TP legs kept resting,
                   remainder 429.614 went naked on the downside
  10-01→10-03      exit triggers fired every 10-min event tick but the
                   pre-WO-1011 orchestrator skipped the exit step on
                   NO_OPPORTUNITIES rounds; the guardian's clamped OCO
                   floor chased the price down instead of arresting it
  10-03 02:39 UTC  SELL 428 @ 0.008808 = -13.96% (design band is 5-7%),
                   54h held vs max_hold 48h

Fixed in WO-1011 (starved/no-opportunity rounds now run the exit step;
evaluate_one enforces the portfolio.stop_loss trailing floor) — this
suite proves the two acceptance scenarios actually close end-to-end on
today's code, replays the #91 timeline, and pins the reconciler's new
commission-residual gate (the "PENGU gap 0.614" Path C loop: the BUY
fee settles in the base asset, the ledger books BUYs gross, so a fully
booked lifecycle keeps a permanent commission-sized net residue that
re-flagged the symbol every round with nothing left to book).
"""
import logging
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import os
import tempfile

from src.state_db import get_state_db
from src.exit_check import evaluate_exits, execute_exit
from src.ledger import record_fill
from src.portfolio_reconciler import (
    reconcile_exchange_fills,
    _buy_base_commission,
)

# live PENGU filters (verified 10/3): the remainder is NOT dust
PENGU_FILTERS = {"stepSize": 1.0, "tickSize": 1e-06, "minQty": 1,
                 "minNotional": 1.0}

ENTRY = 0.010237
TRAILING_FLOOR = 0.009725   # guardian had trailed to -5% (live value)
SL_FILL_QTY = 185.0         # the 30% stop cover, filled 10-01 05:31
SL_FILL_PX = 0.00952        # -6.99%


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


class PenguClient:
    """Fake exchange carrying the #91 state after the SL leg filled:
    TP legs (406) still resting, free 23.614, price configurable."""

    def __init__(self, *, price):
        self.price = price
        self.orders = [
            {"symbol": "PENGUUSDT", "side": "SELL", "status": "NEW",
             "orderId": 3585506710, "type": "LIMIT_MAKER"},
            {"symbol": "PENGUUSDT", "side": "SELL", "status": "NEW",
             "orderId": 3585506711, "type": "LIMIT_MAKER"},
        ]
        self.sold = None
        self.buy_fee = 0.614    # PENGU-denominated BUY fee (live value)

    # -- exit-chain surface --
    def get_ticker_price(self, symbol):
        return self.price

    def get_klines(self, symbol, interval="1h", limit=40):
        return None

    def get_open_orders(self, symbol=None):
        return self.orders

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
        return dict(PENGU_FILTERS)

    def get_free_balance(self, asset="USDT"):
        return 10_000.0

    # -- reconciler surface --
    def get_account(self):
        return {"balances": [
            {"asset": "PENGU", "free": "23.614", "locked": "406"},
            {"asset": "USDT", "free": "100", "locked": "0"},
        ]}

    def get_my_trades(self, symbol, limit=100, from_id=None):
        t0 = self.t_buy
        def f(fid, oid, qty, price, ts, buyer):
            return {"id": fid, "price": str(price), "qty": str(qty),
                    "quoteQty": str(qty * price),
                    "commission": str(self.buy_fee / 2.0) if buyer else "0",
                    "commissionAsset": "PENGU",
                    "time": int(ts * 1000), "isBuyer": buyer,
                    "isMaker": False, "orderId": oid, "symbol": symbol}
        rows = [f(1, 3585506576, 147, ENTRY, t0, True),
                f(2, 3585506576, 467, ENTRY, t0 + 2, True),
                f(3, 3585506719, SL_FILL_QTY, SL_FILL_PX, self.t_sl, False)]
        if getattr(self, "final_sell_ts", None):
            rows.append(f(4, 3590056416, 428, 0.008808,
                          self.final_sell_ts, False))
        return rows


@pytest.fixture
def db():
    d = tempfile.mkdtemp()
    os.environ["STATE_DB_PATH"] = os.path.join(d, "wo1019.db")
    os.environ["TESTING"] = "1"
    db = get_state_db()
    yield db
    # isolation: force a fresh singleton for the next test
    os.environ["STATE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "n.db")


def _seed_position(db, *, t_buy, t_sl, stop_loss):
    """BUY 614 @0.010237 booked gross; portfolio row as the executor
    and guardian left it (trailing floor optional)."""
    db.kv_set("ledger:shadow:bootstrap_ts", time.time() - 86400)
    db.trade_add("PENGUUSDT", "BUY", 614, ENTRY)
    conn = db._get_conn()
    conn.execute(
        "UPDATE trades SET timestamp = ? "
        "WHERE symbol = 'PENGUUSDT' AND side = 'BUY'",
        (t_buy,))
    conn.commit()
    st = record_fill(
        {"type": "BUY", "symbol": "PENGUUSDT", "qty": 614, "price": ENTRY,
         "ts": t_buy, "order_id": "3585506576", "source": "wo1019.seed"},
        observe_only=True, db=db)
    assert st["status"] == "ok", st
    row = {"quantity": 614, "entry_price": ENTRY,
           "strategy": "technical_v2", "opened_at": _iso(t_buy)}
    if stop_loss is not None:
        row["stop_loss"] = stop_loss
    db.portfolio_set("PENGUUSDT", row)


def _reconcile_sl_fill(cli, db):
    """The reconciler sees the server-side SL fill and books it."""
    entries = reconcile_exchange_fills(cli, db, log=logging.getLogger("t"))
    pos = db.portfolio_get("PENGUUSDT")
    # the remainder is trimmed to the exchange truth, floor column kept
    assert pos is not None
    assert abs(float(pos["quantity"]) - 429.614) < 1.0
    return entries, pos


# ── acceptance A: remainder exits inside the SL design band ────────────

class TestPartialExitStopLossBand:
    def test_trailing_floor_enforced_on_remainder(self, db):
        """#91 shape: floor 0.009725, price -5.58% — above the fixed -6%
        line, below the trailed floor. Pre-WO-1011 this sat unenforced."""
        now = time.time()
        cli = PenguClient(price=0.009666)
        cli.t_buy, cli.t_sl = now - 9 * 3600 - 50 * 60, now - 50 * 60
        _seed_position(db, t_buy=cli.t_buy, t_sl=cli.t_sl,
                       stop_loss=TRAILING_FLOOR)
        _, pos = _reconcile_sl_fill(cli, db)
        assert float(pos.get("stop_loss") or 0) == TRAILING_FLOOR
        decs = evaluate_exits(cli, db, now=now, include_momentum=False)
        hit = [d for d in decs if d["symbol"] == "PENGUUSDT"]
        assert hit and hit[0]["kind"] == "stop_loss"
        assert "trailing stop breach" in hit[0]["reason"]
        out = execute_exit(cli, db, hit[0], now=now)
        assert out.get("status") == "ok", out
        assert cli.sold >= 428          # the WHOLE remainder, not 185
        assert getattr(cli, "emergency", None) is None

    def test_fixed_band_enforced_without_floor_column(self, db):
        """Legacy rows carry no stop_loss column value: the fixed design
        band must still fire at -6.5% (live outcome was -13.96%)."""
        now = time.time()
        cli = PenguClient(price=0.00957)          # -6.51%
        cli.t_buy, cli.t_sl = now - 9 * 3600 - 50 * 60, now - 50 * 60
        _seed_position(db, t_buy=cli.t_buy, t_sl=cli.t_sl, stop_loss=None)
        _reconcile_sl_fill(cli, db)
        decs = evaluate_exits(cli, db, now=cli.t_buy + 9 * 3600,
                              include_momentum=False)
        hit = [d for d in decs if d["symbol"] == "PENGUUSDT"]
        assert hit and hit[0]["kind"] == "stop_loss"
        assert -7.5 <= hit[0]["pnl_pct"] <= -6.0  # inside the design band
        out = execute_exit(cli, db, hit[0], now=cli.t_buy + 9 * 3600)
        assert out.get("status") == "ok", out
        assert cli.sold >= 428


# ── acceptance B: max_hold 48h enforced on the remainder ───────────────

class TestPartialExitMaxHold:
    def test_hold_expiry_fires_at_48h(self, db):
        """Price benign (-3%, above floor and fixed band), 49h+ held:
        the remainder must still leave via hold_expiry (live case sat
        54h because the exit step never ran)."""
        now = time.time()
        cli = PenguClient(price=0.00993)          # -3.0%
        cli.t_buy = now - 49 * 3600 - 50 * 60
        cli.t_sl = now - 50 * 60
        _seed_position(db, t_buy=cli.t_buy, t_sl=cli.t_sl,
                       stop_loss=TRAILING_FLOOR)
        _reconcile_sl_fill(cli, db)
        decs = evaluate_exits(cli, db, now=now, include_momentum=False)
        hit = [d for d in decs if d["symbol"] == "PENGUUSDT"]
        assert hit and hit[0]["kind"] == "hold_expiry"
        assert hit[0]["held_hours"] > 48.0
        out = execute_exit(cli, db, hit[0], now=now)
        assert out.get("status") == "ok", out
        assert cli.sold >= 428


# ── #91 timeline replay: one continuous chain ──────────────────────────

class TestPengu91Replay:
    def test_full_timeline_closes_in_design_band(self, db):
        """BUY → resting TP+SL cover → server-side SL fill → reconciler
        books + trims → same round the exit step liquidates the
        remainder at the trailing floor instead of the -14% chase."""
        now = time.time()
        cli = PenguClient(price=0.009666)
        cli.t_buy, cli.t_sl = now - 9 * 3600 - 50 * 60, now - 50 * 60
        _seed_position(db, t_buy=cli.t_buy, t_sl=cli.t_sl,
                       stop_loss=TRAILING_FLOOR)
        entries, pos = _reconcile_sl_fill(cli, db)
        # the partial fill itself is booked exactly once
        assert any(abs(e.get("qty", 0) - SL_FILL_QTY) < 1e-6
                   for e in entries)
        assert float(pos["stop_loss"]) == TRAILING_FLOOR
        # remainder liquidation in the SAME round
        decs = evaluate_exits(cli, db, now=now, include_momentum=False)
        assert any(d["symbol"] == "PENGUUSDT" and
                   d["kind"] == "stop_loss" and
                   "trailing stop breach" in d["reason"] for d in decs)
        hit = next(d for d in decs
                   if d["symbol"] == "PENGUUSDT")
        out = execute_exit(cli, db, hit, now=now)
        assert out.get("status") == "ok", out
        assert cli.sold >= 428
        assert 0.0095 <= hit["price"] <= TRAILING_FLOOR   # band, not -14%


# ── reconciler: BUY-side base-commission residual gate (Path C) ────────

class TestPathCBuyCommissionResidual:
    def test_helper_sums_base_asset_buy_fees(self):
        fills = [
            {"isBuyer": True, "commissionAsset": "PENGU",
             "commission": "0.307"},
            {"isBuyer": True, "commissionAsset": "PENGU",
             "commission": "0.307"},
            {"isBuyer": False, "commissionAsset": "PENGU",
             "commission": "185"},          # SELL fee must not count
            {"isBuyer": True, "commissionAsset": "BNB",
             "commission": "9"},            # non-base fee must not count
            {"isBuyer": True, "commissionAsset": "PENGU",
             "commission": "oops"},         # malformed counts as zero
        ]
        assert abs(_buy_base_commission(fills, "PENGU") - 0.614) < 1e-9
        assert _buy_base_commission(None, "PENGU") == 0.0
        assert _buy_base_commission(fills, "BTC") == 0.0

    def _gap_loop_state(self, db):
        """#91 aftermath: everything booked, exchange left with dust
        0.386, ledger net permanently 1 (614-185-428). Path C used to
        re-flag 'gap 0.614' every round for 24h."""
        now = time.time()
        t_buy = now - 5 * 3600
        db.kv_set("ledger:shadow:bootstrap_ts", now - 86400)
        for side, qty, px, oid in (
                ("BUY", 614, ENTRY, "3585506576"),
                ("SELL", 185, SL_FILL_PX, "3585506719"),
                ("SELL", 428, 0.008808, "3590056416")):
            db.trade_add("PENGUUSDT", side, qty, px)
            conn = db._get_conn()
            conn.execute(
                "UPDATE trades SET timestamp = ?, client_order_id = ? "
                "WHERE symbol = 'PENGUUSDT' AND side = ? AND qty = ?",
                (t_buy, oid, side, qty))
            conn.commit()
            st = record_fill(
                {"type": side, "symbol": "PENGUUSDT", "qty": qty,
                 "price": px, "ts": t_buy, "order_id": oid,
                 "source": "wo1019.seed"},
                observe_only=True, db=db)
            assert st["status"] == "ok", st

    def test_commission_residual_gap_not_rebooked(self, db):
        """gap 0.614 fully explained by the 0.614 PENGU BUY fee: Path C
        skips (balanced) instead of looping — and never books anything."""
        self._gap_loop_state(db)

        class DustClient(PenguClient):
            def __init__(self):
                super().__init__(price=0.009666)
                self.sold = None

            def get_account(self):
                return {"balances": [
                    {"asset": "PENGU", "free": "0.386", "locked": "0"},
                    {"asset": "USDT", "free": "100", "locked": "0"},
                ]}

            def get_my_trades(self, symbol, limit=100, from_id=None):
                t0 = self.t_buy
                def f(fid, oid, qty, price, ts, buyer):
                    return {"id": fid, "price": str(price), "qty": str(qty),
                            "quoteQty": str(qty * price),
                            "commission": str(self.buy_fee / 2.0) if buyer else "0",
                            "commissionAsset": "PENGU",
                            "time": int(ts * 1000), "isBuyer": buyer,
                            "isMaker": False, "orderId": oid, "symbol": symbol}
                return [f(1, 3585506576, 147, ENTRY, t0, True),
                        f(2, 3585506576, 467, ENTRY, t0 + 2, True),
                        f(3, 3585506719, 185, SL_FILL_PX, t0 + 60, False),
                        f(4, 3590056416, 428, 0.008808, t0 + 120, False)]

        cli = DustClient()
        cli.t_buy = time.time() - 5 * 3600
        entries = reconcile_exchange_fills(cli, db,
                                           log=logging.getLogger("t"))
        assert entries == []   # nothing left to book — and no loop

    def test_genuine_gap_still_books_despite_fees(self, db):
        """The gate must not swallow real unbooked exits: drop the final
        SELL from the ledger while the exchange shows it — Path C books
        it even though the BUY fee also exists in the fills."""
        self._gap_loop_state(db)
        conn = db._get_conn()
        conn.execute(
            "DELETE FROM trades WHERE symbol='PENGUUSDT' "
            "AND side='SELL' AND qty=428")
        conn.commit()
        from src.state_db import StateDB
        # ledger_events keeps its row (record_fill wrote it) — remove it
        # so the fill is genuinely unbooked on both axes
        conn.execute(
            "DELETE FROM ledger_events WHERE symbol='PENGUUSDT' "
            "AND type='SELL' AND qty=428")
        conn.commit()

        class GapClient(PenguClient):
            def __init__(self):
                super().__init__(price=0.009666)

            def get_account(self):
                return {"balances": [
                    {"asset": "PENGU", "free": "0.386", "locked": "0"},
                    {"asset": "USDT", "free": "100", "locked": "0"},
                ]}

        cli = GapClient()
        cli.t_buy = time.time() - 5 * 3600
        cli.t_sl = cli.t_buy + 60
        cli.final_sell_ts = cli.t_buy + 120
        entries = reconcile_exchange_fills(cli, db,
                                           log=logging.getLogger("t"))
        assert any(abs(e.get("qty", 0) - 428.0) < 1e-6 for e in entries), \
            "genuine 428 gap must still book despite 0.614 BUY fee"
