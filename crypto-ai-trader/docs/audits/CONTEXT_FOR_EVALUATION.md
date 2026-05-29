# Crypto-AI-Trader 系統信息包

**生成日期**: 2026-05-09
**用途**: 供外部 Agent 評估此交易系統的完整上下文信息
**路徑**: ~/crypto-ai-trader/
**Python**: Python 3.11.15

---

## 1. 系統概覽

### 文件統計
- Python 模組數: 62
- 總代碼行數: 19089
- 項目大小: 260M

### 代碼行數分佈 (Top 30)
```
19089 total
  1284 src/backtest.py
  1128 src/data_feed.py
   927 src/binance_client.py
   873 src/market_scanner.py
   863 src/grid_trader.py
   809 src/risk_manager.py
   765 src/state_db.py
   646 src/scan_orchestrator.py
   630 src/indicators.py
   599 src/trade_executor.py
   594 src/market_researcher.py
   550 src/sector_classifier.py
   550 src/position_optimizer.py
   523 src/smart_order.py
   404 src/portfolio.py
   358 src/fundamental_analyst.py
   343 src/dynamic_coin_pool.py
   341 src/bear_analyst.py
   324 src/strategy_adaptor.py
   311 src/notifier.py
   308 src/multi_timeframe.py
   306 src/trade_journal.py
   301 src/funding_arb.py
   298 src/agents/technical_agent.py
   293 src/fee_optimizer.py
   269 src/portfolio_state.py
   259 src/backtester.py
   246 src/sector_clustering.py
   239 src/ws_user_stream.py
```

### config/strategies.yaml
```yaml
# Strategy Configurations - Optimized for extreme fear market
# Grid and Trend disabled during extreme fear (F&G < 25)
# Only DCA and RSI active for mean-reversion buying
# SPOT ONLY — all strategies are spot trading, no futures/leverage

strategies:
  grid:
    name: "Grid Trading"
    enabled: false  # Disabled in extreme fear - causes cascading losses
    params:
      grid_levels: 8
      price_range_pct: 8.0
      order_size_pct: 8
      stop_loss_pct: 5.0

  dca:
    name: "Dollar Cost Averaging"
    enabled: true   # Best strategy for fear markets
    params:
      interval_hours: 12
      order_size_pct: 8
      dip_threshold_pct: -3.0   # More sensitive, buy smaller dips
      max_dca_rounds: 8         # More rounds allowed

  trend:
    name: "Trend Following"
    enabled: false  # Disabled - too many false signals in fear market
    params:
      fast_ma: 9
      slow_ma: 21
      volume_threshold: 1.5
      stop_loss_pct: 5.0
      take_profit_pct: 10.0

  rsi_reversion:
    name: "RSI Mean Reversion"
    enabled: true   # Good for fear market - buy oversold bounces
    params:
      rsi_oversold: 35    # Slightly higher threshold to catch more signals
      rsi_overbought: 65
      stop_loss_pct: 5.0
      take_profit_pct: 8.0

  bollinger:
    name: "Bollinger Band Reversion"  # Changed from Breakout to Reversion
    enabled: true
    params:
      period: 20
      std_dev: 2.0
      volume_threshold: 1.5
      stop_loss_pct: 5.0
      take_profit_pct: 8.0

  vwap:
    name: "VWAP Distribution"
    enabled: true
    params:
      vwap_threshold_pct: -2.0   # More sensitive entry
      order_size_pct: 8
      stop_loss_pct: 5.0
      take_profit_pct: 8.0
```

### config/risk_limits.yaml
```yaml
# Risk Management Configuration - Optimized for extreme fear markets
# Key changes: wider stop-loss, longer hold times, more positions allowed

risk:
  max_position_pct: 15
  max_total_exposure_pct: 70
  cash_reserve_pct: 30
  max_daily_loss_pct: 5
  max_drawdown_pct: 15
  max_open_positions: 3
  max_hold_hours: 168

strategies:
  grid:
    stop_loss_pct: 5.0
    take_profit_levels:
      - { pct: 3.0, size_pct: 30 }
      - { pct: 6.0, size_pct: 40 }
      - { pct: 10.0, size_pct: 30 }
    max_hold_hours: 72

  dca:
    stop_loss_pct: 8.0
    take_profit_levels:
      - { pct: 5.0, size_pct: 30 }
      - { pct: 10.0, size_pct: 40 }
      - { pct: 20.0, size_pct: 30 }
    max_hold_hours: 336

  trend:
    stop_loss_pct: 5.0
    take_profit_levels:
      - { pct: 5.0, size_pct: 40 }
      - { pct: 10.0, size_pct: 40 }
      - { pct: 15.0, size_pct: 20 }
    max_hold_hours: 72

  rsi:
    stop_loss_pct: 5.0
    take_profit_levels:
      - { pct: 5.0, size_pct: 40 }
      - { pct: 10.0, size_pct: 40 }
      - { pct: 15.0, size_pct: 20 }
    max_hold_hours: 168

  bollinger:
    stop_loss_pct: 5.0
    take_profit_levels:
      - { pct: 5.0, size_pct: 30 }
      - { pct: 10.0, size_pct: 40 }
      - { pct: 15.0, size_pct: 30 }
    max_hold_hours: 168

  vwap:
    stop_loss_pct: 5.0
    take_profit_levels:
      - { pct: 5.0, size_pct: 40 }
      - { pct: 10.0, size_pct: 40 }
      - { pct: 15.0, size_pct: 20 }
    max_hold_hours: 72

default:
  stop_loss_pct: 5.0
  take_profit_levels:
    - { pct: 5.0, size_pct: 40 }
    - { pct: 10.0, size_pct: 40 }
    - { pct: 15.0, size_pct: 20 }
  max_hold_hours: 168
```

---

## 2. 當前投資組合

### StateDB portfolio
```
TRXUSDT|141.8|0.3497|0.332215|0.370682|synced|2026-05-09T17:30:15.695172
ENAUSDT|398.16276|0.1286|0.12217|0.136316|synced|2026-05-09T17:30:17.080069
WLDUSDT|74.2|0.2721|0.258495|0.288426|synced|2026-05-09T17:30:16.582494
BNBUSDT|0.00258998|648.91|616.4645|687.8446|synced|2026-05-09T18:03:43.917179
```

### StateDB trades (最近 30 筆)
```
SAHARAUSDT|SELL|695.0|0.03089|0.480999999999998|2026-05-09 04:47:31
TAOUSDT|SELL|0.0918|312.5|0.520251999999999|2026-05-09 04:47:31
SAHARAUSDT|BUY|695.0|0.0302|0.0|2026-05-09 01:05:59
SAHARAUSDT|BUY|1042.956|0.03029|0.0|2026-05-08 21:05:43
WLDUSDT|BUY|74.2257|0.2721|0.0|2026-05-08 20:06:44
ENAUSDT|BUY|398.16276|0.1286|0.0|2026-05-08 19:09:41
JTOUSDT|BUY|54.6|0.581533882783883|0.0|2026-05-08 18:05:39
FILUSDT|BUY|42.48747|1.205|0.0|2026-05-08 17:05:35
JTOUSDT|BUY|53.2798|0.5565|0.0|2026-05-08 16:07:56
ONDOUSDT|BUY|46.3628|0.4112|0.0|2026-05-08 15:07:38
STRKUSDT|BUY|503.20629|0.0533|0.0|2026-05-08 14:06:26
ONDOUSDT|BUY|107.5092|0.39032347|0.0|2026-05-08 14:00:43
TAOUSDT|BUY|0.0918894|306.85560299|0.0|2026-05-08 14:00:43
OPUSDT|BUY|454.52502|0.1462|0.0|2026-05-08 14:00:42
TRXUSDT|BUY|141.858|0.3497|0.0|2026-05-08 14:00:42
DUSDT|BUY|1187.811|0.01496|0.0|2026-05-08 12:06:23
ONDOUSDT|BUY|107.5092|0.39032347|0.0|2026-05-08 11:30:24
TAOUSDT|BUY|0.0918894|306.85560299|0.0|2026-05-08 11:30:24
OPUSDT|BUY|454.52502|0.1462|0.0|2026-05-08 11:30:23
TRXUSDT|BUY|141.858|0.3497|0.0|2026-05-08 11:30:22
TAOUSDT|BUY|0.0918894|306.7|0.0|2026-05-08 11:05:35
ONDOUSDT|BUY|107.5|0.3904|0.0|2026-05-08 10:06:12
TRXUSDT|BUY|141.858|0.3497|0.0|2026-05-08 09:06:20
ONDOUSDT|BUY|57.1167|0.36281239|0.0|2026-05-08 08:48:33
TONUSDT|BUY|7.78539|2.65765057|0.0|2026-05-08 08:48:33
OPUSDT|BUY|454.52502|0.1462|0.0|2026-05-08 08:48:32
ONDOUSDT|BUY|57.1167|0.36281239|0.0|2026-05-08 08:35:28
TONUSDT|BUY|7.78539|2.65765057|0.0|2026-05-08 08:35:27
OPUSDT|BUY|454.52502|0.1462|0.0|2026-05-08 08:35:26
ONDOUSDT|BUY|57.1167|0.36281239|0.0|2026-05-08 08:30:44
```

### StateDB decisions
```
SAHARAUSDT|BUY|70.0|0.03046|2026-05-09 01:05:59
SAHARAUSDT|BUY|75.0|0.03027|2026-05-08 21:05:43
WLDUSDT|BUY|72.0|0.273|2026-05-08 20:06:44
ENAUSDT|BUY|77.0|0.1281|2026-05-08 19:09:41
JTOUSDT|BUY|72.0|0.5905|2026-05-08 18:05:39
FILUSDT|BUY|76.0|1.206|2026-05-08 17:05:35
JTOUSDT|BUY|75.0|0.5553|2026-05-08 16:07:56
ONDOUSDT|BUY|73.0|0.4114|2026-05-08 15:07:38
STRKUSDT|BUY|75.0|0.0532|2026-05-08 14:06:26
DUSDT|BUY|70.0|0.01497|2026-05-08 12:06:23
TAOUSDT|BUY|69.0|307.0|2026-05-08 11:05:35
ONDOUSDT|BUY|72.0|0.3913|2026-05-08 10:06:12
TRXUSDT|BUY|75.0|0.3497|2026-05-08 09:06:20
ONDOUSDT|BUY|73.0|0.3629|2026-05-08 06:05:55
TONUSDT|BUY|71.0|2.667|2026-05-08 03:05:26
TONUSDT|BUY|71.0|2.713|2026-05-08 02:04:59
BTCUSDT|BUY|75.5|82000.0|2026-05-07 13:16:00
```

### StateDB trailing_stop
```
TRXUSDT|0.3518|0.3518|0.0|0|1778320830.30281
```

### Binance 實時數據
```json

```

---

## 3. Cron 定時任務

### crypto-scan
- Schedule: {'kind': 'cron', 'expr': '2 * * * *', 'display': '2 * * * *'}
- Script: None
- Prompt (2428 chars):


### crypto-report
- Schedule: {'kind': 'cron', 'expr': '0 22 * * *', 'display': '0 22 * * *'}
- Script: None
- Prompt (1390 chars):


### crypto-state-backup
- Schedule: {'kind': 'cron', 'expr': '0 3 * * *', 'display': '0 3 * * *'}
- Script: backup_state.py
- Prompt (631 chars):


### crypto-unified-monitor
- Schedule: {'kind': 'cron', 'expr': '*/30 * * * *', 'display': '*/30 * * * *'}
- Script: None
- Prompt (4331 chars):


### crypto-health-check
- Schedule: {'kind': 'cron', 'expr': '30 * * * *', 'display': '30 * * * *'}
- Script: health_check.py
- Prompt (0 chars):


### crypto-weekly-backtest
- Schedule: {'kind': 'cron', 'expr': '30 9 * * 1', 'display': '30 9 * * 1'}
- Script: weekly_backtest.py
- Prompt (0 chars):


### crypto-dust-cleanup
- Schedule: {'kind': 'cron', 'expr': '0 */6 * * *', 'display': '0 */6 * * *'}
- Script: clean_dust.py
- Prompt (0 chars):
---

## 4. 核心模塊源碼

### position_optimizer.py
```python
"""
Position Optimizer - Smart position switching based on opportunity cost analysis.

Rules:
- Trigger: existing position 24h change < -5% OR new coin score - existing score > 20
- Frequency: max 1 switch per coin per 4 hours
- Ratio: 100% full switch
- Blacklist: skip coins with 24h change > +30%
- Fee: Binance VIP0 spot 0.1% per side, total switch cost = 0.2%
"""

import logging
import math
import time
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class PositionOptimizer:
    """Analyzes existing positions vs market opportunities and triggers switches."""

    # Thresholds
    EXISTING_LOSS_THRESHOLD = -3.0  # 24h change < -3% triggers switch (was -5, too conservative)
    SCORE_GAP_THRESHOLD = 10.0      # new score - existing score > 10 triggers switch (was 20, unreachable)
    BLACKLIST_24H_CHANGE = 30.0     # skip coins with 24h change > +30%
    SWITCH_FEE_PCT = 0.2            # total fee for sell+buy (0.1% * 2)
    MIN_SWITCH_INTERVAL_HOURS = 2   # min hours between switches for same coin (was 4, too slow)
    LOW_SCORE_EXIT_THRESHOLD = 50.0 # score below this → exit to USDT (was 40, too lenient)
    DUST_EXIT_USDT = 20.0           # exit positions below this value (dust)
    MIN_EXPECTED_GAIN_PCT = 0.5     # minimum expected gain after fees to justify switch

    # Smart activation thresholds
    VOLATILITY激活_THRESHOLD = 2.0  # BTC 24h > 2% → activate optimizer
    POSITION_LOSS激活_THRESHOLD = -2.0  # any position 24h < -2% → activate

    def __init__(self, binance_client, portfolio, market_scanner):
        self.bc = binance_client
        self.portfolio = portfolio
        self.scanner = market_scanner
        self._last_switch_time: Dict[str, float] = {}  # symbol -> timestamp
        self._load_switch_times()

    def should_activate(self, btc_change_24h: float = 0.0, position_24h_changes: Dict[str, float] = None) -> bool:
        """Smart activation: only run optimizer when market conditions warrant it.

        Activates when ANY of:
        - BTC 24h change > ±2% (volatile market)
        - Any position 24h change < -2% (underperforming)
        - Market regime is GREED/EXTREME_GREED (opportunity-rich)

        Returns False in flat markets with stable positions (no-op saves resources).
        """
        # Condition 1: BTC volatility
        if abs(btc_change_24h) >= self.VOLATILITY激活_THRESHOLD:
            logger.info(f"Optimizer activated: BTC 24h={btc_change_24h:+.1f}% (volatility)")
            return True

        # Condition 2: Any position losing
        if position_24h_changes:
            for sym, change in position_24h_changes.items():
                if change < self.POSITION_LOSS激活_THRESHOLD:
                    logger.info(f"Optimizer activated: {sym} 24h={change:+.1f}% (underperforming)")
                    return True

        # Condition 3: Flat market — skip optimization
        logger.info(f"Optimizer skipped: BTC 24h={btc_change_24h:+.1f}%, no position losses >2%")
        return False

    def _load_switch_times(self):
        """Restore switch cooldowns from StateDB kv store."""
        try:
            from src.state_db import get_state_db
            db = get_state_db()
            # Scan all kv keys starting with 'switch:last:'
            # Since kv doesn't have prefix scan, we use a different approach
            # Store all switch times in a single key
            stored = db.kv_get("position_optimizer:switch_times", {})
            if stored:
```

### trade_executor.py (L67-200)
```python
def get_position_tier(score):
    """Determine position size tier based on opportunity score.

    Returns (base_pct, tier_label). Used by execute_auto_trade for position sizing.
    TODO: Migrate to KellyPositionSizer when fully integrated.
    """
    if score >= 90:
        return 0.50, "HIGH"
    elif score >= 75:
        return 0.30, "MEDIUM-HIGH"
    elif score >= 65:
        return 0.20, "MEDIUM"
    elif score >= 60:
        return 0.15, "CAUTIOUS"
    else:
        return 0.0, "SKIP"




def count_active_positions(client):
    """Count number of active positions (non-USDT balances with value > $1).

    Filters out NTRN (delisted) and dust coins worth less than $1.
    Uses batch ticker fetch (1 API call) instead of per-asset calls.
    """
    try:
        acct = client.get_account()
        # Batch fetch all prices in one call
        price_map = {}
        try:
            all_tickers = client.get_24hr_stats()
            if isinstance(all_tickers, list):
                price_map = {
                    t["symbol"]: float(t.get("last_price", 0))
                    for t in all_tickers
                    if "symbol" in t
                }
        except Exception as e:
            logger.debug(f"count_active_positions: batch ticker fetch failed: {e}")
        count = 0
        for b in acct['balances']:
            free = float(b['free']) + float(b['locked'])
            if free > 0 and b['asset'] not in ('USDT', 'NTRN'):
                sym = b['asset'] + 'USDT'
                price = price_map.get(sym, 0)
                if price > 0 and free * price >= 1.0:
                    count += 1
                elif price == 0:
                    # Can't get price — conservatively skip (don't inflate count)
                    logger.warning("count_active_positions: no price for %s, skipping", sym)
        return count
    except Exception:
        logger.warning(f"count_active_positions: account fetch failed")
        return 0




def execute_auto_trade(symbol, price, strategy, stop_loss_pct, tp_levels, stop_price, max_hold, signals, reason, score=70):
    """Execute trade automatically with Kelly-optimal position sizing.

    Position size uses Half-Kelly criterion based on historical win rate
    and trade outcome data. Falls back to tier-based sizing when
    insufficient history exists.

    Returns dict with success status and order details.
    """
    import numpy as np  # deferred import for compatibility
    client = BinanceClient(testnet=False)
    notifier = FeishuNotifier()

    # Get available USDT balance
    usdt_bal = client.get_free_balance('USDT')
    if usdt_bal < 10:
        return {"success": False, "error": f"Insufficient USDT: ${usdt_bal:.2f}"}

    # Circuit breaker: block trades when system is in failure/drawdown state
    try:
        from src.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker()
        if cb.is_tripped():
            logger.warning("Circuit breaker tripped — blocking trade")
            return {"success": False, "error": "Circuit breaker tripped"}
    except Exception:
        pass  # fail-open if circuit breaker unavailable

    # Count existing positions
    active_positions = count_active_positions(client)
    max_positions = 3

    if active_positions >= max_positions:
        return {"success": False, "error": f"Max positions reached: {active_positions}/{max_positions}"}

    # Score below minimum threshold — no trade regardless of Kelly
    if score < 60:
        logger.info(f"Score {score} below minimum threshold (60), skipping trade")
        return {"success": False, "error": f"Score too low: {score} (min 60)"}

    # ── Position sizing: Kelly-first, tier-fallback ──
    # KellyPositionSizer uses historical win-rate data for optimal sizing.
    # When insufficient history (< 10 trades), falls back to tier-based sizing.
    from src.kelly_sizer import KellyPositionSizer
    from src.fee_optimizer import FeeOptimizer
    from src.state_db import get_state_db

    db = get_state_db()
    kelly = KellyPositionSizer(state_db=db)
    fee_opt = FeeOptimizer(client)

    # Primary TP level as take_profit_pct for Kelly R/R calculation
    tp_pct = tp_levels[0]["pct"] if tp_levels else 10.0

    kelly_result = kelly.get_position_size(
        symbol=symbol,
        balance=usdt_bal,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=tp_pct,
        signal_score=score,
        use_historical=True,
    )

    # Actual fee rate (not flat 1%)
    fees = fee_opt.get_effective_fees()
    fee_rate = fees["taker_fee"]  # 0.001 or 0.00075 with BNB
    fee_reserve = 1.0 - fee_rate * 2  # buy + sell

    kelly_confidence = kelly_result.get("confidence", "")
    kelly_active = "estimated" not in kelly_confidence.lower()

    if kelly_active:
        # ── Kelly-driven sizing (sufficient history) ──
        kelly_result = kelly.adjust_for_portfolio(
            kelly_result,
```

### risk_manager.py TrailingStop (L177-350)
```python
class TrailingStop:
    """Manage trailing stop-loss for open positions.

    PRIMARY STORAGE: SQLite state.db (via StateDB)
    Format: {symbol: {entry_price, highest_price, sl_price, activated, atr}}
    """

    ACTIVATION_ATR_MULT = 1.5    # activate trailing when profit >= 1.5 * ATR
    TRAILING_ATR_MULT = 0.5      # stop distance from high = 0.5 * ATR

    def __init__(self):
        self._filepath = _DATA_DIR / "trailing_stops.json"
        self._last_save_ts = 0.0
        self._save_debounce = 2.0  # seconds — don't write SQLite more than once per 2s
        # Load from SQLite first, fallback to JSON
        self._state: Dict[str, Dict] = self._load_from_db()
        if not self._state:
            self._state = _load_json(self._filepath, default={})

    def _load_from_db(self) -> Dict[str, Dict]:
        """Load trailing stops from SQLite (primary)."""
        try:
            from src.state_db import get_state_db
            db = get_state_db()
            rows = db.ts_get_all()
            if rows:
                logger.info(f"TrailingStop: loaded {len(rows)} entries from StateDB")
                return rows
        except Exception as e:
            logger.warning(f"TrailingStop: failed to load from StateDB: {e}")
        return {}

    def _save(self, force: bool = False) -> bool:
        """Persist to SQLite with debounce (2s min interval)."""
        now = time.time()
        if not force and (now - self._last_save_ts) < self._save_debounce:
            return True  # skip — saved recently
        self._last_save_ts = now
        try:
            from src.state_db import get_state_db
            db = get_state_db()
            for sym, data in self._state.items():
                db.ts_set(sym, data)
            # Remove deleted entries from DB
            db_rows = db.ts_get_all()
            for sym in db_rows:
                if sym not in self._state:
                    db.ts_remove(sym)
            return True
        except Exception as e:
            logger.error(f"TrailingStop: SQLite save failed: {e}")
            return False

    def update(self, symbol: str, current_price: float, atr: float, entry_price: float = None) -> Dict:
        """Update trailing stop for a symbol.

        Returns:
            {activated: False}                  – not yet profitable enough
            {activated: True, sl_price, ...}    – trailing active, no trigger
            {triggered: True}                   – price hit stop, should close
        """
        symbol = symbol.upper()
        # Normalize symbol to always include USDT suffix for consistency with portfolio
        if not symbol.endswith("USDT"):
            symbol = symbol + "USDT"
        price = float(current_price)
        atr = float(atr)

        if atr <= 0:
            # Use last known ATR as fallback instead of skipping entirely
            last_atr = self._state.get(symbol, {}).get("atr", 0) if symbol in self._state else 0
            if last_atr > 0:
                logger.warning("TrailingStop: ATR=%.4f for %s — using last valid ATR=%.4f", atr, symbol, last_atr)
                atr = last_atr
            elif symbol in self._state:
                logger.warning("TrailingStop: ATR=%.4f for %s — no valid ATR, using price-based fallback", atr, symbol)
                atr = price * 0.02  # 2% of price as emergency ATR
            else:
                logger.warning("TrailingStop: ATR=%.4f for %s — new symbol with no ATR, skipping", atr, symbol)
                return {"activated": False}

        # Add new symbol if not tracked
        if symbol not in self._state:
            # entry_price can be provided by caller (from trade history),
            # otherwise falls back to current market price
            if entry_price is None:
                logger.error("TrailingStop: entry_price not provided for %s, using market price as last resort", symbol)
                entry_price = price

            self._state[symbol] = {
                "entry_price": entry_price,
                "highest_price": price,
                "sl_price": 0,
                "activated": False,
                "atr": atr,
            }
            logger.info("TrailingStop: started tracking %s entry=%.4f ATR=%.4f", symbol, entry_price, atr)

        state = self._state[symbol]
        entry = state["entry_price"]
        highest = state["highest_price"]

        # Update ATR if it changed significantly (>20% drift)
        if atr > 0 and abs(atr - state.get("atr", atr)) / max(state.get("atr", 1e-9), 1e-9) > 0.2:
            state["atr"] = atr
            logger.debug("TrailingStop: updated ATR for %s to %.4f", symbol, atr)

        # Update highest price
        if price > highest:
            state["highest_price"] = price
            highest = price

        # Check if trailing is already activated
        if state["activated"]:
            # Recalculate stop based on new highest
            new_sl = highest - self.TRAILING_ATR_MULT * atr
            # Only move stop UP, never down
            if new_sl > state["sl_price"]:
                state["sl_price"] = new_sl

            # Check if triggered
            if price <= state["sl_price"]:
                callback_pct = ((highest - price) / highest) * 100 if highest > 0 else 0
                result = {
                    "triggered": True,
                    "symbol": symbol,
                    "entry_price": entry,
                    "highest_price": highest,
                    "sl_price": state["sl_price"],
                    "current_price": price,
                    "callback_pct": round(callback_pct, 2),
                }
                logger.warning(
                    "TrailingStop: TRIGGERED %s entry=%.4f highest=%.4f sl=%.4f price=%.4f callback=%.2f%%",
                    symbol, entry, highest, state["sl_price"], price, callback_pct,
                )
                self._save()
                return result

            callback_pct = ((highest - state["sl_price"]) / highest) * 100 if highest > 0 else 0
            self._save()
            return {
                "activated": True,
                "symbol": symbol,
                "sl_price": round(state["sl_price"], 6),
                "highest_price": round(highest, 6),
                "callback_pct": round(callback_pct, 2),
            }

        # Not yet activated – check activation threshold
        profit = price - entry
        if profit >= self.ACTIVATION_ATR_MULT * atr:
            state["activated"] = True
            state["sl_price"] = highest - self.TRAILING_ATR_MULT * atr
            logger.info(
                "TrailingStop: ACTIVATED %s profit=%.4f >= %.4f threshold",
                symbol, profit, self.ACTIVATION_ATR_MULT * atr,
            )
            self._save()
            return {
                "activated": True,
                "symbol": symbol,
                "sl_price": round(state["sl_price"], 6),
                "highest_price": round(highest, 6),
                "callback_pct": round(((highest - state["sl_price"]) / highest) * 100, 2) if highest > 0 else 0,
            }

        self._save()
        return {"activated": False, "symbol": symbol}

    def remove(self, symbol: str) -> None:
        """Remove a symbol from trailing stop tracking."""
        symbol = symbol.upper()
        # Normalize symbol to always include USDT suffix for consistency with portfolio
```

### strategy_adaptor.py (前 120 行)
```python
"""
Strategy Adaptor - Dynamic strategy selection based on market regime.

Determines trading strategy based on Fear & Greed Index, BTC trend, and volatility.
"""

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


class StrategyAdaptor:
    """Adapts trading strategy based on market regime."""

    # Cache for adapt() results
    _cache: Optional[Dict] = None
    _cache_ts: float = 0.0
    _cache_ttl: float = 300  # 5 minutes

    # Strategy definitions
    STRATEGIES = {
        "grid": {
            "name": "Grid Trading",
            "description": "Range-bound oscillation capture",
            "enabled_by_default": False,
        },
        "dca": {
            "name": "DCA",
            "description": "Dollar-cost averaging on dips",
            "enabled_by_default": True,
        },
        "trend": {
            "name": "Trend Following",
            "description": "Momentum-based directional trades",
            "enabled_by_default": True,
        },
        "rsi_reversion": {
            "name": "RSI Mean Reversion",
            "description": "RSI oversold/overbought reversal",
            "enabled_by_default": True,
        },
        "bollinger": {
            "name": "Bollinger Bands",
            "description": "Volatility breakout/reversal",
            "enabled_by_default": True,
        },
        "vwap": {
            "name": "VWAP",
            "description": "Volume-weighted average price deviation",
            "enabled_by_default": True,
        },
    }

    def __init__(self):
        """Initialize StrategyAdaptor."""
        pass

    @classmethod
    def _determine_regime(cls, fear_greed: int) -> str:
        """Determine market regime from Fear & Greed Index."""
        if fear_greed <= 20:
            return "EXTREME_FEAR"
        elif fear_greed <= 40:
            return "FEAR"
        elif fear_greed <= 60:
            return "NEUTRAL"
        elif fear_greed < 80:
            return "GREED"
        else:
            return "EXTREME_GREED"

    @classmethod
    def _determine_volatility(cls, btc_price_change_24h: float) -> str:
        """Determine volatility regime from BTC 24h change."""
        abs_change = abs(btc_price_change_24h)
        if abs_change < 2:
```

### market_scanner.py _analyze_coin (L174-230)
```python
def _analyze_coin(self, coin_data: Dict) -> Optional[Dict]:
        """Analyze a single coin with multi-timeframe + sentiment scoring."""
        symbol = coin_data["symbol"]

        # 1. Multi-timeframe analysis
        try:
            mtf_result = self.mtf_analyzer.analyze(symbol)
        except Exception as e:
            logger.debug(f"MTF analysis failed for {symbol}: {e}")
            return None

        # 2. Volume surge detection on 1h klines (BEFORE scoring so it feeds into _factor_volume_momentum)
        volume_surge = False
        new_signals_data = {}  # OBV, BB squeeze, RSI divergence, consolidation
        try:
            self._rate_limiter.wait()
            klines_1h = self.client.get_klines(symbol, "1h", limit=50)
            if len(klines_1h) >= 21:
                recent_volumes = [k["volume"] for k in klines_1h[-21:-1]]
                current_volume = klines_1h[-1]["volume"]
                avg_volume = sum(recent_volumes) / len(recent_volumes)
                if avg_volume > 0 and current_volume > avg_volume * 1.5:
                    volume_surge = True

            # NEW: Compute pre-pump indicators from 1h klines
            if len(klines_1h) >= 35:
                new_signals_data["obv_div"] = Indicators.obv_divergence(klines_1h, lookback=20)
                new_signals_data["bb_squeeze"] = Indicators.bb_squeeze(klines_1h)
                new_signals_data["rsi_div"] = Indicators.rsi_divergence(klines_1h)

            # NEW: Consolidation breakout from 4h klines (need more history)
            try:
                self._rate_limiter.wait()
                klines_4h = self.client.get_klines(symbol, "4h", limit=80)
                if len(klines_4h) >= 35:
                    new_signals_data["consolidation"] = Indicators.consolidation_breakout(klines_4h)
            except Exception:
                pass
        except Exception:
            pass

        # Inject volume_surge into coin_data so _factor_volume_momentum can use it
        coin_data["volume_surge"] = volume_surge

        # 3. Sentiment data (graceful degradation) — per-symbol funding/OI
        sentiment_data = None
        try:
            sentiment_data = self.data_feed.scorer.get_symbol_sentiment(symbol)
        except Exception:
            sentiment_data = None

        # 3b. Global market sentiment (Fear & Greed) — market-wide emotion
        fng_value = 50
        try:
            fng = self.data_feed.fng.get_current()
            if fng:
                fng_value = int(fng.get("value", 50))
```

### smart_order.py (前 60 行)
```python
"""
Smart Order Module - Intelligent order placement with dynamic SL/TP
Based on ATR (Average True Range) for adaptive risk management.
"""

import json
import logging
from typing import Dict, Optional, Tuple, List
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.exchange_client import ExchangeClient
from src.binance_client import BinanceClient  # runtime fallback


logger = logging.getLogger(__name__)


class SmartOrder:
    """Intelligent order placement with dynamic SL/TP."""

    # Risk limits
    MAX_POSITIONS = 3
    MAX_SINGLE_POSITION_PCT = 15  # max 15% of USDT per trade
    MAX_TOTAL_EXPOSURE_PCT = 70
    CASH_RESERVE_PCT = 30

    # ATR-based SL/TP multipliers
    SL_ATR_MULTIPLIER = 2.0      # SL = entry - 2*ATR
    TP1_ATR_MULTIPLIER = 2.0     # TP1 = entry + 2*ATR (1:1 risk/reward)
    TP2_ATR_MULTIPLIER = 4.0     # TP2 = entry + 4*ATR (1:2)
    TP3_ATR_MULTIPLIER = 6.0     # TP3 = entry + 6*ATR (1:3)

    # TP sizing (percentage of position to close at each TP)
    TP1_SIZE_PCT = 40
    TP2_SIZE_PCT = 40
    TP3_SIZE_PCT = 20

    # SL/TP distance constraints (ATR-based, not percentage-clamped)
    MIN_SPREAD_ATR_MULT = 0.5  # minimum distance between levels = 0.5 * ATR
    MAX_SL_ATR_MULT = 6.0      # cap SL distance at 6 * ATR (prevents excessive risk)
    # No TP cap — let profits scale naturally with volatility

    def __init__(self, client: 'ExchangeClient'):
        self.client = client
        self._symbol_info_cache: Dict[str, Dict] = {}

    def get_usdt_balance(self) -> float:
        return self.client.get_free_balance('USDT')

    def get_price(self, symbol: str) -> Optional[float]:
        """Get current price via 24hr stats."""
        try:
            stats = self.client.get_24hr_stats(symbol)
            if isinstance(stats, dict) and stats.get('last_price'):
                return float(stats['last_price'])
        except Exception as e:
            logger.error(f"Failed to get price for {symbol}: {e}")
        return None

    def get_symbol_filters(self, symbol: str) -> Optional[Dict]:
        """Get LOT_SIZE and PRICE_FILTER for a symbol (cached)."""
        if symbol in self._symbol_info_cache:
            return self._symbol_info_cache[symbol]

        try:
            exchange_info = self.client.get_exchange_info()
            sym_info = next(
                (s for s in exchange_info['symbols'] if s['symbol'] == symbol),
                None
            )
            if not sym_info:
                return None

            filters = {}
            for f in sym_info.get('filters', []):
                if f['filterType'] == 'LOT_SIZE':
                    filters['minQty'] = float(f['minQty'])
                    filters['maxQty'] = float(f['maxQty'])
                    filters['stepSize'] = float(f['stepSize'])
                    # Calculate quantity decimals from stepSize
```

### kelly_sizer.py (L82-180)
```python
def get_position_size(
        self,
        symbol: str,
        balance: float,
        stop_loss_pct: float,
        take_profit_pct: float,
        signal_score: float = 70,
        use_historical: bool = True,
    ) -> Dict:
        """Calculate optimal position size for a trade.

        Args:
            symbol: Trading pair
            balance: Available USDT balance
            stop_loss_pct: Stop loss percentage (e.g., 5.0 for 5%)
            take_profit_pct: Take profit percentage (e.g., 10.0 for 10%)
            signal_score: Signal confidence score (0-100)
            use_historical: Whether to use historical trade data for win rate

        Returns:
            {
                position_pct: float,  # fraction of balance to allocate
                position_usdt: float,
                kelly_fraction: float,
                win_rate: float,
                reward_risk: float,
                confidence: str,  # HIGH/MEDIUM/LOW based on data quality
                reason: str,
            }
        """
        # Base reward-to-risk from this trade's SL/TP
        reward_risk = take_profit_pct / max(stop_loss_pct, 0.1)

        win_rate = 0.0
        avg_win = 0.0
        avg_loss = 0.0
        confidence = "LOW"

        if use_historical:
            trades = self._get_trade_history(symbol)
            if len(trades) >= 10:
                wins = [t["pnl"] for t in trades if t["pnl"] > 0]
                losses = [t["pnl"] for t in trades if t["pnl"] < 0]

                if wins and losses:
                    win_rate = len(wins) / len(trades)
                    avg_win = np.mean(wins) if wins else 0
                    avg_loss = abs(np.mean(losses)) if losses else 0
                    confidence = "HIGH" if len(trades) >= 30 else "MEDIUM"

        # Fallback: estimate win rate from signal score
        if win_rate == 0:
            # Map score 60-100 to win rate 0.45-0.65
            win_rate = 0.35 + (signal_score / 100) * 0.30
            avg_win = take_profit_pct / 100
            avg_loss = stop_loss_pct / 100
            confidence = "LOW (estimated from score)"

        # Calculate Kelly fraction
        kelly = self.calculate_kelly_fraction(win_rate, avg_win, avg_loss)

        # Apply minimum threshold
        if kelly < MIN_POSITION_PCT:
            kelly = MIN_POSITION_PCT
            reason = f"Kelly={kelly:.1%} below minimum, using floor {MIN_POSITION_PCT:.0%}"
        else:
            reason = f"Kelly={kelly:.1%} (win_rate={win_rate:.1%}, R/R={reward_risk:.1f})"

        position_usdt = balance * kelly

        return {
            "position_pct": round(kelly, 4),
            "position_usdt": round(position_usdt, 2),
            "kelly_fraction": round(kelly / KELLY_FRACTION, 4) if KELLY_FRACTION > 0 else 0,
            "win_rate": round(win_rate, 4),
            "reward_risk": round(reward_risk, 2),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "confidence": confidence,
            "reason": reason,
        }

    def adjust_for_portfolio(
        self,
        kelly_result: Dict,
        current_positions: int,
        max_positions: int = 3,
    ) -> Dict:
        """Scale down Kelly size as portfolio fills up.

        More positions = less concentration risk per trade.
        """
        kelly = kelly_result["position_pct"]

        # Scale factor based on current exposure
        if current_positions >= max_positions:
            scale = 0.0
        elif current_positions == max_positions - 1:
            scale = 0.3
```

### circuit_breaker.py (前 60 行)
```python
"""
Global Circuit Breaker — system-wide safety halt for crypto trading.

Trips under these conditions:
1. Consecutive API failures >= 5 within 10 minutes → 30min pause
2. Total drawdown from ATH >= 20% → indefinite pause until manual reset
3. Unusual portfolio state (negative cash, ghost positions > 3)

Persisted to StateDB so cron runs pick up the tripped state.

Usage:
    from src.circuit_breaker import CircuitBreaker
    cb = CircuitBreaker()
    
    if cb.is_tripped():
        return  # stop all trading
    
    try:
        do_trade()
    except Exception:
        cb.record_failure()

    cb.record_success()
"""
import logging
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Thresholds
CONSECUTIVE_FAILURES_MAX = 5       # trips after 5 consecutive API failures
FAILURE_WINDOW_SEC = 600           # 10 min window for counting failures
TRIP_DURATION_SEC = 1800           # 30 min auto-reset after trip
DRAWDOWN_TRIP_PCT = 20.0           # 20% drawdown from ATH
MAX_GHOST_POSITIONS = 3            # unusual if >3 ghost positions detected


class CircuitBreaker:
    """System-wide safety halt when anomalies accumulate."""

    def __init__(self):
        self._failure_count = 0
        self._first_failure_ts: Optional[float] = None
        self._tripped_until: Optional[float] = None
        self._trip_reason: str = ""
        # Load persisted state
        self._load_state()

    # ── Persistence ──

    def _load_state(self):
        """Load circuit breaker state from StateDB kv store."""
        try:
            from src.state_db import get_state_db
            db = get_state_db()
            state = db.kv_get("circuit_breaker:state", {})
            if state:
                self._failure_count = state.get("failure_count", 0)
                self._first_failure_ts = state.get("first_failure_ts")
                self._tripped_until = state.get("tripped_until")
                self._trip_reason = state.get("trip_reason", "")
        except Exception as e:
            logger.warning(f"CircuitBreaker: failed to load state: {e}")

    def _save_state(self):
        """Persist circuit breaker state to StateDB."""
        try:
            from src.state_db import get_state_db
            db = get_state_db()
            db.kv_set("circuit_breaker:state", {
                "failure_count": self._failure_count,
                "first_failure_ts": self._first_failure_ts,
                "tripped_until": self._tripped_until,
                "trip_reason": self._trip_reason,
            })
        except Exception as e:
            logger.warning(f"CircuitBreaker: failed to save state: {e}")

    # ── Public API ──
```

### bear_analyst.py (前 60 行)
```python
"""
Bear Analyst — Provides bearish counter-arguments for high-scoring opportunities.

When the scanner identifies a promising trade, this agent plays devil's advocate:
it inverts key metrics (RSI, funding rate, Fear & Greed, TVL, volume) into a
bear_score (0-100) and, when warranted, asks DeepSeek LLM for additional risk
factors. If the bear case is stronger than the opportunity score, the trade is
vetoed.

Bear score > 70 AND bear_score > opportunity_score  →  trade vetoed.
"""

import json
import logging
import os
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


class BearResult:
    """Typed container for bearish analysis output."""

    __slots__ = ("bear_score", "veto", "reasons", "risk_factors", "confidence")

    def __init__(
        self,
        bear_score: float = 0.0,
        veto: bool = False,
        reasons: Optional[List[str]] = None,
        risk_factors: Optional[List[str]] = None,
        confidence: str = "LOW",
    ):
        self.bear_score = bear_score
        self.veto = veto
        self.reasons = reasons or []
        self.risk_factors = risk_factors or []
        self.confidence = confidence

    # Make it behave like a dict for easy serialisation / printing
    def to_dict(self) -> Dict:
        return {
            "bear_score": self.bear_score,
            "veto": self.veto,
            "reasons": self.reasons,
            "risk_factors": self.risk_factors,
            "confidence": self.confidence,
        }

    def __repr__(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


class BearAnalyst:
    """Bearish counter-argument agent for high-score opportunities.

    Pipeline:
    1. Extract raw metrics from opportunity_data
    2. Compute bear_score using inverted factor logic (0-100)
    3. If bear_score >= 50, call DeepSeek LLM for additional risk factors
    4. Determine veto: bear_score > 70 AND bear_score > opportunity_score
    """

    # --- Inverted factor thresholds / weights ---
    RSI_OVERBOUGHT_HIGH = 70       # +25 bear pts
    RSI_OVERBOUGHT_MID = 60        # +15 bear pts
    FUNDING_CROWDED_THRESHOLD = 0.01  # +20 bear pts (fraction, e.g. 0.0001 = 0.01%)
    FNG_EUPHORIA_HIGH = 70         # +20 bear pts
    FNG_EUPHORIA_MID = 60          # +10 bear pts
    TVL_DROP_THRESHOLD = -3.0      # +15 bear pts
    VOLUME_DECLINING_BONUS = 10    # +10 bear pts
    MAX_BEAR_SCORE = 100

    # Veto thresholds
    VETO_ABSOLUTE_THRESHOLD = 70   # bear_score must exceed this
    LLM_CALLOUT_THRESHOLD = 50    # only call LLM if bear_score >= this

    def analyze(
```

### state_db.py (前 50 行)
```python
"""
SQLite-backed state persistence for crypto-ai-trader.
Replaces scattered JSON files with ACID-compliant single-database storage.

Tables:
- trailing_stop: TrailingStop state (symbol, entry_price, highest, sl, activated)
- portfolio: Portfolio positions (symbol, qty, entry, strategy, opened_at)
- drawdown: Drawdown breaker state (single row)
- risk_guard: RiskManager loss_guard state (daily_pnl, streak, last_reset)
- trades: Trade history for PnL tracking
- kv: Generic key-value store for adapter configs, etc.
- grid_state: Grid trading state (replaces grid_state.json)
- dca_state: DCA strategy state (replaces dca_state.json)
- strategy_state: Strategy adaptor state (replaces strategy_state.json)
- audit_log: Audit trail
"""
import sqlite3
import json
import os
import time
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)

# Default DB path: project_root/data/state.db
DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "state.db"


class StateDB:
    """Thread-safe SQLite state persistence with connection pooling."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local connection (sqlite3 is not thread-safe by default).
        
        FIX M1: Auto-close stale connections to prevent file descriptor leaks.
        Connections older than 5 minutes are recycled.
        """
        now = time.monotonic()
        # Check if existing connection is stale (>5 min old)
        if hasattr(self._local, "conn") and self._local.conn is not None:
            conn_age = getattr(self._local, "conn_created", 0)
            if now - conn_age > 300:  # 5 minutes
                try:
                    self._local.conn.close()
                except Exception:
                    pass
                self._local.conn = None
                self._local.conn_created = 0
        
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn_created = now
        return self._local.conn

    def transaction(self):
        """Context manager for atomic multi-operation transactions.
        Usage:
            with db.transaction() as conn:
                db.portfolio_set(...)
                db.portfolio_set_cash_balance(...)
        All operations within the block share the same connection and
        are committed together, or rolled back on exception.
        """
        class _TransactionCtx:
            def __init__(self, db):
                self.db = db
                self.conn = None
```

---

## 5. 歷史數據

### 每日決策統計
```
2026-05-09|1|1
2026-05-08|15|15
2026-05-07|1|1
```

### 評分分佈
```
69.0|1
70.0|2
71.0|2
72.0|3
73.0|2
75.0|4
75.5|1
76.0|1
77.0|1
```

### 板塊分類
```json
{
  "classifications": {
    "TAO": "AI_INFRA",
    "ONDO": "RWA",
    "PENGU": "MEME",
    "JTO": "L2DEFI",
    "EUR": "OTHER",
    "KSM": "CORE",
    "CFG": "RWA",
    "LINK": "L2DEFI",
    "WLFI": "RWA",
    "RLUSD": "RWA",
    "AR": "OTHER",
    "PSG": "MEME",
    "BIO": "OTHER",
    "D": "MEME",
    "TON": "CORE",
    "PENDLE": "L2DEFI",
    "ZBT": "OTHER",
    "BANANAS31": "MEME",
    "FARTCOIN": "MEME",
    "ZEC": "CORE",
    "DASH": "CORE",
    "ICP": "CORE",
    "MNT": "L2DEFI",
    "GAS": "CORE",
    "CRO": "CORE",
    "MORPHO": "L2DEFI",
    "SAFE": "OTHER",
    "LQTY": "L2DEFI",
    "KMNO": "L2DEFI",
    "OMNI": "L2DEFI",
    "TRUMP": "MEME",
    "STX": "L2DEFI",
    "BMT": "L2DEFI",
    "GRIFFAIN": "AI_AGENT",
    "SPEC": "AI_INFRA",
    "COOKIE": "MEME",
    "OP": "CORE",
    "TRX": "CORE"
  },
  "last_updated": "2026-05-08 00:23:00",
  "version": 2
}
```

---

## 6. 腳本目錄

- **backtest_runner.py**: 
- **classify_sectors.py**: 
- **clean_dust.py**: 
- **data_consistency_audit.py**: 
- **data_health_dashboard.py**: 
- **ensure_tp_sl.py**: 
- **fetch_market.py**: 
- **fetch_tao.py**: 
- **gen_context_doc.py**: 
- **health_check.py**: 
- **migrate_decisions_to_db.py**: 
- **poc_onchain_sentiment.py**: 
- **technical_analysis.py**: 
- **test_onchain_sentiment_integration.py**: 
- **test_p0_p1_p2_smoke.py**: 
- **test_tp_breach.py**: 
- **trailing_tp.py**: 
- **validate_strategy_params.py**: 
- **weekly_backtest.py**: 

---

## 7. 依賴項 (Top 30)
```
annotated-types==0.7.0
anyio==4.13.0
backtrader==1.9.78.123
binance-connector==3.12.0
certifi==2026.2.25
charset-normalizer==3.4.7
distro==1.9.0
h11==0.16.0
httpcore==1.0.9
httpx==0.28.1
idna==3.11
iniconfig==2.3.0
jiter==0.14.0
numpy==1.26.4
openai==2.31.0
packaging==26.0
pandas==3.0.2
pluggy==1.6.0
pycryptodome==3.23.0
pydantic==2.13.0
pydantic_core==2.46.0
Pygments==2.20.0
PyMuPDF==1.27.2.3
pytest==9.0.3
python-dateutil==2.9.0.post0
python-dotenv==1.2.2
python-telegram-bot==22.7
pytz==2026.1.post1
PyYAML==6.0.3
requests==2.33.1
```

## 8. 已知問題與 TODO

```
src/trade_executor.py:71:    TODO: Migrate to KellyPositionSizer when fully integrated.
src/sector_classifier.py:386:                "suggested_sectors": {},  # TODO: map clusters to sectors
```