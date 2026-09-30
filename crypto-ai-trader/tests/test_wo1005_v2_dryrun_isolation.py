"""WO-1005 v2: DRYRUN isolation enforced at the StateDB constructor.

The 18:11 write-through: reside_scan._load_repo_env() injected
.env's STATE_DB_PATH=/root/trading-state/state.db into the DRYRUN
process env, which short-circuited the get_state_db-level guard
(`not os.environ.get("STATE_DB_PATH")`), so the entire dry-run chain
(sync / reconciler / ledger / config_guard) resolved to LIVE state.db.
The fix moves the fail-safe into StateDB.__init__ — the single choke
point every path passes through.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
PY = sys.executable
LIVE = "/root/trading-state/state.db"


def _run(code, env_add=None, drop=("DRYRUN", "STATE_DB_PATH", "TESTING")):
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(env_add or {})
    return subprocess.run([PY, "-c", code], cwd=REPO, capture_output=True,
                          text=True, timeout=60, env=env)


def test_bare_ctor_resolves_dryrun():
    r = _run(
        "import sys; sys.path.insert(0,'.')\n"
        "from src.state_db import StateDB\n"
        "print(str(StateDB().db_path))", {"DRYRUN": "1"})
    assert r.returncode == 0, r.stderr
    assert "dryrun.db" in r.stdout


def test_env_path_pointing_at_live_is_overruled():
    """The exact 18:11 scenario: .env injected STATE_DB_PATH=live."""
    r = _run(
        "import sys; sys.path.insert(0,'.')\n"
        "from src.state_db import get_state_db\n"
        "print(str(get_state_db().db_path))",
        {"DRYRUN": "1", "STATE_DB_PATH": LIVE})
    assert r.returncode == 0, r.stderr
    assert "dryrun.db" in r.stdout, r.stdout


def test_explicit_live_param_is_overruled():
    r = _run(
        "import sys; sys.path.insert(0,'.')\n"
        "from src.state_db import StateDB\n"
        f"print(str(StateDB({LIVE!r}).db_path))", {"DRYRUN": "1"})
    assert r.returncode == 0, r.stderr
    assert "dryrun.db" in r.stdout


def test_nondefault_explicit_path_respected():
    """Test-isolation tmp paths stay honoured under DRYRUN."""
    r = _run(
        "import sys, tempfile; sys.path.insert(0,'.')\n"
        "from src.state_db import StateDB\n"
        "tmp = tempfile.mktemp(suffix='.db')\n"
        "print(str(StateDB(tmp).db_path))", {"DRYRUN": "1"})
    assert r.returncode == 0, r.stderr
    assert ".db" in r.stdout and "state.db" not in r.stdout
    assert "dryrun.db" not in r.stdout


def test_no_dryrun_unchanged():
    r = _run(
        "import sys; sys.path.insert(0,'.')\n"
        "from src.state_db import StateDB\n"
        "print(str(StateDB().db_path))")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("state.db")


def test_state_db_path_env_still_wins_without_dryrun():
    """Env override semantics preserved when DRYRUN is unset (get_state_db
    layer keeps its STATE_DB_PATH handling; bare StateDB() never read
    env — that behaviour predates WO-1005 and is unchanged)."""
    r = _run(
        "import sys; sys.path.insert(0,'.')\n"
        "from src.state_db import get_state_db\n"
        "print(str(get_state_db().db_path))", {"STATE_DB_PATH": "/tmp/x.db"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "/tmp/x.db"


def test_paper_trader_has_ccxt_shaped_get_my_trades(db):
    """Reconciler duck-typing: isBuyer/orderId/time/price/qty present."""
    from src.paper_trader import PaperTrader
    from src.state_db import get_state_db
    d = get_state_db()
    conn = d._get_conn()
    conn.execute(
        "INSERT INTO paper_trades (id, symbol, side, order_type,"
        " quantity, fill_price, slippage_pct, fee_usdt, notional_usdt,"
        " status, timestamp) VALUES"
        " ('t-1','BTCUSDT','BUY','MARKET',1.0,100.0,0.0,0.075,100.0,"
        "  'filled', 1000000.0)")
    conn.commit()
    pt = object.__new__(PaperTrader)
    pt._db = d
    pt._in_transaction = False
    fills = pt.get_my_trades("BTCUSDT", limit=10)
    assert len(fills) == 1
    f = fills[0]
    assert f["isBuyer"] is True
    assert f["symbol"] == "BTCUSDT"
    assert f["price"] == 100.0 and f["qty"] == 1.0
    assert int(f["orderId"]) > 0 and int(f["time"]) > 0
    assert f["fee"]["cost"] == pytest.approx(0.075)


def test_audit_source_tagged_paper_sim_under_dryrun(tmp_path):
    """WO-1005 followup: DRYRUN audit rows carry a paper_sim: prefix so
    simulated actions stay distinguishable in forensics; live rows and
    travis_ops markers pass through untouched."""
    code = (
        "import sys, tempfile; sys.path.insert(0,'.')\n"
        "from src.state_db import StateDB\n"
        "db = StateDB(tempfile.mktemp(suffix='.db'))\n"
        "db.audit_log('T','d',source='binance_api')\n"
        "db.audit_log('T','d',source='travis_ops')\n"
        "print([r[0] for r in db._get_conn().execute("
        "'SELECT source FROM audit_log').fetchall()])")
    env = {k: v for k, v in os.environ.items()
           if k not in ("DRYRUN", "STATE_DB_PATH", "TESTING")}
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                       capture_output=True, text=True, timeout=60,
                       env={**env, "DRYRUN": "1"})
    assert r.returncode == 0, r.stderr
    assert "paper_sim:binance_api" in r.stdout
    assert "'travis_ops'" in r.stdout
    # live semantics untouched
    r2 = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                        capture_output=True, text=True, timeout=60,
                        env=env)
    assert r2.returncode == 0, r2.stderr
    assert "paper_sim" not in r2.stdout


def test_sync_branch_source_pins_dryrun_guard():
    """Structure lock: scan_phases sync gate checks _is_dryrun()."""
    src = (REPO / "src" / "scan_phases.py").read_text()
    assert "not is_paper_mode() and not _is_dryrun():" in src


@pytest.fixture
def db():
    from src.state_db import get_state_db
    yield get_state_db()
