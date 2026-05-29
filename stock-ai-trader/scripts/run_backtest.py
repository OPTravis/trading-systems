"""
Backtest Runner — Fetch historical data, compute factors, run walk-forward validation.

Usage:
    python scripts/run_backtest.py                    # Full global universe
    python scripts/run_backtest.py --universe sp500   # S&P 500 only
    python scripts/run_backtest.py --symbols AAPL MSFT NVDA
    python scripts/run_backtest.py --period 5y        # 5 years history
    python scripts/run_backtest.py --skip-ingest      # Use existing data
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src.data.historical_store import HistoricalStore
from src.data.feature_store import FeatureStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("backtest")


# ─── Factor Computation ──────────────────────────────────────────────────

def compute_factors(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute trading factors from OHLCV data.

    Args:
        df: DataFrame with columns: date, open, high, low, close, volume

    Returns:
        DataFrame with date as index and factor columns.
    """
    if len(df) < 60:
        return pd.DataFrame()

    df = df.copy().sort_values("date").reset_index(drop=True)
    close = df["close"]
    volume = df["volume"].astype(float).replace(0, np.nan)

    factors = pd.DataFrame({"date": df["date"]})

    # Momentum factors
    factors["momentum_5d"] = close.pct_change(5)
    factors["momentum_10d"] = close.pct_change(10)
    factors["momentum_20d"] = close.pct_change(20)
    factors["momentum_60d"] = close.pct_change(60)

    # Volatility
    factors["volatility_20d"] = close.pct_change().rolling(20).std() * np.sqrt(252)
    factors["volatility_60d"] = close.pct_change().rolling(60).std() * np.sqrt(252)

    # RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    factors["rsi_14"] = 100 - (100 / (1 + rs))

    # Volume surge
    factors["volume_ratio_20d"] = volume / volume.rolling(20).mean()

    # Moving average position
    sma_20 = close.rolling(20).mean()
    sma_50 = close.rolling(50).mean()
    factors["price_vs_sma20"] = (close - sma_20) / sma_20
    factors["price_vs_sma50"] = (close - sma_50) / sma_50
    factors["sma20_vs_sma50"] = (sma_20 - sma_50) / sma_50

    # Bollinger Band position
    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    factors["bb_position"] = (close - bb_mid) / (2 * bb_std.replace(0, np.nan))

    # ATR (normalized)
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - close.shift()).abs()
    low_close = (df["low"] - close.shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    factors["atr_14_pct"] = (tr.rolling(14).mean() / close) * 100

    # Price range (52-week position)
    rolling_high = close.rolling(252, min_periods=60).max()
    rolling_low = close.rolling(252, min_periods=60).min()
    factors["price_range_pct"] = (close - rolling_low) / (rolling_high - rolling_low).replace(0, np.nan)

    factors = factors.set_index("date")
    return factors


# ─── Simple Strategy for Backtest ────────────────────────────────────────

def momentum_strategy(
    df: pd.DataFrame,
    entry_threshold: float = 0.02,
    exit_threshold: float = -0.01,
    stop_loss_pct: float = 0.05,
    rsi_oversold: float = 30,
    rsi_overbought: float = 70,
) -> pd.DataFrame:
    """
    Simple momentum + RSI strategy for backtesting.

    Returns DataFrame with columns: date, signal (1=buy, -1=sell, 0=hold)
    """
    factors = compute_factors(df)
    if factors.empty:
        return pd.DataFrame()

    signals = pd.DataFrame(index=factors.index)
    signals["signal"] = 0

    # Buy: momentum_20d > threshold AND rsi < overbought AND volume surge
    buy_mask = (
        (factors["momentum_20d"] > entry_threshold)
        & (factors["rsi_14"] < rsi_overbought)
        & (factors["volume_ratio_20d"] > 1.2)
        & (factors["price_vs_sma20"] > 0)
    )
    signals.loc[buy_mask, "signal"] = 1

    # Sell: momentum_20d < exit_threshold OR rsi > overbought
    sell_mask = (
        (factors["momentum_20d"] < exit_threshold)
        | (factors["rsi_14"] > rsi_overbought)
    )
    signals.loc[sell_mask, "signal"] = -1

    return signals


# ─── Simple Backtest Engine ──────────────────────────────────────────────

def run_backtest(
    df: pd.DataFrame,
    signals: pd.DataFrame,
    initial_capital: float = 100000.0,
    position_size_pct: float = 0.10,
    stop_loss_pct: float = 0.05,
    take_profit_pct: float = 0.15,
    commission_pct: float = 0.001,
) -> dict:
    """
    Simple backtest engine.

    Returns dict with performance metrics and equity curve.
    """
    if df.empty or signals.empty:
        return {"total_return": 0, "sharpe": 0, "max_drawdown": 0, "trades": 0}

    df = df.copy().sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")

    # Align signals
    sig = signals.reindex(df.index).fillna(0)

    capital = initial_capital
    position = 0
    entry_price = 0
    entry_date = None
    trades = []
    equity = [capital]

    for i in range(1, len(df)):
        price = df["close"].iloc[i]
        signal = sig["signal"].iloc[i]

        # Check stop loss / take profit if in position
        if position > 0:
            pnl_pct = (price - entry_price) / entry_price
            if pnl_pct <= -stop_loss_pct or pnl_pct >= take_profit_pct:
                # Close position
                proceeds = position * price * (1 - commission_pct)
                trade_pnl = proceeds - (position * entry_price)
                trades.append({
                    "entry_date": entry_date,
                    "exit_date": df.index[i],
                    "entry_price": entry_price,
                    "exit_price": price,
                    "pnl": trade_pnl,
                    "pnl_pct": pnl_pct,
                })
                capital += proceeds
                position = 0

        # Process signals
        if signal > 0 and position == 0:
            # Buy
            invest = capital * position_size_pct
            shares = invest / (price * (1 + commission_pct))
            cost = shares * price * (1 + commission_pct)
            capital -= cost
            position = shares
            entry_price = price
            entry_date = df.index[i]

        elif signal < 0 and position > 0:
            # Sell
            proceeds = position * price * (1 - commission_pct)
            pnl_pct = (price - entry_price) / entry_price
            trades.append({
                "entry_date": entry_date,
                "exit_date": df.index[i],
                "entry_price": entry_price,
                "exit_price": price,
                "pnl": proceeds - (position * entry_price),
                "pnl_pct": pnl_pct,
            })
            capital += proceeds
            position = 0

        # Track equity
        portfolio_value = capital + (position * price if position > 0 else 0)
        equity.append(portfolio_value)

    # Close any remaining position at last price
    if position > 0:
        last_price = df["close"].iloc[-1]
        proceeds = position * last_price * (1 - commission_pct)
        pnl_pct = (last_price - entry_price) / entry_price
        trades.append({
            "entry_date": entry_date,
            "exit_date": df.index[-1],
            "entry_price": entry_price,
            "exit_price": last_price,
            "pnl": proceeds - (position * entry_price),
            "pnl_pct": pnl_pct,
        })
        capital += proceeds

    # Compute metrics
    equity_series = pd.Series(equity, index=df.index[:len(equity)])
    total_return = (equity_series.iloc[-1] / initial_capital) - 1
    daily_returns = equity_series.pct_change().dropna()
    sharpe = (daily_returns.mean() / daily_returns.std() * np.sqrt(252)) if daily_returns.std() > 0 else 0
    max_dd = ((equity_series / equity_series.cummax()) - 1).min()
    win_trades = [t for t in trades if t["pnl"] > 0]
    win_rate = len(win_trades) / len(trades) if trades else 0

    return {
        "total_return": total_return,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "total_trades": len(trades),
        "win_rate": win_rate,
        "avg_trade_pnl": np.mean([t["pnl_pct"] for t in trades]) if trades else 0,
        "profit_factor": (
            sum(t["pnl"] for t in win_trades) / abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
            if trades and sum(t["pnl"] for t in trades if t["pnl"] < 0) != 0
            else 0
        ),
        "equity_curve": equity,
        "trades": trades,
    }


# ─── Main ────────────────────────────────────────────────────────────────

def load_universe_symbols(universe_name: str) -> list[str]:
    """Load symbols from universes.yaml."""
    import yaml
    config_path = Path(__file__).resolve().parent.parent / "config" / "universes.yaml"
    with open(config_path) as f:
        data = yaml.safe_load(f)

    universe = data.get("universes", {}).get(universe_name, {})
    if not universe:
        logger.error("Universe '%s' not found", universe_name)
        return []

    symbols = []
    for sector, tickers in universe.get("sectors", {}).items():
        symbols.extend(tickers)

    # Deduplicate
    seen = set()
    unique = []
    for s in symbols:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    return unique


def main():
    parser = argparse.ArgumentParser(description="Backtest runner")
    parser.add_argument("--universe", default="global", help="Universe name")
    parser.add_argument("--symbols", nargs="+", help="Override symbols")
    parser.add_argument("--period", default="2y", help="History period")
    parser.add_argument("--skip-ingest", action="store_true", help="Use existing DB data")
    parser.add_argument("--top", type=int, default=10, help="Show top N results")
    parser.add_argument("--min-sharpe", type=float, default=0.5, help="Min Sharpe to report")
    args = parser.parse_args()

    # Load symbols
    if args.symbols:
        symbols = args.symbols
    else:
        symbols = load_universe_symbols(args.universe)

    if not symbols:
        logger.error("No symbols to backtest")
        return

    logger.info("Backtest universe: %d symbols (%s)", len(symbols), args.universe)

    # Step 1: Ingest historical data
    store = HistoricalStore()

    if not args.skip_ingest:
        logger.info("Step 1: Ingesting %s of historical data...", args.period)
        t0 = time.time()
        results = store.ingest_batch(symbols, period=args.period)
        elapsed = time.time() - t0
        ok = sum(1 for v in results.values() if v > 0)
        total_rows = sum(results.values())
        logger.info("Ingested: %d/%d symbols, %d rows in %.1fs", ok, len(symbols), total_rows, elapsed)
    else:
        logger.info("Step 1: Skipping ingest (using existing data)")

    # Step 2: Run backtest for each symbol
    logger.info("Step 2: Running backtests...")
    all_results = []

    for sym in symbols:
        df = store.get_ohlcv(sym)
        if df.empty or len(df) < 120:
            logger.debug("Skipping %s (insufficient data: %d rows)", sym, len(df))
            continue

        signals = momentum_strategy(df)
        if signals.empty:
            continue

        result = run_backtest(df, signals)
        result["symbol"] = sym
        result["data_rows"] = len(df)
        date_range = store.get_date_range(sym)
        result["date_range"] = f"{date_range[0]} to {date_range[1]}"
        all_results.append(result)

    if not all_results:
        logger.warning("No backtest results generated")
        return

    # Step 3: Summary
    logger.info("\n" + "=" * 80)
    logger.info("BACKTEST RESULTS — %d stocks, %s history", len(all_results), args.period)
    logger.info("=" * 80)

    # Sort by Sharpe ratio
    all_results.sort(key=lambda r: r["sharpe"], reverse=True)

    # Print top results
    print(f"\n{'Symbol':<12} {'Return':>8} {'Sharpe':>8} {'MaxDD':>8} {'Trades':>7} {'WinRate':>8} {'PF':>6} {'Rows':>6}")
    print("-" * 75)

    profitable = 0
    for r in all_results[:args.top]:
        ret = r["total_return"]
        if ret > 0:
            profitable += 1
        print(
            f"{r['symbol']:<12} {ret:>+7.1%} {r['sharpe']:>7.2f} {r['max_drawdown']:>+7.1%} "
            f"{r['total_trades']:>7} {r['win_rate']:>7.1%} {r['profit_factor']:>5.2f} {r['data_rows']:>6}"
        )

    # Aggregate stats
    returns = [r["total_return"] for r in all_results]
    sharpes = [r["sharpe"] for r in all_results]
    win_rates = [r["win_rate"] for r in all_results if r["total_trades"] > 0]

    print(f"\n{'AGGREGATE':─<75}")
    print(f"  Stocks tested:      {len(all_results)}")
    print(f"  Profitable:         {profitable}/{len(all_results)} ({profitable/len(all_results):.0%})")
    print(f"  Avg Return:         {np.mean(returns):+.1%}")
    print(f"  Median Return:      {np.median(returns):+.1%}")
    print(f"  Avg Sharpe:         {np.mean(sharpes):.2f}")
    print(f"  Avg Win Rate:       {np.mean(win_rates):.1%}" if win_rates else "")
    print(f"  Best:               {all_results[0]['symbol']} ({all_results[0]['total_return']:+.1%}, Sharpe {all_results[0]['sharpe']:.2f})")
    print(f"  Worst:              {all_results[-1]['symbol']} ({all_results[-1]['total_return']:+.1%}, Sharpe {all_results[-1]['sharpe']:.2f})")

    # Save results
    output_path = Path("data/backtest_results.json")
    output_data = []
    for r in all_results:
        output_data.append({
            "symbol": r["symbol"],
            "total_return": r["total_return"],
            "sharpe": r["sharpe"],
            "max_drawdown": r["max_drawdown"],
            "total_trades": r["total_trades"],
            "win_rate": r["win_rate"],
            "profit_factor": r["profit_factor"],
            "data_rows": r["data_rows"],
        })

    with open(output_path, "w") as f:
        json.dump(output_data, f, indent=2, default=str)
    logger.info("\nResults saved to %s", output_path)

    store.close()


if __name__ == "__main__":
    main()
