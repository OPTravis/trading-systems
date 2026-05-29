"""
Compute historical factors from OHLCV data and populate FeatureStore.
Run once to bootstrap, then daily via cron to update.

Usage:
    python scripts/compute_factors.py                 # All symbols
    python scripts/compute_factors.py --symbols AAPL MSFT
    python scripts/compute_factors.py --update-today  # Only latest date
"""
import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src.data.historical_store import HistoricalStore
from src.data.feature_store import FeatureStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("compute_factors")


def compute_all_factors(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute 6-dimension factors from OHLCV data.

    Returns DataFrame indexed by date with factor columns:
    - technical: rsi_14, bb_position, macd_signal
    - momentum: momentum_5d, 20d, 60d, 12_minus_1
    - volatility: volatility_20d, 60d
    - volume: volume_ratio_20d
    - value_proxy: price_range_pct (cheap = near 52w low)
    - quality_proxy: volatility inverse + earnings stability proxy
    """
    if len(df) < 60:
        return pd.DataFrame()

    df = df.copy().sort_values("date").reset_index(drop=True)
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float).replace(0, np.nan)

    factors = pd.DataFrame(index=df.index)

    # ── Technical ──
    # RSI 14
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    factors["rsi_14"] = 100 - (100 / (1 + rs))

    # Bollinger Band position (-1 to +1, 0 = at middle)
    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    factors["bb_position"] = (close - bb_mid) / (2 * bb_std.replace(0, np.nan))

    # MACD signal
    ema_12 = close.ewm(span=12).mean()
    ema_26 = close.ewm(span=26).mean()
    macd = ema_12 - ema_26
    signal_line = macd.ewm(span=9).mean()
    factors["macd_signal"] = macd - signal_line

    # ── Momentum ──
    factors["momentum_5d"] = close.pct_change(5)
    factors["momentum_20d"] = close.pct_change(20)
    factors["momentum_60d"] = close.pct_change(60)
    # 12-1 month momentum (skip last month)
    if len(close) >= 252:
        factors["momentum_12m"] = close.shift(21).pct_change(231)
    else:
        factors["momentum_12m"] = close.pct_change(min(len(close) - 1, 252))

    # ── Volatility ──
    daily_ret = close.pct_change()
    factors["volatility_20d"] = daily_ret.rolling(20).std() * np.sqrt(252)
    factors["volatility_60d"] = daily_ret.rolling(60).std() * np.sqrt(252)

    # ── Volume ──
    factors["volume_ratio_20d"] = volume / volume.rolling(20).mean()

    # ── Value Proxy ──
    # 52-week position (0 = at low, 1 = at high) → invert for value (low = cheap = high score)
    rolling_high = close.rolling(252, min_periods=60).max()
    rolling_low = close.rolling(252, min_periods=60).min()
    price_range = (rolling_high - rolling_low).replace(0, np.nan)
    factors["price_52w_pct"] = (close - rolling_low) / price_range

    # SMA cross (trend strength)
    sma_20 = close.rolling(20).mean()
    sma_50 = close.rolling(50).mean()
    factors["sma_cross"] = (sma_20 - sma_50) / sma_50.replace(0, np.nan)

    # ── Quality Proxy ──
    # Low volatility + stable returns = higher quality
    # Rolling return consistency (Sharpe-like)
    rolling_ret = daily_ret.rolling(60).mean()
    rolling_vol = daily_ret.rolling(60).std()
    factors["return_stability"] = rolling_ret / rolling_vol.replace(0, np.nan)

    # ATR normalized
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    factors["atr_14_pct"] = (tr.rolling(14).mean() / close) * 100

    # Add date column for storage
    factors["date"] = df["date"]
    factors = factors.set_index("date")

    return factors


def normalize_to_0_100(series: pd.Series, lower_is_better: bool = False) -> pd.Series:
    """Normalize a series to 0-100 percentile score."""
    if series.empty or series.isna().all():
        return series
    ranked = series.rank(pct=True, na_option="keep")
    if lower_is_better:
        ranked = 1 - ranked
    return ranked * 100


def compute_cross_sectional_scores(
    all_factors: dict[str, pd.DataFrame],
    latest_date = None,
) -> pd.DataFrame:
    """
    Compute cross-sectional factor scores for all symbols on a given date.
    Returns DataFrame: symbol × factor_name → score (0-100).
    """
    # Collect latest factor values for all symbols
    rows = []
    for sym, fdf in all_factors.items():
        if fdf.empty:
            continue
        if latest_date:
            if latest_date in fdf.index:
                row = fdf.loc[latest_date]
            else:
                continue
        else:
            row = fdf.iloc[-1]

        row_dict = row.to_dict()
        row_dict["symbol"] = sym
        rows.append(row_dict)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).set_index("symbol")

    # Normalize each factor to 0-100
    scored = pd.DataFrame(index=df.index)

    # Technical: RSI → oversold is bullish (low RSI = high score)
    scored["technical"] = normalize_to_0_100(df["rsi_14"], lower_is_better=True) * 0.4 + \
                          normalize_to_0_100(df["bb_position"], lower_is_better=True) * 0.3 + \
                          normalize_to_0_100(df["macd_signal"]) * 0.3

    # Momentum: higher is better
    if "momentum_12m" in df.columns and df["momentum_12m"].notna().sum() > 5:
        scored["momentum"] = normalize_to_0_100(df["momentum_12m"]) * 0.4 + \
                             normalize_to_0_100(df["momentum_60d"]) * 0.3 + \
                             normalize_to_0_100(df["momentum_20d"]) * 0.3
    else:
        scored["momentum"] = normalize_to_0_100(df["momentum_60d"]) * 0.5 + \
                             normalize_to_0_100(df["momentum_20d"]) * 0.5

    # Value: low price_52w_pct = near 52w low = value (inverted)
    scored["value_score"] = normalize_to_0_100(df["price_52w_pct"], lower_is_better=True)

    # Quality: high return_stability + low volatility
    scored["quality"] = normalize_to_0_100(df["return_stability"]) * 0.6 + \
                        normalize_to_0_100(df["volatility_60d"], lower_is_better=True) * 0.4

    # Low volatility factor
    scored["low_volatility"] = normalize_to_0_100(df["volatility_20d"], lower_is_better=True)

    # Volume confirmation
    scored["volume_surge"] = normalize_to_0_100(df["volume_ratio_20d"])

    # Trend (SMA cross)
    scored["trend"] = normalize_to_0_100(df["sma_cross"])

    return scored


def main():
    parser = argparse.ArgumentParser(description="Compute and store historical factors")
    parser.add_argument("--symbols", nargs="+", help="Override symbols")
    parser.add_argument("--universe", default="global", help="Universe name")
    parser.add_argument("--update-today", action="store_true", help="Only compute latest date")
    args = parser.parse_args()

    hist_store = HistoricalStore()
    feat_store = FeatureStore()

    # Get symbols
    if args.symbols:
        symbols = args.symbols
    else:
        symbols = hist_store.get_all_symbols()

    if not symbols:
        logger.error("No symbols with historical data. Run run_backtest.py first.")
        return

    logger.info("Computing factors for %d symbols", len(symbols))

    # Step 1: Compute per-symbol time-series factors
    t0 = time.time()
    all_factors = {}
    for sym in symbols:
        df = hist_store.get_ohlcv(sym)
        if df.empty or len(df) < 60:
            continue
        factors = compute_all_factors(df)
        if not factors.empty:
            all_factors[sym] = factors

    logger.info("Computed factors for %d symbols in %.1fs", len(all_factors), time.time() - t0)
    stored_count = 0

    # Step 2: Store factor values in FeatureStore
    if args.update_today:
        # Use each symbol's latest date (not a global date)
        # This handles different market calendars (US vs HK vs EU)
        scored = compute_cross_sectional_scores(all_factors, latest_date=None)
        if not scored.empty:
            # Use today's date as the storage date
            from datetime import date as date_cls
            today_str = str(date_cls.today())
            factor_df = scored.reset_index()
            count = feat_store.save_factor_values(today_str, factor_df)
            stored_count = count
            logger.info("Stored %d factor values for %s (per-symbol latest)", count, today_str)
    else:
        # Store all dates
        dates_to_store = set()
        for sym, fdf in all_factors.items():
            for d in fdf.index:
                dates_to_store.add(str(d))

        logger.info("Storing factors for %d dates", len(dates_to_store))

        for date_str in sorted(dates_to_store):
            scored = compute_cross_sectional_scores(all_factors, latest_date=date_str)
            if scored.empty:
                continue
            factor_df = scored.reset_index()
            count = feat_store.save_factor_values(date_str, factor_df)
            stored_count += count

        logger.info("Stored %d factor values across %d dates", stored_count, len(dates_to_store))

    # Step 3: Compute and store IC (Information Coefficient) for each factor
    logger.info("Computing IC values...")
    ic_count = compute_and_store_ic(all_factors, feat_store)
    logger.info("Stored %d IC values", ic_count)

    # Step 4: Summary
    all_factor_names = feat_store.get_all_factors()
    logger.info("Factors in store: %s", all_factor_names)

    for fn in all_factor_names[:6]:
        stats = feat_store.get_factor_stats(fn, "2024-01-01", "2026-12-31")
        logger.info("  %s: mean=%.1f, std=%.1f, count=%d, IC=%.3f",
                     fn, stats["mean"], stats["std"], stats["count"], stats["ic_mean"])

    hist_store.close()
    feat_store.close()


def compute_and_store_ic(all_factors: dict, feat_store: FeatureStore) -> int:
    """
    Compute Information Coefficient (IC) for each factor.
    IC = rank correlation between factor value and forward 20d return.
    """
    # Collect factor-forward_return pairs
    ic_data = {}

    for sym, fdf in all_factors.items():
        if fdf.empty or "momentum_20d" not in fdf.columns:
            continue

        # Forward 20d return as the "target"
        close_proxy = fdf["momentum_20d"].shift(-20)  # Approximate

        for col in fdf.columns:
            if col.startswith("momentum_20d"):
                continue  # Skip the target itself
            if col not in ic_data:
                ic_data[col] = []

            valid = fdf[[col]].dropna()
            if len(valid) > 30:
                # Rank correlation
                corr = fdf[col].corr(fdf["momentum_20d"].shift(-20), method="spearman")
                if not np.isnan(corr):
                    ic_data[col].append((str(fdf.index[-1]), corr))

    # Store IC values
    count = 0
    for factor_name, ic_pairs in ic_data.items():
        if ic_pairs:
            ic_dict = {date: ic for date, ic in ic_pairs[-60:]}  # Last 60 observations
            feat_store.save_ic_history(factor_name, ic_dict)
            count += len(ic_dict)

    return count


if __name__ == "__main__":
    main()
