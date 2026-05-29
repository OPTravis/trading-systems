# Code Review Report — T10 Audit Fixes (commits 73ae9cc..HEAD)

**Reviewer:** reviewer  
**Date:** 2026-05-02  
**Commits:** 73ae9cc, 01eff3c, c9da10a, d3ba211, b11cb1c, 24f36e0, ba4fb7c

---

## Executive Summary

The changes span ~850 additions across 20 files. Overall the direction is good — multi-factor trend scoring, graduated position sizing, fee-aware trade execution, and phantom spike protection are all sound improvements. However there are **2 blocking issues** and several medium-severity concerns that should be addressed before merging.

---

## 🔴 BLOCKING Issues

### B1: `FUTURES_BASE` URL changed to `www.binance.com` — WRONG domain
**Files:** `src/data_feed.py` (lines ~456, ~729), `src/funding_arb.py` (line ~125)  
**Severity:** CRITICAL — will break funding rate and open interest queries

The `FUTURES_BASE` URL was changed from `https://fapi.binance.com` to `https://www.binance.com`. The `/fapi/v1/premiumIndex` and `/fapi/v1/openInterest` endpoints are **futures API endpoints** and must use `fapi.binance.com`. The `www.binance.com` domain serves the website, not the API.

```python
# WRONG (current):
FUTURES_BASE = "https://www.binance.com"

# CORRECT:
FUTURES_BASE = "https://fapi.binance.com"
```

This will cause 404 errors on every funding rate query and every open interest fetch, silently degrading the system's market awareness.

---

### B2: Test failure — `test_extreme_fear_regime` broken by btc_score change
**File:** `tests/test_crypto_system.py:357`  
**Severity:** HIGH — test suite broken

The `_decide_strategy` method now uses `btc_score` (default 50.0) instead of `btc_trend` for the bollinger-in-EXTREME_FEAR logic. The test passes `btc_trend="BEARISH"` but doesn't pass `btc_score`, so bollinger gets disabled (score 50 >= threshold 45).

Either:
- Update the test to pass `btc_score=30` (or similar) to simulate BEARISH
- Or make the bollinger EXTREME_FEAR logic also check `btc_trend` as fallback

---

## 🟡 Medium Severity

### M1: `count_active_positions` — no-price tokens silently skipped
**File:** `src/trade_executor.py:65-67`

Old code: `count += 1` (conservative — assumed real position)  
New code: `logger.warning(...); skip` (aggressive — assumes not real)

For tokens with $0 price (e.g., delisted, API error), the old behavior was safer: treat as active position to avoid over-allocating. The new behavior risks allocating too many positions if price data is temporarily unavailable. Consider a compromise: still count, but log a warning.

### M2: `_round_qty` uses `floor()` — correct but inconsistent with rest of codebase
**File:** `src/trade_executor.py:270`

The helper `_round_qty` correctly floors to avoid exceeding balance, which is good. But other places in the file still use `round(...)` for step-size alignment (e.g., `sl_qty = round(floor(...) * _step_size, _qty_decimals)` at line ~354). Consider using `_round_qty` consistently throughout.

### M3: OCO `aboveType`/`belowType` — verify these are accepted by Binance Spot API
**File:** `src/binance_client.py:702-703`

The OCO order now includes `aboveType="LIMIT_MAKER"` and `belowType="STOP_LOSS_LIMIT"`. These parameters exist on the **Futures** OCO endpoint (`/fapi/v1/order/oco`) but are **not standard on Spot OCO** (`/api/v3/order/oco`). Verify these are accepted; if not, the OCO order will be rejected and fall through to Strategy C every time.

### M4: `DrawdownBreaker.reset()` silently does nothing when no balance passed
**File:** `src/drawdown_breaker.py:175-179`

```python
if new_balance is not None and new_balance > 0:
    state["high_watermark"] = new_balance
else:
    pass  # no-op
```

The `pass` with no comment or log makes it easy to call `reset()` without a balance and get silent non-reset. Consider logging a warning or returning a status indicating nothing changed.

### M5: Phantom spike check uses hardcoded 3x multiplier
**File:** `src/drawdown_breaker.py:83-95`

The 3x watermark rejection is reasonable but may be too aggressive for high-volatility assets or after a long drawdown recovery. Consider making it configurable or using a time-decay (e.g., allow larger spikes if enough time has passed since the last watermark).

---

## 🟢 Low Severity / Style

### L1: `import time` moved to module level in `trade_executor.py`
Good cleanup — the `import time; time.sleep(1)` pattern was removed. Clean.

### L2: `FeishuNotifier = TelegramNotifier` alias removed
The class was renamed/refactored to just `FeishuNotifier`. Clean, no backward compat issues since all call sites updated.

### L3: `portfolio.db` is an empty file
Created as an empty file (0 bytes). If this is a SQLite database placeholder, it should either be created properly or removed and generated at runtime.

### L4: Research JSON files missing trailing newlines
`PENDLE_20260501.json`, `TAO_20260501.json`, `XUSD_20260430.json` — all end without `\n`. Minor but causes `git diff` noise.

### L5: `get_btc_dominance` — CoinGecko free API has rate limits
**File:** `src/data_feed.py:692-718`

The CoinGecko free API is rate-limited to ~10-30 req/min. If called frequently (e.g., per scan), this will start failing. Consider caching the result or using a longer TTL.

---

## What's Good ✅

1. **Multi-factor BTC trend scoring** — replacing SMA200-only with EMA cross + RSI + MACD + structure + volume is a solid upgrade. The weighted scoring (30/20/20/15/15) is reasonable.

2. **Fee-adjusted quantity calculation** — querying actual free balance after buy instead of using fills qty prevents the "not enough balance for SL" race condition. Good fix.

3. **SL/TP allocation rewrite** — pre-calculating all TP quantities first, then SL = total - sum(TP), ensures full coverage with no rounding gaps. Much cleaner than the previous approach.

4. **Graduated position sizing** — score-based scaling (100%/70%/50%/30%) instead of binary on/off is more nuanced and appropriate for a trading system.

5. **Phantom spike protection in DrawdownBreaker** — rejecting equity spikes >3x watermark prevents corrupting the high watermark from bad balance data.

6. **`extra_sl_qty` uses `floor()` instead of `round()`** — correctly prevents exceeding balance on the safety net order.

7. **`aboveType`/`belowType` on OCO** — if accepted by the API, this clarifies order intent and reduces ambiguity.

---

## Verdict

**REQUEST CHANGES** — fix B1 (futures URL) and B2 (test failure) before merge. M3 (OCO params) should also be verified. Everything else is optional but recommended.
