"""bug#31 (2026-08-30): SL order filled on exchange but SELL never booked.

Timeline anchor — BANK 8/30:
  08-30 00:07 SL fires on exchange, actual fill 141.993 @ 0.0365
           (outcome qty 142.0; commission/LOT_SIZE residue 0.007 = 0.005%).
  00:07+   portfolio_state._save_state clears the portfolio row, but
           reconcile_fills (trailing-check, every 5 min) kept flagging
           "position gone but insufficient SELL fills (exit 141.993/142.0)"
           — its absolute 1e-8 tolerance can never accept the residue, so
           the outcome stays open and NO SELL is ever booked (trailing-check
           log 01:00-02:35: 20+ identical anomalies).
  08-30 08:30 daily sync_outcomes then DOUBLE-books: manual patch row
           (trades id 222, pnl -0.3408) + sync re-insert (id 227, pnl
           -0.337619) — trades has no unique constraint, so the old
           INSERT OR IGNORE was a no-op guard.

Fix:
  1. reconcile_fills closes the loop on a RELATIVE 2% tolerance (aligned
     with insert_sell_dedup ±2% semantics) and books the ACTUAL exit_qty.
  2. sync_trade_outcomes routes its SELL insert through the shared
     insert_sell_dedup helper (bug#24): qty±2%/price±1%/±1h window.
"""
import os
import sys
import time
from unittest.mock import patch as mock_patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.state_db import StateDB
from scripts.ensure_tp_sl import insert_sell_dedup
from scripts.reconcile_fills import reconcile_fills


# ----------------------------------------------------------------- fixtures

@pytest.fixture
def fresh_statedb(tmp_path):
    db = StateDB(str(tmp_path / "test_state.db"))
    yield db
    db.close()


def _b(ts, qty, price, buyer=False):
    return {"time": int(ts * 1000), "qty": str(qty), "price": str(price),
            "isBuyer": buyer, "commission": "0", "commissionAsset": "BNB"}


class _FakeClient:
    def __init__(self, held, trades):
        self._held = held
        self._trades = trades

    def get_account(self):
        return {"balances": [
            {"asset": a, "free": str(v), "locked": "0"}
            for a, v in self._held.items()
        ] + [{"asset": "USDT", "free": "999", "locked": "0"}]}

    def get_my_trades(self, symbol, limit=50):
        return self._trades


def _real_write_verify(db, write_fn, verify_fn, label, attempts=3, backoff_sec=1.0):
    """Execute the real write, skip the retry/verify loop (bug#29 lesson:
    return_value=True stubs never run the closure → outcome stays open)."""
    write_fn()
    return True


def _insert_open_outcome(db, symbol, qty, entry_price, entry_time):
    conn = db._get_conn()
    cur = conn.execute(
        """INSERT INTO trade_outcomes (symbol, entry_time, entry_price, qty,
           status, peak_price, trough_price, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?)""",
        (symbol, entry_time, entry_price, qty, entry_price, entry_price,
         entry_time, entry_time),
    )
    conn.commit()
    return cur.lastrowid


def _count_sells(db, symbol):
    return db._get_conn().execute(
        "SELECT COUNT(*) c FROM trades WHERE symbol=? AND side='SELL'",
        (symbol,),
    ).fetchone()["c"]


ENTRY_TS = time.time() - 7200
FILL_TS = time.time() - 600

# BANK re-enactment constants
BANK_QTY_OUTCOME = 142.0
BANK_QTY_FILL = 141.993          # 0.005% residue (commission/LOT_SIZE)
BANK_ENTRY = 0.0535
BANK_EXIT = 0.0365


# ------------------------------------- 1) SL fill micro-residue closes loop

class TestResidualFillClosesLoop:
    def test_sl_fill_residue_is_booked_and_outcome_closed(self, fresh_statedb):
        """Old code: 141.993 < 142 - 1e-8 → anomaly forever, zero booking.
        New code: relative 2% → book SELL (actual fill qty) + close outcome."""
        oid = _insert_open_outcome(
            fresh_statedb, "BANKUSDT", BANK_QTY_OUTCOME, BANK_ENTRY, ENTRY_TS)
        client = _FakeClient({}, [_b(FILL_TS, BANK_QTY_FILL, BANK_EXIT)])

        with mock_patch("src.state_db.db_write_with_verify",
                        side_effect=_real_write_verify):
            out = reconcile_fills(client=client, db=fresh_statedb)

        assert out["anomalies"] == [], out["anomalies"]
        assert out["errors"] == [], out["errors"]
        assert any(f"#{oid}" in p for p in out["patched"]), out

        row = fresh_statedb._get_conn().execute(
            "SELECT qty, price, pnl FROM trades "
            "WHERE symbol='BANKUSDT' AND side='SELL'"
        ).fetchone()
        assert row is not None, "SELL row missing — bug#31 not fixed"
        # booked qty = ACTUAL exchange fill, not the outcome qty
        assert abs(row["qty"] - BANK_QTY_FILL) < 1e-6
        assert abs(row["price"] - BANK_EXIT) < 1e-9
        # pnl computed on exit_qty
        expected_pnl = (BANK_EXIT - BANK_ENTRY) * BANK_QTY_FILL
        assert abs(row["pnl"] - expected_pnl) < 1e-4

        status = fresh_statedb._get_conn().execute(
            "SELECT status FROM trade_outcomes WHERE id=?", (oid,)
        ).fetchone()["status"]
        assert status == "closed"

    def test_sub_2pct_but_over_1pct_residue_still_closes(self, fresh_statedb):
        """1.5% residue (>bug#13 1e-8, <2% tolerance) must also close."""
        oid = _insert_open_outcome(
            fresh_statedb, "NILUSDT", 100.0, 0.05, ENTRY_TS)
        client = _FakeClient({}, [_b(FILL_TS, 98.5, 0.03)])

        with mock_patch("src.state_db.db_write_with_verify",
                        side_effect=_real_write_verify):
            out = reconcile_fills(client=client, db=fresh_statedb)

        assert out["anomalies"] == [], out["anomalies"]
        assert any(f"#{oid}" in p for p in out["patched"]), out

    def test_grossly_short_fill_still_flags_anomaly(self, fresh_statedb):
        """Coverage <98% (e.g. PROM#105: 0/0.96) must KEEP the anomaly —
        the tolerance must not swallow genuine missing fills."""
        oid = _insert_open_outcome(
            fresh_statedb, "PROMUSDT", 0.96, 0.05, ENTRY_TS)
        client = _FakeClient({}, [])  # position gone, zero fills

        out = reconcile_fills(client=client, db=fresh_statedb, dry_run=True)

        assert any("insufficient SELL fills" in a for a in out["anomalies"]), out
        assert out["patched"] == []


# ------------------------------------- 2) no double-booking on re-runs

class TestNoDoubleBooking:
    def test_repeat_reconcile_books_only_one_sell(self, fresh_statedb):
        """Second reconcile pass must not add a second SELL (bug#13/24 family)."""
        oid = _insert_open_outcome(
            fresh_statedb, "BANKUSDT", BANK_QTY_OUTCOME, BANK_ENTRY, ENTRY_TS)
        client = _FakeClient({}, [_b(FILL_TS, BANK_QTY_FILL, BANK_EXIT)])

        with mock_patch("src.state_db.db_write_with_verify",
                        side_effect=_real_write_verify):
            out1 = reconcile_fills(client=client, db=fresh_statedb)
            out2 = reconcile_fills(client=client, db=fresh_statedb)

        assert any(f"#{oid}" in p for p in out1["patched"]), out1
        assert out2["anomalies"] == [], out2["anomalies"]
        assert not any(f"#{oid}" in p for p in out2["patched"]), out2
        assert _count_sells(fresh_statedb, "BANKUSDT") == 1

    def test_prior_sell_row_closes_outcome_without_new_insert(self, fresh_statedb):
        """227-pattern: SELL already booked (manual 222) while outcome left
        open → reconcile must CLOSE ONLY, never insert a duplicate."""
        oid = _insert_open_outcome(
            fresh_statedb, "NILUSDT", 106.8, 0.05, ENTRY_TS)
        conn = fresh_statedb._get_conn()
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, timestamp) "
            "VALUES ('NILUSDT', 'SELL', 106.0, 0.0365, -1.4, ?)", (FILL_TS,))
        conn.commit()
        client = _FakeClient({}, [_b(FILL_TS, 106.0, 0.0365)])

        with mock_patch("src.state_db.db_write_with_verify",
                        side_effect=_real_write_verify):
            out = reconcile_fills(client=client, db=fresh_statedb)

        assert out["anomalies"] == [], out["anomalies"]
        assert any(f"#{oid}" in p for p in out["closed_only"]), out
        assert _count_sells(fresh_statedb, "NILUSDT") == 1

    def test_sync_helper_dedup_blocks_227_reinsert(self, fresh_statedb):
        """sync_trade_outcomes now routes through insert_sell_dedup: with
        manual row 222 present, the daily-sync insert attempt is a no-op."""
        conn = fresh_statedb._get_conn()
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, timestamp) "
            "VALUES ('BANKUSDT', 'SELL', 141.993, 0.0365, -0.3408, ?)", (FILL_TS,))
        conn.commit()

        inserted = insert_sell_dedup(
            conn, "BANKUSDT", 141.993, 0.0365, -0.337619, FILL_TS)

        assert inserted is False
        assert _count_sells(fresh_statedb, "BANKUSDT") == 1

    def test_sync_helper_still_books_legit_exit(self, fresh_statedb):
        conn = fresh_statedb._get_conn()
        inserted = insert_sell_dedup(
            conn, "PROMUSDT", 0.96, 0.05, 0.001, FILL_TS)
        assert inserted is True
        assert _count_sells(fresh_statedb, "PROMUSDT") == 1

    def test_sync_source_no_insert_or_ignore(self):
        """Guard: sync_trade_outcomes must keep routing through the dedup
        helper — INSERT OR IGNORE has zero protection on an unconstrained
        table. (bug#31 regression guard)"""
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(
            os.path.join(repo_root, "scripts", "sync_trade_outcomes.py"),
            encoding="utf-8",
        ).read()
        assert "INSERT OR IGNORE INTO trades" not in src
        assert "insert_sell_dedup" in src


# ------------------------------------- 3) SELL insert + outcome close pairing

class TestBookingAtomicity:
    def test_sell_and_close_both_landed_in_one_pass(self, fresh_statedb):
        """Full patch = SELL row AND closed outcome; partial success without
        the other is a loud error, never a silent pass."""
        oid = _insert_open_outcome(
            fresh_statedb, "BANKUSDT", BANK_QTY_OUTCOME, BANK_ENTRY, ENTRY_TS)
        client = _FakeClient({}, [_b(FILL_TS, BANK_QTY_FILL, BANK_EXIT)])

        with mock_patch("src.state_db.db_write_with_verify",
                        side_effect=_real_write_verify):
            out = reconcile_fills(client=client, db=fresh_statedb)

        assert out["errors"] == [], out["errors"]
        assert _count_sells(fresh_statedb, "BANKUSDT") == 1
        status = fresh_statedb._get_conn().execute(
            "SELECT status FROM trade_outcomes WHERE id=?", (oid,)
        ).fetchone()["status"]
        assert status == "closed"
        # exactly one patched entry (not split into close_only)
        assert sum(1 for p in out["patched"] if f"#{oid}" in p) == 1

    def test_failed_verified_write_is_loud_and_changes_nothing(self, fresh_statedb):
        """bug#8 contract: a write that fails verify must surface in errors[]
        and leave BOTH ledger artifacts untouched."""
        oid = _insert_open_outcome(
            fresh_statedb, "BANKUSDT", BANK_QTY_OUTCOME, BANK_ENTRY, ENTRY_TS)
        client = _FakeClient({}, [_b(FILL_TS, BANK_QTY_FILL, BANK_EXIT)])

        with mock_patch("src.state_db.db_write_with_verify", return_value=False):
            out = reconcile_fills(client=client, db=fresh_statedb)

        assert out["errors"], "silent ledger gap — violates bug#8 contract"
        assert out["patched"] == []
        assert _count_sells(fresh_statedb, "BANKUSDT") == 0
        status = fresh_statedb._get_conn().execute(
            "SELECT status FROM trade_outcomes WHERE id=?", (oid,)
        ).fetchone()["status"]
        assert status == "open"
