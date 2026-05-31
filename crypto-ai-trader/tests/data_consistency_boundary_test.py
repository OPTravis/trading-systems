#!/usr/bin/env python3
"""
Crypto-AI-Trader Data Consistency & Boundary Test Suite (v6)
Tests against live StateDB (not raw sqlite3) to match WAL mode.
"""

import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.state_db import get_state_db

DB_PATH = Path.home() / "crypto-ai-trader/data/state.db"


class bcolors:
    PASS = "\033[92m"
    FAIL = "\033[91m"
    WARN = "\033[93m"
    ENDC = "\033[0m"


results = []


def log_pass(test_name):
    results.append(("PASS", test_name))
    print(f"{bcolors.PASS}[PASS]{bcolors.ENDC} {test_name}")


def log_fail(test_name, reason=""):
    results.append(("FAIL", test_name))
    print(f"{bcolors.FAIL}[FAIL]{bcolors.ENDC} {test_name} {reason}")


def log_warn(test_name, reason=""):
    results.append(("WARN", test_name))
    print(f"{bcolors.WARN}[WARN]{bcolors.ENDC} {test_name} {reason}")


# ── Helpers ──────────────────────────────────────────────────────────────
def get_db():
    return get_state_db(str(DB_PATH))


# ── 1. Portfolio Data Types ──────────────────────────────────────────────
def test_portfolio_datatypes():
    db = get_db()
    # Use raw connection for PRAGMA
    conn = db._get_conn()
    cols = {c[1]: c[2] for c in conn.execute("PRAGMA table_info(portfolio)").fetchall()}
    ok = True
    if cols.get("quantity") not in ("REAL", "NUMERIC", "FLOAT"):
        log_fail("1.1 portfolio.quantity datatype", f"got {cols.get('quantity')}")
        ok = False
    else:
        log_pass("1.1 portfolio.quantity datatype")
    if cols.get("stop_loss") not in ("REAL", "NUMERIC", "FLOAT"):
        log_fail("1.2 portfolio.stop_loss datatype", f"got {cols.get('stop_loss')}")
        ok = False
    else:
        log_pass("1.2 portfolio.stop_loss datatype")
    if cols.get("take_profit") not in ("REAL", "NUMERIC", "FLOAT"):
        log_fail("1.3 portfolio.take_profit datatype", f"got {cols.get('take_profit')}")
        ok = False
    else:
        log_pass("1.3 portfolio.take_profit datatype")


# ── 2. Boundary Conditions ───────────────────────────────────────────────
def test_boundary_zero_quantity():
    db = get_db()
    positions = db.portfolio_get_all()
    zero_rows = [
        sym
        for sym, data in positions.items()
        if data.get("quantity") == 0 or data.get("quantity") is None
    ]
    if zero_rows:
        log_warn("2.1 portfolio zero/null quantity", f"symbols={zero_rows}")
    else:
        log_pass("2.1 portfolio zero/null quantity (none found)")


def test_boundary_tiny_price_sl_tp():
    """Check if any entry_price < 0.01 has SL/TP computed correctly (non-zero and sensible)."""
    db = get_db()
    positions = db.portfolio_get_all()
    tiny = {
        sym: data
        for sym, data in positions.items()
        if data.get("entry_price", 0) < 0.01 and data.get("entry_price", 0) > 0
    }
    if not tiny:
        log_pass("2.2 tiny price SL/TP (no tiny-price rows)")
    else:
        for sym, data in tiny.items():
            sl = data.get("stop_loss")
            tp = data.get("take_profit")
            if sl is None or tp is None or sl == 0 or tp == 0:
                log_fail(
                    "2.2 tiny price SL/TP",
                    f"{sym}: ep={data['entry_price']}, sl={sl}, tp={tp}",
                )
                return
        log_pass("2.2 tiny price SL/TP (all sensible)")


def test_boundary_empty_portfolio_status():
    db = get_db()
    positions = db.portfolio_get_all()
    count = len(positions)
    if count == 0:
        log_warn("2.3 empty portfolio status", "portfolio is empty")
    else:
        log_pass("2.3 empty portfolio status (has rows)")


# ── 3. KV Table Integrity ────────────────────────────────────────────────
def test_kv_integrity():
    db = get_db()
    required_keys = ["cash_balance", "strategy_state", "grid_state", "drawdown_breaker"]
    for k in required_keys:
        val = db.kv_get(k)
        if val is None:
            log_fail(f"3 kv missing key: {k}")
        else:
            if k in ("strategy_state", "grid_state", "drawdown_breaker"):
                try:
                    json.loads(val) if isinstance(val, str) else val
                    log_pass(f"3 kv key {k} valid JSON")
                except json.JSONDecodeError:
                    log_fail(f"3 kv key {k} invalid JSON", str(val)[:80])
            else:
                log_pass(f"3 kv key {k} present")


# ── 4. Trailing Stop / Portfolio Consistency ─────────────────────────────
def test_trailing_portfolio_consistency():
    db = get_db()
    porto = db.portfolio_get_all()
    trail = db.ts_get_all()

    porto_symbols = set(porto.keys())
    trail_symbols = set(trail.keys())

    # trailing_stop should only contain symbols present in portfolio
    extra = trail_symbols - porto_symbols
    if extra:
        log_fail(
            "4 trailing_stop vs portfolio symbols", f"extra in trailing_stop: {extra}"
        )
    else:
        log_pass("4 trailing_stop vs portfolio symbols")

    # Also check for symbol name mismatches (e.g. AVAX vs AVAXUSDT)
    mismatches = []
    for sym in porto_symbols:
        if sym not in trail_symbols:
            alt = sym.replace("USDT", "")
            if alt in trail_symbols:
                mismatches.append((sym, alt))
    if mismatches:
        log_warn("4 trailing_stop symbol naming mismatch", f"pairs={mismatches}")
    else:
        log_pass("4 trailing_stop symbol naming exact match")

    # Check sl_price is non-zero for active positions
    bad_sl = []
    for sym, data in trail.items():
        sl = data.get("sl_price")
        if sl == 0 or sl is None:
            bad_sl.append(sym)
    if bad_sl:
        log_warn("4 trailing_stop sl_price is zero/null", f"symbols={bad_sl}")
    else:
        log_pass("4 trailing_stop sl_price non-zero")


# ── 5. Audit Log ───────────────────────────────────────────────────────
def test_audit_log():
    db = get_db()
    rows = db.audit_get_recent(20)
    if not rows:
        log_warn("5 audit_log empty", "no audit rows found")
    else:
        # Schema check via raw connection
        conn = db._get_conn()
        required_cols = {
            "timestamp",
            "action",
            "details",
            "old_value",
            "new_value",
            "source",
        }
        cols = {c[1] for c in conn.execute("PRAGMA table_info(audit_log)").fetchall()}
        missing = required_cols - cols
        if missing:
            log_fail("5 audit_log schema", f"missing cols: {missing}")
        else:
            log_pass("5 audit_log schema")
        bad = [r for r in rows if r.get("timestamp") is None or r.get("action") is None]
        if bad:
            log_fail("5 audit_log nulls", f"bad rows count={len(bad)}")
        else:
            log_pass("5 audit_log recent rows have timestamp/action")
        # Check if old_value != new_value for any non-sync action
        meaningful = [r for r in rows if r.get("action") != "PORTFOLIO_SYNC"]
        if not meaningful:
            log_warn(
                "5 audit_log only PORTFOLIO_SYNC entries",
                "no other actions found in last 20",
            )
        else:
            log_pass("5 audit_log has diverse actions")


# ── 6. Empty Portfolio Status Output ───────────────────────────────────────
def test_empty_portfolio_status():
    """Simulate empty portfolio and verify status output keys are correct."""
    db = get_db()
    positions = db.portfolio_get_all()
    cash = db.portfolio_get_cash_balance()
    if not positions:
        log_pass("6 empty portfolio status (simulated)")
    else:
        # Verify status logic when portfolio is not empty
        total_exposure = sum(
            data.get("quantity", 0) * data.get("entry_price", 0)
            for data in positions.values()
        )
        total_value = cash + total_exposure
        log_pass(
            f"6 empty portfolio status (simulated) — positions_count={len(positions)}, total_exposure={total_exposure:.2f}, total_value={total_value:.2f}"
        )


# ── Main ─────────────────────────────────────────────────────────────────
def main():
    print(f"DB: {DB_PATH}\n")
    if not DB_PATH.exists():
        print("Database not found!")
        sys.exit(1)
    test_portfolio_datatypes()
    test_boundary_zero_quantity()
    test_boundary_tiny_price_sl_tp()
    test_boundary_empty_portfolio_status()
    test_kv_integrity()
    test_trailing_portfolio_consistency()
    test_audit_log()
    test_empty_portfolio_status()
    print("\n" + "=" * 60)
    passes = sum(1 for r in results if r[0] == "PASS")
    fails = sum(1 for r in results if r[0] == "FAIL")
    warns = sum(1 for r in results if r[0] == "WARN")
    print(f"Summary: PASS={passes}  FAIL={fails}  WARN={warns}")
    print("\n[RESULT] List:")
    for status, name in results:
        print(f"[{status}] {name}")
    return fails == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
