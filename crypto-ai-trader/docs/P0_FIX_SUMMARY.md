# P0 Fix Summary

**Date**: 2026-06-12  
**Scope**: 7 P0 security/stability fixes for crypto-ai-trader  
**Constraint**: SPOT-only trading system, no futures API access

---

## P0-1. Safety Checks: fail-open → fail-closed

**File**: `src/trade_executor.py`  
**Lines**: L46-50, L65-73

### Changes
- `_check_price_deviation()`: Exception handler now returns `False` (block trade) instead of `True` (allow trade)
- `_check_duplicate_order()`: Exception handler now returns `False` (block trade) instead of `True` (allow trade)
- Log level changed from `warning` to `error`
- Log message changed from "allowing trade (fail-open)" to "BLOCKING trade (fail-closed)"

### Rationale
When a safety check itself fails (e.g., API timeout), the system should NOT allow the trade to proceed. Fail-closed prevents trades during uncertain conditions.

### Call-site verification
Callers use `if not _check_price_deviation(...)` and `if not _check_duplicate_order(...)` — returning `False` correctly blocks the trade. No call-site changes needed.

---

## P0-2. WebSocket Proxy Support

**File**: `src/ws_user_stream.py`  
**Lines**: L36, L131-137, L141, L175, L205, L314-330

### Changes
- Added `_get_proxy_url()` helper: reads `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY` env vars, normalizes socks5→http, falls back to `http://127.0.0.1:17890`
- Added `_get_requests_proxies()` helper: returns `{"http": proxy, "https": proxy}` dict for requests library
- `_get_listen_key()`: REST POST now passes `proxies=_get_requests_proxies()`
- `_keepalive_listen_key()`: REST PUT now passes `proxies=_get_requests_proxies()`
- `_run_websocket()`: `run_forever()` now passes `http_proxy_host` and `http_proxy_port` parsed from proxy URL

### Rationale
Binance WebSocket (`wss://stream.binance.com:9443`) and REST API (`api3.binance.com`) are unreachable from domestic cloud without proxy. All connections now route through the sing-box HTTP proxy.

### Dependencies
- `websocket-client` 1.9.0 already installed ✅

---

## P0-3. Futures API (fapi.binance.com) Removal

**Files**: 
- `src/scan_orchestrator.py` (L167-180): Removed BTC funding rate fetch from fapi.binance.com, set `btc_funding_rate = 0.0`
- `src/market_researcher.py` (L423-512): Removed futures funding rate, long/short ratio, taker ratio, and open interest fetches
- `src/data_feed_funding.py` (L44-49): Activated `ENABLE_FUTURES` gate (was commented out)
- `src/data_feed_oi.py`: Already had `ENABLE_FUTURES` gate active

### Remaining references
- `data_feed_funding.py`/`data_feed_oi.py` class constants (`FUTURES_BASE`): Retained but gated by `ENABLE_FUTURES` env var (not set → all calls return empty/None)
- `funding_arb.py`: Dead code (not imported anywhere in the codebase)
- Comments in `scan_orchestrator.py` and `market_researcher.py`: Explanatory only

### Rationale
This system only does SPOT trading. Futures API calls to `fapi.binance.com` are unreachable from domestic cloud and unnecessary. StrategyAdaptor receives `funding_rate=0.0` and adapts accordingly.

---

## P0-4. Circuit Breaker Thread Safety

**File**: `src/circuit_breaker.py`  
**Lines**: L27, L42, L89-119, L122-168, L174-205, L220-232

### Changes
- Added `import threading` and `self._lock = threading.Lock()` in `__init__`
- `is_tripped()`: Wrapped in `with self._lock:`, calls `_reset_unlocked()` instead of `reset()` to avoid deadlock
- `record_failure()`: Wrapped in `with self._lock:`
- `record_success()`: Wrapped in `with self._lock:`
- `check_drawdown()`: Wrapped in `with self._lock:`
- `reset()`: Wrapped in `with self._lock:`, delegates to `_reset_unlocked()`
- `get_status()`: Wrapped in `with self._lock:` (removed `is_tripped()` call to avoid recursive lock)
- Added `_reset_unlocked()`: Internal reset without lock acquisition (for use when lock already held)
- Singleton `get_circuit_breaker()`: Added double-checked locking with `_cb_singleton_lock`

### Rationale
Cron jobs and WebSocket threads can access the CircuitBreaker concurrently. Without thread safety, race conditions could cause incorrect trip/reset behavior, potentially allowing trades during failure states.

---

## P0-5. execute_auto_trade Refactoring

**File**: `src/trade_executor.py`  
**Lines**: L100-500 (original), L132-225 (new helpers)

### Changes

#### Extracted helper functions:
1. **`_send_execution_notification()`** (L132-157): Formats and sends Feishu notification after trade execution. Previously inline (30+ lines).
2. **`_record_trade_portfolio()`** (L160-225): Records trade in portfolio state and publishes events to event bus. Previously inline (60+ lines).

#### Bare `except Exception: pass` fixes (5 total):
All 5 instances of bare `except Exception: pass` replaced with `logger.debug(..., exc_info=True)`:
- L479: ContextualBandit HMM regime fetch
- L488: ContextualBandit FearGreed fetch  
- L496: ContextualBandit BTC trend fetch
- L548: Portfolio value ticker price fetch
- L644: Max loss calc ticker price fetch

### Rationale
The 500+ line God function is reduced by ~90 lines through extraction. All extracted functions maintain identical logic. Bare except patterns now log exceptions for debugging instead of silently swallowing errors.

---

## P0-6. Trailing Stop SL Swap — Naked Window Elimination

**File**: `src/cmd_trailing_check.py`  
**Lines**: L207-280

### Changes

**Before (vulnerable)**:
```
1. Cancel old SL
2. Place new SL  ← gap: if this fails, position is naked
```

**After (safe)**:
```
1. Place new SL (retry up to 3 times)
2. If success → cancel old SL
3. If old SL cancel fails → OK (both SLs exist, both are SELL)
4. If new SL fails 3x → keep old SL, send alert
```

### Key safety guarantees
- Position is **never** without SL protection during the swap
- If new SL placement fails after 3 retries, old SL is preserved
- Alert notification sent when retries exhausted
- If old SL cancel fails (but new SL succeeded), both orders coexist harmlessly

---

## P0-7. Proxy Password from Environment Variable

**File**: `run_cron.sh`  
**Line**: L27

### Changes
```bash
# Before:
NODE_PASSWORD="passwd"

# After:
NODE_PASSWORD="${SINGBOX_PASSWORD:-passwd}"
```

### Rationale
The hardcoded password is now configurable via the `SINGBOX_PASSWORD` environment variable, with `passwd` as fallback default. The `.env` file can optionally include `SINGBOX_PASSWORD=<new_password>` to override. No other logic in `run_cron.sh` was modified.

---

## Verification Results

```
✅ All modified modules import OK
✅ Fail-closed safety checks verified
✅ CircuitBreaker thread safety verified (_lock, _reset_unlocked)
✅ ws_user_stream proxy support verified (_get_proxy_url, _get_requests_proxies)
✅ execute_auto_trade refactoring verified (_send_execution_notification, _record_trade_portfolio)
✅ Zero remaining bare except:pass patterns in trade_executor.py
✅ python3 main.py --help: system starts correctly
```

## Files Modified

| File | P0 Items |
|------|----------|
| `src/trade_executor.py` | P0-1, P0-5 |
| `src/ws_user_stream.py` | P0-2 |
| `src/scan_orchestrator.py` | P0-3 |
| `src/market_researcher.py` | P0-3 |
| `src/data_feed_funding.py` | P0-3 |
| `src/circuit_breaker.py` | P0-4 |
| `src/cmd_trailing_check.py` | P0-6 |
| `run_cron.sh` | P0-7 |
