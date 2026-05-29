# Grid Trading Bot Specification

## Overview
Build a spot grid trading bot for Binance, integrated into the existing `crypto-ai-trader` project at `~/crypto-ai-trader/`.

## Architecture

### Files to Create
1. `src/grid_trader.py` — Core GridBot class (all logic)
2. `grid_bot.py` — CLI entry point (init, start, stop, status, backtest commands)

### Existing Code to Reuse
- `src/binance_client.py` — `BinanceClient` class with: `get_klines()`, `get_free_balance()`, `get_open_orders()`, `place_limit_buy()`, `place_limit_sell()`, `cancel_order()`, `cancel_all_orders()`, `get_price_precision()`, `get_position()`
- Import: `from src.binance_client import BinanceClient`
- Python 3.11, dependencies: `binance`, `python-dotenv`, `numpy`, `pandas` (already installed)
- Must include monkey-patch at top: `import random as _r; _r.randbits = _r.getrandbits if not hasattr(_r, 'randbits') else _r.randbits`
- **DO NOT** use `PYTHONPATH=src:.` — causes import conflicts

## GridBot Class (`src/grid_trader.py`)

### State File
`data/grid_state.json` — persists all grid state across restarts. Read on init, write on every state change.

### State Schema
```json
{
  "symbol": "SOLUSDT",
  "status": "running|paused|stopped",
  "config": {
    "grid_count": 8,
    "range_pct": 5.0,
    "capital_per_grid": 50.0,
    "total_capital": 400.0,
    "fee_rate": 0.001,
    "rebalance_interval_hours": 24,
    "max_range_pct": 15.0
  },
  "grid_levels": [
    {"index": 0, "price": 78.82, "buy_order_id": null, "sell_order_id": null, "coin_qty": 0.0, "status": "empty"},
    ...
  ],
  "stats": {
    "total_trades": 0,
    "realized_pnl": 0.0,
    "total_fees": 0.0,
    "started_at": "2026-04-15T15:00:00",
    "last_rebalance": "2026-04-15T15:00:00",
    "last_check": "2026-04-15T15:30:00"
  },
  "created_at": "2026-04-15T15:00:00"
}
```

### GridBot Methods

#### `__init__(self, client: BinanceClient)`
Load state from `data/grid_state.json` if exists, else initialize empty.

#### `init_grid(self, symbol: str, total_capital: float, grid_count: int = 8, range_pct: float = 5.0)`
- Calculate grid range: current price ± range_pct%
- Generate arithmetic grid levels
- Validate: each grid's capital_per_grid >= min_notional ($5), spacing > 2 * fee_rate * price (profitability check)
- Save initial state to `data/grid_state.json`
- **DO NOT place orders yet** — just configure

#### `start(self)`
- Place buy limit orders at all grid levels below current price
- Place sell limit orders at all grid levels above current price (if holding coin from previous run)
- Set status to "running"
- Save state

#### `stop(self)`
- Cancel ALL open orders for the symbol
- Set status to "stopped"
- Save state
- Return summary (final equity, PnL, open position)

#### `pause(self)`
- Cancel all orders
- Set status to "paused"
- Save state

#### `check_and_rebalance(self) -> dict`
Core loop — called by cron every 15 minutes:
1. Check all open orders — detect filled buys/sells
2. For each filled buy: place a sell limit at the next grid level up
3. For each filled sell: place a buy limit at the next grid level down
4. Check if price has moved outside grid range by > max_range_pct → trigger rebalance
5. Rebalance: cancel all orders, recalculate grid range based on current price + ATR, place new orders
6. Track PnL: realized = sum of (sell_price - buy_price) * qty - fees
7. Save state, return status dict

#### `get_status(self) -> dict`
Return current grid state, equity, unrealized PnL, trade stats.

#### `backtest(self, symbol: str, total_capital: float, grid_count: int, range_pct: float, days: int = 30) -> dict`
- Fetch 1h klines for `days` period
- Run simulation (same logic as check_and_rebalance but on historical data)
- Return: total_return%, max_drawdown%, total_trades, avg_trades_per_day, buy_hold_return%

### Grid Level Logic (Critical)

```
Grid Level Index:  0       1       2       3       4       5       6       7       8
Prices:          78.82   79.79   80.76   81.73   82.71   83.68   84.65   85.62   86.59
                 |-------|-------|-------|-------|-------|-------|-------|-------|
Current=83.50:   BUY     BUY     BUY     BUY     [here]  SELL    SELL    SELL    SELL

When buy at level 3 (81.73) fills → place sell at level 4 (82.71)
When sell at level 4 (82.71) fills → place buy at level 3 (81.73)
Each round-trip profit = (82.71 - 81.73) * qty - 2 * fee
```

- Buy fills trigger sell at next higher level
- Sell fills trigger buy at next lower level  
- This creates a continuous ping-pong pattern in ranging markets

### Order Filled Detection
Compare current open orders against previous open orders. If an order_id from state is no longer in open_orders, it was filled. Check order history to confirm fill price.

### Rebalance Trigger Conditions
1. Price > grid_upper * (1 + max_range_pct/100) → grid is too low, rebalance up
2. Price < grid_lower * (1 - max_range_pct/100) → grid is too high, rebalance down
3. Last rebalance > rebalance_interval_hours ago AND ATR has changed significantly → adjust grid spacing
4. On rebalance: cancel all, sell any held coin at market, recalculate from current price, re-place orders

### Error Handling
- Binance API errors: retry 3x with exponential backoff
- Insufficient balance: skip that grid level, log warning
- Order not found: treat as filled, verify via order history
- State corruption: backup old state, reinitialize

## CLI Entry Point (`grid_bot.py`)

```bash
# Initialize grid with config
python grid_bot.py init --symbol SOLUSDT --capital 400 --grids 8 --range 5

# Start trading (place orders)
python grid_bot.py start

# Stop trading (cancel all orders)
python grid_bot.py stop

# Pause (cancel orders but keep state)
python grid_bot.py pause

# Show status
python grid_bot.py status

# Run one check cycle (for cron)
python grid_bot.py tick

# Backtest
python grid_bot.py backtest --symbol SOLUSDT --capital 400 --grids 8 --range 5 --days 30

# Dry run (simulate without real orders)
python grid_bot.py start --dry-run
```

Output format: JSON for machine parsing, human-readable table for status.

## Cron Integration
`grid_bot.py tick` will be called every 15 minutes by Hermes cron. It should:
1. Load state
2. If status != "running", exit silently
3. Run check_and_rebalance()
4. Print a one-line status summary (for cron push notification)

## Important Constraints
- Only Binance SPOT API (no futures/margin)
- Fee rate: 0.1% per side (0.2% round-trip)
- Minimum notional: $5 per order
- All prices/quantities must respect Binance tick size and lot size
- State must persist in `data/grid_state.json`
- No external dependencies beyond what's already installed
- Use `from src.binance_client import BinanceClient` (already handles key loading, retry, precision)
- The BinanceClient loads keys from env vars (BINANCE_API_KEY, BINANCE_API_SECRET) — these are set via `source crypto-secrets.env`
