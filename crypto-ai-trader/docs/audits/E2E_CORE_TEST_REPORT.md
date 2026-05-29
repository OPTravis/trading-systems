# Crypto-AI-Trader E2E Core Trading Flow Test Report
# Generated: 2026-04-25

## Test 1: `python main.py status` — Portfolio Status Query
**[RESULT] PASS**

- Command executed successfully with `.venv` activated.
- Output format verified:
  - Portfolio Summary block present (Total Value, Cash, Exposure, Total PnL, Positions count).
  - Positions list rendered with symbol, quantity, entry price, current price, and PnL%.
- Observed behavior:
  - Loaded 5 positions from StateDB (SQLite primary storage).
  - Loaded cash_balance from StateDB.
  - Performed Binance sync (`_sync_from_binance`) automatically before displaying status.
  - Dust filter correctly skipped BNB dust position ($0.0036 < $1 threshold).
- No errors or crashes.

## Test 2: `python main.py scan` — Market Scanner
**[RESULT] PASS**

- Command executed successfully.
- Output format verified:
  - Top Gainers section with symbol, 24h change %, and volume (e.g., `APEUSDT: +66.52% (Vol: $128.4M)`).
  - Top Losers section with same format.
  - Opportunities list with Score, 24h Change, Volume, and Signals.
- Observed behavior:
  - Scanned 40 candidate coins from DynamicCoinPool.
  - Found 10 opportunities after filtering.
  - Multiple "Not enough OI history data" warnings are benign (OI data not available for all symbols).
  - Output is human-readable and structured.

## Test 3: Binance Sync (Read-Only) — `sync_from_binance`
**[RESULT] PASS**

- Note: There is no `python main.py sync` CLI command. The sync logic is embedded in `cmd_status()` and `cmd_cron_scan()` via `_sync_from_binance()`.
- Tested the underlying `PortfolioManager.sync_from_binance()` method directly.
- Result: returned `True`, successfully pulled 5 non-dust positions from Binance SPOT API.
- Cash balance correctly set to USDT free balance ($368.32).
- Positions populated with real quantities and market-estimated entry prices.
- Minor issue noted: `entry_price` module import fails (`No module named 'entry_price'`), causing fallback to market price. This is a pre-existing code issue, not a test failure.

## Test 4: SmartOrder ATR-based SL/TP Calculation
**[RESULT] PASS**

- Tested `SmartOrder.calculate_sl_tp(price=100.0, atr=5.0)` with known inputs.
- Verified outputs:
  - SL price = 90.0 (expected 90.0) ✓
  - SL % = 10.0 (expected 10.0) ✓
  - TP1 price = 110.0 (expected 110.0) ✓
  - TP1 % = 10.0 (expected 10.0) ✓
  - TP2 price = 120.0 (expected 120.0) ✓
  - TP2 % = 20.0 (expected 20.0) ✓
  - TP3 price = 130.0 (expected 130.0) ✓
  - TP3 % = 30.0 (expected 30.0) ✓
- Logic matches documented multipliers:
  - SL_ATR_MULTIPLIER = 2.0 → SL distance = 2 * ATR = 10
  - TP1_ATR_MULTIPLIER = 2.0 → TP1 distance = 2 * ATR = 10
  - TP2_ATR_MULTIPLIER = 4.0 → TP2 distance = 4 * ATR = 20
  - TP3_ATR_MULTIPLIER = 6.0 → TP3 distance = 6 * ATR = 30
- Minimum spread enforcement and MAX_SL_ATR_MULT capping logic confirmed in source.

## Test 5: Trailing Stop Logic — Price Simulation
**[RESULT] PASS**

- Tested `TrailingStop.update()` from `src/risk_manager.py` with simulated price path.
- Scenario: entry=100, ATR=2, activation threshold=2.5*ATR=5.0, trailing distance=1.2*ATR=2.4.
- Verified activation conditions:
  - Price 100: not activated ✓
  - Price 104: not activated (profit=4 < 5) ✓
  - Price 106: activated (profit=6 >= 5), SL=103.6 ✓
- Verified trailing behavior:
  - Price rises to 110: SL moves up to 107.6 (110 - 2.4) ✓
  - SL only moves UP, never down (confirmed in source code line 255).
- Verified trigger:
  - Price drops to 107.5 (below SL 107.6): triggered=True, callback_pct=2.27% ✓
- SQLite persistence of trailing stop state confirmed by `_save()` calls in the class.

## Test 6: Portfolio SQLite CRUD Operations
**[RESULT] PASS**

- Tested `StateDB` and `PortfolioManager` against a temporary SQLite database.
- Verified operations:
  1. **Add**: `portfolio_set('BTCUSDT', ...)` → row inserted correctly.
  2. **Update**: `portfolio_set('BTCUSDT', ...)` with new qty/entry → row updated, `stop_loss` preserved via `COALESCE`.
  3. **Multi-add**: Added `ETHUSDT` → both positions coexist.
  4. **Remove**: `portfolio_remove('BTCUSDT')` → row deleted, `ETHUSDT` remains.
  5. **Cash balance**: `portfolio_set_cash_balance(1234.56)` → retrieved correctly.
  6. **PortfolioManager integration**: `add_position()` persists to DB via `_save_state()`.
- Schema verified: `portfolio` table has columns `symbol, quantity, entry_price, strategy, opened_at, updated_at, stop_loss, take_profit`.
- `ON CONFLICT(symbol) DO UPDATE SET ... COALESCE(...)` logic works correctly for partial updates.

---

## Summary

| Test | Component | Result |
|------|-----------|--------|
| 1 | `main.py status` | PASS |
| 2 | `main.py scan` | PASS |
| 3 | Binance sync (read-only) | PASS |
| 4 | SmartOrder ATR SL/TP | PASS |
| 5 | Trailing stop logic | PASS |
| 6 | Portfolio SQLite CRUD | PASS |

**Issues Noted (non-blocking):**
- `entry_price` module import fails in `sync_from_binance()`, causing fallback to market price for entry price estimation. This is a pre-existing code path issue.
- `main.py sync` CLI command does not exist; sync is performed automatically by `status` and `cron-scan`. Consider adding a dedicated `sync` CLI for explicit user control.
