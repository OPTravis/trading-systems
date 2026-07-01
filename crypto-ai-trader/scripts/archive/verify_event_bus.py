#!/usr/bin/env python3
"""Verification script for the in-process Event Bus."""

import sys
import os
import time
import tempfile
import json
from pathlib import Path

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from event_bus import EventBus, get_event_bus, reset_event_bus, DB_PATH


def test_basic_publish_and_query():
    """Publish 3 events, verify count and retrieval."""
    print("TEST 1: Basic publish and query...", end=" ")
    # Use a temp DB to avoid polluting real data
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    bus = EventBus(db_path=Path(tmp.name))
    try:
        e1 = bus.publish("trade_executed", {"symbol": "BTC", "amount": 1.0})
        e2 = bus.publish("position_opened", {"symbol": "ETH"})
        e3 = bus.publish("risk_alert", {"level": "high"})

        assert isinstance(e1, str) and len(e1) == 36, "Bad event_id"
        assert bus.get_event_count() == 3, f"Expected 3 events, got {bus.get_event_count()}"

        events = bus.get_events(limit=100)
        assert len(events) == 3, f"Expected 3 events from get_events, got {len(events)}"

        typed = bus.get_events(event_type="trade_executed")
        assert len(typed) == 1 and typed[0]["event_type"] == "trade_executed"
        assert typed[0]["data"]["symbol"] == "BTC"
        print("PASS")
    finally:
        os.unlink(tmp.name)


def test_subscribe_callback():
    """Subscribe and verify callback fires."""
    print("TEST 2: Subscribe callback...", end=" ")
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    bus = EventBus(db_path=Path(tmp.name))
    try:
        received = []

        def handler(event_id, event_type, data, timestamp):
            received.append({"id": event_id, "type": event_type, "data": data})

        bus.subscribe("trade_executed", handler)
        bus.publish("trade_executed", {"symbol": "SOL"})
        time.sleep(0.05)  # fire-and-forget is sync, but just in case

        assert len(received) == 1, f"Expected 1 callback, got {len(received)}"
        assert received[0]["type"] == "trade_executed"
        assert received[0]["data"]["symbol"] == "SOL"

        # Test unsubscribe
        bus.unsubscribe("trade_executed", handler)
        bus.publish("trade_executed", {"symbol": "DOGE"})
        assert len(received) == 1, "Callback fired after unsubscribe"
        print("PASS")
    finally:
        os.unlink(tmp.name)


def test_wildcard_subscribe():
    print("TEST 3: Wildcard subscribe...", end=" ")
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    bus = EventBus(db_path=Path(tmp.name))
    try:
        received = []
        bus.subscribe("*", lambda eid, et, d, ts: received.append(et))
        bus.publish("trade_executed", {})
        bus.publish("risk_alert", {})
        assert len(received) == 2 and set(received) == {"trade_executed", "risk_alert"}
        print("PASS")
    finally:
        os.unlink(tmp.name)


def test_callback_exception_resilience():
    """Bad callback shouldn't crash the bus."""
    print("TEST 4: Exception resilience...", end=" ")
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    bus = EventBus(db_path=Path(tmp.name))
    try:
        def bad_cb(*a):
            raise RuntimeError("boom")
        def good_cb(event_id, event_type, data, ts):
            good_cb.called = True
        good_cb.called = False

        bus.subscribe("trade_executed", bad_cb)
        bus.subscribe("trade_executed", good_cb)
        bus.publish("trade_executed", {})
        assert good_cb.called, "Good callback wasn't called after bad one"
        assert bus.get_event_count() == 1
        print("PASS")
    finally:
        os.unlink(tmp.name)


def test_singleton():
    """Singleton pattern works."""
    print("TEST 5: Singleton...", end=" ")
    reset_event_bus()
    try:
        a = get_event_bus()
        b = get_event_bus()
        assert a is b, "Singleton failed"
        print("PASS")
    finally:
        reset_event_bus()


def test_auto_prune():
    """Auto-prune keeps DB <= 10000."""
    print("TEST 6: Auto-prune (this may take a moment)...", end=" ", flush=True)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    bus = EventBus(db_path=Path(tmp.name))
    try:
        for i in range(10001):
            bus.publish("score_update", {"i": i})
        count = bus.get_event_count()
        assert count <= 10000, f"Expected <= 10000, got {count}"
        print(f"PASS (count={count})")
    finally:
        os.unlink(tmp.name)


if __name__ == "__main__":
    tests = [
        test_basic_publish_and_query,
        test_subscribe_callback,
        test_wildcard_subscribe,
        test_callback_exception_resilience,
        test_singleton,
        test_auto_prune,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"FAIL: {e}")
            failed += 1

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
    print("All tests passed!")
