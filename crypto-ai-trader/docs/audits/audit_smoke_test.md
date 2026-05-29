# Smoke Test Report — crypto-ai-trader

**Date:** 2026-04-04 00:50 HKT  
**Tester:** smoke-tester (subagent)  
**Environment:** Real Binance (testnet=False)

---

## Results

| # | Test | Status | Time | Notes |
|---|------|--------|------|-------|
| 1 | `main.py status` | ✅ Pass | <1s | Portfolio $403.32, 0 positions. Clean output. |
| 2 | `main.py scan` | ✅ Pass | 8s | Scanned 50 coins, found 0 opportunities (expected for conservative thresholds). Top gainers/losers displayed. |
| 3 | `cron-report` / `daily_report` | ⚠️ Partial | <1s | Data fetching works. **Known issue:** Feishu notifier has no webhook URL → `WARNING: No Feishu webhook URL configured`. Report generated but not sent anywhere. |
| 4a | BinanceClient: connect | ✅ Pass | <1s | Instantiated successfully |
| 4b | BinanceClient: get_account | ✅ Pass | <1s | 750 balances returned |
| 4c | BinanceClient: get_ticker_24h | ❌ Fail | — | **Method does not exist.** `AttributeError: 'BinanceClient' object has no attribute 'get_ticker_24h'`. Only `get_balance()` and `get_free_balance()` are exposed. |
| 5a | PortfolioManager: load state | ❌ Fail | — | **Method is private.** `_load_state()` exists but `load_state()` is not public. Same for `_save_state()`. |
| 5b | PortfolioManager: calculate_balances | ❌ Fail | — | No `calculate_balances()` method. Public methods are `get_balance(asset)` and `get_available_balance(asset)`. |
| 6 | Logging errors/warnings | ⚠️ Partial | — | Only warning: Feishu webhook missing. No crashes or unexpected errors in status/scan/daily_report. |

---

## Summary

**Pass: 4** | **Fail: 3** | **Partial: 2**

### Broken Commands / APIs

1. **BinanceClient.get_ticker_24h()** — method doesn't exist. The `main.py scan` path works because `MarketScanner` uses internal methods, not this public API. If external callers rely on `get_ticker_24h()`, they'll crash.

2. **PortfolioManager.load_state()** / **calculate_balances()** — both are private (`_load_state`, `_save_state`) or non-existent. Public API is `get_balance(asset)` and `get_available_balance(asset)`. State loads automatically on init.

3. **cron-report notifier** — Feishu webhook not configured. Data pipeline works; delivery is broken.

### Performance Notes

- `status`: instant
- `scan`: ~8s (acceptable for 50 coins with technical analysis)
- All API calls responsive, no timeouts

### Risk Assessment

The core CLI commands (`status`, `scan`, `daily_report`) work fine. The API surface has naming inconsistencies (private vs public methods) that would break external integrations. No read-only crash risk.
