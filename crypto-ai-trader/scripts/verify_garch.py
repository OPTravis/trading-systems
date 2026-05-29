"""Verification script for garch_vol module."""

import sys
sys.path.insert(0, "/home/travis/crypto-ai-trader/src")

import numpy as np
from garch_vol import forecast_volatility, get_dynamic_sl_tp, get_vol_regime, train_from_klines

VALID_REGIMES = {"low", "normal", "high", "extreme"}

# Test 1: forecast_volatility with synthetic returns
np.random.seed(42)
returns = (np.random.randn(200) * 0.02).tolist()
result = forecast_volatility(returns)

assert result["forecast_vol"] > 0, f"forecast_vol must be > 0, got {result['forecast_vol']}"
assert result["vol_regime"] in VALID_REGIMES, f"Invalid regime: {result['vol_regime']}"
assert result["current_vol"] > 0, f"current_vol must be > 0"
assert result["annualized_vol"] > 0, f"annualized_vol must be > 0"
print(f"[PASS] forecast_volatility: vol={result['forecast_vol']:.4f}, regime={result['vol_regime']}")

# Test 2: get_dynamic_sl_tp
sl_tp = get_dynamic_sl_tp("BTCUSDT", 50000, 0.03)
assert sl_tp["sl_pct"] < 0, f"sl_pct must be negative, got {sl_tp['sl_pct']}"
assert sl_tp["tp_pct"] > 0, f"tp_pct must be positive, got {sl_tp['tp_pct']}"
assert sl_tp["trailing_activation"] > 0
assert sl_tp["trailing_step"] > 0
print(f"[PASS] get_dynamic_sl_tp: sl={sl_tp['sl_pct']}, tp={sl_tp['tp_pct']}")

# Test 3: fallback with insufficient data
short_returns = (np.random.randn(10) * 0.02).tolist()
fallback = forecast_volatility(short_returns)
assert fallback["forecast_vol"] > 0
assert fallback["vol_regime"] in VALID_REGIMES
print(f"[PASS] Fallback with {len(short_returns)} points: regime={fallback['vol_regime']}")

# Test 4: train_from_klines with insufficient data
klines = [{"close": str(100 + i)} for i in range(10)]
assert train_from_klines("TEST", klines) == False
print("[PASS] train_from_klines rejects insufficient data")

# Test 5: train_from_klines with enough data
klines = [{"close": str(100 + np.random.randn())} for i in range(100)]
ok = train_from_klines("TESTVERIFY", klines)
assert ok, "train_from_klines should succeed with 100 klines"
print("[PASS] train_from_klines succeeds with enough data")

# Test 6: vol regimes
assert get_vol_regime(0.20) == "low"
assert get_vol_regime(0.45) == "normal"
assert get_vol_regime(0.80) == "high"
assert get_vol_regime(1.50) == "extreme"
print("[PASS] All vol regime thresholds correct")

print("\n=== ALL TESTS PASSED ===")
