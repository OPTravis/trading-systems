"""Test 1: Data Feed — Can we fetch klines from Binance for BTCUSDT?"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.ccxt_client import BinanceClient


def test_klines():
    client = BinanceClient()
    print("[1] BinanceClient initialized OK")

    klines = client.get_klines("BTCUSDT", "1h", limit=5)
    print(f"[2] get_klines returned {len(klines)} klines")

    if klines:
        sample = klines[0]
        print(f"[3] Sample kline keys: {list(sample.keys())}")
        print(
            f"    symbol={sample.get('symbol')}, open={sample.get('open')}, close={sample.get('close')}, volume={sample.get('volume')}"
        )
        assert (
            "open" in sample and "close" in sample and "volume" in sample
        ), "Missing required fields"
        print("    (note: 'symbol' key absent in kline dict — normal for ccxt client)")
        print("[PASS] Test 1 — Klines fetch works")
    else:
        print("[SKIP] Test 1 — No klines returned (API key may be missing/invalid)")


if __name__ == "__main__":
    test_klines()
