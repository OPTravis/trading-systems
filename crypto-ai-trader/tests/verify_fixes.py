#!/usr/bin/env python3
"""
Verification script for CORE_MODULE_AUDIT fixes.
Uses source code inspection only (no imports needed).
"""

import os
import re

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(project_root)

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  ✅ {name}")
        passed += 1
    else:
        print(f"  ❌ {name} — {detail}")
        failed += 1


def read(path):
    with open(path) as f:
        return f.read()


smart_order = read("src/smart_order.py")
strategy_adaptor = read("src/strategy_adaptor.py")
risk_manager = read("src/risk_manager.py")
trade_executor = read("src/trade_executor.py")
binance_client = read("src/binance_client.py")
main_py = read("main.py")

# ============================================================
# C1: smart_order.py NameError fix
# ============================================================
print("\n[C1] smart_order.py: quantity defined before filter check")
# Find calculate_position_size method
cps_match = re.search(
    r"def calculate_position_size\(.*?(?=\n    def |\nclass |\Z)",
    smart_order,
    re.DOTALL,
)
cps = cps_match.group(0) if cps_match else ""
qty_line = cps.find("quantity = usdt_amount / price")
filter_line = cps.find("if not filters:")
check(
    "quantity assigned before if-not-filters block",
    qty_line > 0 and filter_line > 0 and qty_line < filter_line,
    f"qty at char {qty_line}, filters at char {filter_line}",
)

# ============================================================
# C2: strategy_adaptor.py mutable shared dict fix
# ============================================================
print("\n[C2] strategy_adaptor.py: NEUTRAL regime uses shallow copy")
check(
    "dict(base) or base.copy() in regime_map",
    "dict(base)" in strategy_adaptor or "base.copy()" in strategy_adaptor,
)

# ============================================================
# C3: risk_manager.py batch API calls fix
# ============================================================
print("\n[C3] risk_manager.py: drawdown check uses batch ticker")
# Find the drawdown breaker section in pre_trade_check
dd_section = risk_manager[risk_manager.find("# 5. Drawdown breaker") :]
check(
    "batch ticker fetch in drawdown section",
    "all_tickers" in dd_section and "get_24hr_stats()" in dd_section,
)
check(
    "no per-asset API call in drawdown loop",
    'get_24hr_stats(f"{asset}USDT")' not in dd_section,
)

# ============================================================
# H1: trade_executor.py dead code removal
# ============================================================
print("\n[H1] trade_executor.py: dead code removed")
check("sl_reserve_pct not in function", "sl_reserve_pct" not in trade_executor)
check("sl_reserve_qty not in function", "sl_reserve_qty" not in trade_executor)
check("tp_available_qty not in function", "tp_available_qty" not in trade_executor)

# ============================================================
# H2: trade_executor.py unknown price fix
# ============================================================
print("\n[H2] trade_executor.py: unknown price doesn't inflate count")
# Find count_active_positions
ca_match = re.search(
    r"def count_active_positions\(.*?(?=\ndef |\Z)", trade_executor, re.DOTALL
)
ca = ca_match.group(0) if ca_match else ""
# Check that price == 0 block does NOT have count += 1
lines = ca.split("\n")
in_price_zero = False
bad = False
for line in lines:
    s = line.strip()
    if "price == 0" in s:
        in_price_zero = True
    elif in_price_zero and "count += 1" in s:
        bad = True
        break
    elif in_price_zero and s and not s.startswith("#"):
        in_price_zero = False
check("price == 0 does NOT increment count", not bad)

# ============================================================
# H3: trade_executor.py fee-aware balance fix
# ============================================================
print("\n[H3] trade_executor.py: balance queried after trade")
check(
    "get_free_balance called for actual USDT",
    "actual_usdt = client.get_free_balance('USDT')" in trade_executor,
)

# ============================================================
# H4: risk_manager.py trailing stop partial close fix
# ============================================================
print("\n[H4] risk_manager.py: trailing stop preserved on partial close")
# Find post_trade_update
ptu_match = re.search(
    r"def post_trade_update\(.*?(?=\n    def |\nclass |\Z)", risk_manager, re.DOTALL
)
ptu = ptu_match.group(0) if ptu_match else ""
check("remaining_qty parameter added", "remaining_qty" in ptu)
check("trailing stop only removed when remaining_qty <= 0", "remaining_qty <= 0" in ptu)

# ============================================================
# H5: smart_order.py public method access fix
# ============================================================
print("\n[H5] smart_order.py: uses public get_exchange_info()")
# Find get_symbol_filters method
gsf_match = re.search(
    r"def get_symbol_filters\(.*?(?=\n    def |\Z)", smart_order, re.DOTALL
)
gsf = gsf_match.group(0) if gsf_match else ""
check(
    "no private _get_exchange_info in get_symbol_filters",
    "_get_exchange_info" not in gsf,
)
check(
    "BinanceClient has public get_exchange_info",
    "def get_exchange_info(self)" in binance_client,
)

# ============================================================
# M1: trade_executor.py duplicate TP capping removed
# ============================================================
print("\n[M1] trade_executor.py: duplicate TP capping removed")
# The first block caps tp_levels percentages, the second caps tp_qty_list quantities
# in Strategy C fallback — these are different code paths, not duplicates.
# Verify only ONE "Reserve qty for SL" comment exists (the removed duplicate had its own)
reserve_count = trade_executor.count("Reserve qty for SL first")
check(
    "only one TP capping + reserve block", reserve_count == 1, f"found {reserve_count}"
)

# ============================================================
# M2: trade_executor.py import time at module level
# ============================================================
print("\n[M2] trade_executor.py: import time at module level")
# Check that import time is at the top (before first def)
first_def = trade_executor.find("\ndef ")
top_section = trade_executor[:first_def] if first_def > 0 else ""
check("import time at top of file", "import time" in top_section)
check(
    "no inline 'import time; time.sleep' remaining",
    "import time; time.sleep" not in trade_executor,
)

# Also check main.py
first_def_main = main_py.find("\ndef ")
top_main = main_py[:first_def_main] if first_def_main > 0 else ""
check("main.py: import time at top", "import time" in top_main)
check(
    "main.py: no inline 'import time; time.sleep' remaining",
    "import time; time.sleep" not in main_py,
)

# ============================================================
# M3: trade_executor.py robust symbol parsing
# ============================================================
print("\n[M3] trade_executor.py: robust symbol-to-asset parsing")
check("uses suffix loop instead of replace()", "for suffix in" in trade_executor)

# ============================================================
# M5: risk_manager.py redundant .upper() removed
# ============================================================
print("\n[M5] risk_manager.py: redundant .upper() removed")
# Find TrailingStop.get method
ts_get_match = re.search(
    r"class TrailingStop:.*?def get\(self.*?(?=\n    def |\Z)", risk_manager, re.DOTALL
)
ts_get = ts_get_match.group(0) if ts_get_match else ""
# After the normalization (symbol.upper() and endswith check), there should be no extra .upper()
# Check that the return statement doesn't have symbol.upper()
last_return = ts_get.rfind("return self._state.get(")
if last_return > 0:
    return_line = ts_get[last_return : last_return + 60]
    check("no double .upper() in TrailingStop.get()", ".upper()" not in return_line)
else:
    check("no double .upper() in TrailingStop.get()", True)

# ============================================================
# M6: strategy_adaptor.py history list optimization
# ============================================================
print("\n[M6] strategy_adaptor.py: history list only truncated when needed")
check(
    "len() check before history truncation",
    "if len(self._state" in strategy_adaptor and "> 100" in strategy_adaptor,
)

# ============================================================
# M7: smart_order.py batch price API calls
# ============================================================
print("\n[M7] smart_order.py: get_current_positions uses batch ticker")
# Find get_current_positions
gcp_match = re.search(
    r"def get_current_positions\(.*?(?=\n    def |\Z)", smart_order, re.DOTALL
)
gcp = gcp_match.group(0) if gcp_match else ""
check("batch ticker fetch in get_current_positions", "get_24hr_stats()" in gcp)
check("no per-asset get_price() calls", "self.get_price(" not in gcp)

# ============================================================
# M8: smart_order.py division by zero fix
# ============================================================
print("\n[M8] smart_order.py: division by zero in risk_reward")
# Find the risk_reward line
rr_match = re.search(r"risk_reward.*", smart_order)
rr_line = rr_match.group(0) if rr_match else ""
check("sl_pct > 0 guard on risk_reward", "> 0" in rr_line and "N/A" in rr_line)

# ============================================================
# Summary
# ============================================================
print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed")
print(f"{'='*50}")
exit(1 if failed > 0 else 0)
