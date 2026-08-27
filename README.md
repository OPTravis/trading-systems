# trading-systems

Autonomous AI crypto trading system for **Binance SPOT** (spot only — no futures, no margin, no leverage).

## Repository layout

```
trading-systems/
├── crypto-ai-trader/   # The trading system (~30k LOC Python, 95+ modules)
├── start_singbox.sh    # Proxy keepalive helper used by cron
└── CLAUDE.md           # Guidance for AI coding agents
```

Project-specific docs live in `crypto-ai-trader/` (`CLAUDE.md`, `docs/`, `wiki/`).

## What it does

`crypto-ai-trader` runs a full scan → research → adapt → execute loop on a cron schedule:

- **Market regime detection** — Fear & Greed index, BTC trend filter (EMA/RSI/MACD/ADX), BTC vs 100/200-SMA regime gate (`CONFIRMED_BULL` / normal), GARCH volatility adjustment
- **6 strategies** — grid, DCA, trend, RSI reversion, Bollinger, VWAP; enabled/disabled per regime by `StrategyAdaptor`
- **Six-dimension resonance analysis** — on-chain, liquidity, macro, sentiment, technical, regulatory, each weighted and scored before entry
- **Dynamic coin pool** — account-restricted symbols, dust, blacklist filtering; surge-adjusted scoring thresholds
- **Risk layer** — CircuitBreaker, DailyLossBreaker, DrawdownBreaker, ConsecutiveLossGuard, Kelly sizer, CVaR/correlation checks
- **A/B engine experiment (P0-C)** — group A (baseline percent stops) vs group B (ATR-based R-multiple scale-outs + Chandelier trailing), fully isolated paper portfolios
- **Paper + live trading** — identical logic, isolated state; live execution is SPOT market orders only
- **Learning loop** — trade outcomes sync, weekly backtest, weekly strategy review, contextual bandit priors
- **Ops scripts** — health checks, TP/SL enforcement, trailing-stop checks, dust cleanup, daily report

## Quick start

```bash
cd crypto-ai-trader
pip install -r requirements.txt
cp .env.example .env   # fill in BINANCE_API_KEY / BINANCE_API_SECRET
python main.py status  # portfolio status
```

Common commands (see `CLAUDE.md` for the full list):

```bash
python main.py scan             # market scan only
python main.py cron-scan        # full scan → research → adapt → execute
python main.py analyze BTCUSDT  # multi-timeframe analysis
python main.py backtest
pytest                          # test suite (60% coverage threshold)
```

## Operations (cron)

| Job | Cadence | Purpose |
|-----|---------|---------|
| `cron-scan` | hourly (dynamic gate) | full trading cycle |
| `trailing-check` | every 5 min | adaptive trailing stops |
| `ensure_tp_sl.sh` | every 30 min | TP/SL consistency |
| `run_health.sh` | every 30 min | system health check |
| `sync-outcomes` | daily | trade outcome learning data |
| `cron-report` / `run_weekly_backtest.sh` / learning pipeline | daily/weekly | reporting, backtests, strategy review |

Runtime state (SQLite, WAL mode) is gitignored and lives outside the repo; only code and config are tracked. Secrets come from `.env` files and are never committed.

## Branching

- `main` — stable, production
- `feature/p0c-ab-engine` — A/B engine experiment line (kept in sync with `main`)
