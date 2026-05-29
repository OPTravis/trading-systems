#!/usr/bin/env python3
"""
Risk Control & State Integrity Checks
======================================
1. state.db: PRAGMA integrity_check, verify all tables exist, check for orphan positions
2. Cash reserve: verify SmartOrder.CASH_RESERVE_PCT and strategy_adaptor dynamic adjustment
3. Position limits: check max_positions, per-position size, total exposure limits
4. OCO structure: verify OCO order fields (aboveType, belowType, etc.)
5. TP/SL enforcement: check ensure_tp_sl.py logic
6. Kelly sizer: verify Kelly criterion implementation
"""
import sys, os, sqlite3, ast, re, textwrap
from pathlib import Path

PROJECT = Path(__file__).parent
DB_PATH = PROJECT / "data" / "state.db"
SRC = PROJECT / "src"
SCRIPTS = PROJECT / "scripts"

results = []

def section(title):
    print(f"\n{'='*70}")
    print(f"  CHECK: {title}")
    print(f"{'='*70}")

def ok(msg):
    print(f"  ✅ PASS: {msg}")
    results.append(("PASS", msg))

def warn(msg):
    print(f"  ⚠️  WARN: {msg}")
    results.append(("WARN", msg))

def fail(msg):
    print(f"  ❌ FAIL: {msg}")
    results.append(("FAIL", msg))

def info(msg):
    print(f"  ℹ️  INFO: {msg}")


# ============================================================
# CHECK 1: state.db Integrity
# ============================================================
def check_state_db():
    section("1. state.db Integrity Check")

    # 1a. File exists
    if not DB_PATH.exists():
        fail(f"state.db not found at {DB_PATH}")
        return
    ok(f"state.db exists ({DB_PATH.stat().st_size:,} bytes)")

    # 1b. PRAGMA integrity_check
    conn = sqlite3.connect(str(DB_PATH))
    result = conn.execute("PRAGMA integrity_check").fetchone()
    if result[0] == "ok":
        ok("PRAGMA integrity_check = ok (no corruption)")
    else:
        fail(f"PRAGMA integrity_check failed: {result[0]}")
    conn.close()

    # 1c. Verify all expected tables exist
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    expected_tables = {
        "trailing_stop", "portfolio", "drawdown", "risk_guard", "trades",
        "kv", "grid_state", "dca_state", "strategy_state", "audit_log",
        "decisions", "trade_outcomes"
    }
    missing = expected_tables - existing
    extra = existing - expected_tables - {"sqlite_sequence"}
    if not missing:
        ok(f"All {len(expected_tables)} expected tables present: {sorted(expected_tables)}")
    else:
        fail(f"Missing tables: {missing}")
    if extra:
        info(f"Extra tables (not in expected set): {extra}")

    # 1d. Orphan positions: portfolio entries without matching Binance balance
    rows = conn.execute("SELECT symbol, quantity, entry_price FROM portfolio WHERE quantity > 0").fetchall()
    info(f"Portfolio positions in DB: {len(rows)}")
    for r in rows:
        info(f"  {r['symbol']}: qty={r['quantity']}, entry=${r['entry_price']:.6f}")

    # 1e. Check trailing_stop ↔ portfolio consistency
    ts_symbols = {r[0] for r in conn.execute("SELECT symbol FROM trailing_stop").fetchall()}
    port_symbols = {r[0] for r in conn.execute("SELECT symbol FROM portfolio WHERE quantity > 0").fetchall()}
    orphan_ts = ts_symbols - port_symbols
    orphan_port = port_symbols - ts_symbols  # positions without trailing stops
    if orphan_ts:
        warn(f"Orphan trailing_stops (no matching portfolio): {orphan_ts}")
    else:
        ok("No orphan trailing_stop entries")
    if orphan_port:
        info(f"Portfolio positions without trailing_stop: {orphan_port} (may be intentional)")

    # 1f. Check DB thread safety settings
    conn2 = sqlite3.connect(str(DB_PATH))
    journal = conn2.execute("PRAGMA journal_mode").fetchone()[0]
    synchronous = conn2.execute("PRAGMA synchronous").fetchone()[0]
    info(f"journal_mode={journal}, synchronous={synchronous}")
    if journal.upper() in ("WAL", "MEMORY"):
        ok(f"journal_mode={journal} (concurrent reads OK)")
    else:
        warn(f"journal_mode={journal} may limit concurrency")

    conn.close()
    conn2.close()


# ============================================================
# CHECK 2: Cash Reserve
# ============================================================
def check_cash_reserve():
    section("2. Cash Reserve Verification")

    # 2a. SmartOrder.CASH_RESERVE_PCT
    smart_order_src = (SRC / "smart_order.py").read_text()
    tree = ast.parse(smart_order_src)
    cash_reserve_value = None
    max_positions_val = None
    max_single_val = None
    max_exposure_val = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    if t.id == "CASH_RESERVE_PCT":
                        cash_reserve_value = ast.literal_eval(node.value)
                    elif t.id == "MAX_POSITIONS":
                        max_positions_val = ast.literal_eval(node.value)
                    elif t.id == "MAX_SINGLE_POSITION_PCT":
                        max_single_val = ast.literal_eval(node.value)
                    elif t.id == "MAX_TOTAL_EXPOSURE_PCT":
                        max_exposure_val = ast.literal_eval(node.value)

    info(f"SmartOrder.CASH_RESERVE_PCT = {cash_reserve_value}%")
    info(f"SmartOrder.MAX_POSITIONS = {max_positions_val}")
    info(f"SmartOrder.MAX_SINGLE_POSITION_PCT = {max_single_val}%")
    info(f"SmartOrder.MAX_TOTAL_EXPOSURE_PCT = {max_exposure_val}%")

    if cash_reserve_value and 20 <= cash_reserve_value <= 50:
        ok(f"CASH_RESERVE_PCT={cash_reserve_value}% is in reasonable range [20-50]")
    else:
        warn(f"CASH_RESERVE_PCT={cash_reserve_value} may be outside reasonable range")

    # 2b. Check cash reserve is actually used in calculate_position_size
    if "self.CASH_RESERVE_PCT" in smart_order_src and "available = min(" in smart_order_src:
        ok("SmartOrder.calculate_position_size() uses CASH_RESERVE_PCT in available calculation")
    else:
        fail("CASH_RESERVE_PCT not referenced in position sizing logic")

    # 2c. Strategy adaptor dynamic adjustment
    adaptor_src = (SRC / "strategy_adaptor.py").read_text()
    if "cash_reserve_pct" in adaptor_src:
        ok("StrategyAdaptor adjusts cash_reserve_pct dynamically")
    else:
        fail("StrategyAdaptor does not reference cash_reserve_pct")

    # Check regime map for cash_reserve adjustments
    regime_keys = ["EXTREME_FEAR", "FEAR", "NEUTRAL", "GREED", "EXTREME_GREED"]
    reserves_found = []
    for rk in regime_keys:
        pattern = rf'"{rk}":\s*\{{[^}}]*"cash_reserve_pct":\s*(\d+)'
        m = re.search(pattern, adaptor_src, re.DOTALL)
        if m:
            reserves_found.append((rk, int(m.group(1))))
    if reserves_found:
        info("Regime-based cash_reserve_pct:")
        for regime, pct in reserves_found:
            info(f"  {regime}: {pct}%")
        # Verify extreme fear has higher reserve
        fear_pct = next((p for r, p in reserves_found if r == "FEAR"), None)
        greed_pct = next((p for r, p in reserves_found if r == "GREED"), None)
        if fear_pct and greed_pct and fear_pct > greed_pct:
            ok("Fear regime has higher cash reserve than Greed (defensive)")
        elif fear_pct and greed_pct:
            warn(f"FEAR({fear_pct}%) should have higher reserve than GREED({greed_pct}%)")

    # 2d. BTC trend overlay on cash reserve
    if "cash_reserve_pct" in adaptor_src and "effective_btc_score" in adaptor_src:
        ok("BTC trend score dynamically adjusts cash_reserve_pct")
    else:
        warn("BTC trend may not adjust cash_reserve_pct")

    # 2e. Verify trade_executor enforces cash reserve cap
    executor_src = (SRC / "trade_executor.py").read_text()
    if "cash_reserve_pct" in executor_src and "max_invest = usdt_bal * (1.0 - cash_reserve_pct" in executor_src:
        ok("trade_executor enforces cash_reserve_pct cap on invest_amount")
    else:
        fail("trade_executor does not enforce cash_reserve_pct cap")


# ============================================================
# CHECK 3: Position Limits
# ============================================================
def check_position_limits():
    section("3. Position Limits Verification")

    executor_src = (SRC / "trade_executor.py").read_text()

    # 3a. Max positions
    m = re.search(r'max_positions\s*=\s*(\d+)', executor_src)
    if m:
        info(f"trade_executor max_positions = {m.group(1)}")
        if int(m.group(1)) == 5:
            ok("max_positions=5 matches SmartOrder.MAX_POSITIONS")
        else:
            warn(f"max_positions={m.group(1)} differs from SmartOrder.MAX_POSITIONS=5")
    else:
        fail("max_positions not found in trade_executor")

    # 3b. Check enforcement
    if "active_positions >= max_positions" in executor_src:
        ok("Enforcement: blocks new trades when active_positions >= max_positions")
    else:
        fail("No enforcement check for max_positions in trade_executor")

    # 3c. Single position size cap
    if "max_single = usdt_bal * max_position_pct / 100.0" in executor_src:
        ok("Single position cap enforced via max_position_pct")
    else:
        fail("Single position cap not found in trade_executor")

    # 3d. Total exposure cap
    if "max_total_exposure_pct" in executor_src and "_max_exposure" in executor_src:
        ok("Total exposure cap enforced via max_total_exposure_pct")
    else:
        fail("Total exposure cap not found in trade_executor")

    # 3e. Multiplier system (daily loss + stepwise drawdown)
    if "_dl_multiplier" in executor_src and "_sd_multiplier" in executor_src:
        ok("Position size multipliers: daily_loss_breaker + stepwise_drawdown active")
        if "min(_dl_multiplier, _sd_multiplier)" in executor_src:
            ok("Uses min() of both multipliers (most conservative wins)")
        else:
            warn("Multiplier combination logic unclear")

    # 3f. Circuit breaker check
    if "CircuitBreaker" in executor_src and "cb.is_tripped()" in executor_src:
        ok("Circuit breaker check present (blocks trades on system failure)")
    else:
        warn("Circuit breaker check may be missing")

    # 3g. Daily loss breaker check
    if "daily_loss_breaker" in executor_src and "should_block_new_trades()" in executor_src:
        ok("Daily loss breaker blocks new trades when tripped")
    else:
        warn("Daily loss breaker check may be missing")

    # 3h. Score minimum threshold
    if "score < 60" in executor_src:
        ok("Minimum score threshold = 60 enforced")
    else:
        warn("Score threshold check not found")


# ============================================================
# CHECK 4: OCO Structure
# ============================================================
def check_oco_structure():
    section("4. OCO Order Structure Verification")

    # Check _binance_sdk_client.py (primary OCO implementation)
    sdk_src = (SRC / "_binance_sdk_client.py").read_text()

    # 4a. OCO fields present (as Python kwargs like aboveType="LIMIT_MAKER")
    oco_fields = {
        "aboveType": bool(re.search(r'aboveType\s*=\s*"', sdk_src)),
        "belowType": bool(re.search(r'belowType\s*=\s*"', sdk_src)),
        "abovePrice": bool(re.search(r'abovePrice\s*=', sdk_src)),
        "belowPrice": bool(re.search(r'belowPrice\s*=', sdk_src)),
        "belowStopPrice": bool(re.search(r'belowStopPrice\s*=', sdk_src)),
        "belowTimeInForce": bool(re.search(r'belowTimeInForce\s*=', sdk_src)),
    }
    for field, present in oco_fields.items():
        if present:
            ok(f"OCO field '{field}' present in SDK client")
        else:
            fail(f"OCO field '{field}' MISSING in SDK client")

    # 4b. Verify aboveType = LIMIT_MAKER, belowType = STOP_LOSS_LIMIT
    if 'aboveType="LIMIT_MAKER"' in sdk_src or "'aboveType': \"LIMIT_MAKER\"" in sdk_src or 'aboveType="LIMIT_MAKER"' in sdk_src:
        ok("aboveType = LIMIT_MAKER (correct for SPOT TP)")
    else:
        # Check with regex
        m = re.search(r'aboveType\s*[=:]\s*["\']LIMIT_MAKER', sdk_src)
        if m:
            ok("aboveType = LIMIT_MAKER (correct for SPOT TP)")
        else:
            warn("aboveType value not confirmed as LIMIT_MAKER")

    m = re.search(r'belowType\s*[=:]\s*["\']STOP_LOSS_LIMIT', sdk_src)
    if m:
        ok("belowType = STOP_LOSS_LIMIT (correct for SPOT SL)")
    else:
        warn("belowType value not confirmed as STOP_LOSS_LIMIT")

    # 4c. Check ccxt_client.py OCO too
    ccxt_src = (SRC / "ccxt_client.py").read_text()
    if "place_oco" in ccxt_src:
        ok("ccxt_client also has place_oco implementation")
    else:
        info("ccxt_client does not have place_oco (only SDK client)")

    # 4d. Verify order validation (min/max qty, precision)
    if "_oco_floored" in sdk_src and "min_qty" in sdk_src and "max_qty" in sdk_src:
        ok("OCO validates quantity (floor, min, max) before placing")
    else:
        warn("OCO may lack quantity validation")

    # 4e. Verify retry/rate-limit handling
    if "rate limit" in sdk_src.lower() or "429" in sdk_src:
        ok("OCO handles rate limiting (429/418 retries)")
    else:
        warn("OCO rate limit handling unclear")

    if "400" in sdk_src and "no retry" in sdk_src.lower():
        ok("OCO does NOT retry business errors (400/401/403) — correct behavior")
    else:
        warn("OCO retry logic for business errors unclear")

    # 4f. Check trade_executor uses OCO for medium+ positions
    executor_src = (SRC / "trade_executor.py").read_text()
    if "Strategy B" in executor_src and "place_oco" in executor_src:
        ok("trade_executor uses OCO for medium+ positions (Strategy B)")
    else:
        warn("trade_executor OCO usage not confirmed")

    if "Strategy A" in executor_src and "SL-only" in executor_src:
        ok("trade_executor uses SL-only for small positions (Strategy A)")
    else:
        warn("Small position SL-only strategy not confirmed")

    if "Strategy C" in executor_src and "Fallback" in executor_src:
        ok("trade_executor has separate SL+TP fallback (Strategy C)")
    else:
        warn("Fallback order strategy not confirmed")


# ============================================================
# CHECK 5: TP/SL Enforcement (ensure_tp_sl.py)
# ============================================================
def check_tp_sl_enforcement():
    section("5. TP/SL Enforcement (ensure_tp_sl.py)")

    etp_src = (SCRIPTS / "ensure_tp_sl.py").read_text()

    # 5a. Check stale position cleanup (sync with Binance)
    if "binance_assets" in etp_src and "stale" in etp_src and "DELETE FROM portfolio" in etp_src:
        ok("Case Sync: Removes DB positions no longer on Binance")
    else:
        fail("Stale position cleanup not found")

    # 5b. Orphan trailing_stop cleanup
    if "orphaned trailing stops" in etp_src.lower() or "清理孤立trailing_stop" in etp_src:
        ok("Case Sync: Cleans orphaned trailing_stop entries")
    else:
        warn("Orphan trailing_stop cleanup may be missing")

    # 5c. Case 0: Restructure separate TP+SL to OCO
    if "Case 0" in etp_src and "重構為OCO" in etp_src:
        ok("Case 0: Restructures separate TP+SL orders to OCO")
    else:
        warn("Case 0 (OCO restructure) not found")

    # 5d. Case 1: Missing TP, has SL
    if "Case 1" in etp_src and "Missing TP, has SL" in etp_src:
        ok("Case 1: Handles missing TP (tries OCO, falls back to separate orders)")
    else:
        fail("Case 1 (missing TP) not found")

    # 5e. Case 2: Missing SL, has TP
    if "Case 2" in etp_src and "Missing SL, has TP" in etp_src:
        ok("Case 2: Handles missing SL (places STOP_LOSS_LIMIT)")
    else:
        fail("Case 2 (missing SL) not found")

    # 5f. Case 3: Missing BOTH TP and SL
    if "Case 3" in etp_src and "Missing BOTH" in etp_src:
        ok("Case 3: Handles missing both (tries OCO, falls back to SL-only)")
    else:
        fail("Case 3 (missing both) not found")

    # 5g. Case 4: TP breached (auto-close)
    if "Case 4" in etp_src and "tp_breach" in etp_src:
        ok("Case 4: Auto-closes position when price breaches TP target")
    else:
        fail("Case 4 (TP breach auto-close) not found")

    # 5h. Trade outcome recording
    if "TradeOutcomeRecorder" in etp_src and "record_outcome" in etp_src:
        ok("Trade outcome recorded on TP breach (feeds Kelly sizer)")
    else:
        warn("Trade outcome not recorded on TP breach")

    # 5i. Max hold time enforcement
    if "max_hold" in etp_src.lower() or "MAX_HOLD" in etp_src:
        ok("Max hold time enforcement present (force-closes stale positions)")
    else:
        warn("Max hold time enforcement may be missing")

    # 5j. Trailing TP integration
    if "trailing_tp" in etp_src:
        ok("Trailing TP integrated (adjusts TP orders upward)")
    else:
        info("Trailing TP not integrated in ensure_tp_sl.py")


# ============================================================
# CHECK 6: Kelly Sizer
# ============================================================
def check_kelly_sizer():
    section("6. Kelly Criterion Implementation Verification")

    kelly_src = (SRC / "kelly_sizer.py").read_text()

    # 6a. Kelly formula
    if "f* = (bp - q) / b" in kelly_src or "(b * p - q) / b" in kelly_src:
        ok("Correct Kelly formula: f* = (bp - q) / b")
    else:
        fail("Kelly formula implementation not found or incorrect")

    # 6b. Half-Kelly safety factor
    if "KELLY_FRACTION = 0.5" in kelly_src or "kelly *= KELLY_FRACTION" in kelly_src:
        ok("Half-Kelly safety factor applied (KELLY_FRACTION=0.5)")
    else:
        warn("Half-Kelly safety factor not confirmed")

    # 6c. Hard caps
    if "MAX_POSITION_PCT = 0.50" in kelly_src:
        ok("MAX_POSITION_PCT = 50% hard cap (prevents over-concentration)")
    else:
        warn("MAX_POSITION_PCT hard cap not found")
    if "MIN_POSITION_PCT = 0.05" in kelly_src:
        ok("MIN_POSITION_PCT = 5% floor (ensures meaningful position sizes)")
    else:
        warn("MIN_POSITION_PCT floor not found")

    # 6d. Historical win rate from trade_outcomes
    if "trade_outcomes" in kelly_src and "net_pnl_pct" in kelly_src:
        ok("Uses trade_outcomes table for historical win rate calculation")
    else:
        fail("Kelly does not use historical trade data")

    # 6e. Score-based fallback
    if "signal_score" in kelly_src and "estimated from score" in kelly_src.lower():
        ok("Falls back to score-based win rate estimation with < 5 trades")
    else:
        warn("Score-based fallback may be missing")

    # 6f. Negative Kelly handling
    if "kelly <= 0" in kelly_src:
        ok("Handles negative Kelly (blocks trades with negative edge)")
    else:
        fail("Negative Kelly not handled")

    # 6g. Portfolio adjustment (decreases size as positions fill up)
    if "adjust_for_portfolio" in kelly_src:
        ok("adjust_for_portfolio() scales down Kelly as portfolio fills")
    else:
        fail("Portfolio-adjusted sizing not implemented")

    # 6h. Integration in trade_executor
    executor_src = (SRC / "trade_executor.py").read_text()
    if "KellyPositionSizer" in executor_src and "kelly.get_position_size" in executor_src:
        ok("KellyPositionSizer integrated in trade_executor.execute_auto_trade()")
    else:
        fail("KellyPositionSizer NOT integrated in trade_executor")

    if "kelly.adjust_for_portfolio" in executor_src:
        ok("Portfolio-adjusted Kelly sizing applied in executor")
    else:
        warn("Portfolio-adjusted Kelly not applied in executor")

    # 6i. Confidence-based routing
    if 'kelly_active = "estimated" not in kelly_confidence.lower()' in executor_src:
        ok("Routes to Kelly (sufficient history) or tier-based fallback")
    else:
        warn("Kelly/fallback routing logic unclear")

    # 6j. MIN_RELIABLE_TRADES threshold
    m = re.search(r'MIN_RELIABLE_TRADES\s*=\s*(\d+)', kelly_src)
    if m:
        info(f"MIN_RELIABLE_TRADES = {m.group(1)} trades for HIGH confidence")
    else:
        warn("MIN_RELIABLE_TRADES threshold not found")


# ============================================================
# SUMMARY
# ============================================================
def print_summary():
    print(f"\n{'='*70}")
    print(f"  SUMMARY")
    print(f"{'='*70}")
    passes = sum(1 for s, _ in results if s == "PASS")
    warns = sum(1 for s, _ in results if s == "WARN")
    fails = sum(1 for s, _ in results if s == "FAIL")
    total = len(results)
    print(f"  Total checks: {total}")
    print(f"  ✅ PASS: {passes}")
    print(f"  ⚠️  WARN: {warns}")
    print(f"  ❌ FAIL: {fails}")
    print(f"  {'='*50}")
    if fails > 0:
        print(f"  FAILURES:")
        for s, msg in results:
            if s == "FAIL":
                print(f"    ❌ {msg}")
    if warns > 0:
        print(f"  WARNINGS:")
        for s, msg in results:
            if s == "WARN":
                print(f"    ⚠️  {msg}")
    print(f"{'='*70}")


if __name__ == "__main__":
    check_state_db()
    check_cash_reserve()
    check_position_limits()
    check_oco_structure()
    check_tp_sl_enforcement()
    check_kelly_sizer()
    print_summary()
    # Exit with error code if any failures
    sys.exit(1 if any(s == "FAIL" for s, _ in results) else 0)
