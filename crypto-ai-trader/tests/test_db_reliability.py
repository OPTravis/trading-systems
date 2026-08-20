"""
Regression tests for bug#8 (2026-08-20 20:30 incident):
ensure_tp_sl TP-breach close — exchange fill OK, three DB writes
"committed" without exception (errors=[]) yet nothing landed in
data/state.db; a later stale-snapshot portfolio save resurrected the
closed row and inflated it (27.8 + 52 -> 79.8 ACE).

Covers the three reliability gaps that produced the incident:
  1. SQLite concurrency config: WAL + busy_timeout (was DELETE-journal
     exclusive-lock mode with three cron processes colliding at :30).
  2. Loud failure: critical writes are verified by read-back; a silent
     loss (commit acknowledged, data absent) retries and then escalates
     (error log + cron_failures.jsonl) instead of returning success.
  3. _save_state race guard: a stale in-memory PortfolioManager snapshot
     must not resurrect externally-deleted rows, clobber externally-
     updated rows, or bulldoze externally-added rows.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.state_db import StateDB, db_write_with_verify, record_db_failure  # noqa: E402
from src.portfolio_state import StateMixin  # noqa: E402


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

class TestPortfolio(StateMixin):
    """Concrete portfolio bound to a REAL StateDB (not a mock) so the
    concurrency guards are exercised against actual SQLite behavior."""

    def __init__(self, db):
        self.DUST_THRESHOLD_USD = 5.0
        self._db = db
        self.config = {
            "stop_loss": {"default_pct": 5.0},
            "take_profit": {"default_pct": 6.0},
        }
        self.positions = {}
        self.cash_balance = 0.0
        self._last_save_time = 0.0
        self._save_debounce_sec = 0.0


@pytest.fixture
def db(tmp_path):
    """Real StateDB on a temp file (conftest also sets STATE_DB_PATH)."""
    inst = StateDB(str(tmp_path / "regr_state.db"))
    yield inst
    try:
        inst.close()
    except Exception:
        pass


@pytest.fixture
def failures_file(tmp_path, monkeypatch):
    f = tmp_path / "cron_failures.jsonl"
    monkeypatch.setenv("CRON_FAILURES_FILE", str(f))
    return f


def _failures(failures_file):
    if not failures_file.exists():
        return []
    return [json.loads(line) for line in failures_file.read_text().splitlines() if line.strip()]


# ────────────────────────────────────────────────────────────
# 1. SQLite concurrency configuration
# ────────────────────────────────────────────────────────────

class TestConcurrencyConfig:
    def test_wal_mode_enabled_on_new_connections(self, db):
        conn = db._get_conn()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal", (
            "state.db must run in WAL mode: rollback-journal (DELETE) mode "
            "forced exclusive locks during the 20:30 three-process collision"
        )

    def test_busy_timeout_configured(self, db):
        conn = db._get_conn()
        timeout_us = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert int(timeout_us) >= 30000, "busy_timeout must be >= 30s"

    def test_wal_persists_on_file(self, db, tmp_path):
        # WAL is a persistent property of the file — a second, independent
        # connection (e.g. another cron process) must see WAL too.
        db._get_conn().execute(
            "INSERT INTO kv (key, value, updated_at) VALUES ('k', 'v', ?)",
            (time.time(),),
        ).connection.commit()
        other = sqlite3.connect(str(tmp_path / "regr_state.db"))
        mode = other.execute("PRAGMA journal_mode").fetchone()[0]
        other.close()
        assert str(mode).lower() == "wal"

    def test_wal_switch_refusal_is_loud(self, tmp_path, caplog):
        """If the WAL pragma is refused (concurrent holder / unsupported FS),
        a warning must be logged instead of silently running degraded."""
        import logging

        class RefusedConn(sqlite3.Connection):
            """Connection whose PRAGMA journal_mode=WAL reports refusal
            (returns the current mode instead of 'wal')."""

            def execute(self, sql, *a, **kw):
                if "journal_mode=WAL" in sql:
                    class _R:
                        def fetchone(self):
                            return ("delete",)  # switch refused

                    return _R()
                return super().execute(sql, *a, **kw)

        real_connect = sqlite3.connect

        def fake_connect(*a, **kw):
            kw.pop("factory", None)
            return real_connect(*a, factory=RefusedConn, **kw)

        with caplog.at_level(logging.WARNING):
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(sqlite3, "connect", fake_connect)
                inst = StateDB(str(tmp_path / "refused.db"))
        assert any("WAL switch refused" in r.message for r in caplog.records), (
            "a refused WAL switch must be visible in logs (ops signal), "
            "not a silent degradation"
        )


# ────────────────────────────────────────────────────────────
# 2. Loud failure: silent write loss is detected & escalated
# ────────────────────────────────────────────────────────────

class TestVerifiedWrites:
    def test_verified_write_success(self, db, failures_file):
        def write():
            c = db._get_conn()
            c.execute(
                "INSERT INTO trades (symbol, side, qty, price, pnl, timestamp) "
                "VALUES ('ACEUSDT', 'SELL', 27.8, 0.2286, 0.73, ?)",
                (time.time(),),
            )
            c.commit()

        def verify():
            row = db._get_conn().execute(
                "SELECT 1 FROM trades WHERE symbol='ACEUSDT' AND side='SELL'"
            ).fetchone()
            return row is not None

        assert db_write_with_verify(db, write, verify, "test-ok") is True
        assert _failures(failures_file) == []

    def test_silent_write_loss_is_detected_and_escalated(self, db, failures_file, caplog):
        """THE bug#8 signature: INSERT raises nothing, commit returns, yet the
        row is not in the table. Simulated with a RAISE(IGNORE) trigger — the
        exact semantic of 'storage layer acknowledged then dropped the write'.
        The verified wrapper must fail LOUDLY (error log + cron_failures.jsonl)
        instead of reporting success like the old errors=[] path."""
        import logging
        c = db._get_conn()
        c.execute(
            "CREATE TRIGGER swallow_sell BEFORE INSERT ON trades "
            "BEGIN SELECT RAISE(IGNORE); END"
        )
        c.commit()

        def write():
            c = db._get_conn()
            c.execute(
                "INSERT INTO trades (symbol, side, qty, price, pnl, timestamp) "
                "VALUES ('ACEUSDT', 'SELL', 27.8, 0.2286, 0.73, ?)",
                (time.time(),),
            )
            c.commit()

        def verify():
            row = db._get_conn().execute(
                "SELECT 1 FROM trades WHERE symbol='ACEUSDT' AND side='SELL'"
            ).fetchone()
            return row is not None

        with caplog.at_level(logging.ERROR):
            ok = db_write_with_verify(
                db, write, verify, "ensure_tp_sl[ACEUSDT] SELL trade INSERT",
                attempts=2, backoff_sec=0.01,
            )
        assert ok is False, "a vanished write must never report success"
        # loud: error log mentions silent write loss
        assert any("SILENT WRITE LOSS" in r.message for r in caplog.records)
        # loud: recorded to cron_failures.jsonl (monitoring channel)
        entries = _failures(failures_file)
        assert len(entries) == 1
        assert entries[0]["type"] == "db_write_failure"
        assert "SELL trade INSERT" in entries[0]["detail"] or "SELL trade INSERT" in entries[0]["job"]

    def test_raising_write_is_retried_then_escalated(self, db, failures_file):
        attempts = {"n": 0}

        def write():
            attempts["n"] += 1
            raise sqlite3.OperationalError("database is locked")

        ok = db_write_with_verify(
            db, write, lambda: True, "locked-write", attempts=3, backoff_sec=0.01
        )
        assert ok is False
        assert attempts["n"] == 3, "must retry the declared number of times"
        assert _failures(failures_file), "final failure must land in cron_failures.jsonl"

    def test_record_db_failure_writes_jsonl(self, failures_file):
        record_db_failure("unit-test-job", "boom")
        entries = _failures(failures_file)
        assert entries and entries[-1]["job"] == "unit-test-job"
        assert "timestamp" in entries[-1]


# ────────────────────────────────────────────────────────────
# 3. Cross-process lock contention (real subprocesses, WAL)
# ────────────────────────────────────────────────────────────

class TestCrossProcessContention:
    def test_write_succeeds_across_concurrent_lock_holder(self, tmp_path):
        """Regression for the 20:30 three-process collision: with WAL +
        busy_timeout, a write racing another process's open write
        transaction must either land (verified) or fail LOUDLY — never
        silently disappear."""
        db_path = tmp_path / "cross.db"
        StateDB(str(db_path)).close()  # create file + schema + WAL

        holder = subprocess.Popen(
            [sys.executable, "-c", f"""
import sqlite3, time
c = sqlite3.connect({str(db_path)!r}, timeout=30)
c.execute("PRAGMA busy_timeout=30000")
c.execute("BEGIN IMMEDIATE")
c.execute("INSERT INTO kv (key, value, updated_at) VALUES ('holder','h',1)")
print("HELD", flush=True)
time.sleep(3)
c.commit()
c.close()
"""], stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == "HELD"
            t0 = time.time()
            inst = StateDB(str(db_path))

            def write():
                c = inst._get_conn()
                c.execute(
                    "INSERT INTO trades (symbol, side, qty, price, pnl, timestamp) "
                    "VALUES ('XUSDT', 'SELL', 1, 1, 0, ?)", (time.time(),)
                )
                c.commit()

            def verify():
                row = inst._get_conn().execute(
                    "SELECT 1 FROM trades WHERE symbol='XUSDT'"
                ).fetchone()
                return row is not None

            ok = db_write_with_verify(
                inst, write, verify, "cross-proc write", attempts=3, backoff_sec=0.5
            )
            assert ok is True, "write must land once the holder commits (WAL/busy_timeout)"
            assert verify(), "read-back must see the row after contention"
            assert time.time() - t0 < 25, "must not hang past the busy_timeout budget"
        finally:
            holder.wait(timeout=30)


# ────────────────────────────────────────────────────────────
# 4. _save_state race guard (stale-snapshot bulldozer)
# ────────────────────────────────────────────────────────────

class TestSaveStateRaceGuard:
    def _seed(self, db):
        db.portfolio_set("ACEUSDT", {
            "quantity": 27.8, "entry_price": 0.2021, "strategy": "trend",
        })
        db.portfolio_set("TRXUSDT", {
            "quantity": 31.8, "entry_price": 0.3345, "strategy": "switch",
        })

    def test_no_resurrect_after_external_delete(self, db):
        """20:30 scenario: ensure_tp_sl (another process) deleted ACE after
        this manager loaded it. A later save here must NOT resurrect the row
        (old behavior resurrected 27.8 which then inflated to 79.8)."""
        self._seed(db)
        pm = TestPortfolio(db)
        pm._load_state_from_db()
        assert "ACEUSDT" in pm.positions

        db.portfolio_remove("ACEUSDT")  # external close (ensure_tp_sl)

        pm._save_state(force=True)
        rows = db.portfolio_get_all()
        assert "ACEUSDT" not in rows, "externally-closed row must not resurrect"
        assert "ACEUSDT" not in pm.positions, "stale in-memory row must be dropped"

    def test_no_clobber_after_external_update(self, db):
        """20:32 scenario: another process wrote qty=52 (new BUY); our stale
        snapshot still says 27.8. Saving must adopt 52, not overwrite it."""
        self._seed(db)
        pm = TestPortfolio(db)
        pm._load_state_from_db()
        time.sleep(0.01)

        # external writer bumps the row AFTER our snapshot
        db.portfolio_set("ACEUSDT", {
            "quantity": 52.0, "entry_price": 0.2228, "strategy": "trend",
        })
        stale_qty = pm.positions["ACEUSDT"]["quantity"]
        assert stale_qty == 27.8

        pm._save_state(force=True)
        assert db.portfolio_get_all()["ACEUSDT"]["quantity"] == pytest.approx(52.0), (
            "newer external write must not be clobbered by a stale snapshot"
        )

    def test_no_bulldoze_of_externally_added_row(self, db):
        """A row added by another process after our (empty) snapshot must
        survive our save — the old delete-loop removed any row not in
        memory."""
        pm = TestPortfolio(db)
        pm._load_state_from_db()  # loads nothing, stamps snapshot
        time.sleep(0.01)
        db.portfolio_set("NEWUSDT", {
            "quantity": 10, "entry_price": 1.0, "strategy": "trend",
        })
        pm._save_state(force=True)
        assert "NEWUSDT" in db.portfolio_get_all(), (
            "externally-added row must be protected from the bulldozer delete"
        )

    def test_own_close_still_deletes_promptly(self, db):
        """close_position() (tracked as OUR closure) must still remove the row
        even when its updated_at is newer than the load snapshot."""
        self._seed(db)
        pm = TestPortfolio(db)
        pm._load_state_from_db()
        pm.positions["ACEUSDT"]["quantity"] = 30.0  # our own post-load edit
        pm._save_state(force=True)                  # bumps DB updated_at

        # simulate close_position bookkeeping (pop + marker)
        pm.positions.pop("ACEUSDT")
        pm._closed_symbols = getattr(pm, "_closed_symbols", set()) | {"ACEUSDT"}
        pm._save_state(force=True)
        assert "ACEUSDT" not in db.portfolio_get_all(), (
            "positions closed by this manager must be removed without delay"
        )

    def test_stale_snapshot_save_does_not_double_count(self, db):
        """End-to-end bug#8 shape: stale manager holds 27.8; executor BUY of
        52 lands externally; stale save must NOT merge/overwrite to 79.8."""
        self._seed(db)
        stale = TestPortfolio(db)
        stale._load_state_from_db()
        time.sleep(0.01)

        # executor process: new BUY → DB row updated to 79.8? NO — the real
        # bug produced 79.8 because the stale save wrote qty 27.8+52. Here the
        # executor writes its OWN correct value first; stale save must keep it.
        db.portfolio_set("ACEUSDT", {
            "quantity": 52.0, "entry_price": 0.2228, "strategy": "trend",
        })
        stale.positions["ACEUSDT"]["quantity"] = 79.8  # poisoned stale view
        stale._save_state(force=True)
        assert db.portfolio_get_all()["ACEUSDT"]["quantity"] == pytest.approx(52.0), (
            "stale snapshot must not overwrite the executor's newer row"
        )
