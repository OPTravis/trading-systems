#!/usr/bin/env python3
"""
Verification script for the Feature Store module.

Tests:
  1. Store features for 2 symbols
  2. Retrieve and verify
  3. Snapshot to training
  4. Get training data
  5. Get stats
  6. Test fallback (force in-memory mode)
"""

import sys
import os

# Ensure the src package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.feature_store import FeatureStore

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def main():
    global PASS, FAIL

    print("=" * 60)
    print("Feature Store Verification")
    print("=" * 60)

    # ------------------------------------------------------------------ #
    # 1. Store features for 2 symbols
    # ------------------------------------------------------------------ #
    print("\n1. Storing features for 2 symbols …")
    fs = FeatureStore()
    check("Redis connected", fs._redis_available, "(Redis might not be running)")

    btc_features = {
        "price": 64500.0,
        "rsi_14": 55.2,
        "ema_20": 64000.0,
        "volume_sma": 1200.5,
        "macd_signal": 0.003,
    }
    eth_features = {
        "price": 3200.0,
        "rsi_14": 48.7,
        "ema_20": 3150.0,
        "volume_sma": 800.3,
        "macd_signal": -0.001,
    }

    check("Store BTC features", fs.store_features("BTC-USDT", btc_features))
    check("Store ETH features", fs.store_features("ETH-USDT", eth_features))

    # ------------------------------------------------------------------ #
    # 2. Retrieve and verify
    # ------------------------------------------------------------------ #
    print("\n2. Retrieving and verifying …")
    btc = fs.get_features("BTC-USDT")
    check("BTC features not None", btc is not None)
    if btc:
        check("BTC price correct", btc["price"] == 64500.0, f"got {btc.get('price')}")
        check("BTC rsi correct", btc["rsi_14"] == 55.2, f"got {btc.get('rsi_14')}")
        check("BTC has 5 keys", len(btc) == 5, f"got {len(btc)} keys")

    eth = fs.get_features("ETH-USDT")
    check("ETH features not None", eth is not None)
    if eth:
        check("ETH price correct", eth["price"] == 3200.0, f"got {eth.get('price')}")

    missing = fs.get_features("SOL-USDT")
    check("Missing symbol returns None", missing is None)

    # ------------------------------------------------------------------ #
    # 3. Snapshot to training
    # ------------------------------------------------------------------ #
    print("\n3. Snapshotting to training …")
    check("Snapshot BTC label=1", fs.snapshot_for_training("BTC-USDT", label=1))
    check("Snapshot BTC label=0", fs.snapshot_for_training("BTC-USDT", label=0))
    check("Snapshot ETH label=1", fs.snapshot_for_training("ETH-USDT", label=1))
    check("Snapshot missing symbol fails",
          not fs.snapshot_for_training("DOGE-USDT", label=1))

    # ------------------------------------------------------------------ #
    # 4. Get training data
    # ------------------------------------------------------------------ #
    print("\n4. Getting training data …")
    btc_train = fs.get_training_data("BTC-USDT")
    check("BTC training data not empty", len(btc_train) > 0, f"got {len(btc_train)} samples")
    if btc_train:
        check("BTC training has 'features' key", "features" in btc_train[0])
        check("BTC training has 'label' key", "label" in btc_train[0])
        check("BTC training has 'timestamp' key", "timestamp" in btc_train[0])
        check("BTC training label correct",
              btc_train[0]["label"] in (0, 1))

    all_train = fs.get_training_data()
    check("All training data >= 2 samples", len(all_train) >= 2, f"got {len(all_train)}")

    # ------------------------------------------------------------------ #
    # 5. Feature names
    # ------------------------------------------------------------------ #
    print("\n5. Getting feature names …")
    names = fs.get_feature_names()
    check("Feature names is a list", isinstance(names, list))
    check("Feature names not empty", len(names) > 0, f"got {names}")
    check("price in feature names", "price" in names, f"got {names}")

    # ------------------------------------------------------------------ #
    # 6. Clear namespace
    # ------------------------------------------------------------------ #
    print("\n6. Clearing namespace …")
    cleared = fs.clear_namespace("online")
    check("Clear online returns int", isinstance(cleared, int))
    check("Cleared at least 1 key", cleared >= 1, f"cleared {cleared}")
    check("Online features gone after clear", fs.get_features("BTC-USDT") is None)

    # ------------------------------------------------------------------ #
    # 7. Stats
    # ------------------------------------------------------------------ #
    print("\n7. Getting stats …")
    stats = fs.get_stats()
    check("Stats is a dict", isinstance(stats, dict))
    check("Stats has training_count", "training_count" in stats, f"keys: {list(stats.keys())}")
    check("Stats has backend", "backend" in stats)

    # ------------------------------------------------------------------ #
    # 8. Test in-memory fallback
    # ------------------------------------------------------------------ #
    print("\n8. Testing in-memory fallback …")
    fs.force_fallback()
    check("Fallback mode active", not fs._redis_available)

    check("Fallback: store features",
          fs.store_features("SOL-USDT", {"price": 150.0, "rsi_14": 60.0}))
    sol = fs.get_features("SOL-USDT")
    check("Fallback: retrieve features", sol is not None)
    if sol:
        check("Fallback: price correct", sol["price"] == 150.0)
    check("Fallback: snapshot",
          fs.snapshot_for_training("SOL-USDT", label=1))
    sol_train = fs.get_training_data("SOL-USDT")
    check("Fallback: training data", len(sol_train) > 0, f"got {len(sol_train)}")
    fallback_stats = fs.get_stats()
    check("Fallback: stats backend", fallback_stats.get("backend") == "in_memory")

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #
    print("\n" + "=" * 60)
    total = PASS + FAIL
    print(f"Results: {PASS}/{total} passed, {FAIL} failed")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
