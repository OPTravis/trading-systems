"""Test 3: Market Scanner — Can we run a single-symbol scan?"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.ccxt_client import BinanceClient
from src.market_scanner import MarketScanner


def test_single_scan():
    client = BinanceClient()
    print("[1] BinanceClient initialized")

    scanner = MarketScanner(binance_client=client)
    print("[2] MarketScanner initialized")

    # Build a minimal coin_data dict for _analyze_coin
    coin_data = {
        "symbol": "BTCUSDT",
        "price": 0,
        "volume_24h": 0,
        "price_change_24h": 0,
        "rank": 0,
        "volume_surge": False,
    }

    print("[3] Running _analyze_coin for BTCUSDT...")
    result = scanner._analyze_coin(coin_data)

    if result is None:
        print("[INFO] _analyze_coin returned None (score < 50 or no entry signal)")
        print("       This is EXPECTED if BTCUSDT doesn't meet the threshold.")
        print(
            "[PASS] Test 3 — Scanner ran without crashing (result=None is acceptable)"
        )
    else:
        print(
            f"[3] Result: symbol={result['symbol']}, score={result['score']}, entry_signal={result.get('entry_signal')}"
        )
        print(f"    signals: {result.get('signals', [])}")
        print("[PASS] Test 3 — Single-symbol scan completed successfully")


if __name__ == "__main__":
    test_single_scan()
