# Grid Bot Conflict Analysis Report

**Date:** 2026-04-15
**Analyst:** Automated review of grid_bot.py + grid_trader.py vs existing trading system

---

## 1. Order Conflicts

### Risk Level: 🟢 NONE (normal operation) / 🟡 MEDIUM (edge cases)

**Description:**
The grid bot operates on SOLUSDT only. The existing system (BARD, TREE, ZAMA) operates on different symbols. The key question is whether `cancel_all_orders(symbol)` is symbol-scoped.

**Findings:**
- `BinanceClient.cancel_all_orders(symbol)` calls `self.client.cancel_open_orders(symbol=symbol)` — this is **symbol-scoped** by the Binance API. It will only cancel orders for the specified symbol.
- The grid bot's `stop()` and `pause()` methods call `cancel_all_orders(symbol)` with the grid's symbol (SOLUSDT). This will NOT touch BARDUSDT, TREEUSDT, or ZAMAUSDT orders.
- The grid bot's `_rebalance()` method also calls `cancel_all_orders(symbol)` — same scope guarantee.
- The `cmd_trailing_check()` function in `main.py` line 1028 also calls `cancel_all_orders(symbol)` per-symbol.

**Edge Case (🟡):** If the trailing-check cron ever encounters SOLUSDT (e.g., if the grid bot holds SOL), it will call `client.cancel_all_orders("SOLUSDT")` — this would cancel ALL SOLUSDT open orders including grid limit orders. This is a real risk if SOL ends up being tracked as a "position" in the trailing stop system.

**Recommended Action:**
- Add SOLUSDT to an exclusion list in `cmd_trailing_check()` so the trailing-stop system never cancels grid orders.
- Alternatively, the grid bot should tag its orders with a specific `newClientOrderId` prefix (e.g., `GRID_`) so they can be identified and excluded.

---

## 2. API Rate Limits

### Risk Level: 🟢 NONE

**Description:**
Calculate total API weight per 15-minute cycle to ensure we stay under Binance limits (1200 req/min, 6000 weight/min).

**Grid bot `tick()` API calls per invocation:**
| API Call | Weight | Count | Total Weight |
|---|---|---|---|
| `get_24hr_stats(SOLUSDT)` | 2 | 1 | 2 |
| `get_open_orders(SOLUSDT)` | 2 | 1 | 2 |
| `get_order(symbol, orderId)` | 2 | 0-9 (per detected fill) | 0-18 |
| `place_limit_buy/sell` | 1 | 0-9 (counter orders) | 0-9 |
| `_calculate_equity` → `get_free_balance` + `get_24hr_stats` + `get_position` | 2+2+10 | 1 | 14 |
| `_get_symbol_precision` → `exchange_info` | 10 | 0-1 | 0-10 |

**Max grid tick weight:** ~55 per tick (worst case with 9 fills)

**Existing `trailing-check` API calls per invocation (3 positions):**
| API Call | Weight | Count | Total Weight |
|---|---|---|---|
| `client.account()` | 10 | 1 | 10 |
| `get_24hr_stats` | 2 | 3 | 6 |
| `get_klines` | 2 | 3 | 6 |
| `get_open_orders(symbol)` | 2 | 3 | 6 |
| Various cancel/place (if triggered) | 1-2 | 0-6 | 0-12 |

**Max trailing-check weight:** ~40 per run

**Existing `cron-scan` API calls:** ~80-120 weight per run

**Total per 15-min cycle:** ~200-280 weight (worst case)
**Binance limit:** 6000 weight/min → 90,000 weight per 15 min

**Conclusion:** Even at peak, we use <1% of the 15-minute weight budget. Rate limits are not a concern.

**Recommended Action:** No action needed. The combined API usage is far below limits.

---

## 3. Balance Conflicts

### Risk Level: 🟡 MEDIUM

**Description:**
Grid bot is configured for $400 USDT capital. Current USDT balance is $409.62. However, existing BARD OCO/SL/TP orders may lock a portion of USDT, leaving insufficient free balance for grid orders.

**Findings:**
- Portfolio state shows $409.62 cash balance.
- BARD position: 44.886 units @ $0.3336 = ~$14.97 cost. Any BARD SL/TP orders are in BARD (not USDT), so they lock BARD not USDT.
- TREE position: 0.088 units @ $0.0662 = ~$0.01 — dust.
- ZAMA position: 0.736 units @ $0.03225 = ~$0.02 — dust.
- The existing positions don't lock significant USDT since they're already bought (SL/TP orders lock the asset, not USDT).
- Grid bot needs $400 / 8 grids = $50 per grid level. Only levels below current price place BUY orders, so ~4 buy orders × $50 = ~$200 USDT needed initially.

**Analysis:** With ~$409.62 free USDT and ~$200 needed for grid buy orders, there's enough balance. However, if `cron-scan` triggers an `execute_auto_trade()` (AUTO_EXECUTE=true), it could consume USDT needed by the grid.

**Recommended Action:**
- Ensure grid bot's $400 is "reserved" and won't be spent by auto-trades. Either:
  - Set `AUTO_EXECUTE=false` while grid is running, OR
  - Reduce grid capital to $350 to leave buffer for BARD system trades, OR
  - Add a balance check in `execute_auto_trade()` to respect a "grid_reserve" amount.

---

## 4. State File Conflicts

### Risk Level: 🟢 NONE

**Description:**
Check if grid_state.json and portfolio_state.json have any overlapping data that could cause conflicts.

**Findings:**
- `portfolio_state.json` is managed by `PortfolioManager` (src/portfolio.py). Path: `<project>/data/portfolio_state.json`. Contains positions, cash_balance.
- `grid_state.json` is managed by `GridBot` (src/grid_trader.py). Path: `<project>/data/grid_state.json`. Contains grid levels, config, stats.
- These are **completely separate files** with no shared keys or data structures.
- The grid bot does NOT call `PortfolioManager.add_position()` or `update_balance()`. Grid trades are tracked independently.
- No file locking is used by either system (both use atomic writes via temp+rename).

**Concern:** If the grid bot buys SOL, the SOL position won't appear in portfolio_state.json. This means `cmd_status()` and `cmd_cron_report()` won't show grid-managed SOL positions in their portfolio overview. The Binance API direct calls in those commands WILL show SOL balance, creating inconsistency.

**Recommended Action:**
- Consider having the grid bot report its SOL holdings to PortfolioManager, or add a note in status reports that grid-managed positions are tracked separately.
- The atomic write pattern prevents file corruption even if both write simultaneously.

---

## 5. Cron Overlap

### Risk Level: 🟡 MEDIUM

**Description:**
The existing `trailing-check` command and the proposed grid `tick` could both run every 15 minutes. If they execute simultaneously, they could interfere.

**Findings:**
- Current crontab only has log rotation (`0 3 * * *`). The `crypto-position-check` / `trailing-check` cron is apparently managed elsewhere (not in this user's crontab).
- Both scripts create separate `BinanceClient()` instances, so they're independent processes.
- **The key risk:** If both run at the same time and both call `get_open_orders("SOLUSDT")`, they'll see the same order set. If `trailing-check` detects SOL as an uncovered position (free SOL balance from grid buys), it could place unwanted SL orders on grid-managed SOL.
- **Rebalance risk:** If `trailing-check` triggers `cancel_all_orders("SOLUSDT")` while grid is running, it would cancel all grid orders.

**Recommended Action:**
- **Stagger the cron schedules.** If `trailing-check` runs at :00, :15, :30, :45, run grid tick at :05, :20, :35, :50.
- Add SOL to trailing-check's exclusion list so it never manages SOLUSDT.
- Consider a lightweight lockfile mechanism (e.g., `/tmp/grid_bot.lock`) to prevent simultaneous execution.

---

## 6. Symbol Filtering (ALLOWED_SYMBOLS)

### Risk Level: 🟡 MEDIUM

**Description:**
Check if SOLUSDT needs to be added to the ALLOWED_SYMBOLS environment variable.

**Findings:**
- `BinanceClient.validate_symbol()` (line 134-139) checks `ALLOWED_SYMBOLS` env var.
- If `ALLOWED_SYMBOLS` is set and non-empty, only listed symbols are allowed.
- The grid bot's `init_grid()` does NOT call `validate_symbol()` — it proceeds directly to `get_24hr_stats(symbol)`.
- The grid bot's `start()` and `tick()` also don't call `validate_symbol()`.
- However, `place_order()` might call it. Let me check:

  Looking at `place_order()` (line 369+): It calls `self.client.new_order(...)` directly without validation. So `validate_symbol()` is only used by the scan/auto-trade path.

**Analysis:** The grid bot bypasses `ALLOWED_SYMBOLS` entirely — it places orders directly. If ALLOWED_SYMBOLS is configured (e.g., only "BARDUSDT,TREEUSDT,ZAMAUSDT"), the grid bot would still place SOLUSDT orders without any check.

**Recommended Action:**
- Add SOLUSDT to `ALLOWED_SYMBOLS` if it's configured.
- Add a `validate_symbol()` call in `GridBot.init_grid()` for safety.

---

## 7. Monkey Patch (random.randbits)

### Risk Level: 🟢 NONE

**Description:**
Both `main.py` and `grid_bot.py` patch `random.randbits` to alias `random.getrandbits` for Python 3.11.15 compatibility.

**Findings:**
- `main.py` (line 17-18): `if not hasattr(_random, 'randbits'): _random.randbits = _random.getrandbits`
- `grid_bot.py` (line 21-23): `if not hasattr(_r, 'randbits'): _r.randbits = _r.getrandbits`
- `grid_trader.py` (line 19-21): Same pattern with `_r` variable name.

**Analysis:**
- Both patches modify the same `random` module object (there's only one `random` module in Python).
- Both patches are **idempotent** — they check `hasattr` before patching, so running them multiple times is safe.
- If both scripts run simultaneously in separate processes, each process patches its own copy of the module — no conflict.
- If imported in the same process (e.g., main.py imports grid_trader.py), the second patch is a no-op since the first already applied it.

**Recommended Action:** No action needed. The patches are safe and idempotent.

---

## Summary Table

| # | Conflict | Risk | Key Action |
|---|---|---|---|
| 1 | Order cancellation scope | 🟢 NONE | Add SOLUSDT exclusion to trailing-check |
| 2 | API rate limits | 🟢 NONE | None needed |
| 3 | Balance competition | 🟡 MEDIUM | Reserve $400 for grid, reduce auto-trade budget |
| 4 | State file overlap | 🟢 NONE | Consider adding grid positions to portfolio reports |
| 5 | Cron simultaneous execution | 🟡 MEDIUM | Stagger schedules + exclude SOL from trailing-check |
| 6 | ALLOWED_SYMBOLS bypass | 🟡 MEDIUM | Add SOLUSDT to allowlist + add validation to grid bot |
| 7 | Monkey patch conflict | 🟢 NONE | None needed |

### Critical Recommendations (before enabling grid bot in production):

1. **Exclude SOLUSDT from trailing-check** — The most important fix. Add `EXCLUDED_SYMBOLS=SOLUSDT` or a hardcoded skip in `cmd_trailing_check()` to prevent the existing system from canceling grid orders or placing conflicting SLs on SOL.

2. **Stagger cron schedules** — Don't run `trailing-check` and `grid tick` at the same minute. Use a 5-minute offset.

3. **Add SOLUSDT to ALLOWED_SYMBOLS** (if configured) — And add `validate_symbol()` call to `GridBot.init_grid()`.

4. **Balance reservation** — Ensure auto-trade won't spend grid capital. Either reduce grid capital to leave buffer or add a reservation mechanism.
