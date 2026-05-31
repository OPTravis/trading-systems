"""
Backtesting Engine - Test strategies on historical data
"""

import itertools
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

import yaml

if TYPE_CHECKING:
    pass
from .binance_client import BinanceClient  # runtime fallback
from .strategies import (
    BollingerStrategy,
    DCAStrategy,
    GridStrategy,
    RSIStrategy,
    TrendStrategy,
    VWAPStrategy,
)

logger = logging.getLogger(__name__)


def load_strategy_config() -> Dict:
    """Load strategy configs from risk_limits.yaml"""
    config_path = Path(__file__).parent.parent / "config" / "risk_limits.yaml"
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f)
            return config.get("strategies", {})
    return {}


class Backtester:
    """Backtest trading strategies"""

    def __init__(
        self,
        initial_capital: float = 10000,
        slippage: float = 0.001,
        fee_rate: float = 0.001,
        position_size: float = 0.1,
    ):
        self.initial_capital = initial_capital
        self.slippage = slippage
        self.fee_rate = fee_rate
        self.position_size = position_size
        self.client = BinanceClient(testnet=False)
        self._klines_cache: Dict[str, tuple] = {}  # key -> (timestamp, data)

        # Strategy registry
        self.strategies = {
            "grid": GridStrategy,
            "dca": DCAStrategy,
            "trend": TrendStrategy,
            "rsi": RSIStrategy,
            "bollinger": BollingerStrategy,
            "vwap": VWAPStrategy,
        }

    def backtest_strategy(
        self,
        strategy_name: str,
        symbol: str,
        interval: str = "1h",
        days: int = 30,
        params: Optional[Dict] = None,
    ) -> Dict:
        """Backtest a single strategy on a symbol"""
        logger.info(f"Backtesting {strategy_name} on {symbol} for {days} days")

        # Get historical data
        klines = self._get_historical_data(symbol, interval, days)
        if not klines:
            return {"error": "Failed to get data"}

        strategy_class = self.strategies.get(strategy_name)
        if not strategy_class:
            return {"error": f"Unknown strategy: {strategy_name}"}

        strategy = strategy_class(params or {})  # type: ignore[abstract]

        # Run backtest (pass symbol directly, not from kline)
        results = self._run_backtest(
            strategy,
            klines,
            symbol,
            slippage=self.slippage,
            fee_rate=self.fee_rate,
            position_size=self.position_size,
        )

        return {
            "strategy": strategy_name,
            "symbol": symbol,
            "interval": interval,
            "days": days,
            "results": results,
        }

    def _get_historical_data(self, symbol: str, interval: str, days: int) -> List[Dict]:
        """Get historical kline data"""
        # Estimate number of candles needed
        interval_minutes = {
            "1m": 1,
            "5m": 5,
            "15m": 15,
            "1h": 60,
            "4h": 240,
            "1d": 1440,
        }

        mins = interval_minutes.get(interval, 60)
        candles_needed = (days * 24 * 60) // mins
        limit = min(candles_needed, 1500)  # Binance limit

        # Cache klines to avoid redundant API calls (60s TTL)
        cache_key = f"{symbol}:{interval}:{limit}"
        now = time.monotonic()
        cached = self._klines_cache.get(cache_key)
        if cached and now - cached[0] < 60:
            return cached[1]

        data = self.client.get_klines(symbol, interval, limit=limit)
        self._klines_cache[cache_key] = (now, data)
        return data

    def _run_backtest(
        self,
        strategy,
        klines: List[Dict],
        symbol: str,
        slippage: float = 0.001,
        fee_rate: float = 0.001,
        position_size: float = 0.1,
    ) -> Dict:
        """Run backtest simulation"""
        capital = self.initial_capital
        position = None
        trades = []

        for i in range(50, len(klines)):  # Need warmup period
            current_price = klines[i]["close"]

            # O(n) optimization: pass full klines + current index instead of slicing
            signal = strategy.analyze(symbol, klines, position, idx=i)

            # Execute signal
            if signal.signal.value == "BUY" and not position:
                # Buy with slippage and fees
                effective_price = current_price * (1 + slippage)
                qty = (capital * position_size) / effective_price
                fee = qty * effective_price * fee_rate
                cost = qty * effective_price + fee
                if cost > 10:  # Min $10
                    capital -= cost
                    position = {
                        "entry_price": effective_price,
                        "entry_cost": cost,
                        "total": qty,
                        "entry_time": klines[i]["open_time"],
                    }
                    trades.append(
                        {
                            "type": "BUY",
                            "price": effective_price,
                            "qty": qty,
                            "fee": fee,
                            "time": klines[i]["open_time"],
                        }
                    )

            elif signal.signal.value == "SELL" and position:
                # Sell with slippage and fees
                effective_price = current_price * (1 - slippage)
                qty = position["total"]
                fee = qty * effective_price * fee_rate
                proceeds = qty * effective_price - fee
                pnl = proceeds - position["entry_cost"]
                capital += proceeds
                trades.append(
                    {
                        "type": "SELL",
                        "price": effective_price,
                        "qty": qty,
                        "fee": fee,
                        "pnl": pnl,
                        "time": klines[i]["open_time"],
                    }
                )
                position = None

        # Calculate metrics - include open position at last price
        open_value = 0
        if position:
            last_price = klines[-1]["close"]
            open_value = position["total"] * last_price * (1 - slippage)
        final_capital = capital + open_value
        total_return = (
            (final_capital - self.initial_capital) / self.initial_capital
        ) * 100

        # Win rate: only count SELL trades
        sell_trades = [t for t in trades if t["type"] == "SELL"]
        winning_trades = [t for t in sell_trades if t.get("pnl", 0) > 0]
        losing_trades = [t for t in sell_trades if t.get("pnl", 0) < 0]

        return {
            "initial_capital": self.initial_capital,
            "final_capital": final_capital,
            "total_return_pct": total_return,
            "total_trades": len(sell_trades),
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "win_rate": (
                len(winning_trades) / len(sell_trades) * 100 if sell_trades else 0
            ),
            "total_pnl": final_capital - self.initial_capital,
            "trades": trades[-20:],  # Last 20 trades
        }

    def optimize_strategy(
        self,
        strategy_name: str,
        symbol: str,
        param_grid: Dict,
        interval: str = "1h",
        days: int = 30,
    ) -> Dict:
        """Optimize strategy parameters via grid search"""
        results = []

        # Generate parameter combinations
        keys = list(param_grid.keys())
        values = list(param_grid.values())

        for combo in itertools.product(*values):
            params = dict(zip(keys, combo))
            result = self.backtest_strategy(
                strategy_name, symbol, interval, days, params
            )
            results.append(
                {
                    "params": params,
                    "return_pct": result.get("results", {}).get("total_return_pct", 0),
                }
            )

        # Sort by return
        results.sort(key=lambda x: x["return_pct"], reverse=True)

        return {
            "strategy": strategy_name,
            "symbol": symbol,
            "best_params": results[0]["params"] if results else {},
            "best_return": results[0]["return_pct"] if results else 0,
            "all_results": results[:10],  # Top 10
        }

    def compare_strategies(
        self, symbol: str, interval: str = "1h", days: int = 30
    ) -> Dict:
        """Compare all strategies on a symbol"""
        comparison = {}

        for name in self.strategies.keys():
            result = self.backtest_strategy(name, symbol, interval, days)
            if "error" not in result:
                comparison[name] = result["results"]

        return {
            "symbol": symbol,
            "interval": interval,
            "days": days,
            "strategies": comparison,
            "best_strategy": (
                max(
                    comparison.items(),
                    key=lambda x: x[1]["total_return_pct"],
                    default=(None, {"total_return_pct": 0}),
                )[0]
                if comparison
                else None
            ),
        }
