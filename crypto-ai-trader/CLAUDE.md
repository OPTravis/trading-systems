# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Binance SPOT AI trading system. Scans crypto markets, scores opportunities via multi-agent analysis, researches top candidates with LLM, and auto-executes trades with layered risk management. No futures/leverage/options — spot only.

## Commands

```bash
# Run the full scan → research → execute pipeline (cron job)
python main.py cron-scan

# Other main.py commands
python main.py scan              # Market scan only
python main.py status            # Portfolio status (syncs from Binance)
python main.py sentiment         # Sentiment analysis
python main.py analyze <SYM>     # Multi-timeframe technical analysis
python main.py onchain <SYM>     # On-chain/exchange data
python main.py backtest          # Strategy backtesting
python main.py trade             # Single trading cycle
python main.py trailing-check    # Update trailing stop-loss orders
python main.py dust-check        # Convert dust (<$1) to USDT/BNB
python main.py cron-report       # Daily portfolio report
python main.py strategy-status   # Current adapted strategy config

# Grid trading bot (separate from main pipeline)
python grid_bot.py init --symbol SOLUSDT --capital 400 --grids 8 --range 5
python grid_bot.py start [--dry-run]

# Tests
pytest                                    # Run all tests (30s timeout)
pytest tests/test_crypto_system.py        # Single test file
pytest tests/test_crypto_system.py::test_name  # Single test
pytest -m "not slow"                      # Skip slow tests
pytest -m integration                     # Integration tests only

# Utility scripts
python scripts/auto_heal.py               # Self-heal state inconsistencies
python scripts/health_check.py            # System health check
python scripts/ensure_tp_sl.py            # Ensure all positions have TP/SL
python scripts/code_quality_guard.py      # Lint guard

# Cron wrappers (source crypto-secrets.env before running)
bash run_scan.sh           # → cron-scan
bash run_daily_report.sh   # → cron-report
```

## Architecture

### Trading Pipeline (cron-scan)

Three sequential steps in `src/scan_orchestrator.py`:

1. **Scan** — Sync portfolio from Binance (source of truth), fetch sentiment/F&G, detect market regime via `StrategyAdaptor`, run six-dimension resonance scoring via `DimensionScorer`, scan market via `MarketScanner`, apply dynamic score threshold, optimize positions via `PositionOptimizer`.

2. **Research** — Pre-trade risk checks via `RiskManager.pre_trade_check()`, research top 3 candidates in parallel via `MarketResearcher` (news + LLM sentiment + on-chain metrics), run `BearAnalyst` as devil's advocate (can veto), pick best strategy via `StrategyRegistry` weighted voting.

3. **Execute** — If `AUTO_EXECUTE=true`, `trade_executor.execute_auto_trade()` handles: circuit breaker → daily loss → stepwise drawdown → position limits → Kelly sizing → price anomaly filter → market buy → OCO order (TP+SL).

### Exchange Abstraction

All code imports `from src.binance_client import BinanceClient`. This is a **proxy module** (8 lines) that checks `USE_CCXT` env var:
- `USE_CCXT=1` → `src/ccxt_client.py` (ccxt-based, with auto-retry and endpoint failover)
- Default → `src/_binance_sdk_client.py` (python-binance SDK, with idempotent order IDs and symbol filter validation)

`src/exchange_client.py` defines the `Protocol` interface (~20 methods) both clients implement.

### State & Persistence

- **StateDB** (`src/state_db.py`) — SQLite at `data/state.db`, thread-safe with WAL mode. 13 tables including `portfolio`, `trailing_stop`, `risk_guard`, `drawdown`, `trades`, `kv`, `trade_outcomes`, `decisions`. Generic `kv` table holds circuit breaker state, daily loss, stepwise drawdown, strategy weights, cash balance. Singleton via `get_state_db()`.
- **EventBus** (`src/event_bus.py`) — In-process pub/sub backed by `data/events.db`. Currently publishes after trade execution but has no active subscribers.
- **PortfolioManager** (`src/portfolio.py`) — Composed class: `PortfolioManager(PnlMixin, RiskMixin, StateMixin)`. In-memory `positions` dict loaded from SQLite. Binance sync is source of truth (reconciles positions, removes ghosts, updates quantities).

### Strategy System

Six strategies in `src/strategies/` (all extend `BaseStrategy`): Grid, DCA, Trend, RSI, Bollinger, VWAP.

- `strategy_adaptor.py` — Dynamically enables/disables strategies and adjusts params based on market regime (FEAR/NEUTRAL/GREED), BTC trend, volatility, funding rate, HMM regime, CVaR, GARCH. Overrides static YAML configs at runtime.
- `strategy_registry.py` — Runs all enabled strategies per coin, selects best by confidence weighted by historical performance from `trade_outcomes` table.
- `strategy_evolver.py` — Auto-promotes/demotes strategies: disables at <40% win rate (10+ trades), re-enables at >55%.

### Risk Management Stack

Layered checks in execution order:

| Component | File | Effect |
|-----------|------|--------|
| CircuitBreaker | `circuit_breaker.py` | 5+ API failures in 10min → 30min halt; 20% drawdown → indefinite halt |
| DailyLossBreaker | `daily_loss_breaker.py` | 3-tier: -1%→defensive, -2%→block new, -3%→close all+24h halt |
| DrawdownBreaker | `drawdown_breaker.py` | 10% portfolio drawdown → hard stop, manual reset |
| StepwiseDrawdown | `stepwise_drawdown.py` | Graduated: 3-5%→x0.7, 5-8%→x0.4, 8%+→block/close |
| ConsecutiveLossGuard | `risk_manager.py` | 3+ consecutive losses → 24h pause |
| CorrelationRisk | `correlation_risk.py` | Blocks trades with >0.7 pairwise correlation |
| KellyPositionSizer | `kelly_sizer.py` | Kelly Criterion sizing, half-Kelly, capped at 50% |
| CVaR | `cvar_risk.py` | Scales sizes 0.3x-1.2x based on tail risk |

`RiskManager` in `risk_manager.py` orchestrates all via `pre_trade_check()` and `post_trade_update()`.

### Agent/Scoring System

Seven agents in `src/agents/` (each returns `SpecialistResult` with score 0-100, signals, confidence): Technical, Trend, Volume, Sentiment, OnChain, MarketSentiment, PrePump.

`DimensionScorer` (`src/dimension_scorer.py`) implements a separate "Six-Dimension Resonance Framework" for market-wide assessment (On-Chain, Liquidity, Macro, Sentiment, Technical, Regulatory).

### Research/LLM Pipeline

- `MarketResearcher` (`src/market_researcher.py`) — News via Jina/DDGS, sentiment via dual-model LLM cross-verification (DeepSeek primary + mimo-v2.5-pro second opinion), on-chain from Binance futures API. Results cached 1hr in `data/research/{COIN}_{DATE}.json`.
- `LLMClient` (`src/llm_client.py`) — DeepSeek primary, GPT-4o-mini fallback, automatic retry on timeout/429/5xx.
- `BearAnalyst` (`src/bear_analyst.py`) — Devil's advocate: inverts metrics into bear_score, vetoes if bear_score>70 and >opportunity_score.

### Key Data Structures

**Position** (dict in `portfolio.positions`):
```python
{"symbol", "quantity", "entry_price", "current_price", "highest_price",
 "stop_loss", "take_profit", "trailing_stop_pct", "strategy",
 "created_at", "updated_at"}
```

**Signal/Opportunity** (dict from `MarketScanner.scan_all()`):
```python
{"symbol", "score" (0-100), "price", "signals" (list), "factor_scores",
 "technical_score", "trend_score", "volume_surge", "funding_rate"}
```

## Configuration

- `config/config.yaml` — LLM providers, retry settings, API key env var references
- `config/strategies.yaml` — Per-strategy params (RSI thresholds, MA periods, etc.). Static defaults; `StrategyAdaptor` overrides at runtime.
- `config/risk_limits.yaml` — Global limits (max_position_pct, max_exposure, cash_reserve), per-strategy SL/TP levels with tiered take-profit
- Secrets loaded from `crypto-secrets.env` (not tracked): `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `XIAOMI_API_KEY`

## Testing

Tests use `unittest.mock` extensively — no live API calls. `conftest.py` provides:
- `mock_binance_spot` — Mocked `binance.spot.Spot` with standard return values
- `make_binance_client` — Factory creating `BinanceClient` with mocked exchange
- `_isolate_statedb` (autouse) — Redirects StateDB to temp file per test
- `_set_env` (autouse) — Sets test env vars for BINANCE_API_KEY, TELEGRAM, AUTO_EXECUTE

StateDB and DailyLossBreaker singletons are reset between tests via autouse fixtures.

## Key Patterns

- Singletons: `StateDB` via `get_state_db()`, `EventBus` via `get_event_bus()`, `DailyLossBreaker` via `_dlb_instance`
- Python 3.11.15 compat shim at top of `main.py` and `grid_bot.py`: patches `random.randbits`
- Binance is the source of truth for positions — `_sync_from_binance()` reconciles local state
- OCO orders are preferred for TP+SL; falls back to separate SL/TP orders if OCO fails
- Grid bot (`grid_bot.py` / `src/grid_trader.py`) is a separate subsystem from the main scan pipeline
- Notifications go through `FeishuNotifier` (Feishu/Lark webhook)
