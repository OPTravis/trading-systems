# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Global stock automated trading system based on factor investing + cross-sectional ranking. SPOT ONLY — no futures, options, or leverage. Supports multi-market trading (US, HK, CN, JP, UK, EU, AU) via IBKR as primary broker with Alpaca as backup.

## Commands

```bash
# CLI commands
python main.py scan [--universe global] [--market US]          # Scan universe, generate trade signals
python main.py status [--detailed] [--live]                    # Portfolio positions, P&L, risk state
python main.py analyze AAPL MSFT                               # Deep analysis (fundamental + technical + sentiment)
python main.py trade [--dry-run] [--confirm]                   # Execute trades
python main.py backtest --strategy momentum --from 2024-01-01  # Walk-forward backtest

# Scripts
./scripts/run_scan.sh                     # Cron-ready scan entry point
./scripts/run_daily_report.sh             # Daily portfolio report via Feishu
python scripts/walk_forward.py            # Walk-forward validation

# Testing
pytest                                    # Run all tests
pytest tests/test_basic.py                # Run single test file
pytest -x                                # Stop on first failure

# Linting / formatting
ruff check src/ tests/
black src/ tests/
```

## Architecture

### 5-Phase Scan Pipeline (`src/scan_orchestrator.py`)

The core execution pipeline, orchestrated by `ScanOrchestrator`:

1. **Phase 1 — Sync & Screen**: Sync portfolio from broker, detect market regime via `RegimeDetector`, screen universe from `config/universes.yaml`
2. **Phase 2 — Score & Rank**: `StockScorer` computes multi-dimensional scores (technical, fundamental, momentum, sentiment, quality, value), `CompositeRanker` produces final cross-sectional ranking
3. **Phase 3 — Research**: `StockResearcher` deep-dives top N candidates (news via `NewsFeed`, sentiment via FinBERT, fundamentals via `FundamentalFeed`, SEC filings)
4. **Phase 4 — Risk Checks**: `StockRiskManager` orchestrates PDT guard, earnings blackout, settlement guard, VIX position scaling
5. **Phase 5 — Execute**: `TradeExecutor` places orders via broker with smart exchange routing (NYSE vs NASDAQ), position sizing via `HybridPositionSizer` (Kelly × CVaR × Vol Target)

### Key Modules

- **`src/brokers/`** — `BrokerProtocol` abstract interface with implementations: `IBKRClient` (async, ib_async), `SyncIBKRWrapper` (sync wrapper for CLI), `PaperClient`, `AlpacaClient`. `CPGClient` connects to a localhost CPG proxy for live IBKR accounts.
- **`src/strategies/`** — `BaseStrategy` abstract class with concrete: `momentum`, `mean_revert`, `trend_strategy`. Each generates `Signal` objects with action/confidence/metadata.
- **`src/scoring/`** — `StockScorer` (multi-dimensional scoring), `CompositeRanker` (cross-sectional percentile ranking), `FundamentalScorer`, `SentimentScorer`.
- **`src/factors/`** — Factor computation pipeline. Factor weights configured in `config/factors.yaml`, with IC (Information Coefficient) dynamic reweighting via `FeatureStore`.
- **`src/risk/`** — `StockRiskManager` orchestrates: `PDTGuard`, `EarningsBlackout`, `SettlementGuard`, `VIXPositionScale`, `VolTargetSizer`. All limits configured in `config/risk_limits.yaml`.
- **`src/data/`** — Data feeds: `StockDataFeed` (OHLCV), `FundamentalFeed`, `NewsFeed`, `SentimentFeed`, `InsiderTrading`, `SEC Filings`, `AnalystRatings`, `EarningsCalendar`, `SectorData`. `FeatureStore` persists factor data in DuckDB.
- **`src/execution/`** — Order execution: `OrderExecutor` base, `TWAPExecutor` (time-sliced for large orders), `VWAPExecutor`.
- **`src/market/`** — `RegimeDetector` (HMM + VIX + SPY 200 EMA + credit spreads → DEFENSIVE/NEUTRAL/AGGRESSIVE), `MarketCalendar`, `MarketHours`, `CorporateActions`.
- **`src/research/`** — `StockResearcher` (per-symbol deep analysis), `MacroAnalyzer`.
- **`src/walk_forward.py`** — Rolling window backtesting with parameter stability checks.

### Shared Module (`shared/`)

Shared with sibling `crypto-ai-trader` project:
- `shared/core/db_lock.py` — `DuckDBLock` (fcntl file lock for DuckDB concurrent write protection)
- `shared/core/state_db.py` — SQLite WAL-mode state persistence
- `shared/risk/risk_manager.py` — Base `RiskManager` class (raises `NotImplementedError` to prevent silent approval)

### Configuration (`config/`)

All YAML-driven, no hardcoded params:
- `config.yaml` — System mode, data sources, IBKR connection, LLM config, notifications
- `strategies.yaml` — Strategy params, weights, holding periods, regime filters
- `risk_limits.yaml` — Daily loss circuit breakers, drawdown limits, position limits, VIX scaling, PDT rules, settlement
- `factors.yaml` — Factor definitions, IC tracker, orthogonalization, cross-sectional ranking
- `universes.yaml` — Stock universes (global, sp500, hang_seng, csi300, nikkei, europe) with sector breakdowns
- `strategy_allocation.yaml` — Per-symbol strategy + factor weights (auto-generated from walk-forward validation)
- `markets.yaml` — Trading hours, holidays, settlement rules per market (US/HK/CN/JP/UK/EU/AU)

### Data Stores

- `data/state.db` — SQLite WAL for portfolio state, order tracking, NAV history
- `data/features/` — DuckDB columnar store for OHLCV + factor values (uses `DuckDBLock` for write safety)
- `data/feature_store.duckdb` — FeatureStore for factor values + IC history

### Infrastructure

- IBKR Gateway runs via Docker (`docker-compose.yml` — `gnzsnz/ib-gateway` image), exposing ports 4001 (paper), 4002 (live), 5900 (VNC)
- Notifications via Feishu webhook (`src/notifier.py`)
- LLM analysis via DeepSeek (primary) and Xiaomi mimo-v2.5-pro (secondary)

## Key Conventions

- `nest_asyncio.apply()` is called in `main.py` to allow async broker operations in the synchronous CLI
- The CLI uses `SyncIBKRWrapper` (not the async `IBKRClient`) for simplicity
- Environment variables sourced from `.env` (API keys) and `~/.hermes/.env` (shared secrets)
- `AUTO_EXECUTE=true` env var enables automatic trade execution during scans (otherwise signals are printed only)
- `CPG_ACCOUNT_ID` env var enables live account status via the CPG proxy at `localhost:5000`
- All monetary values tracked in native currency; `PortfolioManager` handles multi-currency NAV with FX conversion
