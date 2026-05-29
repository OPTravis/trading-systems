"""
Walk-Forward Validation v2 — Optimized for more trades and multiple strategies.

Usage:
    python scripts/walk_forward.py                      # Top 20 from global
    python scripts/walk_forward.py --symbols AAPL NVDA
    python scripts/walk_forward.py --top 20 --train 126 --test 126 --step 42
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src.data.historical_store import HistoricalStore
from src.walk_forward import WalkForwardValidator

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("walk_forward")


# ─── Strategy Functions ──────────────────────────────────────────────────

def momentum_rsi(train_data, test_data, params):
    """Momentum + RSI strategy with configurable thresholds."""
    lookback = params.get("lookback", 20)
    entry_thr = params.get("entry_thr", 0.01)
    exit_thr = params.get("exit_thr", -0.005)
    stop_loss = params.get("stop_loss", 0.05)
    rsi_ob = params.get("rsi_ob", 75)
    vol_mult = params.get("vol_mult", 1.0)

    close = test_data["close"].astype(float)
    volume = test_data["volume"].astype(float).replace(0, np.nan)

    mom = close.pct_change(lookback)
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    vol_ratio = volume / volume.rolling(20).mean()
    sma_20 = close.rolling(20).mean()

    return _simulate_trades(test_data, close, mom, rsi, vol_ratio, sma_20,
                            entry_thr, exit_thr, stop_loss, rsi_ob, vol_mult)


def mean_reversion(train_data, test_data, params):
    """Mean reversion: buy oversold near support, sell at mean."""
    bb_period = params.get("bb_period", 20)
    bb_entry = params.get("bb_entry", -1.5)   # Buy below lower band
    bb_exit = params.get("bb_exit", 0.0)       # Sell at middle band
    stop_loss = params.get("stop_loss", 0.05)
    rsi_ob = params.get("rsi_ob", 80)
    vol_mult = params.get("vol_mult", 0.8)

    close = test_data["close"].astype(float)
    volume = test_data["volume"].astype(float).replace(0, np.nan)

    bb_mid = close.rolling(bb_period).mean()
    bb_std = close.rolling(bb_period).std()
    bb_pos = (close - bb_mid) / (2 * bb_std.replace(0, np.nan))

    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    vol_ratio = volume / volume.rolling(20).mean()

    # Invert: bb_pos is "signal" — buy when very negative, sell when normal
    return _simulate_trades(test_data, close, bb_pos, rsi, vol_ratio, bb_mid,
                            bb_entry, bb_exit, stop_loss, rsi_ob, vol_mult)


def trend_following(train_data, test_data, params):
    """Trend following: buy on SMA crossover + ADX confirmation."""
    fast_ma = params.get("fast_ma", 10)
    slow_ma = params.get("slow_ma", 30)
    entry_thr = params.get("entry_thr", 0.005)
    exit_thr = params.get("exit_thr", -0.005)
    stop_loss = params.get("stop_loss", 0.06)
    rsi_ob = params.get("rsi_ob", 80)
    vol_mult = params.get("vol_mult", 1.0)

    close = test_data["close"].astype(float)
    volume = test_data["volume"].astype(float).replace(0, np.nan)

    fast = close.rolling(fast_ma).mean()
    slow = close.rolling(slow_ma).mean()
    ma_diff = (fast - slow) / slow.replace(0, np.nan)

    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    vol_ratio = volume / volume.rolling(20).mean()

    return _simulate_trades(test_data, close, ma_diff, rsi, vol_ratio, fast,
                            entry_thr, exit_thr, stop_loss, rsi_ob, vol_mult)


def breakout(train_data, test_data, params):
    """Breakout: buy on 20-day high with volume confirmation."""
    lookback = params.get("lookback", 20)
    entry_pct = params.get("entry_pct", 0.98)   # Buy within 2% of high
    exit_pct = params.get("exit_pct", 0.90)     # Sell when drops 10% from high
    stop_loss = params.get("stop_loss", 0.06)
    vol_mult = params.get("vol_mult", 1.5)

    close = test_data["close"].astype(float)
    high = test_data["high"].astype(float)
    volume = test_data["volume"].astype(float).replace(0, np.nan)

    rolling_high = high.rolling(lookback).max()
    pct_of_high = close / rolling_high.replace(0, np.nan)

    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    vol_ratio = volume / volume.rolling(20).mean()

    # Use pct_of_high as signal — buy near high, sell when drops
    return _simulate_trades(test_data, close, pct_of_high, rsi, vol_ratio, rolling_high,
                            entry_pct, exit_pct, stop_loss, 80, vol_mult)


def _simulate_trades(test_data, close, signal, rsi, vol_ratio, sma_ref,
                     entry_thr, exit_thr, stop_loss, rsi_ob, vol_mult):
    """Common trade simulation engine."""
    initial_capital = 100000.0
    position_size = 0.15  # 15% per trade (larger for more impact)
    capital = initial_capital
    position = 0
    entry_price = 0
    trades = []
    equity = [capital]
    min_bars = max(20, 30)  # Skip warmup

    for i in range(min_bars, len(test_data)):
        price = close.iloc[i]
        if np.isnan(price) or price <= 0:
            equity.append(capital + (position * price if position > 0 else 0))
            continue

        sig = signal.iloc[i] if not np.isnan(signal.iloc[i]) else 0
        r = rsi.iloc[i] if not np.isnan(rsi.iloc[i]) else 50
        vr = vol_ratio.iloc[i] if not np.isnan(vol_ratio.iloc[i]) else 1
        above_ref = price > sma_ref.iloc[i] if not np.isnan(sma_ref.iloc[i]) else True

        # Stop loss
        if position > 0:
            pnl_pct = (price - entry_price) / entry_price
            if pnl_pct <= -stop_loss:
                proceeds = position * price * 0.999
                trades.append({"pnl": proceeds - position * entry_price, "pnl_pct": pnl_pct})
                capital += proceeds
                position = 0

        # Buy: signal > threshold AND rsi not overbought AND volume ok AND above reference
        if sig > entry_thr and r < rsi_ob and vr > vol_mult and above_ref and position == 0:
            invest = capital * position_size
            shares = invest / (price * 1.001)
            cost = shares * price * 1.001
            if cost <= capital:
                capital -= cost
                position = shares
                entry_price = price

        # Sell: signal < exit threshold OR rsi overbought
        elif (sig < exit_thr or r > rsi_ob) and position > 0:
            proceeds = position * price * 0.999
            pnl_pct = (price - entry_price) / entry_price
            trades.append({"pnl": proceeds - position * entry_price, "pnl_pct": pnl_pct})
            capital += proceeds
            position = 0

        equity.append(capital + (position * price if position > 0 else 0))

    # Close remaining
    if position > 0:
        last_price = close.iloc[-1]
        proceeds = position * last_price * 0.999
        pnl_pct = (last_price - entry_price) / entry_price
        trades.append({"pnl": proceeds - position * entry_price, "pnl_pct": pnl_pct})
        capital += proceeds

    equity_series = pd.Series(equity, index=test_data.index[:len(equity)])
    return trades, equity_series


# ─── Main ────────────────────────────────────────────────────────────────

STRATEGIES = {
    "momentum_rsi": {
        "fn": momentum_rsi,
        "param_grid": {
            "lookback": [10, 20, 30],
            "entry_thr": [0.005, 0.01, 0.02],
            "exit_thr": [-0.005, -0.01],
            "stop_loss": [0.04, 0.06],
            "rsi_ob": [70, 80],
            "vol_mult": [0.8, 1.0],
        },
    },
    "mean_reversion": {
        "fn": mean_reversion,
        "param_grid": {
            "bb_period": [15, 20, 30],
            "bb_entry": [-1.5, -2.0],
            "bb_exit": [-0.5, 0.0],
            "stop_loss": [0.04, 0.06],
            "rsi_ob": [75, 80],
            "vol_mult": [0.6, 0.8],
        },
    },
    "trend_following": {
        "fn": trend_following,
        "param_grid": {
            "fast_ma": [5, 10, 20],
            "slow_ma": [20, 30, 50],
            "entry_thr": [0.005, 0.01],
            "exit_thr": [-0.005, -0.01],
            "stop_loss": [0.05, 0.08],
            "rsi_ob": [70, 80],
            "vol_mult": [0.8, 1.0],
        },
    },
    "breakout": {
        "fn": breakout,
        "param_grid": {
            "lookback": [10, 20, 30],
            "entry_pct": [0.95, 0.98, 1.0],
            "exit_pct": [0.85, 0.90, 0.95],
            "stop_loss": [0.05, 0.08],
            "vol_mult": [1.0, 1.5],
        },
    },
}


def run_single_strategy(strategy_name, strategy_cfg, symbols, store,
                        train_days, test_days, step_days):
    """Run walk-forward for one strategy across all symbols."""
    validator = WalkForwardValidator(
        train_days=train_days,
        test_days=test_days,
        step_days=step_days,
        min_trades=1,
    )

    results = []
    for sym in symbols:
        df = store.get_ohlcv(sym)
        if df.empty or len(df) < train_days + test_days:
            continue

        df_wf = df.copy()
        df_wf["date"] = pd.to_datetime(df_wf["date"])
        df_wf = df_wf.set_index("date").sort_index()

        try:
            report = validator.validate(
                strategy_fn=strategy_cfg["fn"],
                data=df_wf,
                symbol=sym,
                strategy_name=strategy_name,
                param_grid=strategy_cfg["param_grid"],
            )
            if report.total_windows > 0:
                results.append({
                    "strategy": strategy_name,
                    "symbol": sym,
                    "sharpe": report.avg_sharpe,
                    "sortino": report.avg_sortino,
                    "max_dd": report.avg_max_drawdown,
                    "win_rate": report.avg_win_rate,
                    "profit_factor": report.avg_profit_factor,
                    "total_return": report.total_return,
                    "stability": report.param_stability,
                    "windows": report.total_windows,
                })
        except Exception as e:
            logger.debug("%s/%s failed: %s", strategy_name, sym, e)

    return results


def main():
    parser = argparse.ArgumentParser(description="Walk-Forward v2 — Multi-Strategy")
    parser.add_argument("--symbols", nargs="+", help="Symbols to validate")
    parser.add_argument("--top", type=int, default=20, help="Top N from backtest")
    parser.add_argument("--train", type=int, default=126, help="Training window (days)")
    parser.add_argument("--test", type=int, default=126, help="Testing window (days)")
    parser.add_argument("--step", type=int, default=42, help="Step between windows (days)")
    parser.add_argument("--strategy", default="all", help="Strategy: all/momentum_rsi/mean_reversion/trend_following/breakout")
    args = parser.parse_args()

    store = HistoricalStore()

    # Select symbols
    if args.symbols:
        symbols = args.symbols
    else:
        results_path = Path("data/backtest_results.json")
        if results_path.exists():
            with open(results_path) as f:
                results = json.load(f)
            symbols = [r["symbol"] for r in results[:args.top]]
        else:
            symbols = store.get_all_symbols()[:args.top]

    logger.info("WalkForward v2: %d symbols, train=%d, test=%d, step=%d",
                len(symbols), args.train, args.test, args.step)

    # Select strategies
    if args.strategy == "all":
        strats = STRATEGIES
    else:
        strats = {args.strategy: STRATEGIES[args.strategy]}

    # Run all strategies
    all_results = []
    t0 = time.time()

    for name, cfg in strats.items():
        logger.info("Running %s...", name)
        t1 = time.time()
        results = run_single_strategy(name, cfg, symbols, store,
                                       args.train, args.test, args.step)
        elapsed = time.time() - t1
        logger.info("  %s: %d profitable out of %d (%.1fs)",
                     name, sum(1 for r in results if r["sharpe"] > 0), len(results), elapsed)
        all_results.extend(results)

    total_elapsed = time.time() - t0

    if not all_results:
        logger.warning("No results")
        return

    # Sort by Sharpe
    all_results.sort(key=lambda x: x["sharpe"], reverse=True)

    # Print results
    print(f"\n{'='*95}")
    print(f"WALK-FORWARD v2 — {len(all_results)} results across {len(strats)} strategies, {total_elapsed:.0f}s")
    print(f"{'='*95}")

    print(f"\n{'Strategy':<16} {'Symbol':<10} {'Sharpe':>7} {'Sortino':>8} {'MaxDD':>7} {'WinRate':>8} {'PF':>6} {'Return':>8} {'Stab':>5} {'Win':>4}")
    print("-" * 95)

    for r in all_results[:30]:
        print(f"{r['strategy']:<16} {r['symbol']:<10} {r['sharpe']:>7.2f} {r['sortino']:>7.2f} "
              f"{r['max_dd']:>+6.1%} {r['win_rate']:>7.1%} {r['profit_factor']:>5.2f} "
              f"{r['total_return']:>+7.1%} {r['stability']:>5.2f} {r['windows']:>4}")

    # Per-strategy summary
    print(f"\n{'STRATEGY SUMMARY':─<95}")
    for name in strats:
        sr = [r for r in all_results if r["strategy"] == name]
        if not sr:
            continue
        positive = sum(1 for r in sr if r["sharpe"] > 0)
        sharpes = [r["sharpe"] for r in sr]
        win_rates = [r["win_rate"] for r in sr if r["windows"] > 0]
        returns = [r["total_return"] for r in sr]
        print(f"\n  {name}:")
        print(f"    Tested: {len(sr)}  |  Positive Sharpe: {positive}/{len(sr)} ({positive/len(sr):.0%})")
        print(f"    Avg Sharpe: {np.mean(sharpes):.2f}  |  Median: {np.median(sharpes):.2f}")
        if win_rates:
            print(f"    Avg Win Rate: {np.mean(win_rates):.1%}")
        print(f"    Avg Return: {np.mean(returns):+.1%}  |  Best: {sr[0]['symbol']} ({sr[0]['sharpe']:.2f})")

    # Best combo per symbol
    print(f"\n{'BEST STRATEGY PER SYMBOL':─<95}")
    seen = set()
    for r in all_results:
        if r["symbol"] not in seen and r["sharpe"] > 0:
            seen.add(r["symbol"])
            print(f"  {r['symbol']:<10} → {r['strategy']:<16} Sharpe {r['sharpe']:.2f}  Return {r['total_return']:+.1%}")

    # Save
    output_path = Path("data/walkforward_results.json")
    with open(output_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    logger.info("Results saved to %s", output_path)

    store.close()


if __name__ == "__main__":
    main()
