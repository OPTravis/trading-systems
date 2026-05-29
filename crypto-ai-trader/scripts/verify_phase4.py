#!/usr/bin/env python3
"""
Verify Phase 4 Order Book Analyzer.

Tests (logic only, no real API calls):
1. Score computation with balanced book
2. Score computation with buy pressure
3. Score computation with sell pressure
4. Spread impact on score
5. Whale detection logic
"""

import sys
import os

sys.path.insert(0, os.path.expanduser("~/crypto-ai-trader"))


def test_orderbook_analyzer():
    """Verification of order book score computation."""

    from src.orderbook_analyzer import OrderBookAnalyzer

    # Create analyzer without real client (we test _compute_score directly)
    analyzer = OrderBookAnalyzer.__new__(OrderBookAnalyzer)

    # 1. Balanced book → score ~50
    score = analyzer._compute_score(
        bid_ask_ratio=1.0, spread_pct=0.05,
        whale_bid=10000, whale_ask=10000,
        bid_volume_usdt=50000, ask_volume_usdt=50000,
    )
    assert 45 <= score <= 55, f"Balanced book should score ~50, got {score}"
    print(f"✅ 1. Balanced book: score={score:.1f}")

    # 2. Strong buy pressure → score > 70
    score = analyzer._compute_score(
        bid_ask_ratio=2.5, spread_pct=0.02,
        whale_bid=50000, whale_ask=10000,
        bid_volume_usdt=200000, ask_volume_usdt=80000,
    )
    assert score > 70, f"Buy pressure should score >70, got {score}"
    print(f"✅ 2. Buy pressure: score={score:.1f}")

    # 3. Strong sell pressure → score < 30
    score = analyzer._compute_score(
        bid_ask_ratio=0.3, spread_pct=0.02,
        whale_bid=5000, whale_ask=50000,
        bid_volume_usdt=30000, ask_volume_usdt=200000,
    )
    assert score < 30, f"Sell pressure should score <30, got {score}"
    print(f"✅ 3. Sell pressure: score={score:.1f}")

    # 4. Wide spread → lower score
    tight = analyzer._compute_score(
        bid_ask_ratio=1.0, spread_pct=0.01,
        whale_bid=10000, whale_ask=10000,
        bid_volume_usdt=50000, ask_volume_usdt=50000,
    )
    wide = analyzer._compute_score(
        bid_ask_ratio=1.0, spread_pct=2.0,
        whale_bid=10000, whale_ask=10000,
        bid_volume_usdt=50000, ask_volume_usdt=50000,
    )
    assert tight > wide, f"Tight spread ({tight:.1f}) should score higher than wide ({wide:.1f})"
    print(f"✅ 4. Spread impact: tight={tight:.1f}, wide={wide:.1f}")

    # 5. Whale bid dominance → higher score
    no_whale = analyzer._compute_score(
        bid_ask_ratio=1.0, spread_pct=0.05,
        whale_bid=1000, whale_ask=1000,
        bid_volume_usdt=50000, ask_volume_usdt=50000,
    )
    whale_bid = analyzer._compute_score(
        bid_ask_ratio=1.0, spread_pct=0.05,
        whale_bid=100000, whale_ask=1000,
        bid_volume_usdt=50000, ask_volume_usdt=50000,
    )
    assert whale_bid > no_whale, f"Whale bid ({whale_bid:.1f}) should score higher than no whale ({no_whale:.1f})"
    print(f"✅ 5. Whale detection: whale_bid={whale_bid:.1f}, no_whale={no_whale:.1f}")

    # 6. Score bounds
    for _ in range(20):
        import random
        random.seed(_)
        s = analyzer._compute_score(
            bid_ask_ratio=random.uniform(0.1, 5.0),
            spread_pct=random.uniform(0.001, 5.0),
            whale_bid=random.uniform(0, 100000),
            whale_ask=random.uniform(0, 100000),
            bid_volume_usdt=random.uniform(0, 500000),
            ask_volume_usdt=random.uniform(0, 500000),
        )
        assert 0 <= s <= 100, f"Score {s} out of bounds"
    print("✅ 6. Score bounds: OK (all 0-100)")

    print("\n" + "=" * 50)
    print("ALL PHASE 4 VERIFICATIONS PASSED")
    print("=" * 50)
    return True


if __name__ == "__main__":
    success = test_orderbook_analyzer()
    sys.exit(0 if success else 1)
