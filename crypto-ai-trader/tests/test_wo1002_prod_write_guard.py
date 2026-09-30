"""WO-1002: fail-closed production write guard (connection layer).

Incident 2026-09-30 12:39 — a heredoc verification script (no conftest, no
TESTING/STATE_DB_PATH env) resolved get_state_db() straight to the production
DB and INSERTed a fake "XUSDT / strategy='test'" position (entry_price=100.0,
qty=10). pytest's four-layer conftest isolation was intact; the hole was that
guards only protect processes that opt in. The guard now lives on the sqlite
Connection factory (_GuardedConnection): every DML against the production
path requires TESTING unset + PROD_WRITES_ALLOWED=1.
"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import src.state_db as sd_mod
from src.state_db import StateDB

PROJECT_ROOT = Path(__file__).parent.parent
PROD_DB = "/root/trading-state/state.db"

INSERT_SQL = (
    "INSERT OR REPLACE INTO portfolio (symbol, quantity, entry_price, "
    "strategy, opened_at) VALUES ('GUARDPROBE', 1, 1, 'test', 1)"
)


def _fake_prod(tmp_path):
    """Create a throwaway DB that the guard treats as the production path."""
    p = tmp_path / "fake_prod.db"
    StateDB(str(p))  # initialise schema (tmp path: no guard involved)
    return p


def test_heredoc_subprocess_write_blocked_and_prod_stays_clean():
    """Golden reproduction: subprocess with a clean env (the exact heredoc
    conditions of the incident) must NOT be able to write the real prod DB."""
    code = (
        "from src.state_db import get_state_db\n"
        "db = get_state_db()\n"
        f"conn = db._get_conn()\n"
        f"conn.execute({INSERT_SQL!r})\n"
        "conn.commit()\n"
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("TESTING", "STATE_DB_PATH", "PROD_WRITES_ALLOWED")}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, cwd=str(PROJECT_ROOT), env=env)
    assert r.returncode != 0, "unauthorised write went through!"
    assert "WO-1002" in r.stderr and "BLOCKED" in r.stderr
    # Real production DB must remain untouched (deployment-specific path).
    if os.path.exists(PROD_DB):
        conn = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
        n = conn.execute(
            "SELECT COUNT(*) FROM portfolio WHERE symbol='GUARDPROBE'"
        ).fetchone()[0]
        conn.close()
        assert n == 0


def test_testing_env_blocks_even_with_authorisation(tmp_path, monkeypatch):
    """TESTING=1 outranks PROD_WRITES_ALLOWED: tests never write prod."""
    fake = _fake_prod(tmp_path)
    monkeypatch.setattr(sd_mod, "DEFAULT_DB_PATH", str(fake))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("PROD_WRITES_ALLOWED", "1")
    db = StateDB(str(fake))
    with pytest.raises(RuntimeError, match="TESTING"):
        db._get_conn().execute(INSERT_SQL)
    with pytest.raises(RuntimeError, match="TESTING"):
        db.kv_set("wo1002:probe", "x")  # method-level writes guarded too


def test_unauthorised_write_blocked_without_testing(tmp_path, monkeypatch):
    """No TESTING, no PROD_WRITES_ALLOWED (interactive/debug shells)."""
    fake = _fake_prod(tmp_path)
    monkeypatch.setattr(sd_mod, "DEFAULT_DB_PATH", str(fake))
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.delenv("PROD_WRITES_ALLOWED", raising=False)
    db = StateDB(str(fake))
    with pytest.raises(RuntimeError, match="PROD_WRITES_ALLOWED"):
        db._get_conn().execute(INSERT_SQL)


def test_readonly_inspection_still_works_unauthorised(tmp_path, monkeypatch):
    """Read-only prod inspection (the legitimate heredoc use-case) passes."""
    fake = _fake_prod(tmp_path)
    monkeypatch.setattr(sd_mod, "DEFAULT_DB_PATH", str(fake))
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.delenv("PROD_WRITES_ALLOWED", raising=False)
    db = StateDB(str(fake))
    n = db._get_conn().execute(
        "SELECT COUNT(*) FROM portfolio").fetchone()[0]
    assert n == 0


def test_authorised_prod_write_succeeds(tmp_path, monkeypatch):
    """PROD_WRITES_ALLOWED=1 without TESTING authorises the write (the
    reside_scan / run_cron.sh `source .env` production path)."""
    fake = _fake_prod(tmp_path)
    monkeypatch.setattr(sd_mod, "DEFAULT_DB_PATH", str(fake))
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setenv("PROD_WRITES_ALLOWED", "1")
    db = StateDB(str(fake))
    db._get_conn().execute(INSERT_SQL)
    db._get_conn().commit()
    n = db._get_conn().execute(
        "SELECT COUNT(*) FROM portfolio WHERE symbol='GUARDPROBE'"
    ).fetchone()[0]
    assert n == 1


def test_executemany_and_script_dml_guarded(tmp_path, monkeypatch):
    fake = _fake_prod(tmp_path)
    monkeypatch.setattr(sd_mod, "DEFAULT_DB_PATH", str(fake))
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.delenv("PROD_WRITES_ALLOWED", raising=False)
    db = StateDB(str(fake))
    with pytest.raises(RuntimeError, match="PROD_WRITES_ALLOWED"):
        db._get_conn().executemany(
            INSERT_SQL, [("a",), ("b",)])
    with pytest.raises(RuntimeError, match="PROD_WRITES_ALLOWED"):
        db._get_conn().executescript(
            f"BEGIN; {INSERT_SQL}; COMMIT;")
    # pure DDL script passes (StateDB.__init__ on read-only inspections)
    db._get_conn().executescript(
        "CREATE TABLE IF NOT EXISTS _wo1002_probe (k TEXT);")


def test_tmp_paths_completely_unaffected(tmp_path):
    """Normal test databases (conftest STATE_DB_PATH / explicit tmp) are
    outside the guard entirely — the whole existing suite relies on this."""
    db = StateDB(str(tmp_path / "free.db"))
    db._get_conn().execute(INSERT_SQL)
    db._get_conn().commit()
    db.kv_set("wo1002:probe", "ok")
    assert db.kv_get("wo1002:probe") == "ok"
