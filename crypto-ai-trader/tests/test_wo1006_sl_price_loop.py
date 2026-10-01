"""WO-1006: protection_guardian SL rescue price loop.

Regression lock for the PENGUUSDT 10/1 bug: the TP-only rebuild used the
db stop verbatim; once the market falls through that stop (db 0.009725
vs px 0.0096) every OCO and every demoted plain SL is -2010-rejected
("Stop price would trigger immediately" / "relationship of the prices"),
so the branch degenerates into an endless cancel->reject->restore loop
(3+ rounds/hour, API weight 1423/1200).

Fix under test: the planned stop is clamped to the closest placeable
band (price * 0.93, floored by the 0.87 PERCENT_PRICE band) BEFORE any
cancel happens — guardian via _legalize_sl_price (TP-only rebuild +
oco_swap safety net), ensure_tp_sl via _legal_sl at its four db-SL
placement sites. One OCO rebuild then converges.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _clean_env(**extra):
    env = dict(os.environ)
    for k in ("DRYRUN", "DRYRUN_DB_PATH", "STATE_DB_PATH", "TESTING"):
        env.pop(k, None)
    env.update({k: str(v) for k, v in extra.items()})
    return env


# The PENGUUSDT 10/1 shape: TP-only book, db stop ABOVE the market.
RUNNER = r'''
import sys, json
from pathlib import Path
sys.path.insert(0, %r)
alerts = Path(%r); alerts.mkdir(parents=True, exist_ok=True)
import src.live_alerts as la
la.ALERT_DIR = alerts  # keep test alerts out of the live cloud dir
import src.protection_guardian as pg

class FakeClient:
    def __init__(self):
        self.calls = {"cancel": [], "place_oco": [], "limit_sell": [],
                      "stop_limit": []}
    def get_symbol_filters(self, sym):
        return {"tickSize": 1e-6, "stepSize": 1.0, "minQty": 1.0,
                "minNotional": 1.0}  # $4.11 book must clear the dust guard
    def get_open_orders(self, sym):
        # order_qty() reads origQty/quantity/qty — the raw-exchange
        # field names ccxt_client passes through, not ccxt's `amount`
        return [{"id": "tp1", "orderId": "tp1", "type": "LIMIT",
                 "side": "SELL", "price": 0.010851, "origQty": 428.0,
                 "status": "open", "info": {}}]
    def cancel_order(self, sym, oid):
        self.calls["cancel"].append(oid); return True
    def place_oco(self, sym, qty, tp_px, sl_px, sl_limit=None):
        self.calls["place_oco"].append(
            {"sym": sym, "qty": qty, "tp_px": tp_px, "sl_px": sl_px})
        return {"id": "oco1", "orderListId": 1, "status": "open"}
    def place_limit_sell(self, sym, qty, px):
        self.calls["limit_sell"].append({"qty": qty, "px": px})
        return {"id": "ls1", "status": "open"}
    def place_stop_loss_limit(self, sym, qty, limit_px, stop_px):
        self.calls["stop_limit"].append({"qty": qty, "stop": stop_px})
        return {"id": "sl1", "status": "open"}

class FakePortfolio:
    def get_all_positions(self):
        return [{"symbol": "PENGUUSDT", "quantity": 428.0,
                 "entry_price": 0.0102, "current_price": 0.0096,
                 "stop_loss": 0.00972515, "take_profit": 0.010851}]

cli = FakeClient()
summary = pg.run(cli, FakePortfolio())
print("RESULT_JSON:" + json.dumps({"summary": summary, "calls": cli.calls}))
'''


def _run_guardian(tmp, dryrun=False):
    alerts = tmp / "alerts"
    code = RUNNER % (str(REPO), str(alerts))
    live_db = tmp / "live.db"
    dry_db = tmp / "dry.db"
    # dry side: DRYRUN=1 keeps the paper_sim: audit tagging while the
    # non-default tmp STATE_DB_PATH passes through resolve_dryrun_candidate
    # (test isolation) — same combination v3's twin test asserts.
    env = _clean_env(STATE_DB_PATH=(dry_db if dryrun else live_db))
    if dryrun:
        env.update(DRYRUN="1")
    cp = subprocess.run([sys.executable, "-c", code], capture_output=True,
                        text=True, env=env, cwd=str(tmp), timeout=120)
    assert cp.returncode == 0, cp.stderr
    m = re.search(r"RESULT_JSON:(\{.*\})", cp.stdout, re.S)
    assert m, cp.stdout + cp.stderr
    out = json.loads(m.group(1))
    dbs = {}
    for tag, path in (("live", live_db), ("dry", dry_db)):
        if path.exists():
            conn = sqlite3.connect(str(path))
            dbs[tag] = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN source LIKE 'paper_sim:%%'"
                " THEN 1 ELSE 0 END) FROM audit_log"
                " WHERE (action || ' ' || COALESCE(details,''))"
                " LIKE '%%GUARDIAN_SL_ADAPTED%%'").fetchone()
            conn.close()
    return out, dbs


# ── 1. clamp matrix: the PENGU numbers, boundaries, degenerates ──

def test_legalize_matrix_subprocess(tmp_path):
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import json\n"
        "from src.protection_guardian import _legalize_sl_price as f\n"
        "print('J:' + json.dumps([\n"
        "  f(0.00972515, 0.0096, 1e-6),   # fallen-through -> 0.93 band\n"
        "  f(0.0090,      0.0096, 1e-6),  # placeable stays\n"
        "  f(0.00955,     0.0096, 1e-6),  # just under headroom stays\n"
        "  f(0.009553,    0.0096, 1e-6),  # just over headroom clamps\n"
        "  f(0.05,        0.0096, 1e-6),  # absurd -> clamp, band floor ok\n"
        "  f(0.0,         0.0096, 1e-6),  # degenerate passthrough\n"
        "  f(0.009725,    0.0,    1e-6),  # degenerate passthrough\n"
        "]))\n" % str(REPO))
    cp = subprocess.run([sys.executable, "-c", code], capture_output=True,
                        text=True, env=_clean_env(), cwd=str(tmp_path),
                        timeout=60)
    assert cp.returncode == 0, cp.stderr
    vals = json.loads(re.search(r"J:(\[.*\])", cp.stdout).group(1))
    assert vals[0] == 0.008928          # PENGU 10/1: exact adapted stop
    assert vals[1] == 0.0090
    assert vals[2] == 0.00955           # < price * 0.995 untouched
    assert vals[3] == 0.008928
    assert vals[4] == 0.008928
    assert vals[5] == 0.0
    assert vals[6] == 0.009725
    assert 0.008928 >= 0.87 * 0.0096 - 1e-12  # inside PERCENT_PRICE band


# ── 2. convergence: the loop shape now heals in ONE pass ──

def test_tponly_rebuild_converges_one_pass(tmp_path):
    out, dbs = _run_guardian(tmp_path, dryrun=False)
    assert out["summary"] == {"checked": 1, "healed": 1, "failed": 0,
                              "skipped": 0}
    calls = out["calls"]
    # exactly one OCO, at the CLAMPED stop — never the fallen-through db stop
    assert len(calls["place_oco"]) == 1
    assert calls["place_oco"][0]["sl_px"] == 0.008928
    assert calls["place_oco"][0]["tp_px"] == pytest.approx(0.010851)
    # the TP leg is cancelled exactly once and STAYS cancelled: no more
    # restore-replace churn (the loop's signature was limit_sell + cancel
    # every round)
    assert calls["cancel"] == ["tp1"]
    assert calls["limit_sell"] == []     # no demote ladder, no TP restore
    assert calls["stop_limit"] == []     # no plain-SL -2010 attempt
    # adaptation is audited on the live-side db
    assert dbs["live"][0] >= 1
    assert dbs["live"][1] == 0           # no paper_sim: rows on live side


# ── 3. dry-run side: identical clamp behaviour, audited into the twin ──

def test_dryrun_side_parity_and_audit_twin(tmp_path):
    out, dbs = _run_guardian(tmp_path, dryrun=True)
    assert out["summary"]["healed"] == 1
    assert out["calls"]["place_oco"][0]["sl_px"] == 0.008928  # same clamp
    assert out["calls"]["cancel"] == ["tp1"]
    # adapted-stop audit lands in the dry twin with paper_sim: source
    assert dbs["dry"][0] >= 1
    assert dbs["dry"][1] >= 1
    # live db was never created — nothing leaked off the dry round
    assert "live" not in dbs


# ── 4. structure locks: both call sites + ensure_tp_sl twin clamps ──

def test_guardian_clamp_sites_structural_lock():
    src = (REPO / "src" / "protection_guardian.py").read_text()
    assert "def _legalize_sl_price(" in src
    assert 'context="tp_oco_rebuild"' in src   # TP-only rebuild, pre-cancel
    assert 'context="oco_swap"' in src         # swap safety net
    # clamp sits BEFORE the band check, so the band check can never see an
    # unplaceable stop again
    tp = src.index('context="tp_oco_rebuild"')
    band = src.index("if sl_px_new < price * 0.87", tp)
    assert 0 < band

def test_ensure_tp_sl_clamps_structural_lock():
    src = (REPO / "scripts" / "ensure_tp_sl.py").read_text()
    assert "def _legal_sl(" in src
    # all four db-SL placement sites clamp against the live price
    assert src.count("= _legal_sl(") == 4
    # helper itself keeps the 0.93 / 0.87 / 0.995 contract
    assert "price * 0.93" in src and "price * 0.87" in src
    assert "price * 0.995" in src


# ── 5. ensure_tp_sl._legal_sl matrix (script-side twin) ──

def test_ensure_legal_sl_matrix(tmp_path):
    code = (
        "import sys, json\n"
        "sys.path.insert(0, %r)\n"
        "sys.path.insert(0, %r)\n"
        "from ensure_tp_sl import _legal_sl as f\n"
        "print('J:' + json.dumps([\n"
        "  f(0.00972515, 0.0096, 1e-6, 'PENGUUSDT'),\n"
        "  f(0.0090, 0.0096, 1e-6, 'X'),\n"
        "  f(0.0, 0.0096, 1e-6, 'X'),\n"
        "]))\n" % (str(REPO / "scripts"), str(REPO)))
    cp = subprocess.run([sys.executable, "-c", code], capture_output=True,
                        text=True, env=_clean_env(), cwd=str(tmp_path),
                        timeout=60)
    assert cp.returncode == 0, cp.stderr
    vals = json.loads(re.search(r"J:(\[.*\])", cp.stdout).group(1))
    assert vals[0] == 0.008928
    assert vals[1] == 0.0090
    assert vals[2] == 0.0
    assert "[WO-1006] PENGUUSDT" in cp.stdout  # adaptation is visible
