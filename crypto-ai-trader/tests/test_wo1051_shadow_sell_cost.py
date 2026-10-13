"""WO-1051: shadow SELL must shrink cost_basis by the sold proportion.

Pre-fix bug (Travis 10/13 evidence, TIAUSDT): a partial SELL cut net_qty
but left the full cost_basis on the row; a later re-BUY then merged into
that stale cost and produced an inflated avg_entry_price
(shadow 1.2404 vs true 0.6214, 2x) — which pnl_live_monitor read as a
phantom -49.9% floating loss and a false NEAR_SL alert.

Anchors:
  1. partial SELL shrinks cost_basis proportionally; per-unit avg is
     unchanged (proportional cut is per-unit-neutral).
  2. dust remnant + re-BUY (the TIAUSDT pattern) yields the true avg.
  3. full close still deletes the row; oversell deletes as before.
  4. SELL with no shadow row remains a no-op (legacy divergence path).
  5. invariant cost_basis == net_qty * avg_entry_price holds after
     every partial SELL and after every BUY merge.
  6. cash_delta accounting on SELL is unchanged (proceeds added).
"""

import pytest

from src.ledger import bootstrap_shadow, record_fill
from src.state_db import get_state_db


def _db():
    return get_state_db()


def _row(db, sym):
    r = db._get_conn().execute(
        "SELECT * FROM ledger_shadow_positions WHERE symbol = ?", (sym,)
    ).fetchone()
    return dict(r) if r else None


def _buy(db, sym, qty, price, oid=None):
    return record_fill(
        {"type": "BUY", "symbol": sym, "qty": qty, "price": price,
         "order_id": oid, "deduct_cash": True},
        observe_only=True, db=db,
    )


def _sell(db, sym, qty, price, oid=None):
    return record_fill(
        {"type": "SELL", "symbol": sym, "qty": qty, "price": price,
         "order_id": oid},
        observe_only=True, db=db,
    )


class TestPartialSellCostShrink:
    def test_half_sell_halves_cost_and_keeps_avg(self):
        db = _db()
        bootstrap_shadow(db)
        _buy(db, "LTCUSDT", 10.0, 2.0, oid="b1")     # qty 10, cost 20, avg 2
        _sell(db, "LTCUSDT", 5.0, 1.5, oid="s1")     # qty 5
        r = _row(db, "LTCUSDT")
        assert abs(r["net_qty"] - 5.0) < 1e-9
        assert abs(r["cost_basis"] - 10.0) < 1e-9
        assert abs(r["avg_entry_price"] - 2.0) < 1e-9

    def test_cost_qty_avg_invariant_after_partial_sells(self):
        db = _db()
        bootstrap_shadow(db)
        _buy(db, "AAVEUSDT", 12.7, 0.4711, oid="b1")
        _sell(db, "AAVEUSDT", 12.6, 0.4899, oid="s1")   # dust remnant 0.1
        r = _row(db, "AAVEUSDT")
        assert abs(r["net_qty"] - 0.1) < 1e-9
        assert abs(r["cost_basis"] - r["net_qty"] * r["avg_entry_price"]) < 1e-9
        _sell(db, "AAVEUSDT", 0.05, 0.50, oid="s2")
        r = _row(db, "AAVEUSDT")
        assert abs(r["net_qty"] - 0.05) < 1e-9
        assert abs(r["cost_basis"] - r["net_qty"] * r["avg_entry_price"]) < 1e-9

    def test_avg_unchanged_by_proportional_cut(self):
        # per-unit cost never changes when selling units
        db = _db()
        bootstrap_shadow(db)
        _buy(db, "DOTUSDT", 3.0, 1.2, oid="b1")
        _sell(db, "DOTUSDT", 2.999, 1.1, oid="s1")
        r = _row(db, "DOTUSDT")
        assert abs(r["avg_entry_price"] - 1.2) < 1e-9
        assert abs(r["cost_basis"] - 0.001 * 1.2) < 1e-9


class TestDustReopenTrueAvg:
    def test_tiausdt_reproduction(self):
        # exact production sequence (WO-1051 evidence):
        # 10/09 BUY 11.95803@0.5007 -> SELL 11.95@0.4736 (dust 0.00803)
        # -> 10/13 BUY 9.65837@0.6215 => true avg ~0.6214, cost ~6.0067
        db = _db()
        bootstrap_shadow(db)
        _buy(db, "TIAUSDT", 11.95803, 0.5007, oid="b1")
        _sell(db, "TIAUSDT", 11.95, 0.4736, oid="s1")
        r = _row(db, "TIAUSDT")
        assert abs(r["net_qty"] - 0.00803) < 1e-9
        # pre-fix this row kept the full 5.987 cost
        assert r["cost_basis"] < 0.01
        _buy(db, "TIAUSDT", 9.65837, 0.6215, oid="b2")
        r = _row(db, "TIAUSDT")
        assert abs(r["net_qty"] - 9.6664) < 1e-6
        assert abs(r["avg_entry_price"] - 0.6214) < 2e-4
        assert abs(r["cost_basis"] - 6.0067) < 1e-3

    def test_reopen_after_full_close_still_clean(self):
        # full close deletes the row; a fresh BUY starts from scratch
        db = _db()
        bootstrap_shadow(db)
        _buy(db, "PYTHUSDT", 71.4285, 0.0842, oid="b1")
        _sell(db, "PYTHUSDT", 71.4285, 0.0798, oid="s1")   # full close
        assert _row(db, "PYTHUSDT") is None
        _buy(db, "PYTHUSDT", 70.0584, 0.0854, oid="b2")
        r = _row(db, "PYTHUSDT")
        assert abs(r["avg_entry_price"] - 0.0854) < 1e-9
        assert abs(r["cost_basis"] - 70.0584 * 0.0854) < 1e-9


class TestSellEdgeBehaviour:
    def test_full_close_deletes_row(self):
        db = _db()
        bootstrap_shadow(db)
        _buy(db, "WLDUSDT", 10.0, 0.5643, oid="b1")
        _sell(db, "WLDUSDT", 10.0, 0.5405, oid="s1")
        assert _row(db, "WLDUSDT") is None

    def test_oversell_deletes_row(self):
        # oversell (legacy drift) keeps the delete semantics unchanged
        db = _db()
        bootstrap_shadow(db)
        _buy(db, "NEARUSDT", 1.0, 5.478, oid="b1")
        _sell(db, "NEARUSDT", 1.5, 5.117, oid="s1")
        assert _row(db, "NEARUSDT") is None

    def test_sell_without_row_is_noop(self):
        db = _db()
        bootstrap_shadow(db)
        res = _sell(db, "OGNUSDT", 1.0, 0.034, oid="s1")
        assert res["status"] == "ok"
        assert _row(db, "OGNUSDT") is None

    def test_sell_event_still_recorded_on_noop(self):
        # the event ledger must keep the SELL even when the book ignores it
        db = _db()
        bootstrap_shadow(db)
        _sell(db, "STRKUSDT", 1.0, 0.06, oid="s1")
        ev = db._get_conn().execute(
            "SELECT COUNT(*) c FROM ledger_events WHERE symbol='STRKUSDT'"
        ).fetchone()
        assert ev["c"] == 1


class TestCashDeltaUnchanged:
    def test_proceeds_still_added(self):
        db = _db()
        bootstrap_shadow(db)
        _buy(db, "QNTUSDT", 1.0, 271.47, oid="b1")
        _sell(db, "QNTUSDT", 0.5, 280.0, oid="s1")
        r = _row(db, "QNTUSDT")
        # cash_delta = -271.47 (buy) + 140.0 (sell proceeds)
        assert abs(r["cash_delta"] - (-271.47 + 140.0)) < 1e-9

    def test_dust_sell_then_merge_cash_chain(self):
        db = _db()
        bootstrap_shadow(db)
        _buy(db, "MEGAUSDT", 100.0, 0.05142, oid="b1")
        _sell(db, "MEGAUSDT", 99.9, 0.055, oid="s1")
        _buy(db, "MEGAUSDT", 50.0, 0.06, oid="b2")
        r = _row(db, "MEGAUSDT")
        expected = -100 * 0.05142 + 99.9 * 0.055 - 50 * 0.06
        assert abs(r["cash_delta"] - expected) < 1e-9


class TestMergeInvariants:
    def test_invariant_holds_across_churn(self):
        # randomized-ish churn: invariant must hold at every step
        db = _db()
        bootstrap_shadow(db)
        steps = [
            ("BUY", 12.7, 0.4711), ("SELL", 12.6, 0.4899),
            ("BUY", 24.6, 0.518), ("SELL", 24.6, 0.5387),
            ("BUY", 10.9, 0.5643), ("SELL", 10.8, 0.5405),
        ]
        for i, (t, q, px) in enumerate(steps):
            if t == "BUY":
                _buy(db, "WLDUSDT", q, px, oid=f"b{i}")
            else:
                _sell(db, "WLDUSDT", q, px, oid=f"s{i}")
            r = _row(db, "WLDUSDT")
            if r is not None:
                assert abs(
                    r["cost_basis"] - r["net_qty"] * r["avg_entry_price"]
                ) < 1e-9, f"invariant broke at step {i}"
        # continuous replay ends at dust 0.2 @ true avg ~0.5639
        # (production's live row shows 0.1 @ 0.5643 only because the
        # pre-10/02 row was deleted and re-INSERTED — anchored replay
        # confirms both; see scripts/repair_shadow_cost_basis.py)
        r = _row(db, "WLDUSDT")
        assert abs(r["net_qty"] - 0.2) < 1e-9
        assert abs(r["avg_entry_price"] - 0.5639) < 1e-4
        assert abs(r["cost_basis"] - 0.1128) < 1e-3
