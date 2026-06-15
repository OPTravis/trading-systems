# P2-3: Risk Parameter Centralization

## Overview

Consolidated all scattered risk control parameters from hardcoded Python constants
into a single unified configuration file `config/risk_params.yaml`, with a
centralized loader `src/risk_config.py`.

**Goal**: Single source of truth for all risk parameters, while maintaining
100% backward compatibility (zero behavior change).

## Files Created

| File | Purpose |
|------|---------|
| `config/risk_params.yaml` | Unified risk parameters configuration |
| `src/risk_config.py` | Loader with `load_risk_config()`, `get_risk_param()`, `get_section()` |

## Files Modified

| File | Changes |
|------|---------|
| `src/circuit_breaker.py` | `CONSECUTIVE_FAILURES_MAX`, `FAILURE_WINDOW_SEC`, `TRIP_DURATION_SEC`, `DRAWDOWN_TRIP_PCT`, `MAX_GHOST_POSITIONS` → loaded from config with hardcoded fallback |
| `src/daily_loss_breaker.py` | `TIER_1_LOSS_PCT`, `TIER_2_LOSS_PCT`, `TIER_3_LOSS_PCT` → loaded from config with hardcoded fallback |
| `src/drawdown_breaker.py` | `HARD_STOP_PCT` class attribute → loaded from config with hardcoded fallback |
| `src/stepwise_drawdown.py` | `LEVELS` dict, `ESCALATION_TIMEOUT_SECONDS` → loaded from config with hardcoded fallback |
| `src/trade_executor.py` | `MIN_STOP_LOSS_PCT`, `MAX_SINGLE_LOSS_PCT`, `max_positions`, `SL_LIMIT_BUFFER_PCT` → loaded from config with hardcoded fallback |

## Design Pattern

Each module follows the same backward-compatible pattern:

```python
# 1. Define hardcoded defaults (original values preserved)
_DEFAULT_SOME_PARAM = 5

# 2. Try loading from unified config
try:
    from src.risk_config import get_risk_param
    SOME_PARAM = get_risk_param("section", "key", _DEFAULT_SOME_PARAM)
except Exception:
    SOME_PARAM = _DEFAULT_SOME_PARAM

# 3. Module-level constant remains importable (backward compat)
#    from src.module import SOME_PARAM  # still works
```

**Properties**:
- If `config/risk_params.yaml` exists and has the key → uses config value
- If file exists but key is missing → uses hardcoded default
- If file doesn't exist → uses hardcoded default
- If `risk_config.py` itself fails → uses hardcoded default
- Module-level constant names unchanged → all imports continue to work

## Drawdown Threshold Clarification

Three distinct drawdown mechanisms operate at different layers. They are **not
conflicting** — each serves a different purpose and triggers in sequence:

| Layer | Module | Threshold | Scope | Action |
|-------|--------|-----------|-------|--------|
| 1st | `stepwise_drawdown` | 3% → 5% → 8% | Graduated | Reduce sizes progressively |
| 2nd | `drawdown_breaker` | 10% | Hard stop | Block ALL new trades |
| 3rd | `circuit_breaker` | 20% | Catastrophic | Indefinite system halt |

Additionally, `risk_limits.yaml` has `max_drawdown_pct: 15` used by the
scanner/strategy selection layer for pre-trade risk assessment.

### Trigger Order During Drawdown Event
```
3%  → stepwise: mild (reduce 30%)
5%  → stepwise: moderate (reduce 60%)
8%  → stepwise: severe (block new trades)
10% → stepwise: critical (close all)
      + drawdown_breaker: HARD STOP (block all, manual reset)
20% → circuit_breaker: TRIP (indefinite halt, manual reset)
```

## Parameter Values (unchanged from originals)

### circuit_breaker
| Parameter | Value |
|-----------|-------|
| consecutive_failures_max | 5 |
| failure_window_sec | 600 (10 min) |
| trip_duration_sec | 1800 (30 min) |
| drawdown_trip_pct | 20.0 (%) |
| max_ghost_positions | 3 |

### daily_loss_breaker
| Parameter | Value |
|-----------|-------|
| tier_1_loss_pct | 1.0 (%) |
| tier_2_loss_pct | 2.0 (%) |
| tier_3_loss_pct | 3.0 (%) |

### drawdown_breaker
| Parameter | Value |
|-----------|-------|
| hard_stop_pct | 0.10 (10%) |

### stepwise_drawdown
| Level | Range | Size Multiplier | SL Tightening | Block New | Close All |
|-------|-------|-----------------|---------------|-----------|-----------|
| normal | 0-3% | 1.0 | 1.0 | No | No |
| mild | 3-5% | 0.7 | 1.0 | No | No |
| moderate | 5-8% | 0.4 | 0.7 | No | No |
| severe | 8-10% | 0.0 | 0.5 | Yes | No |
| critical | >10% | 0.0 | 0.5 | Yes | Yes |

Escalation timeout: 7200s (2h in moderate → escalate to severe)

### trade_executor
| Parameter | Value |
|-----------|-------|
| min_stop_loss_pct | 3.0 (%) |
| max_single_loss_pct | 5.0 (%) |
| max_active_positions | 5 |
| sl_limit_buffer_pct | 0.015 (1.5%) |

## Verification Results

- ✅ `risk_config.load_risk_config()` — loads all sections correctly
- ✅ `python3 main.py` — starts without errors
- ✅ `pytest tests/test_circuit_breaker_unit.py tests/test_daily_loss_breaker_unit.py` — 36/36 passed
- ✅ All module-level constants remain importable (backward compat)
- ✅ No parameter values changed

## Migration Guide

To change a risk parameter, edit `config/risk_params.yaml`:

```yaml
# Example: tighten daily loss from 1% to 0.5%
daily_loss_breaker:
  tier_1_loss_pct: 0.5  # was 1.0
```

No code changes needed. The change takes effect on next module import.
To apply immediately, restart the trading system.
