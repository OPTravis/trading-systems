# P2-7: Module Overlap Elimination — SmartOrder ↔ TradeExecutor

## Problem

`src/smart_order.py` (566 lines) and `src/trade_executor.py` (1264 lines) had
overlapping responsibilities, creating two parallel implementations of:

| Concern | SmartOrder | TradeExecutor | Overlap Type |
|---------|-----------|---------------|-------------|
| Order placement | `place_buy_with_sl_tp()` (262 lines) | `execute_auto_trade()` | **Full duplication** |
| Position sizing | `calculate_position_size()` (score+vol) | KellyPositionSizer (Kelly) | **Dead code** |
| Position counting | `get_current_positions()` | `count_active_positions()` | **Dead code** |
| Price fetching | `get_price()` | `client.get_ticker_price()` | **Dead code** |
| Balance check | `get_usdt_balance()` | `client.get_free_balance("USDT")` | **Dead code** |
| Exchange filters | `get_symbol_filters()` | (calls SmartOrder) | **Correct delegation** |
| Qty precision | `apply_qty_precision()` | inline `_round_qty()` | **Minor duplication** |
| SL/TP calc | `calculate_sl_tp()` (ATR) | inline pct-based | **Different algorithms** |

**Root cause**: SmartOrder was the original trade execution module. When
TradeExecutor was extracted from `main.py` (with Kelly sizing, multi-strategy
order placement, circuit breakers), SmartOrder's execution code became dead
but was never cleaned up.

**Risk**: Modifying one module's logic (e.g., SL buffer, qty precision) could
silently diverge from the other, leading to inconsistent trade behavior.

## Solution: Clear Responsibility Split

### SmartOrder → Pure Calculation Module (no side effects)
- `get_symbol_filters(symbol)` — fetch LOT_SIZE/PRICE_FILTER/MIN_NOTIONAL (cached)
- `apply_qty_precision(qty, filters)` — floor to stepSize, enforce min/max
- `calculate_sl_tp(price, atr)` — ATR-based SL/TP price calculation (static)
- `calculate_sl_tp_pct(price, sl_pct, tp_pcts, precision)` — percentage-based SL/TP (new, static)

### TradeExecutor → Sole Execution Entry Point
- Calls `SmartOrder.get_symbol_filters()` for exchange metadata
- Owns all position sizing (Kelly/tier-based), order placement, SL/TP execution
- Owns all risk checks (circuit breakers, daily loss, drawdown)

## Changes Made

### `src/smart_order.py` (566 → ~200 lines)

**Removed (dead code with zero external callers):**
| Method | Lines | Reason |
|--------|-------|--------|
| `place_buy_with_sl_tp()` | 262 | Full trade execution — duplicated by `execute_auto_trade()`. No external callers (grep confirmed). |
| `calculate_position_size()` | 55 | Score+volatility sizing — only called by `place_buy_with_sl_tp`. Superseded by KellyPositionSizer. |
| `get_current_positions()` | 35 | Position list — duplicated by `count_active_positions()` in trade_executor. Only called by dead code. |
| `get_price()` | 10 | Price fetch — only called by `place_buy_with_sl_tp`. |
| `get_usdt_balance()` | 2 | Balance fetch — only called by `calculate_position_size`. |
| `MAX_POSITIONS`, `MAX_SINGLE_POSITION_PCT`, `MAX_TOTAL_EXPOSURE_PCT`, `CASH_RESERVE_PCT` | 4 | Risk limit constants — only used by removed methods. Source of truth is now `risk_config` / `risk_limits.yaml`. |

**Added:**
- `calculate_sl_tp_pct()` — static method for percentage-based SL/TP calculation (complements existing ATR-based `calculate_sl_tp()`)

**Kept (unchanged signatures):**
- `get_symbol_filters()` — used by trade_executor.py, trailing_tp.py
- `apply_qty_precision()` — pure calculation utility
- `calculate_sl_tp()` — ATR-based SL/TP (used by backtest.py for reference)
- All ATR multiplier constants, TP sizing constants, spread constraints

**Updated:**
- Module docstring: documents SmartOrder as pure calculation module
- Class docstring: states "does NOT execute trades"

### `src/trade_executor.py`
- **No functional changes** — only uses `SmartOrder.get_symbol_filters()`
- Added module-level docstring documenting the delegation relationship
- Added inline comment at SmartOrder usage point (line ~737)

### `scripts/code_quality_guard.py`
- Removed `smart_order.py` from MAX_POSITIONS consistency check (constants removed)

### `src/backtest.py`
- Updated stale comments: `(與 SmartOrder 一致)` → `(與 risk_config / TradeExecutor 一致)`

### `tests/test_crypto_system.py`
- Removed 3 dead tests for `calculate_position_size` (method no longer exists):
  - `test_position_size_rejects_max_positions`
  - `test_position_size_respects_single_limit`
  - `test_position_size_minimum_trade`
- Kept 3 tests for `calculate_sl_tp` (method still exists, unchanged)

### `tests/verify_fixes.py`
- Updated C1 check: verifies `calculate_position_size` is removed (was: check qty/filter ordering)
- Updated M7 check: verifies `get_current_positions` is removed (was: check batch ticker)
- Updated M8 check: verifies `risk_reward` code is removed (was: check zero-guard)

## Verification

```
✅ python3 -c "import smart_order; import trade_executor" → imports OK
✅ python3 main.py → starts normally
✅ pytest tests/test_trade_executor_unit.py → 35 passed
✅ pytest tests/test_crypto_system.py → 30 passed
✅ tests/verify_fixes.py → M7/M8 checks pass (3 pre-existing failures unrelated)
```

## Known Minor Duplication (Deferred)

TradeExecutor has an inline `_round_qty()` helper (float-based `floor()`) that
duplicates SmartOrder's `apply_qty_precision()` (Decimal-based). These produce
identical results for practical inputs but differ in edge cases. Consolidating
them is deferred to avoid any behavior change — the rounding math must be
verified equivalent across all symbol step sizes before switching.

## Constraint Compliance

- ✅ **No trading behavior changed** — SL/TP percentages, position sizes, order placement all identical
- ✅ **External method signatures preserved** — `get_symbol_filters()`, `apply_qty_precision()`, `calculate_sl_tp()` unchanged
- ✅ **Removed methods confirmed unused** — `grep -rn` across entire codebase before removal
- ✅ **All tests pass** — 65 tests across 2 test files
