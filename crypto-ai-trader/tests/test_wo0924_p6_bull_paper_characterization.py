"""WO-0924-z2 P6-B1: bull_paper characterization tests (pre-migration pin).

bull_paper (portfolio/ab_metrics/engine_b) had ZERO direct test coverage —
only an indirect touch via test_bug33_tp_ladder_pnl. Before migrating its
54 raw-SQL sites to StateDB API methods (P6-B1), these tests pin the
CURRENT behavior of the writer module (BullPaperPortfolio) so the
migration must be behavior-identical:

- cash/state accounting, open/close/partial-close semantics (incl. the
  bug#33 full-close PnL formula), stops updates, listing orders/filters
- group isolation (A vs B sleeves)
- hold-time percentile math, reset semantics (note: reset only drops
  positions/trades/state — scaleouts/filter_decisions/ab_daily survive;
  pinned as-is)
- ab_metrics: closed-trade stats, snapshot upsert, isolation verify

These run against the CURRENT implementation first; after P6-B1 the same
file must pass unchanged.
"""

import time

import pytest

from src.state_db import StateDB
from src.bull_paper_portfolio import BullPaperPortfolio


@pytest.fixture()
def db():
    return StateDB()


@pytest.fixture()
def pf(db):
    return BullPaperPortfolio(db, start_cash=1000.0, group="A")


class TestOpenCloseSemantics:

    def test_open_deducts_cash_with_fee_and_writes_two_rows(self, db, pf):
        pos = pf.open_position("BTCUSDT", "core", 0.5, 100.0, fee_rate=0.01,
                               notes="n1")
        # cash = 1000 - (50 notional + 0.5 fee) = 949.5
        assert pf.cash == pytest.approx(949.5)
        assert pf.start_cash == 1000.0
        rows = pf.get_open_positions()
        assert len(rows) == 1
        r = rows[0]
        assert r["id"] == pos.id and r["symbol"] == "BTCUSDT"
        assert r["status"] == "open" and r["side"] == "core"
        assert r["quantity"] == pytest.approx(0.5)
        assert r["fees"] == pytest.approx(0.5)
        trades = pf.get_trade_history()
        assert len(trades) == 1 and trades[0]["action"] == "BUY"
        assert trades[0]["fee"] == pytest.approx(0.5)

    def test_open_insufficient_cash_raises(self, pf):
        with pytest.raises(ValueError, match="Insufficient paper cash"):
            pf.open_position("BTCUSDT", "core", 100.0, 100.0)

    def test_full_close_pnl_formula_bug33(self, db, pf):
        """bug#33: total_pnl = prior realized + leg pnl − entry fee.

        Pinned quirk: the RETURNED PaperPosition is the row fetched at
        transaction start, so its realized_pnl is the PRE-update value;
        the DB row carries the computed total. Both pinned here."""
        pf.open_position("ETHUSDT", "core", 1.0, 100.0, fee_rate=0.01)
        # entry fee 1.0; close at 110 with 1% fee → leg pnl 10-1.1=8.9
        pid = pf.get_open_positions()[0]["id"]
        closed = pf.close_position(pid, 110.0, fee_rate=0.01, reason="TP")
        assert closed is not None
        assert closed.realized_pnl == 0.0  # pre-update value in return obj
        with db._get_conn() as conn:
            row = conn.execute(
                "SELECT status, realized_pnl, fees FROM paper_bull_positions"
                " WHERE id=?", (pid,)).fetchone()
        assert row["status"] == "closed"
        assert row["realized_pnl"] == pytest.approx(8.9 - 1.0)  # bug#33
        assert row["fees"] == pytest.approx(1.0 + 1.1)  # entry + exit fee
        # cash: 1000 - 101 (open) + 108.9 (proceeds) = 1007.9
        assert pf.cash == pytest.approx(1007.9)
        assert pf.get_open_positions() == []
        hist = pf.get_trade_history()
        assert hist[0]["action"] == "SELL" and hist[0]["details"] == "TP"

    def test_partial_close_accumulates_pnl_and_appends_notes(self, db, pf):
        pid = pf.open_position("SOLUSDT", "satellite", 2.0, 50.0,
                               fee_rate=0.0).id
        first = pf.close_position(pid, 60.0, quantity=1.0, fee_rate=0.0,
                                  reason="B_TP_1R")
        assert first.quantity == pytest.approx(1.0)  # remaining
        assert first.realized_pnl == pytest.approx(10.0)
        rows = pf.get_open_positions()
        assert len(rows) == 1
        assert "partial close" in rows[0]["notes"]
        second = pf.close_position(pid, 55.0, fee_rate=0.0, reason="B_TP_2R")
        # returned obj = pre-update (first leg's 10.0); DB row = 15.0 total
        assert second.realized_pnl == pytest.approx(10.0)
        with db._get_conn() as conn:
            row = conn.execute(
                "SELECT realized_pnl, status FROM paper_bull_positions"
                " WHERE id=?", (pid,)).fetchone()
        assert row["realized_pnl"] == pytest.approx(15.0)
        assert row["status"] == "closed"
        assert pf.get_open_positions() == []

    def test_close_nonexistent_returns_none(self, pf):
        assert pf.close_position("nope", 100.0) is None

    def test_close_qty_clamped_to_position_qty(self, db, pf):
        pid = pf.open_position("XRPUSDT", "core", 1.0, 10.0,
                               fee_rate=0.0).id
        out = pf.close_position(pid, 12.0, quantity=5.0, fee_rate=0.0)
        assert out.quantity == pytest.approx(1.0)  # clamped
        with db._get_conn() as conn:
            row = conn.execute(
                "SELECT realized_pnl FROM paper_bull_positions WHERE id=?",
                (pid,)).fetchone()
        assert row["realized_pnl"] == pytest.approx(2.0)


class TestListingsAndStops:

    def test_get_open_positions_side_filter(self, pf):
        pf.open_position("AAAUSDT", "core", 1.0, 10.0, fee_rate=0.0)
        pf.open_position("BBBUSDT", "satellite", 1.0, 10.0, fee_rate=0.0)
        assert len(pf.get_open_positions()) == 2
        assert len(pf.get_open_positions(side="core")) == 1
        assert pf.get_open_positions(side="core")[0]["symbol"] == "AAAUSDT"

    def test_get_all_positions_limit_and_order(self, pf):
        for i in range(3):
            pf.open_position(f"S{i}USDT", "core", 1.0, 10.0, fee_rate=0.0)
            time.sleep(0.002)
        rows = pf.get_all_positions(limit=2)
        assert len(rows) == 2
        # ORDER BY entry_time DESC → newest first
        assert rows[0]["symbol"] == "S2USDT"

    def test_update_stops(self, pf):
        pid = pf.open_position("ADAUSDT", "core", 1.0, 10.0, fee_rate=0.0).id
        pf.update_stops(pid, stop_loss=8.0, take_profit=15.0)
        r = pf.get_open_positions()[0]
        assert r["stop_loss"] == pytest.approx(8.0)
        assert r["take_profit"] == pytest.approx(15.0)

    def test_portfolio_value_math(self, pf):
        pf.open_position("COREUSDT", "core", 2.0, 50.0, fee_rate=0.0)
        pf.open_position("SATUSDT", "satellite", 1.0, 20.0, fee_rate=0.0)
        # cash 1000-100-20=880; MV at px {CORE:60, SAT:30} = 120+30=150
        v = pf.portfolio_value({"COREUSDT": 60.0, "SATUSDT": 30.0})
        assert v["cash"] == pytest.approx(880.0)
        assert v["market_value"] == pytest.approx(150.0)
        assert v["core_mv"] == pytest.approx(120.0)
        assert v["sat_mv"] == pytest.approx(30.0)
        assert v["total_value"] == pytest.approx(1030.0)
        assert v["unrealized_pnl"] == pytest.approx(30.0)
        assert v["total_return"] == pytest.approx(0.03)
        assert v["position_count"] == 2
        assert v["core_count"] == 1 and v["sat_count"] == 1

    def test_close_satellites_for_symbol_only_touches_satellites(self, pf):
        pid_core = pf.open_position("DUPUSDT", "core", 1.0, 10.0, fee_rate=0.0)
        pf.open_position("DUPUSDT", "satellite", 1.0, 10.0, fee_rate=0.0)
        pf.open_position("OTHERUSDT", "satellite", 1.0, 10.0, fee_rate=0.0)
        n = pf.close_satellites_for_symbol("DUPUSDT", 11.0)
        assert n == 1
        left = {(r["symbol"], r["side"]) for r in pf.get_open_positions()}
        assert left == {("DUPUSDT", "core"), ("OTHERUSDT", "satellite")}


class TestGroupIsolationAndState:

    def test_group_isolation(self, db):
        a = BullPaperPortfolio(db, start_cash=100.0, group="A")
        b = BullPaperPortfolio(db, start_cash=200.0, group="B")
        a.open_position("AAAUSDT", "core", 1.0, 10.0, fee_rate=0.0)
        b.open_position("BBBUSDT", "core", 1.0, 10.0, fee_rate=0.0)
        assert [r["symbol"] for r in a.get_open_positions()] == ["AAAUSDT"]
        assert [r["symbol"] for r in b.get_open_positions()] == ["BBBUSDT"]
        assert a.cash == pytest.approx(90.0)
        assert b.cash == pytest.approx(190.0)
        assert [t["symbol"] for t in a.get_trade_history()] == ["AAAUSDT"]
        assert [t["symbol"] for t in b.get_trade_history()] == ["BBBUSDT"]

    def test_legacy_group_null_maps_to_A(self, db, pf):
        """ab_group COALESCE('A') semantics: raw NULL rows count as group A."""
        pid = pf.open_position("LEGUSDT", "core", 1.0, 10.0, fee_rate=0.0).id
        with db._get_conn() as conn:
            conn.execute(
                "UPDATE paper_bull_positions SET ab_group=NULL WHERE id=?",
                (pid,))
            conn.commit()
        rows = pf.get_open_positions()
        assert len(rows) == 1 and rows[0]["symbol"] == "LEGUSDT"

    def test_hold_time_stats_percentiles(self, pf):
        # 3 closed positions with hold_seconds 3600/7200/36000
        for i, hold in enumerate((1.0, 2.0, 10.0)):
            pid = pf.open_position(f"H{i}USDT", "core", 1.0, 10.0,
                                   fee_rate=0.0).id
            pf.close_position(pid, 10.0, fee_rate=0.0)
            # close() stamps its own hold_seconds; override afterwards to
            # pin the stats math on deterministic inputs
            with pf.db._get_conn() as conn:
                conn.execute(
                    "UPDATE paper_bull_positions SET hold_seconds=? "
                    "WHERE id=?", (hold * 3600, pid))
                conn.commit()
        st = pf.hold_time_stats()
        assert st["count"] == 3
        assert st["avg_hours"] == pytest.approx((1 + 2 + 10) / 3, abs=0.01)
        assert st["median_hours"] == pytest.approx(2.0, abs=0.01)
        assert st["min_hours"] == pytest.approx(1.0, abs=0.01)
        assert st["max_hours"] == pytest.approx(10.0, abs=0.01)

    def test_hold_time_stats_empty(self, pf):
        assert pf.hold_time_stats() == {"count": 0}

    def test_reset_drops_core_tables_but_keeps_others(self, db, pf):
        """Pins current behavior: reset only drops positions/trades/state;
        scaleouts/filter_decisions/ab_daily survive (arguably a wart,
        but this is the pinned pre-migration behavior)."""
        pid = pf.open_position("RSTUSDT", "core", 1.0, 10.0, fee_rate=0.0).id
        with db._get_conn() as conn:
            conn.execute(
                """INSERT INTO paper_bull_scaleouts
                   (id, position_id, ab_group, symbol, stage, r_multiple,
                    fraction, entry_price, atr_at_entry, original_qty,
                    trigger_price, status, fired_time, fired_price, created_at)
                   VALUES ('so_x', ?, 'B', 'RSTUSDT', 1, 2.0, 0.5, 10.0,
                    0.0, 1.0, 12.0, 'pending', 0, 0.0, 0)""", (pid,))
            conn.commit()
        pf.reset()
        assert pf.get_open_positions() == []
        assert pf.get_trade_history() == []
        assert pf.cash == pytest.approx(1000.0)  # state re-seeded
        with db._get_conn() as conn:
            n = conn.execute(
                "SELECT count(*) FROM paper_bull_scaleouts").fetchone()[0]
        assert n == 1  # scaleouts survive reset (pinned)


class TestAbMetrics:

    def test_compute_group_stats_math(self, db):
        from src.bull_paper_ab_metrics import compute_group_stats
        pf = BullPaperPortfolio(db, start_cash=500.0, group="A")
        # win +10, loss -4, win +2 → win_rate 2/3, pf = 12/4 = 3
        for sym, entry, exit_px in (("W1USDT", 100.0, 110.0),
                                    ("L1USDT", 100.0, 96.0),
                                    ("W2USDT", 100.0, 102.0)):
            pid = pf.open_position(sym, "core", 1.0, entry, fee_rate=0.0).id
            pf.close_position(pid, exit_px, fee_rate=0.0, reason="TP")
        stats = compute_group_stats(db, "A", 500.0, prices={})
        assert stats["n_trades"] == 3
        assert stats["n_wins"] == 2
        assert stats["win_rate"] == pytest.approx(2 / 3)
        assert stats["gross_profit"] == pytest.approx(12.0)
        assert stats["gross_loss"] == pytest.approx(4.0)
        assert stats["profit_factor"] == pytest.approx(3.0)
        assert stats["cash"] == pytest.approx(508.0)
        assert stats["equity"] == pytest.approx(508.0)  # no open positions

    def test_snapshot_daily_upsert_one_row_per_day(self, db):
        from src.bull_paper_ab_metrics import snapshot_daily
        # ab_metrics assumes tables exist — bootstrapped by the portfolio
        # class (pinned dependency)
        BullPaperPortfolio(db, start_cash=500.0, group="A")
        snapshot_daily(db, prices={}, a_start=500.0, b_start=500.0,
                       kelly_f=0.1, kelly_tstat=1.5, grid_active=1,
                       exploration=2, whipsaw=0)
        snapshot_daily(db, prices={}, a_start=500.0, b_start=500.0,
                       kelly_f=0.2, kelly_tstat=2.0, grid_active=0,
                       exploration=0, whipsaw=1)
        with db._get_conn() as conn:
            rows = conn.execute(
                "SELECT ab_group, kelly_f, whipsaw_count "
                "FROM paper_bull_ab_daily ORDER BY ab_group").fetchall()
        assert len(rows) == 2  # A + B, same day upserted not duplicated
        assert {r["ab_group"] for r in rows} == {"A", "B"}
        by_g = {r["ab_group"]: r for r in rows}
        assert by_g["A"]["kelly_f"] == pytest.approx(0.2)  # second run wins
        assert by_g["A"]["whipsaw_count"] == 1

    def test_verify_ab_isolation_clean_db(self, db):
        from src.bull_paper_ab_metrics import verify_ab_isolation
        # both sleeves must be bootstrapped (production flow constructs
        # A and B portfolios before the daily verify runs)
        BullPaperPortfolio(db, start_cash=500.0, group="A")
        BullPaperPortfolio(db, start_cash=400.0, group="B")
        result = verify_ab_isolation(db)
        assert result["ok"] is True
        assert result["anomalies"] == []

    def test_verify_ab_isolation_flags_cross_group_cash(self, db):
        from src.bull_paper_ab_metrics import verify_ab_isolation
        a = BullPaperPortfolio(db, start_cash=500.0, group="A")
        BullPaperPortfolio(db, start_cash=400.0, group="B")
        # corrupt A cash by 10 → cash arithmetic anomaly must surface
        with db._get_conn() as conn:
            conn.execute(
                "UPDATE paper_bull_state SET value='510.0'"
                " WHERE key='cash_balance'")
            conn.commit()
        result = verify_ab_isolation(db)
        assert result["ok"] is False
        assert any("A cash mismatch" in a for a in result["anomalies"])


class TestStoreMigration:
    """P6-B1 migration invariants: SQL choke point + store semantics."""

    TABLES = ("paper_bull_positions", "paper_bull_trades", "paper_bull_state",
              "paper_bull_scaleouts", "paper_bull_filter_decisions",
              "paper_bull_ab_daily")

    def test_no_paper_bull_sql_outside_store(self):
        """Every paper_bull SQL statement outside src/bull_paper_store.py
        is a regression to the pre-P6-B1 raw-SQL paths. Matches uppercase
        SQL keywords (the codebase's SQL style) so docstrings mentioning
        table names in prose don't false-positive."""
        import re
        from pathlib import Path
        sql_pat = re.compile(
            r"(FROM|INTO|UPDATE|TABLE|JOIN)\s+(IF NOT EXISTS\s+)?paper_bull_")
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for mod in list((root / "src").glob("*.py")) + \
                    list((root / "scripts").glob("*.py")) + [root / "main.py"]:
            if mod.name == "bull_paper_store.py":
                continue
            for i, line in enumerate(mod.read_text().splitlines(), 1):
                if sql_pat.search(line):
                    offenders.append(f"{mod.name}:{i}: {line.strip()}")
        assert offenders == [], "raw paper_bull SQL leaked:\n" + "\n".join(offenders)

    def test_transaction_rollback_atomicity(self, db, pf):
        """Exception inside store.transaction() must leave NO partial
        write (position close UPDATE + trade INSERT are all-or-nothing)."""
        pid = pf.open_position("TXNUSDT", "core", 1.0, 100.0, fee_rate=0.0).id
        store = pf.store
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.update_position_full_close(
                    pid, exit_price=110.0, exit_time=1, realized_pnl=10.0,
                    fees=0.0, hold_seconds=1.0)
                store.insert_trade(
                    trade_id="trade_txn", position_id=pid, symbol="TXNUSDT",
                    side="core", action="SELL", quantity=1.0, price=110.0,
                    fee=0.0, notional=110.0, timestamp=1, details="TP",
                    ab_group="A")
                raise RuntimeError("boom mid-transaction")
        row = store.get_position(pid)
        assert row["status"] == "open"          # UPDATE rolled back
        assert row["realized_pnl"] == 0.0
        # only the open-position BUY leg remains; the rolled-back SELL
        # INSERT must not exist
        hist = store.get_trade_history("A")
        assert [t["action"] for t in hist] == ["BUY"]
        assert all(t["id"] != "trade_txn" for t in hist)

    def test_scaleout_lifecycle_arm_pending_fire_void(self, db, pf):
        from src.bull_paper_store import new_scaleout_id
        store = pf.store
        pid = pf.open_position("SOUSDT", "core", 3.0, 10.0, fee_rate=0.0).id
        now = 12345
        store.insert_scaleouts([
            (new_scaleout_id(), pid, "A", "SOUSDT", 1, 2.0, 1/3, 10.0, 0.0,
             3.0, 12.0, "pending", 0, 0.0, now),
            (new_scaleout_id(), pid, "A", "SOUSDT", 2, 3.0, 1/3, 10.0, 0.0,
             3.0, 13.0, "pending", 0, 0.0, now),
        ])
        pend = store.pending_scaleouts(pid)
        assert [p["stage"] for p in pend] == [1, 2]  # ORDER BY stage
        store.fire_scaleout(pend[0]["id"], fired_time=99, fired_price=12.5)
        pend2 = store.pending_scaleouts(pid)
        assert [p["stage"] for p in pend2] == [2]
        fired = [s for s in (store.get_position(pid),) if s]
        store.void_pending_scaleouts(pid)
        assert store.pending_scaleouts(pid) == []

    def test_log_filter_decision_row(self, db, pf):
        store = pf.store
        store.log_filter_decision(
            symbol="FLTUSDT", decision="reject", ab_group="B", score=55.0,
            fail_filter="rvol", atr=1.5, ema20=2.0, ema50=1.9, ema200=1.8,
            adx=22.0, rvol=1.1, r_multiple=2.0, regime="BULL", notes="x")
        top = store.top_reject_fail_filters("B", limit=3)
        assert top == [{"fail_filter": "rvol", "c": 1}]

    def test_table_row_count_whitelist(self, db, pf):
        store = pf.store
        assert store.table_row_count("paper_bull_positions") == 0
        with pytest.raises(ValueError, match="not managed"):
            store.table_row_count("trade_outcomes")
