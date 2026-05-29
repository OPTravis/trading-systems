#!/home/travis/crypto-ai-trader/.venv/bin/python3
"""Quick smoke test for circuit breaker, price deviation, and duplicate order checks."""
import sys, os
sys.path.insert(0, os.path.expanduser("~/crypto-ai-trader"))

import numpy as np
from unittest.mock import MagicMock, patch
from src.circuit_breaker import CircuitBreaker, get_circuit_breaker

# ── Circuit breaker smoke test ──
cb = CircuitBreaker()
assert not cb.is_tripped(), "Fresh CB should not be tripped"

for i in range(5):
    cb.record_failure("test")
assert cb.is_tripped(), "5 consecutive failures should trip CB"

cb.reset()
assert not cb.is_tripped(), "Reset should clear trip"
print("✅ CircuitBreaker: all checks passed")

# ── Price deviation check smoke test ──
from src.trade_executor import _check_price_deviation, _check_duplicate_order

mc = MagicMock()
# Mock 14 klines with varied closes around $100 (std ≈ $0.42)
# Format must match get_klines() return: list of dicts
klines = []
for i in range(14):
    klines.append({
        "open_time": 0, "open": 0.0, "high": 0.0, "low": 0.0,
        "close": 100.0 + i * 0.1, "volume": 0.0,
        "close_time": 0, "quote_volume": 0.0, "trades": 0, "is_closed": True,
    })
mc.get_klines.return_value = klines

assert _check_price_deviation(mc, "BTCUSDT", 100.5), "Price within 1σ should pass"
assert not _check_price_deviation(mc, "BTCUSDT", 105.0), "Price at +10σ should fail"
print("✅ Price deviation: all checks passed")

# ── Duplicate order check smoke test ──
mc2 = MagicMock()
mc2.get_open_orders.return_value = []
assert _check_duplicate_order(mc2, "BTCUSDT"), "No open orders should pass"

mc2.get_open_orders.return_value = [{"symbol": "BTCUSDT", "side": "BUY", "orderId": 123}]
assert not _check_duplicate_order(mc2, "BTCUSDT"), "Existing BUY order should block"
print("✅ Duplicate order: all checks passed")

print("\n🎉 All P0/P1/P2 smoke tests passed!")
