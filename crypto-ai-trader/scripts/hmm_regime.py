#!/usr/bin/env python3
"""
HMM Market Regime Detector — train and predict.

Usage:
    python scripts/hmm_regime.py --train       # Train on BTC 90-day data
    python scripts/hmm_regime.py --predict     # Predict current regime
    python scripts/hmm_regime.py --report      # Show current regime
"""

import sys
import os
import argparse
import logging

sys.path.insert(0, os.path.expanduser("~/trading-systems/crypto-ai-trader"))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _fetch_klines_paged(client, days: int) -> list:
    """Fetch ~days of BTC 1h klines, paging past the single-request bar cap.

    Binance caps one request at 1000 bars (~41.7 days) which silently
    truncated the nominal 90-day training window to 41 days (variance
    collapse root cause). Pages backwards via endTime until `days` reached
    or the API stops returning full pages. Returns oldest-first list.
    """
    total = days * 24
    collected = []
    end_time = None  # ms; None = up to now
    while len(collected) < total:
        need = min(1000, total - len(collected))
        page = client.get_klines("BTCUSDT", "1h", limit=need, end_time=end_time)
        if not page:
            break
        collected = page + collected  # prepend older bars
        oldest = page[0]["open_time"]
        end_time = int(oldest) - 1
        if len(page) < need:
            break  # API exhausted everything it has
    logger.info(
        f"Fetched {len(collected)} x 1h bars = {len(collected)/24:.1f} days "
        f"(requested {days}d)")
    return collected


def main():
    parser = argparse.ArgumentParser(description="HMM Market Regime Detector")
    parser.add_argument("--train", action="store_true", help="Train HMM on BTC data")
    parser.add_argument("--predict", action="store_true", help="Predict current regime")
    parser.add_argument("--report", action="store_true", help="Show cached regime")
    parser.add_argument("--days", type=int, default=90, help="Training data days")
    args = parser.parse_args()

    from src.hmm_regime import HMMRegimeDetector

    detector = HMMRegimeDetector()

    if args.report:
        cached = detector.get_cached_prediction()
        if cached:
            print(detector.format_report(cached))
        else:
            print("無快取預測結果。先執行 --train 再 --predict。")
        return

    if args.train:
        from src.binance_client import BinanceClient
        client = BinanceClient(testnet=False)

        # Fetch BTC 1h klines (paged past the 1000-bar API cap)
        klines = _fetch_klines_paged(client, args.days)
        logger.info(f"Got {len(klines)} klines = {len(klines)/24:.1f} days")

        success = detector.train(klines)
        if success:
            logger.info("✅ HMM training succeeded")

            # Also predict immediately
            prediction = detector.predict(klines)
            if prediction:
                print(detector.format_report(prediction))
        else:
            logger.error("❌ HMM training failed")
        return

    if args.predict:
        from src.binance_client import BinanceClient
        client = BinanceClient(testnet=False)

        # Need 20+ days (480+ klines) for daily aggregation in _compute_features
        klines = client.get_klines("BTCUSDT", "1h", limit=1000)
        prediction = detector.predict(klines)
        if prediction:
            print(detector.format_report(prediction))
        else:
            print("預測失敗。先執行 --train。")
        return

    # Default: show report
    cached = detector.get_cached_prediction()
    if cached:
        print(detector.format_report(cached))
    else:
        print("用法: hmm_regime.py --train | --predict | --report")


if __name__ == "__main__":
    main()
