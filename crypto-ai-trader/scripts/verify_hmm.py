#!/usr/bin/env python3
"""
Verify HMM Regime Detector.

Tests (logic only, no API calls):
1. Feature computation with synthetic data
2. RSI computation
3. BB position computation
4. Regime strategy map completeness
5. Report formatting
"""

import sys
import os
import numpy as np

sys.path.insert(0, os.path.expanduser("~/trading-systems/crypto-ai-trader"))


def test_hmm_regime():
    """Verification of HMM regime detector logic."""

    from src.hmm_regime import HMMRegimeDetector, REGIME_LABELS, REGIME_STRATEGY_MAP

    detector = HMMRegimeDetector.__new__(HMMRegimeDetector)

    # 1. RSI computation
    prices = np.array([100, 102, 101, 103, 105, 104, 106, 108, 107, 109,
                       110, 112, 111, 113, 115, 114, 116, 118, 117, 119])
    rsi = detector._compute_rsi(prices, period=14)
    assert len(rsi) == len(prices)
    assert 0 <= rsi[-1] <= 100
    # Upward trend should have RSI > 50
    assert rsi[-1] > 50, f"Expected RSI > 50 for uptrend, got {rsi[-1]}"
    print(f"✅ 1. RSI computation: {rsi[-1]:.1f}")

    # 2. BB position
    bb = detector._compute_bb_position(prices, period=10, std_dev=2.0)
    assert len(bb) == len(prices)
    # Price near top of range should have bb_pos > 0.5
    assert bb[-1] > 0.5, f"Expected BB pos > 0.5 for uptrend, got {bb[-1]}"
    print(f"✅ 2. BB position: {bb[-1]:.2f}")

    # 3. Regime labels
    assert len(REGIME_LABELS) == 4
    assert "BULL_TREND" in REGIME_LABELS
    assert "BEAR_TREND" in REGIME_LABELS
    assert "RANGE_BOUND" in REGIME_LABELS
    assert "HIGH_VOL" in REGIME_LABELS
    print("✅ 3. Regime labels: 4 regimes defined")

    # 4. Strategy map completeness
    for regime in REGIME_LABELS:
        adj = REGIME_STRATEGY_MAP[regime]
        assert "preferred_strategies" in adj
        assert "avoid_strategies" in adj
        assert "position_scale" in adj
        assert "score_threshold_adj" in adj
    print("✅ 4. Strategy map: all regimes have adjustments")

    # 5. Strategy map logic
    bull = REGIME_STRATEGY_MAP["BULL_TREND"]
    bear = REGIME_STRATEGY_MAP["BEAR_TREND"]
    assert bull["position_scale"] > bear["position_scale"], "Bull should be more aggressive"
    assert bear["score_threshold_adj"] > bull["score_threshold_adj"], "Bear should have higher threshold"
    assert "trend" in bull["preferred_strategies"], "Trend should be preferred in bull"
    assert "trend" in bear["avoid_strategies"], "Trend should be avoided in bear"
    print("✅ 5. Strategy map logic: correct")

    # 6. Feature computation with synthetic 1h klines
    np.random.seed(42)
    n = 100 * 24  # 100 days of 1h data
    prices_1h = 100 * np.exp(np.cumsum(np.random.randn(n) * 0.01))
    klines = []
    for i in range(n):
        o = prices_1h[i]
        c = o * (1 + np.random.randn() * 0.005)
        h = max(o, c) * 1.001
        l = min(o, c) * 0.999
        klines.append([0, f"{o:.2f}", f"{h:.2f}", f"{l:.2f}", f"{c:.2f}", "1000", 0, 0, 0, 0, 0, 0])

    features = detector._compute_features(klines)
    assert features is not None, "Should compute features from 100 days"
    assert features.shape[1] == 4, f"Expected 4 features, got {features.shape[1]}"
    assert features.shape[0] >= 20, f"Expected >= 20 samples, got {features.shape[0]}"
    print(f"✅ 6. Feature computation: {features.shape[0]} samples × {features.shape[1]} features")

    # 7. Report formatting (with mock prediction)
    mock = {
        "regime": "BULL_TREND",
        "regime_idx": 3,
        "probabilities": {
            "BEAR_TREND": 0.05,
            "HIGH_VOL": 0.10,
            "RANGE_BOUND": 0.25,
            "BULL_TREND": 0.60,
        },
        "confidence": 0.60,
        "features": {"return": 0.0012, "volatility": 0.45, "rsi": 62.3, "bb_position": 0.72},
        "timestamp": 1234567890,
    }
    report = detector.format_report(mock)
    assert "HMM 市場體制" in report
    assert "牛市趨勢" in report
    assert "60.0%" in report
    print("✅ 7. Report formatting: OK")

    print("\n" + "=" * 50)
    print("ALL HMM REGIME VERIFICATIONS PASSED")
    print("=" * 50)
    return True


if __name__ == "__main__":
    success = test_hmm_regime()
    sys.exit(0 if success else 1)
