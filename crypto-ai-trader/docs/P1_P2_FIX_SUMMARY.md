# P1 & P2 Fix Summary

**Date**: 2025-07-11  
**Scope**: 10 P1 fixes + 7 P2 fixes (P2-2, P2-3, P2-7 intentionally skipped)  
**Baseline**: Applied on top of P0 fixes (see `P0_FIX_SUMMARY.md`)

---

## P1 Fixes (Priority 1 — Correctness & Safety)

### P1-1: Reduce redundant `get_account()` calls in `execute_auto_trade`
**File**: `src/trade_executor.py`  
**Problem**: `daily_loss`, `exposure_cap`, and `max_loss` checks each independently called `get_account()`, resulting in 3 separate API calls per trade evaluation.  
**Fix**: Fetched `account_data` once after getting USDT balance, then pre-computed `_total_invested`, `_total_portfolio`, and `_account_price_map` for all three checks to reuse.

### P1-2: Narrow overly-broad `except Exception` clauses
**Files**: `src/risk_manager.py`, `src/cmd_trailing_check.py`  
**Problem**: Multiple `except Exception: pass` blocks silently swallowed unexpected errors, hiding bugs.  
**Fix**: 
- `risk_manager.py` (2 occurrences in `_llm_stop_loss_advisory`): Changed to `except (ImportError, AttributeError, KeyError, TypeError, ValueError, ConnectionError)`
- `cmd_trailing_check.py` (3 occurrences): Changed to `except (ConnectionError, TimeoutError, ValueError, KeyError, OSError)`

### P1-3: PnL recovery tier downgrade in circuit breaker
**File**: `src/daily_loss_breaker.py`  
**Problem**: Daily loss breaker upgraded the tier on losses but never downgraded when PnL turned positive, causing the system to stay in a restricted state permanently.  
**Fix**: Added `elif` branch in the tier logic: when realized PnL is positive, downgrade the tier by exactly one level (never below the base tier). Logs the downgrade event.

### P1-4: Eliminate mutable class-level cache in StrategyAdaptor
**File**: `src/strategy_adaptor.py`  
**Problem**: `_cache`, `_cache_ts`, `_btc_klines_cache`, `_btc_klines_ts` were class-level attributes, causing state leakage between instances and potential race conditions in concurrent scans.  
**Fix**: Moved all four attributes to `__init__` as instance attributes. All references updated from `ClassName.xxx` / bare `xxx` to `self.xxx`.

### P1-5: Atomic `sync_from_binance` via build-then-swap
**File**: `src/portfolio_state.py`  
**Problem**: `sync_from_binance` mutated `self.positions` incrementally — a mid-sync failure would leave a partially updated, inconsistent state.  
**Fix**: Implemented build-then-swap pattern: constructs a `_new_positions` temp dict during sync; on full success, swaps `self.positions` to point at it. On exception, rolls back to `old_positions`.

### P1-6: File lock for concurrent scan prevention
**File**: `src/scan_orchestrator.py`  
**Problem**: Overlapping cron-triggered scans could run concurrently, causing duplicate trades and race conditions.  
**Fix**: Added `fcntl.flock` non-blocking file lock (`/tmp/crypto-trader-scan.lock`). If lock acquisition fails, logs a warning and returns early (skips the scan). Lock is released in `finally` block.

### P1-7: Force SSL verification in production mode
**File**: `src/_binance_sdk_client.py`  
**Problem**: `VERIFY_SSL` could be set to `false` via env var even in production, exposing trades to MITM attacks.  
**Fix**: Added check — when `USE_TESTNET` is false and `VERIFY_SSL` env var is set to false, force `VERIFY_SSL = True` and log a warning explaining the override.

### P1-8: Fail-closed `count_active_positions`
**File**: `src/trade_executor.py`, `src/scan_orchestrator.py`  
**Problem**: `count_active_positions` returned `0` on exception, which the caller interpreted as "no positions — safe to trade," potentially opening a new position during an API outage.  
**Fix**: `count_active_positions` now returns `-1` on exception. Callers in `trade_executor.py` and `scan_orchestrator.py` check `if active_positions < 0` and block the trade.

### P1-9: Fix Python shebang in health_check.py
**File**: `scripts/health_check.py`  
**Problem**: Shebang was `#!/usr/bin/python3` (hardcoded path, may not exist on all systems).  
**Fix**: Changed to `#!/usr/bin/env python3`.

### P1-10: Correct PnL calculation in trailing stop check
**File**: `src/cmd_trailing_check.py`  
**Problem**: PnL calculation relied on `pos['total']` which could be `0` if the position qty was cached as zero, leading to incorrect trailing stop decisions.  
**Fix**: PnL now calculated from `client.get_my_trades()` to get actual executed quantities, rather than depending on potentially stale position data.

---

## P2 Fixes (Priority 2 — Robustness & Observability)

### P2-1: Structured trade logging with trade_id correlation
**File**: `src/trade_executor.py`  
**Problem**: Trade execution logs lacked a correlation ID, making it difficult to trace a single trade through START → ORDER → SL/TP → SUCCESS/FAILURE.  
**Fix**: Generates `_trade_id` in format `{symbol}_{timestamp}_{hex6}` at the start of `execute_auto_trade`. Carries it in START and SUCCESS log lines.

### P2-4: WAL checkpoint method for SQLite
**File**: `src/state_db.py`  
**Problem**: Long-running WAL-mode SQLite could accumulate large WAL files, degrading read performance.  
**Fix**: Added public `wal_checkpoint()` method that executes `PRAGMA wal_checkpoint(TRUNCATE)`. Can be called periodically by maintenance scripts.

### P2-5: Retry with exponential backoff for critical notifications
**File**: `src/notifier.py`  
**Problem**: Critical notifications (SL failure, trailing stop trigger) wrote to local JSON files without retry — a transient I/O error would silently lose the notification.  
**Fix**: Both `_append_notification` and `send_message` now retry file writes up to 3 times with exponential backoff (1s, 2s, 4s). On final failure, logs an error with the notification content so it's at least visible in logs.

### P2-6: Graceful shutdown on SIGTERM
**File**: `src/trade_executor.py`  
**Problem**: When the container/process received SIGTERM (e.g., during deployment), in-flight trade execution could start a new position mid-shutdown.  
**Fix**: Registered `signal.SIGTERM` handler that sets a module-level `_shutting_down` flag. `execute_auto_trade` checks this flag at entry and returns `{"success": False, "reason": "shutdown_in_progress"}` if set.

### P2-8: API weight usage monitoring
**File**: `src/_binance_sdk_client.py`  
**Problem**: No visibility into Binance API weight consumption, risking silent rate-limit exhaustion (Binance hard limit: 1200 weight/min).  
**Fix**: Added `_log_used_weight(error)` method that reads `X-MBX-USED-WEIGHT-1M` from ClientError response headers. Logs at DEBUG for normal usage, WARNING when weight > 1000. Called in all 8 `except ClientError` handlers throughout the client (get_klines, get_account, place_order, place_oco, cancel_order, get_open_orders, cancel_all_orders, get_order).

### P2-9: Rate-limit retry for `get_klines` (429/418)
**File**: `src/_binance_sdk_client.py`  
**Problem**: `get_klines` returned an empty list immediately on 429/418 rate-limit responses, even though the existing `max_retries` loop was designed for retries.  
**Fix**: On 429/418, now calls `_parse_retry_after()` to extract the `Retry-After` header value, sleeps, and `continue`s the retry loop (up to `max_retries` times). Only returns `[]` after all retries are exhausted. 400 errors still return immediately (bad request won't succeed on retry).

### P2-10: Fail-fast on proxy unavailability
**File**: `run_cron.sh`  
**Problem**: `ensure_proxy || true` silently continued even when all proxy nodes were unreachable, causing every subsequent API call to fail/timeout.  
**Fix**: Changed to `if ! ensure_proxy; then ... exit 1; fi` — logs "No proxy available, aborting" and exits with code 1 immediately.

---

## Skipped Items

| Item | Reason |
|------|--------|
| P2-2 | Test coverage expansion — out of scope for this maintenance pass |
| P2-3 | Configuration centralization — architectural change, deferred |
| P2-7 | Module overlap refactor — architectural change, deferred |

---

## Verification Results

| Check | Status |
|-------|--------|
| `python3 -c "import trade_executor; import circuit_breaker; ..."` | ✅ Pass |
| `python3 main.py --help` | ✅ Pass (prints usage) |
| `bash -n run_cron.sh` | ✅ Pass (syntax valid) |
| `python3 -c "import notifier; import _binance_sdk_client"` | ✅ Pass |
