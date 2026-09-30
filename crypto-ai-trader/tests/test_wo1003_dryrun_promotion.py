"""WO-1003-5: dry-run parity + three-stage promotion.

Covers: DRYRUN db path isolation, forced paper client, TAKER_FEE
fallback, notification muting, staged-first param reads, verify-dryrun
criteria matrix and the rollback path.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.state_db import DRYRUN_DB_PATH

REPO = Path(__file__).parent.parent
PY = sys.executable


# ── 1) db path isolation (subprocess: module-level env reads) ───────

def test_dryrun_env_resolves_dryrun_db(tmp_path):
    code = (
        "import os; os.environ['DRYRUN']='1'\n"
        "import sys; sys.path.insert(0, '.')\n"
        "from src.state_db import get_state_db, DRYRUN_DB_PATH\n"
        "db = get_state_db()\n"
        "print(str(db.db_path))\n"
    )
    r = subprocess.run([PY, "-c", code], cwd=REPO, capture_output=True,
                       text=True, timeout=60,
                       env={**os.environ, "DRYRUN": "1",
                            "TESTING": "", "STATE_DB_PATH": ""})
    assert r.returncode == 0, r.stderr
    assert str(DRYRUN_DB_PATH) in r.stdout


def test_explicit_path_wins_over_dryrun(tmp_path):
    target = tmp_path / "explicit.db"
    code = (
        "import sys; sys.path.insert(0, '.')\n"
        "from src.state_db import get_state_db\n"
        f"db = get_state_db({str(target)!r})\n"
        "print(str(db.db_path))\n"
    )
    r = subprocess.run([PY, "-c", code], cwd=REPO, capture_output=True,
                       text=True, timeout=60,
                       env={**os.environ, "DRYRUN": "1",
                            "TESTING": "", "STATE_DB_PATH": ""})
    assert r.returncode == 0, r.stderr
    assert str(target) in r.stdout


# ── 2) fee parity + forced paper client + seed (subprocess) ─────────

def test_paper_fee_falls_back_to_taker_fee():
    code = (
        "import sys; sys.path.insert(0, '.')\n"
        "import src.paper_trader as pt\n"
        "from src.backtest import TAKER_FEE_RATE\n"
        "assert pt.PAPER_FEE_RATE == TAKER_FEE_RATE, pt.PAPER_FEE_RATE\n"
        "assert pt.DRYRUN_INITIAL_BALANCE == 400.0\n"
        "from src.paper_trader import get_trading_client, PaperTrader\n"
        "os_env = __import__('os').environ\n"
        "os_env['DRYRUN'] = '1'\n"
        "cli = get_trading_client()\n"
        "assert isinstance(cli, PaperTrader), type(cli)\n"
        "print('OK')\n"
    )
    r = subprocess.run([PY, "-c", code], cwd=REPO, capture_output=True,
                       text=True, timeout=60,
                       env={k: v for k, v in os.environ.items()
                       if k not in ("DRYRUN", "PAPER_FEE_RATE", "TESTING",
                                    "STATE_DB_PATH", "TRADING_MODE")})
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_explicit_fee_env_still_wins():
    r = subprocess.run(
        [PY, "-c",
         "import sys; sys.path.insert(0, '.')\n"
         "import src.paper_trader as pt\n"
         "assert pt.PAPER_FEE_RATE == 0.002, pt.PAPER_FEE_RATE\n"
         "print('OK')"],
        cwd=REPO, capture_output=True, text=True, timeout=60,
        env={**{k: v for k, v in os.environ.items()
                if k not in ("TESTING", "STATE_DB_PATH", "DRYRUN")},
             "PAPER_FEE_RATE": "0.002"})
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


# ── 3) notification muting (in-process, DRYRUN toggled) ─────────────

def test_notifications_muted_under_dryrun(tmp_path, monkeypatch):
    monkeypatch.setenv("DRYRUN", "1")
    import src.notifier as nf
    from src.state_db import get_state_db

    signals = REPO / "signals"
    nf.NOTIFICATIONS_FILE = tmp_path / "pending_notifications.json"
    nf.SIGNALS_FILE = tmp_path / "signals"
    before = list(signals.glob("pending_notifications.json"))
    nf._append_notification("test", "dryrun title", "dryrun body")
    assert not nf.NOTIFICATIONS_FILE.exists()  # JSON pipe untouched
    nf.send_message("t", "b")                  # must not raise, no file
    assert not (tmp_path / "messages.json").exists()
    # outbox DID record (dryrun db in tests = isolated STATE_DB_PATH)
    rows = get_state_db()._get_conn().execute(
        "SELECT COUNT(*) FROM notification_outbox WHERE title='dryrun title'"
    ).fetchone()
    assert rows[0] >= 1
    monkeypatch.delenv("DRYRUN")


def test_notifications_flow_when_not_dryrun(tmp_path, monkeypatch):
    monkeypatch.delenv("DRYRUN", raising=False)
    import src.notifier as nf
    nf.NOTIFICATIONS_FILE = tmp_path / "pending_notifications.json"
    nf.SIGNALS_FILE = tmp_path / "signals"
    nf._ensure_signals_dir()
    nf._append_notification("test", "live title", "live body")
    data = json.loads((tmp_path / "pending_notifications.json").read_text())
    assert any(n["title"] == "live title" for n in data)


# ── 4) staged-first param read ──────────────────────────────────────

def test_strategy_registry_reads_staged_in_dryrun(db, monkeypatch):
    from src.strategy_registry import StrategyRegistry
    db.kv_set("optimized_params", {"score_threshold": 50})
    db.kv_set("optimized_params_staged",
              {"params": {"score_threshold": 65}})
    reg = StrategyRegistry()
    monkeypatch.delenv("DRYRUN", raising=False)
    assert reg._get_optimized_params() == {"score_threshold": 50}
    monkeypatch.setenv("DRYRUN", "1")
    assert reg._get_optimized_params() == {"score_threshold": 65}
    # fallback: no staged record -> live params
    db.kv_remove("optimized_params_staged")
    assert reg._get_optimized_params() == {"score_threshold": 50}


# ── 5) reside_scan artefact separation (subprocess) ─────────────────

def test_reside_scan_paths_split_under_dryrun():
    code = (
        "import os; os.environ['DRYRUN']='1'\n"
        "import importlib, sys; sys.path.insert(0, '.')\n"
        "import scripts.reside_scan as rs\n"
        "print(rs.GATE_FILE); print(rs.LIVE_DIR); print(rs.DB)\n"
    )
    r = subprocess.run([PY, "-c", code], cwd=REPO, capture_output=True,
                       text=True, timeout=60,
                       env={**os.environ, "DRYRUN": "1",
                            "TESTING": "", "STATE_DB_PATH": ""})
    assert r.returncode == 0, r.stderr
    assert ".dryrun_scan_gate" in r.stdout
    assert "live_scan_dryrun" in r.stdout
    assert "dryrun.db" in r.stdout
    assert "state.db" not in r.stdout.splitlines()[2].strip()


# ── 6) verify-dryrun criteria matrix ────────────────────────────────

def _mk_dryrun_db(path, trades, seed_ts=0):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE paper_trades (id TEXT PRIMARY KEY, symbol TEXT, "
        "side TEXT, order_type TEXT, quantity REAL, fill_price REAL, "
        "slippage_pct REAL, fee_usdt REAL, notional_usdt REAL, "
        "status TEXT, timestamp REAL, details TEXT);")
    for i, (sym, side, qty, px, ts) in enumerate(trades):
        conn.execute(
            "INSERT INTO paper_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"t{i}", sym, side, "MARKET", qty, px, 0.0,
             qty * px * 0.00075, qty * px, "filled", ts, None))
    conn.commit()
    conn.close()


def _use_dry_db(tmp_path, monkeypatch, trades):
    dry_db = tmp_path / "dryrun.db"
    _mk_dryrun_db(dry_db, trades)
    monkeypatch.setattr("src.state_db.DRYRUN_DB_PATH", dry_db)
    return dry_db


def _staged(db, ts_offset_days=-8):
    db.kv_set("optimized_params_staged", {
        "params": {"score_threshold": 65},
        "validation": {"validated": True},
        "dry_run_verified": False,
        "staged_at": time.time() + ts_offset_days * 86400,
    })


def test_verify_dryrun_all_pass_marks_verified(db, tmp_path, monkeypatch):
    import scripts.hyperopt as hy
    _staged(db)
    ts = time.time() - 7 * 86400
    trades = []
    for i in range(3):  # 3 completed lifecycles, small win each
        trades.append((f"S{i}USDT", "BUY", 1.0, 100.0, ts + i * 1000))
        trades.append((f"S{i}USDT", "SELL", 1.0, 102.0, ts + i * 1000 + 60))
    _use_dry_db(tmp_path, monkeypatch, trades)
    res = hy._verify_dryrun(_mk_opt(db), _mk_args())
    assert res["status"] == "verified" and res["marked"]
    staged = db.kv_get("optimized_params_staged")
    assert staged["dry_run_verified"] is True


def test_verify_dryrun_rejects_short_window(db, tmp_path, monkeypatch):
    import scripts.hyperopt as hy
    _staged(db, ts_offset_days=-2)          # window not served yet
    _use_dry_db(tmp_path, monkeypatch, [])
    res = hy._verify_dryrun(_mk_opt(db), _mk_args())
    assert res["status"] == "not_ready"
    assert res["criteria"]["window_days"]["pass"] is False
    assert not db.kv_get("optimized_params_staged").get(
        "dry_run_verified")


def test_verify_dryrun_rejects_too_few_lifecycles(db, tmp_path, monkeypatch):
    import scripts.hyperopt as hy
    _staged(db)
    ts = time.time() - 7 * 86400
    trades = [("S0USDT", "BUY", 1.0, 100.0, ts),
              ("S0USDT", "SELL", 1.0, 102.0, ts + 60)]  # only 1 cycle
    _use_dry_db(tmp_path, monkeypatch, trades)
    res = hy._verify_dryrun(_mk_opt(db), _mk_args())
    assert res["status"] == "not_ready"
    assert res["criteria"]["lifecycles"]["actual"] == 1


def test_verify_dryrun_rejects_catastrophic_loss(db, tmp_path, monkeypatch):
    import scripts.hyperopt as hy
    _staged(db)
    ts = time.time() - 7 * 86400
    trades = []
    for i in range(4):   # 4 lifecycles, -3% each ≈ -12% net → reject
        trades.append((f"S{i}USDT", "BUY", 1.0, 100.0, ts + i * 100))
        trades.append((f"S{i}USDT", "SELL", 1.0, 97.0, ts + i * 100 + 60))
    _use_dry_db(tmp_path, monkeypatch, trades)
    res = hy._verify_dryrun(_mk_opt(db), _mk_args())
    assert res["status"] == "not_ready"
    assert res["criteria"]["net_return_pct"]["pass"] is False


def _mk_opt(db):
    from src.param_optimizer import ParamOptimizer
    return ParamOptimizer(db=db)


def _mk_args():
    import argparse
    return argparse.Namespace(dryrun_window_days=7, min_lifecycles=3)


# ── 7) rollback path ────────────────────────────────────────────────

def test_rollback_restores_previous_live_params(db):
    from src.param_optimizer import DEFAULT_PARAMS
    opt = _mk_opt(db)
    prev = {**DEFAULT_PARAMS, "score_threshold": 50}  # full set: live kv
    db.kv_set("optimized_params", prev)
    db.kv_set("optimized_params_staged", {
        "params": {"score_threshold": 65},
        "validation": {"validated": True},
        "dry_run_verified": True,
    })
    import scripts.hyperopt as hy
    promoted = opt.promote_staged_params()
    assert promoted["status"] == "promoted"
    assert db.kv_get("optimized_params") == {"score_threshold": 65}
    res = hy._rollback_live_params(opt)
    assert res["status"] == "rolled_back"
    assert db.kv_get("optimized_params") == prev  # full snapshot restored
    hist = db.kv_get("hyperopt:history")
    assert any(e.get("event") == "rollback" for e in hist)


def test_rollback_without_snapshot_reports_cleanly(db):
    import scripts.hyperopt as hy
    db.kv_set("hyperopt:history", [])
    res = hy._rollback_live_params(_mk_opt(db))
    assert res["status"] == "no_snapshot"


@pytest.fixture
def db():
    from src.state_db import get_state_db
    d = get_state_db()
    yield d
