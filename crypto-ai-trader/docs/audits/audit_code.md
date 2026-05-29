# Code Audit Report — crypto-ai-trader

**Date:** 2026-04-04  
**Auditor:** code-auditor (crypto-audit team)  
**Scope:** Full Python codebase under `/crypto-ai-trader/`

---

## Executive Summary

The system is a crypto spot trading bot with market scanning, multi-strategy signals, copy trading via AI-Trader platform, and automated execution. Overall code quality is reasonable for a solo project, but there are **several issues that could cause financial loss** and **one critical security gap**.

| Severity | Count | Examples |
|----------|-------|---------|
| 🔴 Critical | 3 | Stale price execution, TOCTOU race, uncovered quantity |
| 🟡 Warning | 8 | Thread safety gaps, no file locking, incomplete error handling |
| 🔵 Info | 5 | Code smells, minor improvements |

---

## 🔴 Critical Findings

### C1. Stale Price in Confirmation-Based Trades
**Files:** `src/pending_confirmation.py`, `handle_confirmation.py:39-41`

The pending confirmation saves `price` at scan time. When the user confirms (potentially minutes/hours later), `execute_trade()` uses that **stale price** for:
- Quantity calculation (`qty = int(invest_amount / price)`)
- Stop loss placement (`stop_price` from `pending["stop_price"]`)
- TP level calculation (`tp_price = price * (1 + tp["pct"] / 100)`)

If the price moves significantly, the quantity will be wrong and the SL/TP prices misaligned. A 20% price move could result in buying 20% more or fewer units than intended.

**Fix:** Fetch live price before execution and recalculate qty/SL/TP:
```python
live_stats = client.get_24hr_stats(symbol)
live_price = float(live_stats["last_price"])
# Recalculate qty, sl_price, tp_prices with live_price
```

### C2. TOCTOU Race in Copy Trading — Portfolio Check vs Order Execution
**File:** `run_copy_trading.py:77-109`

The pre-flight portfolio check happens **inside** `portfolio_lock`, but `binance_client.place_market_buy()` runs **outside** the lock. Between the check and the actual order, another thread (or the same callback on a different signal) could pass the same check and both buy the same coin.

The lock is released before the network call, then re-acquired for `add_position`. If two buy signals arrive close together for different coins, both could pass the max-positions check before either increments the count.

**Fix:** Use a "pending" flag inside the lock:
```python
with portfolio_lock:
    if base in portfolio.positions or base in _pending_buys:
        return
    _pending_buys.add(base)
try:
    result = binance_client.place_market_buy(...)
finally:
    with portfolio_lock:
        _pending_buys.discard(base)
```

### C3. Uncovered Quantity After Failed TP/SL Orders
**File:** `main.py:179-214`

In `execute_auto_trade()`, if both TP and SL orders fail (API rejection, insufficient balance after market buy, etc.), the code logs `"未保護: {remainder}"` but takes **no action**. The position exists on Binance with no stop loss or take profit — a market crash could cause total loss.

**Fix:** If SL fails, retry at least once. If still fails, send an urgent alert and consider placing a conditional order or manual monitoring flag. At minimum, the Feishu notification should highlight this as a critical warning.

---

## 🟡 Warning Findings

### W1. No File Locking on `portfolio_state.json`
**File:** `src/portfolio.py:257-279` (`_save_state`), `src/portfolio.py:282-302` (`_load_state`)

Multiple processes (e.g., `run_copy_trading.py` and `main.py cron-scan`) can read/write `portfolio_state.json` concurrently. The atomic `os.replace` prevents partial writes, but a read-modify-write cycle (load → modify → save) can still lose updates from another process.

**Fix:** Use `fcntl.flock()` or `filelock` library for advisory file locking during load+save cycles.

### W2. `portfolio_state.json` Hash Integrity Can Corrupt on Recovery
**File:** `src/portfolio.py:293-296`

If the hash check fails, the method silently returns with empty positions. This means a single corrupted byte causes **all position tracking to be wiped**. Positions still exist on Binance but are invisible to the risk manager.

**Fix:** On hash mismatch, back up the corrupted file, log an error, and either (a) attempt repair or (b) fall back to reading actual Binance positions instead of wiping state.

### W3. `handle_confirmation.py` Uses Hardcoded `int()` for Quantity
**File:** `handle_confirmation.py:41`

`qty = int(invest_amount / price)` — this truncates to integer. For many altcoin pairs, the step size is not 1 (e.g., BTC is 0.001). Using `int()` will place 0 quantity orders or fail.

**Fix:** Use `BinanceClient.get_price_precision` and step size from exchange info to properly round quantity, similar to how `place_order()` handles it.

### W4. `get_free_balance()` Not Cached Like `get_balance()`
**File:** `src/binance_client.py:178-188`

`get_free_balance()` calls `get_account()` every time with no caching, while `get_balance()` has a 30s cache. In `cmd_cron_scan()`, multiple calls to `get_free_balance` and `get_balance` hit the API repeatedly.

**Fix:** Apply the same cache pattern to `get_free_balance()`.

### W5. SSL Verification Can Be Disabled via Environment Variable
**File:** `src/binance_client.py:23`

`VERIFY_SSL = os.environ.get("VERIFY_SSL", "true")...` — while documented, this makes it trivially easy to disable TLS verification via environment, enabling MITM on API keys. An attacker who can set env vars can intercept all API traffic.

**Fix:** Remove the env toggle or require a more deliberate mechanism (e.g., a config file setting with a warning log).

### W6. Copy Trading Subscriber Memory Leak Potential
**File:** `src/ai4trade_subscriber.py:139-149`

`_prune_processed_ids` enforces max 10000 entries, but `_copied_trades` is capped at 100 and `CopiedTrade` objects store full signal data. If the subscriber runs for months without restart, the `_processed_signal_ids` OrderedDict can hold up to 10000 entries — not catastrophic but worth noting.

### W7. DCA State File Not Thread-Safe
**File:** `run_dca.py:44-52`

`save_state()` uses atomic write, but `load_state() → modify → save_state()` is not atomic. If DCA runs concurrently with another process modifying the file, state can be lost.

### W8. `cmd_scan` Publishes Fake Quantities to AI-Trader
**File:** `main.py:284-285`

```python
quantity = 0.01 * (score / 50)  # Scaled quantity
```
This publishes **artificial quantities** that don't correspond to real trades. Other traders copying these signals will get misleading information.

**Fix:** Only publish to AI-Trader when an actual trade is executed, or clearly label these as "analysis signals" not "trade signals."

---

## 🔵 Info Findings

### I1. Strategies Package Missing `strategies.py` — Backtester Import Path Mismatch
**File:** `src/backtester.py:15-18` imports from `src.strategies` (a package), but the actual strategies are in `src/strategies/` (a subpackage with `__init__.py`). This works because of the `__init__.py` exports, but the import path is `from .strategies import GridStrategy, ...` which could confuse maintainers.

### I2. `execute_auto_trade` Has Duplicate `from math import floor`
**File:** `main.py:138, 159` — imported twice in the same function scope.

### I3. `SentimentAnalyzer._score_sentiment` Is Extremely Naive
**File:** `src/sentiment.py:80-105` — keyword matching with fixed ±0.1 per word. The word "down" in "down-to-earth" would trigger a negative score. This is acknowledged as simple, but should not be relied upon for trading decisions.

### I4. `MarketScanner` (not shown but referenced) — No Volume Spike Validation
Market scanning appears to filter by volume and price change but there's no check for wash trading or artificial volume spikes (common in low-cap altcoins).

### I5. Hardcoded Testnet Comment in Production Confirmation Message
**File:** `main.py:335` — `"⚠️ Testnet 測試中，請知悉"` appears in the confirmation message, but `BinanceClient(testnet=False)` is used everywhere. This is a leftover from testing.

---

## Summary of Recommended Actions

| Priority | Action | Effort |
|----------|--------|--------|
| P0 | Fetch live price before executing confirmed trades (C1) | Small |
| P0 | Add pending-set for copy trading TOCTOU (C2) | Small |
| P0 | Alert + retry on failed SL orders (C3) | Small |
| P1 | Add file locking to portfolio state (W1) | Medium |
| P1 | Don't wipe positions on hash mismatch (W2) | Small |
| P1 | Fix qty rounding in handle_confirmation (W3) | Small |
| P2 | Remove fake signal publishing (W8) | Small |
| P2 | Remove VERIFY_SSL env toggle (W5) | Trivial |
| P3 | Cache get_free_balance (W4) | Trivial |
| P3 | Remove testnet comment (I5) | Trivial |
