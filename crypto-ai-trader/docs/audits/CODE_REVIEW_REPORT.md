# Code Quality & Technical Debt Review — crypto-ai-trader
# Date: 2026-05-13 | Reviewer: kanban-worker

## Summary

Total source: 24,934 LOC across 90+ Python files (src/).
105 `except Exception:` clauses, 59 of which silently swallow exceptions.
Test suite hangs on full run and has 1 known failure.
No central config.yaml; config split across 2 YAML files with no unified loader.
All 8 dependencies unpinned (>= only).

---

## CRITICAL — Must Fix Before Production

### C1. Silent Exception Swallowing (59 locations)
Fifty-nine `except` handlers use bare `pass` or `continue`, hiding failures
in a financial trading system. Worst offenders:

| File                       | Count | Lines (sample)                        |
|---------------------------|-------|---------------------------------------|
| trade_executor.py         | 6     | 172,174,336,508,600,653              |
| scan_orchestrator.py      | 5     | 104,120,236,327,733                  |
| data_feed.py              | 4     | 627,756,760,972                      |
| binance_client.py         | 5     | 419,489,673,831,863                  |
| market_researcher.py      | 4     | 357,373,431,740                      |
| position_optimizer.py     | 4     | 121,180,407,495                      |
| state_db.py               | 3     | 55,100,333                           |
| strategy_adaptor.py       | 3     | 140,160,410                          |
| twap_vwap.py              | 3     | 150,248,250                          |

Impact: Failed trades, orphaned orders, silent data corruption. A single
swallowed exception in `trade_executor.py` could lose real money.

### C2. Overly Broad `except Exception:` (105 locations)
105 catch-all handlers across the codebase. While some have logging, many
do not. Combined with C1, this creates a fire-and-forget error culture.
The `except Exception:` pattern also catches `KeyboardInterrupt` subclasses
and `SystemExit` in some Python versions.

### C3. Test Suite Does Not Complete
Full `pytest tests/` times out (>120s) without finishing. Partial run shows:
  - 14 passed, 1 failed
  - Failure: `test_technical_agent_factor_technical_subscores`
  - PytestUnknownMarkWarning: `@pytest.mark.slow` not registered
  - 328 tests collected, but only ~15 complete before timeout

Impact: No CI/CD validation possible. Cannot confirm system health.

### C4. FUTURES_BASE URL Hardcoded Wrong
`data_feed.py:516` and `data_feed.py:800` both set:
  `FUTURES_BASE = "https://www.binance.com"`
This is the web frontend, not the API. Futures API is `https://fapi.binance.com`.
Previous review flagged this but it remains unfixed.

---

## HIGH — Significant Technical Debt

### H1. God Object: scan_orchestrator.py (766 LOC, 17 deps)
- 766 lines, 3 functions (cmd_scan: 145 lines, _sync_from_binance: 98 lines, cmd_cron_scan: 494 lines)
- Imports 17 other src modules — highest coupling in codebase
- `cmd_cron_scan()` at 494 lines handles: scanning, research, adaptation,
  execution, confirmation, trailing stops, reporting
- 5 swallowed exceptions inside

### H2. God Object: data_feed.py (1128 LOC, 7 classes)
- 1128 lines, 7 classes in one file
- Second-largest file in the project
- 4 swallowed exceptions, wrong FUTURES_BASE URL

### H3. All Dependencies Unpinned (supply chain risk)
requirements.txt uses `>=` for all 8 packages:
```
binance-connector>=3.12.0   # installed 3.12.0, latest 3.13.0
pandas>=2.0.0               # installed 3.0.2, latest 3.0.3
numpy>=1.24.0               # installed 1.26.4, latest 2.4.4
requests>=2.31.0            # installed 2.33.1, latest 2.34.0
PyYAML>=6.0
backtrader>=1.9.78
ta>=0.10.0
python-dotenv>=1.0.0
```
numpy 2.x is a major version jump with breaking changes. Any `pip install`
could pull incompatible versions.

### H4. Duplicate Config Files + No Unified Loader
- `config/risk_limits.yaml` — risk params + strategy stop/take-profit
- `config/strategies.yaml` — strategy params + enabled flags
- No single config.yaml; no unified config loader
- Config loaded ad-hoc in 4 different files (portfolio.py, scan_orchestrator.py, notifier.py, backtester.py)
- Strategy params defined in BOTH YAML files with no single source of truth

### H5. Duplicate ENV Keys
`DEEPSEEK_API_KEY` and `DEEPSEEK_BASE_URL` appear in BOTH:
- `.env` (4 keys)
- `crypto-secrets.env` (12 keys)
This creates confusion about which file is authoritative.

### H6. Stray Artifacts in Repo Root
- File literally named: `SELECT symbol, quantity, entry_price, stop_loss, take_profit FROM portfolio` (0 bytes, created May 6)
- `.pending_confirmation.json` — runtime state checked into repo
- `.pending_hint.txt` — runtime state
- `state.db`, `portfolio.db` — SQLite databases in repo root
- `audit_script.py` — audit tool left in root
- `audit_code.md`, `audit_config.md`, `audit_smoke_test.md` — old audit notes

---

## MEDIUM

### M1. Single Points of Failure (coupling)
- `state_db` imported by 23 modules — any change ripples everywhere
- `binance_client` imported by 12 modules
- `exchange_client` imported by 8 modules

### M2. 499 Magic Decimal Numbers
Hardcoded numeric literals throughout source code (0.05, 0.1, 1.5, etc.)
not defined as named constants or pulled from config.

### M3. main_new.py Alternate Entry Point
`main_new.py` (55 lines) appears to be an alternate/simplified entry point.
Exists alongside `main.py` (55+ lines) — unclear which is canonical.

### M4. 8 Deprecated Files Still in .deprecated/
Files from old architecture remain in the repo:
- ai4trade_integration.py
- run_copy_trading.py
- 6 scripts in .deprecated/scripts/

### M5. 19+ Accumulated Audit/Report Markdown Files
19 audit/report files in root directory. Most are outdated (Apr 23-26).
These create noise and make it hard to find the current state.

### M6. No Test Coverage Measurement
- No `pytest-cov` in requirements
- No `.coveragerc` or coverage configuration
- Cannot quantify actual coverage

### M7. Legacy Fallback Code
Three files still have "legacy" fallback paths:
- grid_trader.py:61-70 (legacy single-grid fallback)
- risk_manager.py:119 (legacy format conversion)
- funding_arb.py:77 (legacy JSON fallback)

### M8. 36 time.sleep() Calls in Production Code
Blocking sleeps in src/ code suggest synchronous retry loops or rate
limiting that should use async patterns or proper backoff.

---

## LOW

### L1. TODO/FIXME Markers (3 total — clean)
- trade_executor.py:71 — "TODO: Migrate to KellyPositionSizer"
- sector_classifier.py:386 — "TODO: map clusters to sectors"
- self_heal_check.py:73 — regex pattern (false positive)

### L2. Type Hint Coverage: 64%
467 of 729 functions have return type annotations. Good but incomplete.

### L3. No Circular Imports (good)
Import graph is acyclic. scan_orchestrator is the most coupled but
doesn't create cycles.

### L4. .gitignore Properly Configured
Secrets (.env, *.key, *.pem, *.secret) are gitignored.

---

## Recommendations (Priority Order)

1. **Fix exception handling in trade_executor.py and scan_orchestrator.py**
   — These handle money. Every swallowed exception is potential loss.
   Minimum: add `logger.exception()` before pass/continue.

2. **Pin all dependencies** — use `==` or generate `requirements.lock`.
   Especially numpy (2.x breaking changes).

3. **Fix test suite timeout** — identify hanging test, add timeouts,
   register pytest marks. CI must pass.

4. **Fix FUTURES_BASE URL** in data_feed.py: `fapi.binance.com`

5. **Unify config** — single config.yaml with a loader module.

6. **Clean repo root** — delete stray artifacts, move audit files to docs/,
   gitignore *.db and .pending_*.

7. **Extract God Objects** — split scan_orchestrator.cmd_cron_scan() into
   smaller functions; split data_feed.py into separate modules.

8. **Add pytest-cov** — measure and track test coverage.

9. **Remove .deprecated/** or at minimum document why it's retained.

10. **Deduplicate env keys** — single source of truth for secrets.
