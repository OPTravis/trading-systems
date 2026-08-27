# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Structure

Single AI trading system (crypto):

```
trading-systems/
├── crypto-ai-trader/   # Binance SPOT crypto trading (~30k LOC, 95 .py files)
```

Each subproject has its own `CLAUDE.md` with project-specific details — read those first when working in a subproject.

## Shared Module Imports

Both projects import from `shared/` via `sys.path` manipulation at the top of their `main.py`:

```python
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
```

This means imports like `from shared.core.state_db import StateDB` or `from shared.risk.kelly_sizer import KellySizer` work from within each project. The `shared/utils/project_root.py` helper walks upward to find the repo root.

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

### SPOT ONLY

Neither system uses futures, leverage, or options. This is a hard constraint.

### Notifications

Both systems use `FeishuNotifier` (Feishu/Lark webhook) for trade alerts and daily reports.

### Language

Code is in English. Some config files and comments use Traditional Chinese (繁體中文).
