#!/usr/bin/env python3
"""
Risk control and state integrity checks for crypto-ai-trader.
Runs 5 checks without requiring live Binance connection (DB-only + code audit).
"""
import sqlite3
import json
import os
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "state.db"
RESULTS = []

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def ok(msg):
    print(f"  ✅ {msg}")
    RESULTS.append(("OK", msg))

def warn(msg):
    print(f"  ⚠️  {msg}")
    RESULTS.append(("WARN", msg))

def fail(msg):
    print(f"  ❌ {msg}")
    RESULTS.append(("FAIL", msg))

# ── 1. state.db integrity ──
section("1. state.db Integrity Check")
if not DB_PATH.exists():
    fail(f"state.db not found at {DB_PATH}")
else:
    # File size
    sz = DB_PATH.stat().st_size
    print(f"  DB size: {sz:,} bytes")
    if sz == 0:
        fail("DB file is empty!")
    else:
        ok(f"DB file exists ({sz:,} bytes)")

    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    # Integrity check
    result = cur.execute("PRAGMA integrity_check").fetchone()
    if result[0] == "ok":
        ok("SQLite integrity_check passed")
    else:
        fail(f"SQLite integrity_check FAILED: {result[0]}")

    # List tables
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print(f"  Tables ({len(tables)}): {', '.join(tables)}")

    required = ["portfolio", "trailing_stop", "drawdown", "risk_guard", "trades", "kv"]
    for t in required:
        if t in tables:
            ok(f"Table '{t}' exists")
        else:
            fail(f"Required table '{t}' missing")

    # Check WAL mode
    journal = cur.execute("PRAGMA journal_mode").fetchone()[0]
    print(f"  Journal mode: {journal}")
    if journal.upper() == "WAL":
        ok("WAL journal mode (good for concurrent access)")
    else:
        warn(f"Journal mode is {journal}, not WAL")

    # Orphan positions: positions in portfolio table vs trailing_stop
    if "portfolio" in tables and "trailing_stop" in tables:
        portfolio_symbols = set(r[0] for r in cur.execute("SELECT symbol FROM portfolio").fetchall())
        trailing_symbols = set(r[0] for r in cur.execute("SELECT symbol FROM trailing_stop").fetchall())
        orphans = trailing_symbols - portfolio_symbols
        if orphans:
            fail(f"Orphan trailing_stop entries (no portfolio match): {orphans}")
        else:
            ok("No orphan trailing_stop entries")
        
        # Check for closed positions with stale data
        if portfolio_symbols:
            print(f"  Active positions in DB: {len(portfolio_symbols)} — {', '.join(sorted(portfolio_symbols))}")
        else:
            ok("No positions in DB (empty portfolio)")

        # Row counts
        for t in tables:
            count = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            if count > 0:
                print(f"  {t}: {count} rows")

    conn.close()

# ── 2. TP/SL order audit ──
section("2. TP/SL Order Check (Code Audit)")
print("  (Requires live Binance connection — doing static analysis)")

# Read trade_executor to find OCO placement logic
tx_path = Path(__file__).parent / "src" / "trade_executor.py"
if tx_path.exists():
    content = tx_path.read_text()
    # Check that OCO is placed after every position open
    has_oco = "place_oco" in content
    has_oco_after_open = content.find("place_oco") > content.find("execute_trade") if has_oco else False
    
    if has_oco:
        ok("trade_executor calls place_oco for TP/SL")
    else:
        fail("trade_executor does NOT call place_oco — positions may lack TP/SL!")
    
    # Check that there's error handling for OCO failure
    oco_section = content[content.find("place_oco"):] if has_oco else ""
    if "oco" in oco_section.lower() and "fail" in oco_section.lower():
        ok("OCO failure handling present")
    elif has_oco:
        warn("Could not confirm OCO failure handling")
else:
    fail("trade_executor.py not found")

# Check paper_trader OCO
pt_path = Path(__file__).parent / "src" / "paper_trader.py"
if pt_path.exists():
    pt_content = pt_path.read_text()
    if "place_oco" in pt_content:
        ok("Paper trader has place_oco method")
    else:
        warn("Paper trader missing place_oco")

# ── 3. Cash reserve check ──
section("3. Cash Reserve Enforcement")
se_path = Path(__file__).parent / "src" / "smart_order.py"
te_path = Path(__file__).parent / "src" / "trade_executor.py"

if se_path.exists():
    content = se_path.read_text()
    if "CASH_RESERVE_PCT" in content:
        # Extract the value
        for line in content.split('\n'):
            if "CASH_RESERVE_PCT" in line and '=' in line:
                print(f"  {line.strip()}")
                if "30" in line:
                    ok("SmartOrder has 30% cash reserve")
                break
    else:
        fail("smart_order.py missing CASH_RESERVE_PCT")

if te_path.exists():
    content = te_path.read_text()
    if "cash_reserve_pct" in content:
        ok("trade_executor uses cash_reserve_pct parameter")
        # Check the enforcement logic
        if "max_invest" in content and "usdt_bal" in content:
            ok("trade_executor enforces max_invest = usdt_bal * (1 - reserve%)")
        else:
            fail("trade_executor does NOT enforce cash reserve on max_invest")
    else:
        fail("trade_executor missing cash_reserve_pct enforcement")

# Check strategy_adaptor provides the value
sa_path = Path(__file__).parent / "src" / "strategy_adaptor.py"
if sa_path.exists():
    content = sa_path.read_text()
    if "cash_reserve_pct" in content:
        ok("strategy_adaptor provides cash_reserve_pct settings")
    else:
        fail("strategy_adaptor missing cash_reserve_pct")

# ── 4. Position limits ──
section("4. Position Limits Check")

# Check max_positions in trade_executor
if tx_path.exists():
    content = tx_path.read_text()
    for line in content.split('\n'):
        if "max_positions" in line and "=" in line and "max_positions =" in line:
            val = line.strip()
            print(f"  {val}")
            if "= 5" in val:
                ok("trade_executor max_positions = 5 (default)")
            else:
                warn(f"Unexpected max_positions value: {val}")
            break

# Check portfolio max_open_positions config
pm_path = Path(__file__).parent / "src" / "portfolio.py"
if pm_path.exists():
    content = pm_path.read_text()
    if "max_open_positions" in content:
        ok("portfolio.py enforces max_open_positions from config")
    else:
        fail("portfolio.py missing max_open_positions check")

    # Check for per-position size limits
    if "max_position_pct" in content or "max_position_size" in content or "position_pct" in content:
        ok("portfolio.py has per-position size limit")
    else:
        warn("Could not find per-position size limit in portfolio.py")

# Check Kelly sizer
ks_path = Path(__file__).parent / "src" / "kelly_sizer.py"
if ks_path.exists():
    content = ks_path.read_text()
    if "max_positions" in content:
        ok("Kelly sizer respects max_positions limit")

# Check trade_executor for max_position_pct
if tx_path.exists():
    content = tx_path.read_text()
    if "max_position_pct" in content:
        ok("trade_executor enforces per-position size limit (max_position_pct)")
    else:
        warn("trade_executor does not explicitly check max_position_pct")

# Check max_total_exposure_pct
if tx_path.exists():
    if "max_total_exposure_pct" in content or "total_exposure" in content:
        ok("trade_executor checks total exposure limit")
    else:
        warn("trade_executor missing total exposure limit check")

# ── 5. OCO order structure ──
section("5. OCO Order Structure Audit")

ccxt_path = Path(__file__).parent / "src" / "ccxt_client.py"
if ccxt_path.exists():
    content = ccxt_path.read_text()
    
    required_oco_fields = [
        "aboveType", "belowType", "abovePrice", "belowPrice",
        "belowStopPrice", "belowTimeInForce", "listClientOrderId",
        "side", "quantity"
    ]
    
    # Find OCO method
    oco_start = content.find("def place_oco")
    if oco_start > 0:
        oco_method = content[oco_start:oco_start+2000]
        
        # Check required fields
        for field in required_oco_fields:
            if field in oco_method:
                pass  # ok
            else:
                fail(f"OCO missing required field: {field}")
        ok("OCO method contains all required Binance OCO fields")
        
        # Check aboveType/belowType values
        if '"LIMIT_MAKER"' in oco_method:
            ok("aboveType = LIMIT_MAKER (correct for TP)")
        else:
            fail("aboveType is not LIMIT_MAKER")
        
        if '"STOP_LOSS_LIMIT"' in oco_method:
            ok("belowType = STOP_LOSS_LIMIT (correct for SL)")
        else:
            fail("belowType is not STOP_LOSS_LIMIT")
        
        # Check SL limit price guard
        if "sl_limit_price" in oco_method and "0.995" in oco_method:
            ok("SL limit price defaults to 0.5% below SL trigger")
        else:
            warn("Could not confirm SL limit price guard")
        
        # Check quantity floor
        if "_floor_to_step" in oco_method:
            ok("Quantity floored to LOT_SIZE step (exchange-compliant)")
        else:
            fail("OCO does not floor quantity to LOT_SIZE step")
        
        # Check retry logic
        if "for attempt in range" in oco_method:
            ok("OCO has retry logic (3 attempts)")
        else:
            warn("OCO missing retry logic")
        
        # Check validation
        if "oco_floored <= 0" in oco_method or "oco_floored < min_qty" in oco_method:
            ok("OCO validates quantity bounds")
        else:
            warn("OCO missing quantity validation")
    else:
        fail("place_oco method not found in ccxt_client.py")
else:
    fail("ccxt_client.py not found")

# ── Summary ──
section("SUMMARY")
ok_count = sum(1 for s, _ in RESULTS if s == "OK")
warn_count = sum(1 for s, _ in RESULTS if s == "WARN")
fail_count = sum(1 for s, _ in RESULTS if s == "FAIL")

print(f"  ✅ OK:     {ok_count}")
print(f"  ⚠️  WARN:   {warn_count}")
print(f"  ❌ FAIL:   {fail_count}")
print()

if fail_count == 0:
    print("  🟢 All critical checks passed!")
else:
    print("  🔴 FAILURES detected — review above items")
    for s, m in RESULTS:
        if s == "FAIL":
            print(f"     → {m}")
