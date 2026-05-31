"""Test 2: OrderBook — Can we get depth data and analyze it?"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.ccxt_client import BinanceClient
from src.orderbook_analyzer import OrderBookAnalyzer


def test_orderbook():
    client = BinanceClient()
    print("[1] BinanceClient initialized OK")

    # Test raw depth fetch via client
    depth = client.get_order_book("BTCUSDT", limit=10)
    print(
        f"[2] get_order_book returned bids={len(depth.get('bids', []))} asks={len(depth.get('asks', []))}"
    )
    bids = depth.get("bids", [])
    asks = depth.get("asks", [])
    if not bids or not asks:
        print("[SKIP] Test 2 — Order book empty (API key may be missing/invalid)")
        return
    print(f"    Best bid: {depth['bids'][0]}, Best ask: {depth['asks'][0]}")

    # Test OrderBookAnalyzer
    ob = OrderBookAnalyzer(binance_client=client)
    result = ob.analyze("BTCUSDT", limit=10)
    assert result is not None, "analyze() returned None"
    print("[3] OrderBook analysis result:")
    for k, v in result.items():
        print(f"    {k}: {v}")
    assert 0 <= result["score"] <= 100, f"Score out of range: {result['score']}"
    print(f"[PASS] Test 2 — OrderBook analysis works (score={result['score']})")


if __name__ == "__main__":
    test_orderbook()
