"""WO-1007: scan crash when the strategy config source is missing.

10/2 09:10 chain: evolver vetoed the last ON strategy (bollinger,
dual-window PF<1) -> enabled list empty -> registry block skipped ->
strategy stayed the literal "score_based" default ->
adapted["strategies"].get() returned None -> the dead
notifier.get_strategy_config() branch (method never existed anywhere,
sole reference) raised AttributeError and killed the whole cron-scan
(rc=1).

Fix under test: three-way split — real cfg wins; missing cfg but some
strategy enabled proceeds on safe FIX-11 defaults; every strategy
disabled blocks the trade and lets the scan degrade gracefully
(rc=0). The sibling dead reference scan_orchestrator.send_market_scan
(never-defined method on the manual --notify path) is replaced by a
real _fmt_market_scan_msg + send() composition.
"""
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _src(rel):
    return (REPO / rel).read_text()


# ── 1. dead references are gone from the codebase ──

def test_dead_notifier_references_removed():
    rp = _src("src/research_phase.py")
    so = _src("src/scan_orchestrator.py")
    # no CALL remains (comments may still mention the dead name)
    assert not re.search(r"notifier\.get_strategy_config\(", rp)
    assert not re.search(r"\.send_market_scan\(", so)
    # FeishuNotifier import is retained ONLY as the e2e patch anchor
    # and is annotated as such
    assert "from src.notifier import FeishuNotifier, _append_notification" in rp
    assert "e2e test" in rp

def test_no_other_dead_notifier_calls_remain():
    # every notifier.<attr> call site must map to a real notifier.py
    # symbol (module function or FeishuNotifier method)
    defined = set(re.findall(r"(?m)^def (\w+)", _src("src/notifier.py")))
    defined |= set(re.findall(r"(?m)^    def (\w+)", _src("src/notifier.py")))
    # only live CALL SITES count: strip comment lines first so
    # historical mentions in WO-1007 notes do not read as calls
    import io
    called = set()
    for f in list(REPO.glob("src/*.py")) + list(REPO.glob("scripts/*.py")):
        if f.name == "notifier.py":
            continue
        for line in f.read_text().splitlines():
            code = line.lstrip()
            if code.startswith("#") or code.startswith('"""'):
                continue
            called |= set(re.findall(r"notifier\.(\w+)\(", line))
    called -= {"Notifier"}
    missing = {c for c in called if c not in defined and not c.startswith("_")}
    assert not missing, f"dead notifier references: {missing}"


# ── 2. the three-way branch is wired as specified ──

def test_branch_structure_lock():
    rp = _src("src/research_phase.py")
    assert "def _any_strategy_enabled(" in rp
    assert "elif _any_strategy_enabled(adapted):" in rp
    # safe-defaults branch keeps the FIX-11 semantics
    assert "stop_loss_pct = 4.0" in rp
    # all-disabled branch blocks the trade, does not raise
    assert "ALL_STRATEGIES_DISABLED:" in rp
    assert rp.count("decision=\"BLOCKED\"") >= 2  # registry-fail + all-disabled
    so = _src("src/scan_orchestrator.py")
    assert "def _fmt_market_scan_msg(" in so
    assert "notifier.send(title, body)" in so


# ── 3. _any_strategy_enabled matrix (the gate that routes the branch) ──

def test_any_strategy_enabled_matrix(tmp_path):
    code = (
        "import sys, json\n"
        "sys.path.insert(0, %r)\n"
        "from src.research_phase import _any_strategy_enabled as f\n"
        "cases = [\n"
        "    ({'strategies': {}}, False),\n"
        "    ({'strategies': {'a': {'enabled': False}}}, False),\n"
        "    ({'strategies': {'a': {'enabled': False},"
        " 'b': {'enabled': True}}}, True),\n"
        "    ({}, False),\n"
        "    ({'strategies': {'a': None, 'b': 'junk'}}, False),\n"
        "    ({'strategies': {'a': {'enabled': 1}}}, True),\n"
        "    # no explicit flag counts as ON (matches the enabled-list\n"
        "    # build at the registry call site)\n"
        "    ({'strategies': {'a': {'size_multiplier': 1.0}}}, True),\n"
        "]\n"
        "print('J:' + json.dumps([f(a) == b for a, b in cases]))\n"
        % str(REPO))
    cp = subprocess.run([sys.executable, "-c", code], capture_output=True,
                        text=True, cwd=str(tmp_path), timeout=60)
    assert cp.returncode == 0, cp.stderr
    results = json.loads(re.search(r"J:(\[.*\])", cp.stdout).group(1)) \
        if False else __import__("json").loads(
            re.search(r"J:(\[.*\])", cp.stdout).group(1))
    assert all(results), results


# ── 4. _fmt_market_scan_msg composes a real message ──

def test_fmt_market_scan_msg(tmp_path):
    code = (
        "import sys, json\n"
        "sys.path.insert(0, %r)\n"
        "from src.scan_orchestrator import _fmt_market_scan_msg as f\n"
        "title, body = f(\n"
        "    [{'symbol': 'AAVEUSDT', 'score': 83.0, 'signals': ['whale']}],\n"
        "    [{'symbol': 'XRPUSDT', 'change_pct': 5.5}],\n"
        "    [{'symbol': 'SOLUSDT', 'change_pct': -3.2}])\n"
        "print('J:' + json.dumps({'t': title, 'b': body}))\n"
        % str(REPO))
    cp = subprocess.run([sys.executable, "-c", code], capture_output=True,
                        text=True, cwd=str(tmp_path), timeout=60)
    assert cp.returncode == 0, cp.stderr
    out = __import__("json").loads(
        re.search(r"J:(\{.*\})", cp.stdout).group(1))
    assert "1 opportunities" in out["t"]
    assert "📈 XRPUSDT +5.50%" in out["b"]
    assert "📉 SOLUSDT -3.20%" in out["b"]
    assert "🎯 AAVEUSDT score 83" in out["b"]
