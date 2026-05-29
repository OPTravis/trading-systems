# Silent Except Block Audit Report

## Summary

After thorough manual review of all 12 files (46+ except blocks examined), **the codebase is generally well-instrumented with proper logging**. The majority of except blocks already contain `logger.error()`, `logger.warning()`, or `logger.debug()` calls.

**Truly silent except blocks found: 11** (not 46 as originally estimated — most previously-flagged blocks already have logging).

### Priority Breakdown

| Priority | Count | Description |
|----------|-------|-------------|
| P0 CRITICAL | 0 | No truly silent blocks hiding order/trade errors |
| P1 HIGH | 2 | Silent blocks that could mask data/signal errors |
| P2 MEDIUM | 3 | Silent blocks returning defaults without logging |
| P3 LOW | 6 | Acceptable fallbacks or utility-level catches |

---

## P1 HIGH — Silent blocks that could cause wrong signals/scores

### 1. `market_scanner.py` line 518
```python
try:
    from src.online_learner import OnlineLearner
    _learner = OnlineLearner()
    _w = _learner.get_current_weights()
except Exception:
    _w = {  # hardcoded defaults
        "technical": 15.0, "trend": 15.0, "volume": 10.0,
        ...
    }
```
**Risk**: If OnlineLearner fails (DB corruption, import error, etc.), the scanner silently falls back to default weights. All scoring runs with wrong weights — could cause systematic over/under-trading. No way to know this happened.
**Fix**:
```python
except Exception as e:
    logger.warning("Failed to load learned weights, using defaults: %s", e, exc_info=True)
    _w = { ... }
```

### 2. `ccxt_client.py` line 571-572 — `get_symbol_filters()`
```python
except Exception:
    return {}
```
**Risk**: If exchange_info parsing fails, returns empty filters dict. Caller (place_order) then uses default precision (price_decimals=8, qty_decimals=4) which may be wrong for many symbols, causing orders to fail or use incorrect precision. No logging means silent precision mismatches.
**Fix**:
```python
except Exception as e:
    logger.error("Failed to get symbol filters for %s", symbol, exc_info=True)
    return {}
```

---

## P2 MEDIUM — Silent blocks returning defaults without logging

### 3. `_binance_sdk_client.py` line 895 — `get_symbol_filters()`
```python
except Exception:
    return {}
```
**Risk**: Same as #2 above but in the python-binance SDK client. Returns empty dict on failure, callers fall back to wrong defaults.
**Fix**:
```python
except Exception as e:
    logger.error("Failed to get symbol filters for %s", symbol, exc_info=True)
    return {}
```

### 4. `market_researcher.py` line 359 — exchange flow research
```python
except Exception:
    logger.error("Exchange flow research failed for %s", coin, exc_info=True)
```
**Actually has logging** — NOT truly silent. (False positive from initial audit.)

### 5. `self_healer.py` lines 130, 203, 226 — diagnostic return paths
```python
except Exception as e:
    return {"fixed": False, "msg": f"Verification error: {e}"}
```
**Risk**: These embed the error in the return value but never log to logger. The self_healer's callers may not propagate these messages. Errors in `_verify_price_deviation`, `_fix_hmm_covars_shape`, and `_fix_breaker_false_trigger` could go unnoticed.
**Fix**: Add `logger.error()` before the return in each.

### 6. `ccxt_client.py` line 1138 — `get_server_time()`
```python
except Exception:
    return int(time.time() * 1000)
```
**Risk**: Falls back to local time if Binance time fetch fails. If local clock is drifted >10s (recvWindow), all orders will fail with timestamp errors. Silent fallback makes diagnosis harder.
**Fix**:
```python
except Exception as e:
    logger.warning("Failed to get server time, using local: %s", e)
    return int(time.time() * 1000)
```

---

## P3 LOW — Acceptable fallbacks

### 7. `_binance_sdk_client.py` line 21 — Module import
```python
except ImportError:
    CRYPTO_SECRETS = GENERAL_SECRETS = None
    load_secret_file = lambda x: {}
```
**Assessment**: Acceptable. Optional secrets module import; callers handle None.

### 8. `_binance_sdk_client.py` line 57 — Retry-After parsing
```python
except (ValueError, TypeError):
    wait = default_wait
```
**Assessment**: Acceptable. Falls back to exponential default if Retry-After header is malformed.

### 9. `_binance_sdk_client.py` line 448 — Decimal floor utility
```python
except (InvalidOperation, ValueError):
    return float(value)
```
**Assessment**: Acceptable. Precision utility returns original value on bad input.

### 10. `_binance_sdk_client.py` line 843 — Server time fallback
```python
except Exception:
    return int(time.time() * 1000)
```
**Assessment**: Acceptable. Same as #6 but in the python-binance client; less dangerous since server time is only used for recvWindow validation.

### 11. `ccxt_client.py` line 595 — Decimal floor utility
```python
except (InvalidOperation, ValueError):
    return float(value)
```
**Assessment**: Acceptable. Same as #9.

### 12. `paper_trader.py` line 104 — Market load
```python
except Exception as e:
    logger.warning("PaperTrader: failed to load markets on init: %s", e)
```
**Actually has logging** — NOT silent. (False positive.)

---

## Files with NO silent except blocks

| File | Total except blocks | All have logging? |
|------|-------------------|-------------------|
| `llm_client.py` | 4 | ✅ Yes |
| `state_db.py` | 3 | ✅ Yes |
| `market_researcher.py` | 4 | ✅ Yes |
| `position_optimizer.py` | 4 | ✅ Yes |
| `smart_order.py` | 3 | ✅ Yes |
| `concept_drift.py` | 1 | ✅ Yes |
| `hmm_regime.py` | 2 | ✅ Yes |
| `market_scanner.py` | 1 | ❌ Silent (P1) |

---

## Critical Finding: No P0 CRITICAL Silent Except Blocks

**None of the 12 files have silent except blocks hiding order placement or trade execution errors.** All order-related except blocks in both `_binance_sdk_client.py` and `ccxt_client.py` properly log:
- `logger.error("Order failed (API error): ...")` for business errors
- `logger.error("Order unexpected error: ...")` for catch-alls  
- `logger.warning("Order network error ...")` + retry for network errors
- `logger.error("OCO business error ...")` for OCO failures

The `place_order()`, `place_oco()`, `cancel_order()`, `get_open_orders()`, and `cancel_all_orders()` methods all have proper error logging in both clients.

---

## Recommended Fixes (P1+P2 only)

**Total fixes needed: 5** (all low-effort one-line additions)

| # | File | Line | Fix |
|---|------|------|-----|
| 1 | `market_scanner.py` | 518 | Add `logger.warning("Failed to load learned weights: %s", e, exc_info=True)` |
| 2 | `ccxt_client.py` | 571 | Add `logger.error("Failed to get symbol filters for %s", symbol, exc_info=True)` |
| 3 | `_binance_sdk_client.py` | 895 | Add `logger.error("Failed to get symbol filters for %s", symbol, exc_info=True)` |
| 4 | `self_healer.py` | 130,203,226 | Add `logger.error()` before return in each except block |
| 5 | `ccxt_client.py` | 1138 | Add `logger.warning("Failed to get server time: %s", e)` |

---

*Audit completed: 2026-05-15*
*Auditor: Hermes Agent (automated code review)*
*Scope: All 12 files listed in original audit brief*
