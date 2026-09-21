"""OCO passive-fill reconciler tests (2026-09-17 bridge blind-spot fix).

Covers the two real incidents: 9/16 ARB TP (67.8 @ 0.1611, silent for 10+
rounds) and the 9/15 night SL closes. Uses a real StateDB on a tmp file so
the client_order_id UNIQUE index and INSERT OR IGNORE semantics are the
production ones.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.state_db import StateDB
from src.portfolio_reconciler import (
    reconcile_portfolio_drift, KV_PREV_POSITIONS, PREV_SNAPSHOT_MAX_AGE_S,
)


class FakeClient:
    def __init__(self, balances, trades_by_symbol):
        self._balances = balances          # [{asset, free, locked}]
        self._trades = trades_by_symbol    # {symbol: [SDK-format fills]}
        self.my_trades_calls = []

    def get_account(self):
        return {"balances": self._balances}

    def get_my_trades(self, symbol, limit=100, from_id=None):
        self.my_trades_calls.append(symbol)
        return self._trades.get(symbol, [])


def _bal(asset, total, locked=0.0):
    return {"asset": asset, "free": str(total - locked), "locked": str(locked)}


def _fill(symbol, oid, qty, price, ts, is_buyer=False, commission="0",
          commission_asset="USDT"):
    return {
        "id": oid * 10, "price": str(price), "qty": str(qty),
        "quoteQty": str(round(qty * price, 8)), "commission": commission,
        "commissionAsset": commission_asset, "time": int(ts * 1000),
        "isBuyer": is_buyer, "isMaker": False, "orderId": oid, "symbol": symbol,
    }


@pytest.fixture
def db(tmp_path):
    d = StateDB(db_path=str(tmp_path / "state.db"))
    yield d
    d.close()


# ---------- steady state ----------

def test_clean_round_zero_api_no_writes(db):
    db.trade_add("ARBUSDT", "BUY", 67.8, 0.15)
    db.portfolio_set("ARBUSDT", {"quantity": 67.8, "entry_price": 0.15})
    client = FakeClient([_bal("ARB", 67.8), _bal("USDT", 400)], {})
    booked = reconcile_portfolio_drift(client, db)
    assert booked == []
    assert client.my_trades_calls == []          # no drift → zero extra API
    snap = db.kv_get(KV_PREV_POSITIONS)
    assert snap and "ARBUSDT" in snap and snap["ARBUSDT"] == 67.8


# ---------- Path A: same-round drift (acceptance scenario) ----------

def test_drift_books_sell_with_correct_pnl_and_idempotent(db, caplog):
    # ARB incident: DB holds 67.8, exchange flat, OCO TP filled @ 0.1611
    db.trade_add("ARBUSDT", "BUY", 67.8, 0.15)
    db.portfolio_set("ARBUSDT", {"quantity": 67.8, "entry_price": 0.15,
                                 "strategy": "trend", "opened_at": time.time()})
    fills = [_fill("ARBUSDT", 555, 67.8, 0.1611, time.time() - 3600,
                   commission="0.1", commission_asset="ARB")]
    client = FakeClient([_bal("ARB", 0.0), _bal("USDT", 410)],
                        {"ARBUSDT": fills})
    with caplog.at_level("INFO", logger="src.portfolio_reconciler"):
        booked = reconcile_portfolio_drift(client, db)

    assert len(booked) == 1
    b = booked[0]
    assert b["symbol"] == "ARBUSDT"
    assert b["order_id"] == "555"
    assert b["qty"] == pytest.approx(67.8 - 0.1, abs=1e-9)   # base commission
    assert b["price"] == pytest.approx(0.1611, abs=1e-9)
    assert b["pnl"] == pytest.approx(round((67.8 - 0.1) * (0.1611 - 0.15), 6), abs=1e-6)
    assert b["source"] == "reconcile/oco_fill"

    # bridge-visible log line (reside_scan filter: SELL + @ + USDT)
    line = [r.message for r in caplog.records if "RECONCILE" in r.message][0]
    assert "SELL ARBUSDT @" in line and "oco_fill" in line

    # DB row landed with the orderId anchor
    row = db._get_conn().execute(
        "SELECT * FROM trades WHERE client_order_id = '555'").fetchone()
    assert row and row["symbol"] == "ARBUSDT" and row["side"] == "SELL"

    # stale position cleaned up
    assert db.portfolio_get("ARBUSDT") is None

    # second run: fully idempotent (UNIQUE on client_order_id)
    booked2 = reconcile_portfolio_drift(client, db)
    assert booked2 == []


def test_pnl_uses_db_buy_weighted_average(db):
    # ETHFI-style: two BUY lots → weighted entry, partial SELL booked on drift
    db.trade_add("ETHFIUSDT", "BUY", 33.4, 0.7078)
    db.trade_add("ETHFIUSDT", "BUY", 8.7, 0.6821)
    db.portfolio_set("ETHFIUSDT", {"quantity": 20.0, "entry_price": 0.7024})
    fills = [_fill("ETHFIUSDT", 777, 20.0, 0.7284, time.time() - 7200)]
    client = FakeClient([_bal("ETHFI", 0.0)], {"ETHFIUSDT": fills})
    booked = reconcile_portfolio_drift(client, db)
    assert len(booked) == 1
    wavg = (33.4 * 0.7078 + 8.7 * 0.6821) / 42.1
    assert booked[0]["pnl"] == pytest.approx(round(20.0 * (0.7284 - wavg), 6), abs=1e-6)


def test_partial_drift_trims_position_to_exchange_qty(db):
    db.trade_add("XUSDT", "BUY", 100, 1.0)
    db.portfolio_set("XUSDT", {"quantity": 100.0, "entry_price": 1.0})
    fills = [_fill("XUSDT", 888, 90, 1.2, time.time() - 600)]
    client = FakeClient([_bal("X", 10.0)], {"XUSDT": fills})
    booked = reconcile_portfolio_drift(client, db)
    assert len(booked) == 1 and booked[0]["qty"] == pytest.approx(90.0)
    pos = db.portfolio_get("XUSDT")
    assert pos is not None and pos["quantity"] == pytest.approx(10.0)


# ---------- Path B: cross-round disappearance (the ARB case) ----------

def test_cross_round_disappearance_books_after_sync_cleared(db):
    # last round the position existed (kv snapshot); this round sync's
    # clear-and-rebuild already dropped it — trades never recorded
    db.trade_add("ARBUSDT", "BUY", 67.8, 0.15)
    db.kv_set(KV_PREV_POSITIONS, {"ARBUSDT": 67.8, "_ts": time.time()})
    fills = [_fill("ARBUSDT", 556, 67.8, 0.1611, time.time() - 3600)]
    client = FakeClient([_bal("ARB", 0.0), _bal("USDT", 410)],
                        {"ARBUSDT": fills})
    booked = reconcile_portfolio_drift(client, db)
    assert len(booked) == 1
    assert booked[0]["order_id"] == "556"
    assert booked[0]["source"] == "reconcile/oco_fill"


def test_stale_snapshot_beyond_max_age_ignored(db):
    db.trade_add("OLDUSDT", "BUY", 10, 1.0)
    db.kv_set(KV_PREV_POSITIONS,
              {"OLDUSDT": 10.0, "_ts": time.time() - PREV_SNAPSHOT_MAX_AGE_S - 3600})
    client = FakeClient([_bal("OLD", 0.0)], {"OLDUSDT": [_fill("OLDUSDT", 9, 10, 1.1, 1)]})
    booked = reconcile_portfolio_drift(client, db)
    assert booked == []
    assert client.my_trades_calls == []


# ---------- gap guard ----------

def test_gap_guard_blocks_overbooking_when_ledger_already_balanced(db):
    # ledger already explains everything (BUY 67.8 − SELL 67.8 booked, old
    # NULL-id rows) → even an unbooked stray SELL in history must NOT be added
    db.trade_add("ARBUSDT", "BUY", 67.8, 0.15)
    db.trade_add("ARBUSDT", "SELL", 67.8, 0.1611, client_order_id=None)
    db.portfolio_set("ARBUSDT", {"quantity": 67.8, "entry_price": 0.15})
    stray = _fill("ARBUSDT", 999, 50.0, 0.2, time.time() - 100)
    client = FakeClient([_bal("ARB", 0.0)], {"ARBUSDT": [stray]})
    booked = reconcile_portfolio_drift(client, db)
    assert booked == []
    assert db._get_conn().execute(
        "SELECT COUNT(*) c FROM trades WHERE client_order_id='999'"
    ).fetchone()["c"] == 0


def test_gap_guard_limits_booking_to_missing_qty(db, caplog):
    # gap is 20 (DB net 20, exchange flat) but history holds a 50-qty SELL
    # order. P0-1.5 behavior change (ZAMA incident 9/21): a leg larger than
    # the gap beyond tolerance is a stale/foreign leg — it is SKIPPED, not
    # booked whole. The pre-P0-1.5 semantics (book order 1001 entirely,
    # 50 units against a 20-unit gap) is exactly what fabricated the ZAMA
    # 443-vs-64 record. Neither 50 nor 30 fits a 20-unit gap; both are
    # skipped with a warning and the gap stays visible for follow-up.
    db.trade_add("YUSDT", "BUY", 20, 1.0)
    db.portfolio_set("YUSDT", {"quantity": 20.0, "entry_price": 1.0})
    fills = [
        _fill("YUSDT", 1001, 50, 0.9, time.time() - 500),
        _fill("YUSDT", 1002, 30, 0.9, time.time() - 400),
    ]
    client = FakeClient([_bal("Y", 0.0)], {"YUSDT": fills})
    with caplog.at_level("WARNING"):
        booked = reconcile_portfolio_drift(client, db)
    assert booked == []
    assert db._get_conn().execute(
        "SELECT COUNT(*) c FROM trades WHERE symbol='YUSDT' AND side='SELL'"
    ).fetchone()["c"] == 0
    assert any("stale/foreign leg" in r.message for r in caplog.records)


# ---------- fail-open ----------

def test_api_failure_fail_open_no_state_touched(db):
    db.trade_add("ZUSDT", "BUY", 5, 1.0)
    db.portfolio_set("ZUSDT", {"quantity": 5.0, "entry_price": 1.0})

    class DeadClient:
        def get_account(self):
            return {}

        def get_my_trades(self, *a, **k):
            raise RuntimeError("network down")

    booked = reconcile_portfolio_drift(DeadClient(), db)
    assert booked == []
    assert db.portfolio_get("ZUSDT") is not None
    assert db.kv_get(KV_PREV_POSITIONS) is None      # snapshot not written


def test_multi_leg_order_booked_as_one_trade(db):
    # one OCO SELL order filling in two legs (18.7 + 1.3, the ETHFI pattern)
    db.trade_add("MUSDT", "BUY", 20, 1.0)
    db.portfolio_set("MUSDT", {"quantity": 20.0, "entry_price": 1.0})
    ts = time.time() - 300
    fills = [
        _fill("MUSDT", 2001, 18.7, 1.1, ts),
        _fill("MUSDT", 2001, 1.3, 1.104, ts + 1),
    ]
    client = FakeClient([_bal("M", 0.0)], {"MUSDT": fills})
    booked = reconcile_portfolio_drift(client, db)
    assert len(booked) == 1
    assert booked[0]["qty"] == pytest.approx(20.0)
    assert booked[0]["price"] == pytest.approx(
        (18.7 * 1.1 + 1.3 * 1.104) / 20.0, abs=1e-9)


# ---------- partial-ladder detection (main axis, 9/17 night incidents) -------

def _book_buy(db, sym, qty, px):
    db.trade_add(sym, "BUY", qty, px)


def test_uni_tp1_partial_close_after_sync_flattened_portfolio(db):
    """9/17 21:53 UNI TP1: sync already rebuilt the portfolio row to the
    post-fill balance — portfolio-vs-exchange shows NO drift. Only the
    ledger net (8.04 booked buys vs 6.03 live) exposes the unbooked 2.01."""
    _book_buy(db, "UNIUSDT", 8.04, 6.85)
    db.portfolio_set("UNIUSDT", {"quantity": 6.03, "entry_price": 6.85})  # synced
    fills = [_fill("UNIUSDT", 910, 2.01, 7.143, time.time() - 43200)]
    client = FakeClient([_bal("UNI", 6.03), _bal("USDT", 415)],
                        {"UNIUSDT": fills})
    booked = reconcile_portfolio_drift(client, db)
    assert len(booked) == 1
    assert booked[0]["order_id"] == "910"
    assert booked[0]["qty"] == pytest.approx(2.01)
    assert booked[0]["price"] == pytest.approx(7.143)
    assert booked[0]["pnl"] == pytest.approx(round(2.01 * (7.143 - 6.85), 6), abs=1e-6)
    assert booked[0]["source"] == "reconcile/oco_fill"


def test_uni_tp1_and_tp2_both_booked_in_one_round(db):
    """9/17 night full UNI case: TP1 2.01@7.143 (21:53) + TP2 2.01@7.533
    (00:50) — both unbooked, one reconciliation round books both."""
    _book_buy(db, "UNIUSDT", 8.04, 6.85)
    db.portfolio_set("UNIUSDT", {"quantity": 4.02, "entry_price": 6.85})  # synced
    fills = [
        _fill("UNIUSDT", 910, 2.01, 7.143, time.time() - 43200),
        _fill("UNIUSDT", 911, 2.01, 7.533, time.time() - 32400),
    ]
    client = FakeClient([_bal("UNI", 4.02)], {"UNIUSDT": fills})
    booked = reconcile_portfolio_drift(client, db)
    assert [b["order_id"] for b in booked] == ["910", "911"]
    total_gain = sum(b["pnl"] for b in booked)
    assert total_gain == pytest.approx(
        round(2.01 * (7.143 - 6.85) + 2.01 * (7.533 - 6.85), 6), abs=1e-6)


def test_near_tp2_after_tp1_booked_full_close_via_path_b(db):
    """9/17 night NEAR case: TP1 was booked earlier; TP2 6.1@2.967 never
    booked; position fully closed → sync dropped the row → Path B."""
    _book_buy(db, "NEARUSDT", 12.2, 2.88)
    db.trade_add("NEARUSDT", "SELL", 6.1, 2.92, client_order_id="880")
    db.kv_set(KV_PREV_POSITIONS, {"NEARUSDT": 6.1, "_ts": time.time()})
    fills = [_fill("NEARUSDT", 912, 6.1, 2.967, time.time() - 36000)]
    client = FakeClient([_bal("NEAR", 0.0)], {"NEARUSDT": fills})
    booked = reconcile_portfolio_drift(client, db)
    assert len(booked) == 1
    assert booked[0]["qty"] == pytest.approx(6.1)
    assert booked[0]["price"] == pytest.approx(2.967)
    assert booked[0]["pnl"] == pytest.approx(round(6.1 * (2.967 - 2.88), 6), abs=1e-6)


def test_negative_gap_diagnostic_only_no_action(db):
    """Exchange holds MORE than the ledger explains (unbooked BUY) — SELL
    booker must not act and must not burn API calls."""
    _book_buy(db, "BUSDT", 5.0, 1.0)
    db.portfolio_set("BUSDT", {"quantity": 5.0, "entry_price": 1.0})
    client = FakeClient([_bal("B", 8.0)], {"BUSDT": []})
    booked = reconcile_portfolio_drift(client, db)
    assert booked == []
    assert client.my_trades_calls == []


def test_dust_sized_gap_below_tolerance_ignored(db):
    """Fee-sized ledger slack (< 2% relative) must not trigger booking."""
    _book_buy(db, "DUSDT", 100.0, 1.0)
    db.portfolio_set("DUSDT", {"quantity": 99.5, "entry_price": 1.0})
    client = FakeClient([_bal("D", 99.5)], {"DUSDT": []})
    booked = reconcile_portfolio_drift(client, db)
    assert booked == []
    assert client.my_trades_calls == []


def test_next_round_after_partial_booking_is_clean(db):
    """After the ladder is booked, the following round sees a balanced
    ledger → zero API, zero writes."""
    _book_buy(db, "UNIUSDT", 8.04, 6.85)
    db.trade_add("UNIUSDT", "SELL", 2.01, 7.143, client_order_id="910")
    db.portfolio_set("UNIUSDT", {"quantity": 6.03, "entry_price": 6.85})
    client = FakeClient([_bal("UNI", 6.03)], {"UNIUSDT": []})
    booked = reconcile_portfolio_drift(client, db)
    assert booked == []
    assert client.my_trades_calls == []


# ---------- double-count bug (id=36/id=38): NULL-id fuzzy dedup ----------

def _book_sell(db, sym, qty, px, oid=None):
    db.trade_add(sym, "SELL", qty, px, 0.0, client_order_id=oid)


def test_null_id_row_blocks_rebook_of_same_sell(db):
    """Regression for the id=36/id=38 DOT double-count: the active path
    booked the sell without an orderId; reconcile must not re-book the
    same physical fill under its exchange orderId."""
    import time as _t
    _book_buy(db, "DOTUSDT", 19.63, 1.035)
    _book_sell(db, "DOTUSDT", 19.61037, 1.133, oid=None)   # id=36 equivalent
    # ledger net = 0.01963 > DRIFT_QTY_ABS → Path A suspect via portfolio row
    db.portfolio_set("DOTUSDT", {"quantity": 0.01963, "entry_price": 1.035})
    now = _t.time()
    client = FakeClient(
        [_bal("DOT", 0.0)],
        {"DOTUSDT": [_fill("DOTUSDT", 6202013123, 19.61, 1.133, now)]},
    )
    booked = reconcile_portfolio_drift(client, db)
    assert booked == []
    sells = db._get_conn().execute(
        "SELECT * FROM trades WHERE symbol='DOTUSDT' AND side='SELL'").fetchall()
    assert len(sells) == 1, "same physical sell must stay a single ledger row"


def test_order_id_row_blocks_rebook_after_switch_fix(db):
    """With the fix, switch close_position books with the exchange orderId;
    _order_booked must then short-circuit any reconcile re-book attempt."""
    import time as _t
    _book_buy(db, "DOTUSDT", 19.63, 1.035)
    _book_sell(db, "DOTUSDT", 19.61037, 1.133, oid="6202013123")  # fixed path
    db.portfolio_set("DOTUSDT", {"quantity": 0.01963, "entry_price": 1.035})
    now = _t.time()
    client = FakeClient(
        [_bal("DOT", 0.0)],
        {"DOTUSDT": [_fill("DOTUSDT", 6202013123, 19.61, 1.133, now)]},
    )
    assert reconcile_portfolio_drift(client, db) == []


def test_fuzzy_dedup_far_qty_still_books(db):
    """Fuzzy tolerances must not swallow genuinely unbooked sells: a fill
    whose qty differs >0.5% from the NULL-id row is real drift and books."""
    import time as _t
    _book_buy(db, "UNIUSDT", 8.04, 6.85)
    _book_sell(db, "UNIUSDT", 4.0, 6.85, oid=None)      # unrelated half-close
    db.portfolio_set("UNIUSDT", {"quantity": 4.02, "entry_price": 6.85})
    now = _t.time()
    client = FakeClient(
        [_bal("UNI", 0.0)],
        {"UNIUSDT": [_fill("UNIUSDT", 7001, 4.04, 6.85, now)]},  # 1% off → books
    )
    booked = reconcile_portfolio_drift(client, db)
    assert len(booked) == 1 and booked[0]["symbol"] == "UNIUSDT"


def test_close_position_signature_carries_client_order_id():
    """Guard the plumbing: close_position must accept and forward
    client_order_id (used by the switch path) so active-path bookings are
    de-duplicated by the UNIQUE index instead of fuzzy matching."""
    import inspect
    import src.portfolio as pf
    sig = inspect.signature(pf.PortfolioManager.close_position)
    assert "client_order_id" in sig.parameters
    src_text = inspect.getsource(pf.PortfolioManager.close_position)
    assert "client_order_id=client_order_id" in src_text


# ---------------------------------------------------------------------------
# P0-1.5 (2026-09-21): ZAMA incident — stale fills from a long-closed position
# must never book against the current gap, and no single leg may exceed it.
# ---------------------------------------------------------------------------
from src.portfolio_reconciler import FILL_LOOKBACK_S, LEG_GAP_TOLERANCE


class TestP015StaleLegGuards:
    def test_stale_leg_older_than_lookback_excluded(self, db, caplog):
        """April orderId 98217977 (SELL 443) must be filtered out by the
        time window before the cap guard is even consulted."""
        now = time.time()
        db.trade_add(
            "ZAMAUSDT", "BUY", 64.0, 0.09418, client_order_id=None,
        )
        db.portfolio_set("ZAMAUSDT", {"quantity": 64.0, "entry_price": 0.09418,
                                 "strategy": "trend", "opened_at": time.time()})
        stale = _fill("ZAMAUSDT", 98217977, 443.0, 0.03178, now - 150 * 86400)
        fresh = _fill("ZAMAUSDT", 233705997, 64.0, 0.087829, now - 300)
        c = FakeClient(
            [_bal("ZAMA", 0.0)],
            {"ZAMAUSDT": [stale, fresh]},
        )
        with caplog.at_level("WARNING"):
            booked = reconcile_portfolio_drift(c, db)
        assert len(booked) == 1
        rows = db._get_conn().execute(
            "SELECT side, qty, price, client_order_id FROM trades "
            "WHERE symbol='ZAMAUSDT' ORDER BY id"
        ).fetchall()
        last = tuple(rows[-1])
        assert last == ("SELL", 64.0, 0.087829, "233705997")
        assert all(r["qty"] != 443.0 for r in rows)

    def test_oversized_leg_skipped_younger_leg_books(self, db, caplog):
        """Both legs inside the window, but the first (by min fill time)
        aggregates to 443 vs a 64-unit gap: the cap guard must skip it and
        the following 64-unit leg must book. This reproduces the exact
        incident ordering, with only the window filter disabled."""
        now = time.time()
        db.trade_add(
            "ZAMAUSDT", "BUY", 64.0, 0.09418, client_order_id=None,
        )
        db.portfolio_set("ZAMAUSDT", {"quantity": 64.0, "entry_price": 0.09418,
                                 "strategy": "trend", "opened_at": time.time()})
        # both fills inside FILL_LOOKBACK_S so only the cap guard protects
        big = _fill("ZAMAUSDT", 98217977, 443.0, 0.03178, now - 5000)
        right = _fill("ZAMAUSDT", 233705997, 64.0, 0.087829, now - 300)
        c = FakeClient(
            [_bal("ZAMA", 0.0)],
            {"ZAMAUSDT": [big, right]},
        )
        with caplog.at_level("WARNING"):
            booked = reconcile_portfolio_drift(c, db)
        assert len(booked) == 1
        assert any("stale/foreign leg" in r.message for r in caplog.records)
        qty = db._get_conn().execute(
            "SELECT qty FROM trades WHERE symbol='ZAMAUSDT' "
            "AND side='SELL'"
        ).fetchone()[0]
        assert qty == 64.0

    def test_all_legs_stale_leaves_gap_unbooked(self, db, caplog):
        """Degenerate case: every SELL is historical — nothing may be
        booked, gap stays visible for human follow-up instead of being
        'fixed' with a foreign leg."""
        now = time.time()
        db.trade_add(
            "ZAMAUSDT", "BUY", 64.0, 0.09418, client_order_id=None,
        )
        db.portfolio_set("ZAMAUSDT", {"quantity": 64.0, "entry_price": 0.09418,
                                 "strategy": "trend", "opened_at": time.time()})
        old = _fill("ZAMAUSDT", 98217977, 443.0, 0.03178, now - 90 * 86400)
        c = FakeClient([_bal("ZAMA", 0.0)], {"ZAMAUSDT": [old]})
        with caplog.at_level("WARNING"):
            booked = reconcile_portfolio_drift(c, db)
        assert booked == []
        pos = db._get_conn().execute(
            "SELECT quantity FROM portfolio WHERE symbol='ZAMAUSDT'"
        ).fetchone()
        assert pos is None or pos[0] != 443.0

    def test_leg_within_tolerance_still_books(self, db, caplog):
        """Sanity: a normal leg sized inside gap x tolerance + abs drift
        (e.g. partial fill slightly above the gap due to dust rounding)
        must keep booking as before."""
        now = time.time()
        db.trade_add(
            "ZAMAUSDT", "BUY", 64.0, 0.09418, client_order_id=None,
        )
        db.portfolio_set("ZAMAUSDT", {"quantity": 64.0, "entry_price": 0.09418,
                                 "strategy": "trend", "opened_at": time.time()})
        # 64 x 1.03 is inside 1.05 tolerance
        leg = _fill("ZAMAUSDT", 233705997, 64.0 * 1.03, 0.087829, now - 300)
        c = FakeClient([_bal("ZAMA", 0.0)], {"ZAMAUSDT": [leg]})
        booked = reconcile_portfolio_drift(c, db)
        assert len(booked) == 1
        qty = db._get_conn().execute(
            "SELECT qty FROM trades WHERE symbol='ZAMAUSDT' "
            "AND side='SELL'"
        ).fetchone()[0]
        assert qty == pytest.approx(64.0 * 1.03)
