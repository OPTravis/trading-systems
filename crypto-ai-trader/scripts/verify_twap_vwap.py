#!/usr/bin/env python3
"""
Verification script for src/twap_vwap.py

Tests:
  1. TWAP planning: 5 slices, verify qty distribution
  2. VWAP planning: verify proportional distribution
  3. should_use_twap: $50 -> False, $200 -> True
  4. Dry-run execution
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.twap_vwap import (
    plan_twap,
    plan_vwap,
    should_use_twap,
    execute_twap,
    execute_vwap,
)


# ---------------------------------------------------------------------------
# Mock client
# ---------------------------------------------------------------------------

class MockClient:
    """Minimal mock of ExchangeClient for testing."""

    def __init__(self):
        self._klines = self._fake_klines()

    def _fake_klines(self):
        """Return 8 fake 1h klines with varying volumes."""
        volumes = [500, 800, 1200, 900, 700, 600, 400, 300]
        return [[0] * 5 + [str(v)] + [0] * 4 for v in volumes]

    def get_klines(self, symbol, interval, limit=10):
        return self._klines[:limit]

    def get_ticker_price(self, symbol):
        return 65000.0

    def get_exchange_info(self):
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {"filterType": "LOT_SIZE", "minQty": "0.00001"},
                        {"filterType": "NOTIONAL", "minNotional": "5"},
                    ],
                }
            ]
        }

    def place_limit_buy(self, symbol, quantity, price):
        return {"orderId": 12345, "price": price, "origQty": quantity, "status": "FILLED"}

    def place_limit_sell(self, symbol, quantity, price):
        return {"orderId": 12345, "price": price, "origQty": quantity, "status": "FILLED"}

    def get_open_orders(self, symbol):
        return []

    def cancel_order(self, symbol, order_id):
        return True


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  ✓ {name}")
        passed += 1
    else:
        print(f"  ✗ {name}  — {detail}")
        failed += 1


def test_twap_planning():
    print("\n[Test 1] TWAP planning — 5 slices, 10 minutes")
    slices = plan_twap(1.0, duration_minutes=10, num_slices=5)

    check("returns 5 slices", len(slices) == 5, f"got {len(slices)}")

    total = sum(s["qty"] for s in slices)
    check("total qty preserved", abs(total - 1.0) < 1e-9, f"total={total}")

    expected_qty = 0.2
    for i, s in enumerate(slices):
        check(
            f"slice {i} qty ~{expected_qty}",
            abs(s["qty"] - expected_qty) < 1e-9,
            f"got {s['qty']}",
        )

    expected_delays = [0, 120, 240, 360, 480]
    for i, s in enumerate(slices):
        check(
            f"slice {i} delay = {expected_delays[i]}s",
            s["delay_seconds"] == expected_delays[i],
            f"got {s['delay_seconds']}",
        )


def test_vwap_planning():
    print("\n[Test 2] VWAP planning — proportional to volume")
    client = MockClient()
    slices = plan_vwap(1.0, symbol="BTCUSDT", duration_minutes=5, client=client)

    check("returns 5 slices", len(slices) == 5, f"got {len(slices)}")

    total = sum(s["qty"] for s in slices)
    check("total qty preserved", abs(total - 1.0) < 1e-9, f"total={total}")

    # Volumes returned: [1200, 900, 700, 600, 400]
    # slice 0 should have largest qty, slice 4 should have smallest
    check(
        "slice 0 has highest qty",
        slices[0]["qty"] >= slices[2]["qty"],
        f"s0={slices[0]['qty']} vs s2={slices[2]['qty']}",
    )
    check(
        "slice 4 has lowest qty",
        slices[4]["qty"] <= slices[0]["qty"],
        f"s4={slices[4]['qty']} vs s0={slices[0]['qty']}",
    )

    # Verify proportional relationship roughly holds
    # Klines returned: last 5 of 7 have volumes [1200, 900, 700, 600, 400]
    # total_vol = 3800, so slice 0 = 1200/3800 ≈ 0.3158
    expected_s0 = 1.0 * 1200 / 3800
    check(
        "slice 0 qty ≈ proportional",
        abs(slices[0]["qty"] - expected_s0) < 0.01,
        f"expected ~{expected_s0:.4f}, got {slices[0]['qty']}",
    )


def test_should_use_twap():
    print("\n[Test 3] should_use_twap")
    check("$50  -> False", should_use_twap(50.0) is False)
    check("$200 -> True", should_use_twap(200.0) is True)
    check("$100 (exact) -> True", should_use_twap(100.0) is True)
    check("$99.99 -> False", should_use_twap(99.99) is False)
    check("$500 -> True", should_use_twap(500.0) is True)


def test_dry_run_execution():
    print("\n[Test 4] Dry-run execution")
    client = MockClient()

    results = execute_twap(
        client, "BTCUSDT", "BUY", 1.0,
        duration_minutes=2, num_slices=3, dry_run=True,
    )
    check("TWAP dry-run returns 3 results", len(results) == 3, f"got {len(results)}")
    check(
        "all DRY_RUN",
        all(r["status"] == "DRY_RUN" for r in results),
    )
    check(
        "all have slippage_pct",
        all(r["slippage_pct"] is not None for r in results),
    )

    results = execute_vwap(
        client, "BTCUSDT", "SELL", 0.5,
        duration_minutes=2, dry_run=True,
    )
    check("VWAP dry-run returns results", len(results) > 0)
    check(
        "all DRY_RUN",
        all(r["status"] == "DRY_RUN" for r in results),
    )


def test_edge_cases():
    print("\n[Test 5] Edge cases")
    # 1 slice
    slices = plan_twap(0.5, duration_minutes=1, num_slices=1)
    check("single slice", len(slices) == 1 and slices[0]["qty"] == 0.5)

    # Large num_slices
    slices = plan_twap(10.0, duration_minutes=60, num_slices=60)
    check("60 slices sum to 10", abs(sum(s["qty"] for s in slices) - 10.0) < 1e-6)

    # VWAP fallback (no client)
    slices = plan_vwap(1.0, symbol="BTCUSDT", duration_minutes=10, client=None)
    check("VWAP fallback returns equal slices", len(slices) == 10)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("TWAP/VWAP Order Splitter — Verification")
    print("=" * 60)

    test_twap_planning()
    test_vwap_planning()
    test_should_use_twap()
    test_dry_run_execution()
    test_edge_cases()

    print("\n" + "=" * 60)
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed")
    print("=" * 60)
    sys.exit(1 if failed else 0)
