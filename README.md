# trading-systems

Autonomous AI crypto trading system for **Binance SPOT** (spot only — no futures, no margin, no leverage).

## Repository layout

```
trading-systems/
├── crypto-ai-trader/   # The trading system (~30k LOC Python, 95+ modules)
└── start_singbox.sh    # Proxy keepalive helper used by cron
```

Project-specific docs live in `crypto-ai-trader/` (`docs/`, `wiki/`).

## What it does

`crypto-ai-trader` runs a full scan → research → adapt → execute loop on a cron schedule:

- **Market regime detection** — Fear & Greed index, BTC trend filter (EMA/RSI/MACD/ADX), BTC vs 100/200-SMA regime gate (`CONFIRMED_BULL` / normal), GARCH volatility adjustment
- **6 strategies** — grid, DCA, trend, RSI reversion, Bollinger, VWAP; enabled/disabled per regime by `StrategyAdaptor`
- **Six-dimension resonance analysis** — on-chain, liquidity, macro, sentiment, technical, regulatory, each weighted and scored before entry
- **Dynamic coin pool** — account-restricted symbols, dust, blacklist filtering; surge-adjusted scoring thresholds
- **Risk layer** — CircuitBreaker, DailyLossBreaker, DrawdownBreaker, ConsecutiveLossGuard, Kelly sizer, CVaR/correlation checks
- **A/B engine experiment (P0-C)** — group A (baseline percent stops) vs group B (ATR-based R-multiple scale-outs + Chandelier trailing), fully isolated paper portfolios
- **Paper + live trading** — identical logic, isolated state; live execution is SPOT market orders only. Paper dual-write rows into the trades ledger carry `client_order_id` prefixed `paper_` so readers can segregate simulated rows (WO-1029)
- **Learning loop** — trade outcomes sync, weekly backtest, weekly strategy review, contextual bandit priors, daily gated weight learning + concept-drift report (`scripts/daily_learning.py`)
- **Ops scripts** — health checks, TP/SL enforcement, trailing-stop checks, dust cleanup, daily report

## Quick start

```bash
cd crypto-ai-trader
pip install -r requirements.txt
cp .env.example .env   # fill in BINANCE_API_KEY / BINANCE_API_SECRET
python main.py status  # portfolio status
```

## Commands

```bash
# Core pipeline
python main.py cron-scan        # full scan → research → adapt → execute (cron entrypoint)
python main.py scan             # market scan only
python main.py trade            # single trading cycle
python main.py analyze <SYM>    # multi-timeframe technical analysis
python main.py sentiment        # sentiment analysis
python main.py onchain <SYM>    # on-chain / exchange data
python main.py backtest         # strategy backtesting
python main.py status           # portfolio status (syncs from Binance)
python main.py strategy-status  # current adapted strategy config

# Ops
python main.py trailing-check   # update trailing stop-loss orders
python main.py dust-check       # convert dust (<$1) to USDT/BNB
python main.py cron-report      # daily portfolio report

# Grid trading bot (separate subsystem, not part of the scan pipeline)
python grid_bot.py init --symbol SOLUSDT --capital 400 --grids 8 --range 5
python grid_bot.py start [--dry-run]

# Utility scripts
python scripts/auto_heal.py          # self-heal state inconsistencies
python scripts/health_check.py       # system health check
python scripts/ensure_tp_sl.py       # ensure all positions have TP/SL
python scripts/code_quality_guard.py # lint guard

# Cron wrappers (source crypto-secrets.env first)
bash run_scan.sh           # → cron-scan
bash run_daily_report.sh   # → cron-report
bash run_cron.sh <job>     # generic wrapper with env for any registered job

# Tests
pytest                                    # all tests (30s timeout, 60% coverage threshold)
pytest tests/test_crypto_system.py        # single file
pytest tests/test_crypto_system.py::name  # single test
pytest -m "not slow"                      # skip slow tests
pytest -m integration                     # integration tests only
```

## Architecture

### Exchange abstraction

All code imports `from src.binance_client import BinanceClient` — a proxy module that checks `USE_CCXT`:
- `USE_CCXT=1` → `src/ccxt_client.py` (ccxt-based, auto-retry + endpoint failover)
- default → `src/_binance_sdk_client.py` (python-binance SDK, idempotent order IDs, symbol filter validation)

`src/exchange_client.py` defines the `Protocol` interface both clients implement.

### State & persistence

- **StateDB** (`src/state_db.py`) — SQLite (WAL, thread-safe) at `data/state.db` (runtime state gitignored, lives outside the repo in production). Tables include `portfolio`, `trailing_stop`, `risk_guard`, `drawdown`, `trades`, `kv`, `trade_outcomes`, `decisions`. Generic `kv` holds breaker state, daily loss, strategy weights, cash balance. Singleton via `get_state_db()`.
- **EventBus** (`src/event_bus.py`) — in-process pub/sub backed by `data/events.db`.
- **PortfolioManager** (`src/portfolio.py`) — `PortfolioManager(PnlMixin, RiskMixin, StateMixin)`; Binance sync is the source of truth (reconciles positions, removes ghosts).

### Strategy system

Six strategies in `src/strategies/` (all extend `BaseStrategy`): Grid, DCA, Trend, RSI, Bollinger, VWAP.
- `strategy_adaptor.py` — enables/disables strategies and tunes params per regime (FEAR/NEUTRAL/GREED), overriding static YAML at runtime
- `strategy_registry.py` — per-coin selection by confidence weighted by historical performance (`trade_outcomes`)
- `strategy_evolver.py` — auto-promotes/demotes strategies (<40% win rate over 10+ trades → disable; >55% → re-enable)

### Risk stack (execution order)

| Component | File | Effect |
|-----------|------|--------|
| CircuitBreaker | `circuit_breaker.py` | 5+ API failures in 10min → 30min halt; 20% drawdown → indefinite halt |
| DailyLossBreaker | `daily_loss_breaker.py` | 3-tier: -1%→defensive, -2%→block new, -3%→close all + 24h halt |
| DrawdownBreaker | `drawdown_breaker.py` | 10% portfolio drawdown → hard stop, manual reset |
| StepwiseDrawdown | `stepwise_drawdown.py` | Graduated: 3-5%→x0.7, 5-8%→x0.4, 8%+→block/close |
| ConsecutiveLossGuard | `risk_manager.py` | 3+ consecutive losses → 24h pause |
| CorrelationRisk | `correlation_risk.py` | Blocks trades with >0.7 pairwise correlation |
| KellyPositionSizer | `kelly_sizer.py` | Kelly Criterion sizing, half-Kelly, capped at 50% |
| CVaR | `cvar_risk.py` | Scales sizes 0.3x-1.2x based on tail risk |

`RiskManager.pre_trade_check()` orchestrates the stack; execution adds price-anomaly filter before the market buy.

### Scoring & research

- Seven agents in `src/agents/` (Technical, Trend, Volume, Sentiment, OnChain, MarketSentiment, PrePump) each return `SpecialistResult` (score 0-100, signals, confidence); `DimensionScorer` runs the six-dimension market-wide framework.
- `MarketResearcher` — news via Jina/DDGS (crypto-entity filtered, WO-1040), dual-model LLM cross-verification, on-chain metrics; results cached 1h.
- `BearAnalyst` — devil's advocate; vetoes if bear_score > 70 and > opportunity_score.

### Paper trading

`src/paper_trader.py` mirrors live logic with isolated state (`paper_trades`, `paper_portfolio` tables; `DRYRUN=1` scan path). Since WO-1029 its dual-write rows into the shared `trades` ledger are tagged `client_order_id="paper_{order_id}_{unix_ts}"`, and ledger readers that must count real activity filter the `paper_` prefix (e.g. `trades_count_buys_since`).

## Testing

Tests use `unittest.mock` extensively — no live API calls. `conftest.py` provides:
- `mock_binance_spot` / `make_binance_client` — mocked exchange fixtures
- `_isolate_statedb` (autouse) — redirects StateDB to a temp file per test
- `_set_env` (autouse) — test env vars

Singletons are reset between tests via autouse fixtures. `tests/integration_recent_changes.py` is a **manual** integration checklist (no `test_` prefix — pytest does not collect it; it pins `TESTING=1` and a throwaway `STATE_DB_PATH` so even a direct run can't touch production state).

## Operations (cron)

| Job | Cadence | Purpose |
|-----|---------|---------|
| `cron-scan` | hourly (dynamic gate) | full trading cycle |
| `trailing-check` | every 5 min | adaptive trailing stops |
| `ensure_tp_sl.sh` | every 30 min | TP/SL consistency |
| `run_health.sh` | every 30 min | system health check |
| `sync-outcomes` | daily | trade outcome learning data |
| `daily-learning` | Mon-Sat 08:30 | gated weight learning + drift report (WO-1039) |
| `cron-report` / `run_weekly_backtest.sh` / learning pipeline | daily/weekly | reporting, backtests, strategy review |

Secrets come from `.env` files and are never committed.

## Key patterns

- Singletons: `StateDB` via `get_state_db()`, `EventBus` via `get_event_bus()`, `DailyLossBreaker` via `_dlb_instance`
- Binance is the source of truth for positions; `_sync_from_binance()` reconciles local state
- OCO orders preferred for TP+SL; separate SL/TP fallback if OCO fails
- Grid bot (`grid_bot.py` / `src/grid_trader.py`) is a separate subsystem from the scan pipeline
- Notifications via `FeishuNotifier` (Feishu/Lark webhook)
- Code in English; some config files and comments in Traditional Chinese

## Branching

- `main` — stable, production
- `feature/p0c-ab-engine` — A/B engine experiment line (kept in sync with `main`)
