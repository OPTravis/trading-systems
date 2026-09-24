"""WO-0924 P2: Ledger single-transaction bookkeeping layer.

Acceptance anchors (Travis P2 order, 2026-09-24):
  1. PRIMARY atomicity: a mid-write failure rolls back ALL four sources.
  2. Shadow diff self-test: an injected inconsistency is caught.
  3. Invariant: shadow observe hooks leave the legacy write path untouched
     (bystander contract), never raise into the host path.
  4. Mode flag: off / shadow / primary — kill-switch rollback lever.
"""

import json
import time

import pytest

from src.ledger import (
    BOOTSTRAP_TS_KEY,
    LedgerError,
    bootstrap_shadow,
    get_mode,
    get_stats,
    record_fill,
    reset_shadow,
    set_mode,
    shadow_diff,
)
from src.state_db import get_state_db


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _db():
    return get_state_db()


def _events(db):
    rows = db._get_conn().execute(
        "SELECT * FROM ledger_events ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def _trades(db, symbol=None):
    if symbol:
        rows = db._get_conn().execute(
            "SELECT * FROM trades WHERE symbol = ? ORDER BY id", (symbol,)
        ).fetchall()
    else:
        rows = db._get_conn().execute("SELECT * FROM trades ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def _audit(db, action):
    rows = db._get_conn().execute(
        "SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,)
    ).fetchall()
    return [dict(r) for r in rows]


def _shadow_row(db, symbol):
    row = db._get_conn().execute(
        "SELECT * FROM ledger_shadow_positions WHERE symbol = ?", (symbol,)
    ).fetchone()
    return dict(row) if row else None


def _outcomes(db, symbol):
    rows = db._get_conn().execute(
        "SELECT * FROM trade_outcomes WHERE symbol = ? ORDER BY id", (symbol,)
    ).fetchall()
    return [dict(r) for r in rows]


def _pm_with_cash(cash=10000.0):
    from src.portfolio import PortfolioManager

    pm = PortfolioManager()
    pm.update_balance(cash)
    return pm


def _buy_event(sym="SOLUSDT", qty=1.0, price=100.0, **kw):
    ev = {
        "type": "BUY", "symbol": sym, "qty": qty, "price": price,
        "order_id": kw.pop("order_id", None),
        "deduct_cash": kw.pop("deduct_cash", True),
    }
    ev.update(kw)
    return ev


# ---------------------------------------------------------------------------
# 4. mode flag
# ---------------------------------------------------------------------------

class TestModeFlag:
    def test_default_mode_is_shadow(self):
        assert get_mode(_db()) == "shadow"

    def test_set_mode_persists_and_rolls_back(self):
        db = _db()
        assert set_mode(db, "off") == "off"
        assert get_mode(db) == "off"
        assert set_mode(db, "shadow") == "shadow"
        assert get_mode(db) == "shadow"

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError):
            set_mode(_db(), "turbo")

    def test_off_is_a_hard_noop(self):
        db = _db()
        set_mode(db, "off")
        bootstrap_shadow(db)
        res = record_fill(_buy_event(), observe_only=True, db=db)
        assert res["status"] == "off"
        assert _events(db) == []
        # scan-step diff also skips
        assert shadow_diff(db)["status"] == "off"

    def test_observe_only_forced_noop_in_primary(self):
        db = _db()
        bootstrap_shadow(db)
        set_mode(db, "primary")
        res = record_fill(_buy_event(), observe_only=True, db=db)
        # observe hooks must never write the four sources, even if someone
        # flips the flag to primary while hooks still exist.
        assert res["status"] == "noop"
        assert db.portfolio_get("SOLUSDT") is None
        assert _trades(db) == []


# ---------------------------------------------------------------------------
# 3. bystander invariants: legacy path untouched, host never breaks
# ---------------------------------------------------------------------------

class TestObserveHooksBystander:
    def test_add_position_writes_legacy_AND_event_but_no_double_write(self):
        db = _db()
        bootstrap_shadow(db)
        pm = _pm_with_cash()
        pm.add_position("BTCUSDT", 0.001, 50000.0, "test", deduct_cash=True)

        # legacy four-source writes intact
        assert db.portfolio_get("BTCUSDT") is not None
        assert len(_trades(db, "BTCUSDT")) == 1
        # shadow observed exactly once
        evs = _events(db)
        assert len(evs) == 1
        assert evs[0]["type"] == "BUY"
        row = _shadow_row(db, "BTCUSDT")
        assert row is not None and abs(row["net_qty"] - 0.001) < 1e-12

    def test_close_position_observed_full_close(self):
        db = _db()
        bootstrap_shadow(db)
        pm = _pm_with_cash()
        pm.add_position("BTCUSDT", 0.001, 50000.0, "test", deduct_cash=True)
        pm.close_position("BTCUSDT", close_price=51000.0, exit_reason="test",
                          client_order_id="co-77")
        # legacy SELL row + shadow SELL event, book now flat
        assert len(_trades(db, "BTCUSDT")) == 2
        evs = _events(db)
        assert len(evs) == 2 and evs[1]["type"] == "SELL"
        assert evs[1]["order_id"] == "co-77"
        assert _shadow_row(db, "BTCUSDT") is None

    def test_shadow_failure_never_breaks_host_path(self, monkeypatch):
        db = _db()
        bootstrap_shadow(db)
        import src.ledger as ledger_mod

        def _boom(*a, **k):
            raise RuntimeError("shadow exploded")

        monkeypatch.setattr(ledger_mod, "_apply_shadow", _boom)
        pm = _pm_with_cash()
        pm.add_position("BTCUSDT", 0.001, 50000.0, "test", deduct_cash=True)
        # host path completed regardless: legacy row + trade written
        assert db.portfolio_get("BTCUSDT") is not None
        assert len(_trades(db, "BTCUSDT")) == 1
        assert _events(db) == []

    def test_refused_without_bootstrap(self):
        # events must never land before the baseline snapshot exists,
        # otherwise bootstrap would double-count them.
        db = _db()
        assert db.kv_get(BOOTSTRAP_TS_KEY) is None
        pm = _pm_with_cash()
        pm.add_position("BTCUSDT", 0.001, 50000.0, "test", deduct_cash=True)
        assert _events(db) == []
        assert _shadow_row(db, "BTCUSDT") is None


# ---------------------------------------------------------------------------
# shadow book mechanics
# ---------------------------------------------------------------------------

class TestShadowBook:
    def test_buy_sell_lifecycle_and_four_sources_untouched(self):
        db = _db()
        bootstrap_shadow(db)
        record_fill(_buy_event("LTCUSDT", 0.5, 100.0, deduct_cash=True),
                    observe_only=True, db=db)
        row = _shadow_row(db, "LTCUSDT")
        assert abs(row["net_qty"] - 0.5) < 1e-12
        assert abs(row["avg_entry_price"] - 100.0) < 1e-9
        assert abs(row["cash_delta"] - (-50.0)) < 1e-9

        # partial sell keeps the book open
        record_fill({"type": "SELL", "symbol": "LTCUSDT", "qty": 0.2,
                     "price": 110.0, "order_id": "s1"},
                    observe_only=True, db=db)
        row = _shadow_row(db, "LTCUSDT")
        assert abs(row["net_qty"] - 0.3) < 1e-12
        assert abs(row["cash_delta"] - (-50.0 + 22.0)) < 1e-9

        # full close removes the row
        record_fill({"type": "SELL", "symbol": "LTCUSDT", "qty": 0.3,
                     "price": 95.0, "order_id": "s2"},
                    observe_only=True, db=db)
        assert _shadow_row(db, "LTCUSDT") is None

        # bystander contract: the four real sources stay untouched
        assert db.portfolio_get("LTCUSDT") is None
        assert _trades(db) == []
        assert _outcomes(db, "LTCUSDT") == []

    def test_merge_buy_recomputes_average(self):
        db = _db()
        bootstrap_shadow(db)
        record_fill(_buy_event("LTCUSDT", 0.5, 100.0, deduct_cash=False),
                    observe_only=True, db=db)
        record_fill(_buy_event("LTCUSDT", 0.5, 120.0, deduct_cash=False),
                    observe_only=True, db=db)
        row = _shadow_row(db, "LTCUSDT")
        assert abs(row["net_qty"] - 1.0) < 1e-12
        assert abs(row["avg_entry_price"] - 110.0) < 1e-9

    def test_order_id_ingress_is_idempotent(self):
        db = _db()
        bootstrap_shadow(db)
        r1 = record_fill(_buy_event("LTCUSDT", 0.5, 100.0, order_id="x1"),
                         observe_only=True, db=db)
        r2 = record_fill(_buy_event("LTCUSDT", 0.5, 100.0, order_id="x1"),
                         observe_only=True, db=db)
        assert r1["status"] == "ok"
        assert r2["status"] == "duplicate"
        assert len(_events(db)) == 1
        assert abs(_shadow_row(db, "LTCUSDT")["net_qty"] - 0.5) < 1e-12

    def test_bootstrap_snapshots_live_and_is_idempotent(self):
        db = _db()
        pm = _pm_with_cash()
        # legacy fill BEFORE bootstrap: refused as event, but written live
        pm.add_position("BTCUSDT", 0.001, 50000.0, "test", deduct_cash=True)
        assert _events(db) == []

        ts1 = bootstrap_shadow(db)
        row = _shadow_row(db, "BTCUSDT")
        assert row is not None and abs(row["net_qty"] - 0.001) < 1e-12
        assert bootstrap_shadow(db) == ts1  # idempotent

        # post-bootstrap fills are tracked
        pm.add_position("BTCUSDT", 0.001, 52000.0, "test", deduct_cash=True)
        assert len(_events(db)) == 1
        assert abs(_shadow_row(db, "BTCUSDT")["net_qty"] - 0.002) < 1e-12

    def test_reset_clears_book_but_not_flag(self):
        db = _db()
        bootstrap_shadow(db)
        record_fill(_buy_event(), observe_only=True, db=db)
        reset_shadow(db)
        assert _events(db) == []
        assert _shadow_row(db, "SOLUSDT") is None
        assert db.kv_get(BOOTSTRAP_TS_KEY) is None
        assert get_mode(db) == "shadow"


# ---------------------------------------------------------------------------
# 2. shadow diff: clean rounds + injected inconsistencies
# ---------------------------------------------------------------------------

class TestShadowDiff:
    def _aligned_buy(self):
        """Real flow: legacy add_position writes live rows AND the hook
        writes the shadow event — books aligned by construction."""
        db = _db()
        bootstrap_shadow(db)
        pm = _pm_with_cash()
        pm.add_position("ETHUSDT", 0.02, 2000.0, "test", deduct_cash=True)
        return db, pm

    def test_clean_round_advances_streak(self):
        db, pm = self._aligned_buy()
        rep = shadow_diff(db, emit=False)
        assert rep["clean"] is True and rep["true_diffs"] == []
        assert rep["consecutive_clean"] == 1
        rep = shadow_diff(db, emit=False)
        assert rep["consecutive_clean"] == 2
        assert _audit(db, "LEDGER_SHADOW_DIFF") == []

    def test_detects_missing_trade_row(self):
        db, pm = self._aligned_buy()
        # inject: someone's trades row vanished
        db._get_conn().execute(
            "DELETE FROM trades WHERE symbol = 'ETHUSDT'"
        )
        db._get_conn().commit()
        rep = shadow_diff(db, emit=False)
        assert rep["clean"] is False
        kinds = [d["kind"] for d in rep["true_diffs"]]
        assert "trades_missing_fuzzy" in kinds
        assert rep["consecutive_clean"] == 0
        assert len(_audit(db, "LEDGER_SHADOW_DIFF")) == 1

    def test_detects_position_qty_mismatch(self):
        db, pm = self._aligned_buy()
        pos = db.portfolio_get("ETHUSDT")
        pos["quantity"] = 0.5  # tampered live qty
        db.portfolio_set("ETHUSDT", pos)
        rep = shadow_diff(db, emit=False)
        match = [d for d in rep["true_diffs"] if d["kind"] == "position_qty"]
        assert match and match[0]["shadow_qty"] == pytest.approx(0.02)
        assert match[0]["live_qty"] == pytest.approx(0.5)

    def test_detects_missing_and_extra_live_rows(self):
        db, pm = self._aligned_buy()
        db._get_conn().execute(
            "DELETE FROM portfolio WHERE symbol = 'ETHUSDT'"
        )
        db._get_conn().commit()
        db.portfolio_set("XYZUSDT", {"quantity": 3.0, "entry_price": 2.0,
                                     "strategy": "ghost"})
        rep = shadow_diff(db, emit=False)
        syms = {d["symbol"] for d in rep["true_diffs"]
                if d["kind"] == "position_qty"}
        assert "ETHUSDT" in syms and "XYZUSDT" in syms

    def test_diff_emits_alert_and_audit(self, monkeypatch):
        db, pm = self._aligned_buy()
        emitted = []
        import src.live_alerts as la_mod
        monkeypatch.setattr(la_mod, "emit",
                            lambda *a, **k: emitted.append(a))
        db._get_conn().execute("DELETE FROM trades WHERE symbol = 'ETHUSDT'")
        db._get_conn().commit()
        shadow_diff(db)  # emit=True (default)
        assert emitted and emitted[0][0] == "LEDGER_SHADOW_DIFF"
        assert _audit(db, "LEDGER_SHADOW_DIFF")

    def test_pending_promotes_after_max_rounds(self):
        # reconciler-style event: trades row booked, portfolio drift
        # cleanup lagging -> pending for PENDING_MAX_ROUNDS, then true.
        from src.ledger import PENDING_MAX_ROUNDS

        db = _db()
        bootstrap_shadow(db)
        pm = _pm_with_cash()
        pm.add_position("LTCUSDT", 0.5, 100.0, "test", deduct_cash=True)
        record_fill({"type": "SELL", "symbol": "LTCUSDT", "qty": 0.5,
                     "price": 110.0, "order_id": "r-1"},
                    observe_only=True, db=db)
        # simulate the reconciler having booked the trades row...
        db._get_conn().execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, timestamp, "
            "client_order_id) VALUES ('LTCUSDT', 'SELL', 0.5, 110.0, 5.0, ?, 'r-1')",
            (time.time(),),
        )
        db._get_conn().commit()

        seen_pending = []
        promoted_at = None
        for i in range(PENDING_MAX_ROUNDS + 2):
            rep = shadow_diff(db, emit=False)
            if rep["pending"]:
                seen_pending.append(i)
            if any(d["kind"] == "position_qty" for d in rep["true_diffs"]):
                promoted_at = i
                break
        assert seen_pending, "mismatch must sit in pending before promoting"
        assert promoted_at is not None, (
            f"pending must promote to true diff after {PENDING_MAX_ROUNDS} rounds"
        )

    def test_unmatched_trades_row_reported_as_info(self):
        # a trades row with no shadow event = a write that BYPASSED the
        # Ledger hooks (blind spot for cutover) -> info tier
        db, pm = self._aligned_buy()
        db._get_conn().execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, timestamp) "
            "VALUES ('GHOSTUSDT', 'BUY', 1.0, 50.0, 0, ?)",
            (time.time(),),
        )
        db._get_conn().commit()
        rep = shadow_diff(db, emit=False)
        unmatched = rep["info"]["unmatched_trades_rows"]
        assert any(u["symbol"] == "GHOSTUSDT" for u in unmatched)
        # info tier does not dirty the round
        assert rep["clean"] is True


# ---------------------------------------------------------------------------
# 1. PRIMARY path: single-transaction four-source write + rollback
# ---------------------------------------------------------------------------

class TestPrimaryAtomicity:
    def _primary_buy(self, db):
        return record_fill({
            "type": "BUY", "symbol": "SOLUSDT", "qty": 1.0, "price": 100.0,
            "order_id": "p-1", "deduct_cash": True, "strategy": "qfl",
            "outcome_entry": {
                "score": 75, "strategy": "qfl",
                "factors": {"technical": 80},
                "context": {"regime": "BULL", "fng_score": 60},
            },
            "tracker": {
                "tp_orders": [{"order_id": "tp1", "price": 106.0, "qty": 0.5,
                               "tier": 1, "pct": 50, "side": "LIMIT"}],
                "sl_order": {"order_id": "sl1", "price": 94.0, "qty": 1.0,
                             "stop_price": 94.0},
            },
        }, db=db)

    def test_full_buy_then_sell_writes_all_four_sources(self):
        db = _db()
        set_mode(db, "primary")
        db.portfolio_set_cash_balance(500.0)

        res = self._primary_buy(db)
        assert res["status"] == "ok"
        # 1. portfolio row
        assert db.portfolio_get("SOLUSDT") is not None
        # 2. cash debited in the same tx
        assert db.portfolio_get_cash_balance() == pytest.approx(400.0)
        # 3. trades row with dedup key
        assert len(_trades(db, "SOLUSDT")) == 1
        assert _trades(db, "SOLUSDT")[0]["client_order_id"] == "p-1"
        # 4. outcome open row
        outs = _outcomes(db, "SOLUSDT")
        assert len(outs) == 1 and outs[0]["status"] == "open"
        # 5. tracker kv (same shape as tp_sl_tracker.save_state)
        state = db.kv_get("tp_sl_tracker:SOLUSDT")
        assert state and len(state["tp_orders"]) == 1
        assert state["tp_filled"] == [False] and state["sl_order"]["price"] == 94.0
        # 6. audit row + ledger event log
        assert len(_audit(db, "LEDGER_FILL")) == 1
        assert len(_events(db)) == 1

        res2 = record_fill({
            "type": "SELL", "symbol": "SOLUSDT", "qty": 1.0, "price": 105.0,
            "order_id": "p-2", "pnl": 5.0, "exit_reason": "tp",
            "entry_id": res["outcome"]["entry_id"], "full_close": True,
        }, db=db)
        assert res2["status"] == "ok"
        assert db.portfolio_get("SOLUSDT") is None
        assert db.portfolio_get_cash_balance() == pytest.approx(505.0)
        sells = [t for t in _trades(db, "SOLUSDT") if t["side"] == "SELL"]
        assert len(sells) == 1 and sells[0]["pnl"] == pytest.approx(5.0)
        outs = _outcomes(db, "SOLUSDT")
        assert outs[0]["status"] == "closed"
        assert outs[0]["exit_reason"] == "tp"
        assert outs[0]["is_win"] == 1
        assert db.kv_get("tp_sl_tracker:SOLUSDT") is None  # closed => no tracker
        assert len(_audit(db, "LEDGER_FILL")) == 2
        assert len(_events(db)) == 2

    def test_mid_write_failure_rolls_back_everything(self, monkeypatch):
        db = _db()
        set_mode(db, "primary")
        db.portfolio_set_cash_balance(500.0)
        self._primary_buy(db)  # baseline: all four sources written once

        import src.ledger as ledger_mod

        def _boom(*a, **k):
            raise RuntimeError("outcomes write exploded")

        monkeypatch.setattr(ledger_mod, "_primary_write_outcomes", _boom)
        with pytest.raises(LedgerError):
            record_fill({
                "type": "SELL", "symbol": "SOLUSDT", "qty": 1.0,
                "price": 105.0, "order_id": "p-2", "pnl": 5.0,
                "exit_reason": "tp", "full_close": True,
            }, db=db)

        # NOTHING from the failed event survived — atomic rollback proof
        assert db.portfolio_get("SOLUSDT") is not None          # portfolio intact
        assert db.portfolio_get_cash_balance() == pytest.approx(400.0)  # cash intact
        assert len(_trades(db, "SOLUSDT")) == 1                 # no SELL row
        assert _outcomes(db, "SOLUSDT")[0]["status"] == "open"  # outcome open
        assert db.kv_get("tp_sl_tracker:SOLUSDT") is not None   # tracker intact
        assert len(_audit(db, "LEDGER_FILL")) == 1              # tx audit rolled back
        assert len(_events(db)) == 1                            # event log rolled back
        # failure itself IS auditable (best-effort, outside the dead tx)
        fails = _audit(db, "LEDGER_FILL_FAILED")
        assert len(fails) == 1 and "p-2" in fails[0]["details"]

    def test_primary_duplicate_order_id_no_double_write(self):
        db = _db()
        set_mode(db, "primary")
        db.portfolio_set_cash_balance(500.0)
        r1 = self._primary_buy(db)
        assert r1["status"] == "ok"
        # same order_id retried (e.g. caller retry after a network flap)
        r2 = self._primary_buy(db)
        assert r2["status"] == "duplicate"
        assert db.portfolio_get("SOLUSDT")["quantity"] == pytest.approx(1.0)
        assert db.portfolio_get_cash_balance() == pytest.approx(400.0)
        assert len(_trades(db, "SOLUSDT")) == 1

    def test_primary_sell_without_entry_id_closes_latest_open(self):
        db = _db()
        set_mode(db, "primary")
        db.portfolio_set_cash_balance(500.0)
        self._primary_buy(db)
        res = record_fill({
            "type": "SELL", "symbol": "SOLUSDT", "qty": 1.0, "price": 90.0,
            "order_id": "p-3", "exit_reason": "sl", "full_close": True,
        }, db=db)
        assert res["status"] == "ok"
        outs = _outcomes(db, "SOLUSDT")
        assert outs[0]["status"] == "closed"
        assert outs[0]["is_win"] == 0  # 90 < 100 entry, loss after fees

    def test_primary_buy_without_optional_payloads_skips_gracefully(self):
        db = _db()
        set_mode(db, "primary")
        res = record_fill(_buy_event("BAREUSDT", 2.0, 10.0, order_id="b-1",
                                     deduct_cash=False), db=db)
        assert res["status"] == "ok"
        assert db.portfolio_get("BAREUSDT") is not None
        assert _outcomes(db, "BAREUSDT") == []       # no payload -> no outcome row
        assert db.kv_get("tp_sl_tracker:BAREUSDT") is None  # no tracker payload


# ---------------------------------------------------------------------------
# LIMIT_MAKER price lesson pinned in the Ledger source
# ---------------------------------------------------------------------------

class TestLimitMakerLessonPinned:
    def test_lesson_is_in_ledger_comments(self):
        src = open("src/ledger.py").read()
        assert "LIMIT_MAKER" in src
        assert "stopPrice" in src
        assert "'0.00'" in src


# ---------------------------------------------------------------------------
# scan step wiring
# ---------------------------------------------------------------------------

class TestScanStepNonFatal:
    def test_step_runs_and_never_raises(self):
        from src.scan_orchestrator import _step_ledger_shadow_diff

        _step_ledger_shadow_diff({})  # empty ctx, empty db
        assert get_stats(_db()).get("rounds") == 1

    def test_step_skips_when_mode_off(self):
        from src.scan_orchestrator import _step_ledger_shadow_diff

        db = _db()
        set_mode(db, "off")
        _step_ledger_shadow_diff({})
        assert get_stats(db).get("rounds") is None

    def test_step_swallows_internal_failure(self, monkeypatch):
        from src.scan_orchestrator import _step_ledger_shadow_diff

        import src.ledger as ledger_mod
        monkeypatch.setattr(ledger_mod, "shadow_diff",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        _step_ledger_shadow_diff({})  # must not raise
