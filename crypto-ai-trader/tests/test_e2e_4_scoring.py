"""Test 4: Scoring — Does multi-factor scoring produce valid scores?"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.ccxt_client import BinanceClient
from src.market_scanner import MarketScanner


def test_scoring():
    client = BinanceClient()
    scanner = MarketScanner(binance_client=client)

    print("[1] MarketScanner initialized")

    # Provide realistic mock data for scoring (no API calls for mtf_result)
    mtf_result = {
        "tf_1h": {
            "current_price": 104000,
            "rsi": 55,
            "macd_histogram": 0.02,
            "vwap": 103800,
            "bb_lower": 102000,
            "bb_upper": 106000,
            "bb_position": 0.55,
            "trend": "up",
            "price_action_score": 55,
            "volume_ratio": 1.1,
            "consolidation_score": 50,
            "bb_squeeze": False,
        },
        "tf_4h": {
            "macd_histogram": 0.01,
        },
        "tf_15m": {},
        "trend_alignment": "bullish",
        "trend_score": 65,
        "entry_signal": "long",
        "atr_15m": 150,
    }
    sentiment_data = {
        "sentiment_score": 55,
        "funding_rate": 0.0001,
        "oi_change_pct": 2.5,
    }
    coin_data = {
        "price": 104000,
        "volume_24h": 5e9,
        "price_change_24h": 3.2,
        "volume_surge": True,
    }
    new_signals_data = {
        "obv_div": {"divergence": True, "strength": 0.7},
        "bb_squeeze": {"squeezing": False, "bandwidth": 2.5},
        "rsi_div": {"divergence": False},
        "consolidation": {"breaking_out": True, "volume_confirmed": True},
    }

    print("[2] Calling _calculate_weighted_score with mock data...")
    result = scanner._calculate_weighted_score(
        mtf_result, sentiment_data, coin_data, new_signals_data
    )
    # Function returns (score, factor_scores) tuple
    if isinstance(result, tuple):
        score, factor_scores = result
    else:
        score = result
        factor_scores = {}
    print(f"[3] Weighted score: {score}")
    assert isinstance(score, (int, float)), f"Score is not numeric: {type(score)}"
    assert 0 <= score <= 100, f"Score out of range: {score}"

    # Check factor scores were stored
    if factor_scores:
        print(
            f"[4] Factor scores: {json.dumps({k: round(v, 2) if isinstance(v, (int, float)) else v for k, v in factor_scores.items()}, indent=2)}"
        )

    print(f"[PASS] Test 4 — Scoring produced valid score: {score}")


if __name__ == "__main__":
    import json

    test_scoring()
