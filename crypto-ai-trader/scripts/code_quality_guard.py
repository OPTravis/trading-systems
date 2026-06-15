#!/home/travis/crypto-ai-trader/.venv/bin/python3
"""
Crypto code quality guard — scans for known static defects.
Run via cron (no_agent) every 6 hours. Silent when healthy.
Exit 0 = all checks passed (no output = SILENT).
Exit 1 = defect found (stdout delivered as alert).
"""
import re
import sqlite3
import sys
from pathlib import Path

PROJECT = Path.home() / "trading-systems" / "crypto-ai-trader"
SRC = PROJECT / "src"
CONFIG = PROJECT / "config"

defects = []

def check(name, ok, detail):
    if not ok:
        defects.append(f"[DEFECT] {name}: {detail}")

def read(path):
    try:
        return Path(path).read_text()
    except:
        return ""

# === 1. RSI 60-70 scoring ===
tech = read(SRC / "agents" / "technical_agent.py")
# Must have: elif 60 <= rsi < 70: followed by score += 12 (within 3 lines)
rsi_ok = bool(re.search(r"elif 60 <= rsi < 70:\s*\n\s*score \+= 12", tech))
check("RSI_BLIND_SPOT", rsi_ok, "RSI 60-70 must score +12 (healthy bullish confirmation)")

# === 2. MACD bidirectional ===
macd_ok = bool(re.search(r"elif macd_hist < 0:\s*\n\s*score -= \d+", tech))
check("MACD_ONE_WAY", macd_ok, "MACD bearish must score negative (penalize downtrend)")

# === 3. Fail-open in risk-critical code ===
# Pre-trade checks (price deviation, duplicate order) are ALLOWED to fail-open
# because blocking all trading on transient API errors is worse than allowing through.
# Only flag fail-open in risk limits, position sizing, or circuit breaker code.
te = read(SRC / "trade_executor.py")
# Match fail-open inside functions that deal with risk limits/sizing (not pre-trade checks)
risk_fail_opens = []
for m in re.finditer(r"def\s+(\w+).*?(?=\ndef\s|\Z)", te, re.DOTALL):
    fn_name = m.group(1)
    fn_body = m.group(0)
    if any(kw in fn_name.lower() for kw in ("risk", "limit", "sizing", "kelly", "drawdown", "breaker")):
        fails = re.findall(r"return\s+True\s*#.*fail.open", fn_body)
        risk_fail_opens.extend(fails)
check("FAIL_OPEN", len(risk_fail_opens) == 0,
      f"Found {len(risk_fail_opens)} fail-open pattern(s) in risk-critical code — should all be fail-closed")

# === 4. Kelly threshold ≤ 5 ===
kelly = read(SRC / "kelly_sizer.py")
m = re.search(r"min_trades.*=\s*(\d+)", kelly)
val = int(m.group(1)) if m else 10
check("KELLY_THRESHOLD", val <= 5, f"min_trades={val} — should be ≤5")

# === 5. max_positions consistency ===
patterns = {
    "trade_executor.py": r"max_positions\s*=\s*(\d+)",
    "backtest.py": r"MAX_POSITIONS\s*=\s*(\d+)",
    "portfolio.py": r'"max_open_positions":\s*(\d+)',
}
vals = set()
for fname, pat in patterns.items():
    code = read(SRC / fname)
    m = re.search(pat, code)
    if m:
        vals.add(int(m.group(1)))
rl = read(CONFIG / "risk_limits.yaml")
m = re.search(r"max_open_positions:\s*(\d+)", rl)
if m:
    vals.add(int(m.group(1)))
check("MAX_POSITIONS_INCONSISTENT", len(vals) <= 1,
      f"Values across files: {sorted(vals)} — should all match")

# === 6. Grid TP1 vs SL ===
strat = read(CONFIG / "strategies.yaml")
if "grid:" in strat:
    grid = strat[strat.find("grid:"):strat.find("grid:") + 500]
    tp_m = re.search(r"pct:\s*([\d.]+)", grid)
    sl_m = re.search(r"stop_loss_pct:\s*([\d.]+)", grid)
    if tp_m and sl_m:
        tp1, sl = float(tp_m.group(1)), float(sl_m.group(1))
        check("GRID_NEGATIVE_EV", tp1 >= sl,
              f"Grid TP1={tp1}% < SL={sl}% — negative expected value")

# === 7. Trades source column ===
db_path = PROJECT / "data" / "state.db"
if db_path.exists():
    conn = sqlite3.connect(str(db_path))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
    check("TRADES_SOURCE_COLUMN", "source" in cols,
          "trades table missing 'source' column")
    conn.close()

# === 8. Circuit Breaker wired ===
cb_ok = "CircuitBreaker" in te and "is_tripped" in te
check("CIRCUIT_BREAKER_WIRED", cb_ok,
      "CircuitBreaker not wired into trade_executor.py")

# === 9. Trailing Stop coverage ===
# Trailing stop is in ensure_tp_sl.py (called by crypto-monitor via auto-heal)
ensure_tp = read(PROJECT / "scripts" / "ensure_tp_sl.py")
ts_ok = "trailing" in ensure_tp.lower() or "trailing_stop" in ensure_tp or "highest_price" in ensure_tp
check("TRAILING_STOP_COVERAGE", ts_ok,
      "Trailing Stop logic not found in scripts/ensure_tp_sl.py")

# === Output ===
if not defects:
    sys.exit(0)
else:
    for d in defects:
        print(d)
    sys.exit(1)
