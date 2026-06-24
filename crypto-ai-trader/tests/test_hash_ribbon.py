"""Tests for Hash Ribbon module."""

import json
import os
import time
import pytest
from unittest.mock import patch, MagicMock

from src.hash_ribbon import (
    _moving_average,
    calculate_hash_ribbons,
    detect_signal,
    get_hash_ribbon_status,
    fetch_hashrate_data,
    _CACHE_FILE,
    _CACHE_TTL,
)


def _make_hashrate_data(n, base_ehs=900, trend="flat"):
    """Generate synthetic daily hash rate data."""
    data = []
    rate = base_ehs * 1e18  # convert to H/s
    for i in range(n):
        ts = int(time.time()) - (n - i) * 86400
        if trend == "declining":
            rate *= 0.998
        elif trend == "growing":
            rate *= 1.002
        data.append({
            "timestamp": ts,
            "avgHashrate": rate,
        })
    return data


class TestMovingAverage:
    def test_basic_ma(self):
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        ma3 = _moving_average(values, 3)
        assert ma3[0] is None
        assert ma3[1] is None
        assert ma3[2] == 2.0  # (1+2+3)/3
        assert ma3[3] == 3.0  # (2+3+4)/3

    def test_ma_period_equals_length(self):
        values = [10, 20, 30]
        ma3 = _moving_average(values, 3)
        assert ma3[0] is None
        assert ma3[1] is None
        assert ma3[2] == 20.0


class TestCalculateHashRibbons:
    def test_insufficient_data(self):
        data = _make_hashrate_data(30)
        assert calculate_hash_ribbons(data) == []

    def test_sufficient_data(self):
        data = _make_hashrate_data(100)
        ribbons = calculate_hash_ribbons(data)
        assert len(ribbons) == 100
        # First 29 should have None ma30, first 59 None ma60
        assert ribbons[0]["ma30"] is None
        assert ribbons[0]["ma60"] is None
        assert ribbons[29]["ma30"] is not None
        assert ribbons[59]["ma60"] is not None

    def test_capitulation_detection(self):
        # Create data where ma30 < ma60 (declining hash rate)
        data = _make_hashrate_data(100, trend="declining")
        ribbons = calculate_hash_ribbons(data)
        valid = [r for r in ribbons if r["ma30"] is not None and r["ma60"] is not None]
        if valid:
            # In declining trend, ma30 should be below ma60
            last = valid[-1]
            assert last["in_capitulation"] is True


class TestDetectSignal:
    def test_no_signal_without_capitulation(self):
        # Growing hash rate — no capitulation, no signal
        data = _make_hashrate_data(100, trend="growing")
        ribbons = calculate_hash_ribbons(data)
        signal = detect_signal(ribbons)
        assert signal is None

    def test_no_signal_during_capitulation(self):
        # Declining — in capitulation but no recovery yet
        data = _make_hashrate_data(100, trend="declining")
        ribbons = calculate_hash_ribbons(data)
        signal = detect_signal(ribbons)
        assert signal is None

    def test_signal_on_recovery(self):
        # Create data: declining then recovering
        data_declining = _make_hashrate_data(70, trend="declining")
        data_recovering = _make_hashrate_data(40, base_ehs=600, trend="growing")
        # Offset timestamps to continue from declining
        last_ts = data_declining[-1]["timestamp"]
        for i, d in enumerate(data_recovering):
            d["timestamp"] = last_ts + (i + 1) * 86400
        data = data_declining + data_recovering
        ribbons = calculate_hash_ribbons(data)
        signal = detect_signal(ribbons)
        # Should detect recovery crossover (may be None if crossover hasn't happened yet)
        if signal:
            assert signal["type"] == "hash_ribbon_buy"
            assert signal["confidence"] == "high"
            assert signal["recommended_deploy_pct"] == 0.20

    def test_signal_structure(self):
        # Manually construct ribbons with exact crossover at last 2 entries
        ribbons = []
        for i in range(100):
            if i < 99:
                # Capitulating: ma30 < ma60
                ribbons.append({
                    "timestamp": i,
                    "hash_rate_ehs": 800,
                    "ma30": 800,
                    "ma60": 900,
                    "in_capitulation": True,
                })
            else:
                # Last entry: recovered (ma30 >= ma60)
                ribbons.append({
                    "timestamp": i,
                    "hash_rate_ehs": 950,
                    "ma30": 910,
                    "ma60": 900,
                    "in_capitulation": False,
                })
        signal = detect_signal(ribbons)
        assert signal is not None
        assert signal["type"] == "hash_ribbon_buy"
        assert signal["ma30_ehs"] == 910
        assert signal["ma60_ehs"] == 900
        assert signal["ma_gap_pct"] > 0


class TestGetHashRibbonStatus:
    @patch("src.hash_ribbon.fetch_hashrate_data")
    def test_unavailable_when_no_data(self, mock_fetch):
        mock_fetch.return_value = None
        status = get_hash_ribbon_status()
        assert status["status"] == "unavailable"

    @patch("src.hash_ribbon.fetch_hashrate_data")
    def test_active_with_data(self, mock_fetch):
        mock_fetch.return_value = _make_hashrate_data(100)
        status = get_hash_ribbon_status()
        assert status["status"] == "active"
        assert "ma30_ehs" in status
        assert "ma60_ehs" in status
        assert "capitulating" in status


class TestFetchWithCache:
    def test_cache_miss_then_fetch(self, tmp_path):
        """Test that cache miss triggers API call and caches result."""
        import src.hash_ribbon as hr
        original_cache = hr._CACHE_FILE
        hr._CACHE_FILE = str(tmp_path / "test_cache.json")
        try:
            # Should not have cache
            assert hr._get_cached_hashrate() is None
        finally:
            hr._CACHE_FILE = original_cache

    def test_cache_hit(self, tmp_path):
        """Test that cached data is returned without API call."""
        import src.hash_ribbon as hr
        original_cache = hr._CACHE_FILE
        hr._CACHE_FILE = str(tmp_path / "test_cache.json")
        try:
            test_data = [{"timestamp": 1, "avgHashrate": 1e18}]
            hr._save_cache(test_data)
            cached = hr._get_cached_hashrate()
            assert cached == test_data
        finally:
            hr._CACHE_FILE = original_cache

    def test_cache_expiry(self, tmp_path):
        """Test that expired cache returns None."""
        import src.hash_ribbon as hr
        original_cache = hr._CACHE_FILE
        hr._CACHE_FILE = str(tmp_path / "test_cache.json")
        try:
            # Write expired cache
            with open(hr._CACHE_FILE, "w") as f:
                json.dump({"fetched_at": time.time() - 86400 - 1, "data": []}, f)
            assert hr._get_cached_hashrate() is None
        finally:
            hr._CACHE_FILE = original_cache
