# Core Module Audit Report

**Date:** 2026-05-02
**Scope:** trade_executor.py, risk_manager.py, strategy_adaptor.py, smart_order.py
**Auditor:** Hermes Kanban Worker (researcher profile)

---

## Executive Summary

Found **3 critical**, **5 high**, and **8 medium** issues across the four core modules. The most dangerous bugs are:

1. `smart_order.py` crashes on every call with valid symbol filters (NameError)
2. `strategy_adaptor.py` mutates a shared dict across calls, causing compounding parameter drift
3. `risk_manager.py` makes O(n) API calls per trade check, risking rate-limit bans

---

## CRITICAL Issues

### C1. smart_order.py L200-207: `NameError` crash in `calculate_position_size`

When `get_symbol_filters()` returns a valid result (the normal path), `quantity` is never assigned before it's used:

```python
# Line 196: usdt_amount is assigned
usdt_amount = available * score_factor * vol_factor

# Lines 200-204: early return only if filters are MISSING
filters = self.get_symbol_filters(symbol + 'USDT')
if not filters:
    quantity = usdt_amount / price    # assigned here
    return quantity, usdt_amount      # returns here

# Line 207: reached when filters exist — quantity is UNDEFINED
quantity = self.apply_qty_precision(quantity, filters)  # CRASH: NameError
```

**Impact:** `calculate_position_size()` crashes with `NameError: name 'quantity' is not defined` whenever symbol filters are available. This is the NORMAL code path — the function only works when filters are missing (the fallback path).

**Fix:** Move `quantity = usdt_amount / price` before the `if not filters` block.

---

### C2. strategy_adaptor.py L483: Mutable shared dict causes compounding parameter drift

In `_global_settings()`, the NEUTRAL regime shares the same dict object as `base`:

```python
base = {
    "score_threshold": 70,
    "max_position_pct": 15,
    ...
}
regime_map = {
    ...
    "NEUTRAL": base,  # ← alias, NOT a copy
    ...
}
settings = regime_map.get(regime, base)
# ... settings is mutated in-place below (lines 501-533)
```

When `regime == "NEUTRAL"`, `settings` is a reference to `base`. Mutations to `settings` permanently modify `base`:

- Call 1 (btc_score=60): `base["max_position_pct"]` = 13 (was 15)
- Call 2 (btc_score=60): `base["max_position_pct"]` = 11 (13 - 2)
- Call 3: 9, Call 4: 7, Call 5: 5 (floor)

The bug compounds every 5-minute cache window. After ~5 calls, `max_position_pct` bottoms out at 5%, permanently reducing position sizes to minimum.

**Impact:** Over a 24h trading session (~288 adapt() calls, ~58 with cache expiry), position sizes progressively shrink to minimum. Trades that should be 15% of portfolio become 5% — a 66% reduction in expected profit.

**Fix:** Replace `"NEUTRAL": base` with `"NEUTRAL": dict(base)` to create a shallow copy.

---

### C3. risk_manager.py L859-887: O(n) API calls in drawdown check

The drawdown breaker check in `pre_trade_check()` makes an individual `get_24hr_stats()` API call for EVERY non-dust asset:

```python
for b in acct.get("balances", []):
    if asset == "USDT":
        usdt_bal += total_qty
    elif total_qty > 0.0001:
        cached = self.client.get_24hr_stats(f"{asset}USDT")  # ← 1 API call per asset
```

For an account with 20 assets, this fires 20 API calls. Combined with the account query itself, that's 21 calls per `pre_trade_check()`. Binance spot rate limit is 1200 requests/minute, but this function runs before EVERY trade signal. During high-frequency scanning, this can exhaust the budget.

**Impact:** Rate limit exhaustion → failed API calls → missed trades or broken risk checks.

**Fix:** Use `get_24hr_stats()` (no argument) which returns all tickers in one call, same as `count_active_positions()` in trade_executor.py.

---

## HIGH Issues

### H1. trade_executor.py L204-216: Dead code — unused variables

Lines 204-216 compute `sl_reserve_pct`, `sl_reserve_qty`, and `tp_available_qty`. These variables are never read by any subsequent code. The Strategy A/B/C branches (lines 233+) recalculate everything independently.

```python
# Lines 204-216: computed but never used
sl_reserve_pct = 100 - total_tp_pct
sl_reserve_qty = ...
tp_available_qty = ...

# Lines 233+: Strategy A/B/C don't reference any of these
```

**Impact:** Confusion during maintenance. No runtime effect but signals copy-paste debt.

---

### H2. trade_executor.py L64-66: Unknown price inflates position count, blocks trading

When batch ticker fetch fails, `price_map` is empty. Every non-USDT/NTRN balance with `free > 0` gets `price = 0`, and this code counts them as real positions:

```python
elif price == 0:
    # Can't get price — assume it's a real position
    count += 1
```

If the account has 10 small dust balances and batch tickers fail, `active_positions` returns 10. Since `max_positions = 5`, ALL trading is blocked.

**Impact:** Temporary API outage can completely disable the trading system until tickers recover.

---

### H3. trade_executor.py L431: Portfolio balance doesn't account for trading fees

```python
portfolio.update_balance(usdt_bal - invest_amount)
```

`usdt_bal` was fetched BEFORE the trade. After execution, the actual balance is `usdt_bal - invest_amount - fees`. Binance spot fees are 0.1% (or 0.075% with BNB). Over many trades, this drift accumulates:

- 100 trades × $100 average → ~$10 in phantom balance
- Portfolio state shows more USDT than actually available

**Impact:** Subtle portfolio tracking inaccuracy. Could lead to overestimating available capital.

---

### H4. risk_manager.py L924-926: Trailing stop removed on partial closes

`post_trade_update()` unconditionally removes the trailing stop:

```python
self.trailing_stop.remove(symbol)
```

This doesn't distinguish between "position fully closed" and "partial close". If a TP order partially fills and the remaining position still needs a trailing stop, it's incorrectly removed.

**Impact:** Remaining position after partial TP has no trailing stop protection.

---

### H5. smart_order.py L67: Private method access breaks Protocol

```python
exchange_info = self.client._get_exchange_info()
```

`SmartOrder` calls `_get_exchange_info()` — a private method not in the `ExchangeClient` Protocol. Any non-Binance client implementation would fail with `AttributeError`.

**Impact:** Prevents future exchange migration. Any ExchangeClient that doesn't implement this private method breaks SmartOrder.

---

## MEDIUM Issues

### M1. trade_executor.py L186-201: Duplicate TP capping block

The same TP percentage capping logic appears twice (lines 186-192 and 196-201). After the first capping, `total_tp_pct` is recalculated, and the identical check runs again.

**Impact:** No runtime effect but dead code that creates confusion during maintenance.

---

### M2. trade_executor.py L344/372/396: `import time` inside exception handlers

```python
except Exception as e:
    if attempt == 0:
        import time; time.sleep(1)
```

`time` is imported inside retry exception handlers instead of at module level. While Python caches imports, this is unusual and could mask the real exception if the import fails (e.g., in restricted environments).

---

### M3. trade_executor.py L172: Fragile symbol-to-asset parsing

```python
asset = symbol.replace('USDT', '').replace('BUSD', '')
```

For `symbol = "BUSDUSDT"`, this produces `""` (empty string). For standard symbols like "BTCUSDT" it works fine, but edge cases exist.

---

### M4. risk_manager.py L101: TrendFilter 5-minute cache may serve stale risk data

The trend filter caches for 5 minutes. If BTC trend flips from BULLISH to BEARISH within this window, the stale cached result allows longs that should be blocked.

**Impact:** Up to 5 minutes of latency in risk response to sudden market regime changes.

---

### M5. risk_manager.py L375: Redundant `.upper()` call

```python
return self._state.get(symbol.upper())
```

`symbol` was already uppercased on line 371. The second `.upper()` is a no-op.

---

### M6. strategy_adaptor.py L304-311: History list re-creation on every call

```python
self._state["history"].append({...})
self._state["history"] = self._state["history"][-100:]
```

Creates a new list object every call even when history is below 100 entries. Should check length first.

---

### M7. smart_order.py L137: Individual price API calls in position enumeration

```python
price = self.get_price(asset + 'USDT')
```

`get_current_positions()` makes one API call per asset to get prices. For 10 positions, that's 10 calls. Should use batch ticker.

---

### M8. smart_order.py L482: Division by zero in risk_reward calculation

```python
'risk_reward': f"1:{round(sl_tp['tp1_pct'] / sl_tp['sl_pct'], 1)}",
```

If `sl_pct` is 0 (possible with extreme edge cases), this crashes with `ZeroDivisionError`.

---

## Positive Observations

1. **Atomic file writes** (risk_manager.py `_save_json`): Uses write-then-rename pattern to prevent corruption.
2. **SQLite primary with JSON fallback**: Good disaster recovery strategy in TrailingStop and ConsecutiveLossGuard.
3. **SL placed before TP**: Both trade_executor.py and smart_order.py correctly place stop-loss orders before take-profit to avoid balance locking issues.
4. **minNotional enforcement**: SmartOrder correctly checks and skips orders that would violate Binance's minimum notional filter.
5. **Graduated position sizing**: The tier system in trade_executor.py and regime-based sizing in strategy_adaptor.py are well-designed risk management approaches.
6. **Fail-safe defaults**: TrendFilter and RiskManager correctly default to blocking trades on error rather than allowing risky trades.

---

## Recommendations (Priority Order)

1. **Fix C1 immediately** — `calculate_position_size` is broken on the normal code path
2. **Fix C2 immediately** — mutating shared dict silently degrades trading over time
3. **Fix C3 this week** — rate limit risk grows with more assets
4. **Address H1-H5** in the next maintenance sprint
5. **Batch API calls** in M7 and C3 to reduce rate limit pressure
6. **Add integration tests** for the normal (non-fallback) paths in SmartOrder
