# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Structure

Monorepo with two independent AI trading systems and a shared module layer:

```
trading-systems/
├── crypto-ai-trader/   # Binance SPOT crypto trading (~30k LOC, 95 .py files)
├── stock-ai-trader/    # Global stock trading via IBKR (~10k LOC, 54 .py files)
└── shared/             # Reusable modules imported by both projects
    ├── core/           # StateDB (SQLite WAL), EventBus, LLM client, DuckDB lock
    ├── risk/           # Base RiskManager, CircuitBreaker, DailyLossBreaker, DrawdownBreaker, KellySizer, CVaR
    ├── strategy/       # StrategyEvolver, ContextualBandit
    ├── analysis/       # BearAnalyst, ConceptDrift, DimensionScorer, MultiTimeframe, PricePredictor
    └── utils/          # Indicators, TradeOutcomeRecorder, ProjectRoot
```

Each subproject has its own `CLAUDE.md` with project-specific details — read those first when working in a subproject.

## Shared Module Imports

Both projects import from `shared/` via `sys.path` manipulation at the top of their `main.py`:

```python
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
```

This means imports like `from shared.core.state_db import StateDB` or `from shared.risk.kelly_sizer import KellySizer` work from within each project. The `shared/utils/project_root.py` helper walks upward to find the repo root.

**stock-ai-trader** also has a local `shared/` subdirectory with copies of `db_lock.py`, `state_db.py`, and `risk_manager.py`. Changes to these files may need to be synced to the root `shared/` as well.

## Commands

### crypto-ai-trader

```bash
cd crypto-ai-trader
pip install -r requirements.txt
python main.py cron-scan          # Full scan → research → execute pipeline
python main.py scan               # Market scan only
python main.py status             # Portfolio status
python main.py analyze <SYM>      # Multi-timeframe technical analysis
python main.py trade              # Single trading cycle
python main.py backtest           # Strategy backtesting
pytest                            # All tests (30s timeout, 60% coverage threshold)
pytest tests/test_crypto_system.py::test_name  # Single test
pytest -m "not slow"              # Skip slow tests
bash run_scan.sh                  # Cron wrapper (source crypto-secrets.env first)
```

### stock-ai-trader

```bash
cd stock-ai-trader
pip install -r requirements.txt
docker-compose up -d              # Start IBKR Gateway
python main.py scan [--universe global] [--market US]
python main.py status [--detailed] [--live]
python main.py analyze AAPL MSFT
python main.py trade [--dry-run] [--confirm]
python main.py backtest --strategy momentum --from 2024-01-01
pytest                            # All tests
pytest -x                         # Stop on first failure
ruff check src/ tests/            # Lint
black src/ tests/                 # Format
```

## Shared Architecture Patterns

Both systems follow the same high-level pattern — a multi-phase scan pipeline orchestrated by `scan_orchestrator.py`:

1. **Scan/Screen** — Sync portfolio from exchange, detect market regime, screen universe
2. **Score/Rank** — Multi-dimensional scoring of candidates
3. **Research** — LLM-powered deep dive on top candidates (DeepSeek primary, GPT-4o-mini/mimo fallback)
4. **Risk Checks** — Layered risk gates (circuit breaker, daily loss, drawdown, position limits)
5. **Execute** — Position sizing (Kelly × CVaR) and order placement

### Common Components (in `shared/`)

| Component | Role |
|-----------|------|
| `StateDB` | SQLite WAL-mode persistence; singleton via `get_state_db()` |
| `EventBus` | In-process pub/sub backed by SQLite |
| `RiskManager` | Base class; raises `NotImplementedError` to prevent silent approval |
| `CircuitBreaker` | Halts trading on repeated API failures or severe drawdown |
| `DailyLossBreaker` | Tiered daily loss limits (defensive → block → close all) |
| `KellySizer` | Kelly Criterion position sizing, half-Kelly variant, capped at 50% |
| `BearAnalyst` | Devil's advocate that can veto trades |
| `StrategyEvolver` | Auto-promotes/demotes strategies based on win rate |

### Configuration

All configuration is YAML-driven with no hardcoded parameters. Secrets come from `.env` files (gitignored):
- crypto: `crypto-secrets.env` → `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `DEEPSEEK_API_KEY`, etc.
- stock: `.env` → `IBKR_ACCOUNT_ID`, `FEISHU_WEBHOOK_URL`, `AUTO_EXECUTE`, etc.

### SPOT ONLY

Neither system uses futures, leverage, or options. This is a hard constraint.

### Notifications

Both systems use `FeishuNotifier` (Feishu/Lark webhook) for trade alerts and daily reports.

### Language

Code is in English. Some config files and comments use Traditional Chinese (繁體中文).
