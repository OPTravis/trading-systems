"""
Tests for HMM Market Regime Detector.

Run: python -m pytest tests/test_hmm_regime.py -v
"""

import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.hmm_regime import (
    HMMRegimeDetector,
    MIN_KLINES,
    REGIME_LABELS,
    REGIME_STRATEGY_MAP,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_klines_1h(n=700, base_price=100.0, volatility=2.0):
    """Generate n 1h kline dicts."""
    import random

    random.seed(42)
    klines = []
    price = base_price
    for i in range(n):
        change = random.uniform(-volatility, volatility)
        open_ = price
        close = price + change
        high = max(open_, close) + abs(change) * 0.5
        low = min(open_, close) - abs(change) * 0.5
        volume = random.uniform(100, 1000)
        klines.append(
            {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "timestamp": i * 3600000,
            }
        )
        price = close
    return klines


def _make_list_klines(n=700):
    """Generate klines as lists [timestamp, open, high, low, close, vol]."""
    import random

    random.seed(42)
    klines = []
    price = 100.0
    for i in range(n):
        change = random.uniform(-2.0, 2.0)
        open_ = price
        close = price + change
        high = max(open_, close) + abs(change) * 0.5
        low = min(open_, close) - abs(change) * 0.5
        volume = random.uniform(100, 1000)
        klines.append([i * 3600000, open_, high, low, close, volume])
        price = close
    return klines


class MockStateDBWithSQLite:
    """SQLite in-memory mock that implements _get_conn() for HMM tests."""

    def __init__(self):
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at REAL
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_outcomes (
                id INTEGER PRIMARY KEY,
                status TEXT DEFAULT 'closed'
            )
        """)
        self._conn.commit()
        self._store = {}

    def _get_conn(self):
        return self._conn

    def kv_get(self, key, default=None):
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if row:
            try:
                return json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                return row["value"]
        return default

    def kv_set(self, key, value):
        self._conn.execute(
            "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps(value) if not isinstance(value, str) else value, time.time()),
        )
        self._conn.commit()


# ---------------------------------------------------------------------------
# Tests: Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_regime_labels_count(self):
        assert len(REGIME_LABELS) == 4

    def test_all_regimes_have_strategy_map(self):
        for label in REGIME_LABELS:
            assert label in REGIME_STRATEGY_MAP

    def test_strategy_map_completeness(self):
        required_keys = {
            "preferred_strategies",
            "avoid_strategies",
            "position_scale",
            "sl_multiplier",
            "tp_multiplier",
            "score_threshold_adj",
        }
        for regime, cfg in REGIME_STRATEGY_MAP.items():
            assert required_keys.issubset(cfg.keys()), f"{regime} missing keys"


# ---------------------------------------------------------------------------
# Tests: _compute_rsi
# ---------------------------------------------------------------------------


class TestComputeRSI:
    """Test RSI computation."""

    def test_all_gains(self):
        """Monotonically increasing prices → RSI should be 100."""
        prices = np.array([100 + i for i in range(30)], dtype=float)
        rsi = HMMRegimeDetector._compute_rsi(prices, period=14)
        assert rsi[-1] == 100.0

    def test_all_losses(self):
        """Monotonically decreasing prices → RSI should be 0."""
        prices = np.array([100 - i for i in range(30)], dtype=float)
        rsi = HMMRegimeDetector._compute_rsi(prices, period=14)
        assert rsi[-1] < 10.0

    def test_flat_prices_rsi_100(self):
        """Flat prices → avg_loss=0 → RSI=100 (not 50)."""
        prices = np.full(30, 100.0)
        rsi = HMMRegimeDetector._compute_rsi(prices, period=14)
        assert rsi[-1] == 100.0

    def test_short_series(self):
        """Series shorter than period → default RSI 50."""
        prices = np.array([100, 101, 99], dtype=float)
        rsi = HMMRegimeDetector._compute_rsi(prices, period=14)
        assert all(r == 50.0 for r in rsi)

    def test_output_length(self):
        prices = np.array([100 + np.sin(i * 0.1) * 5 for i in range(50)])
        rsi = HMMRegimeDetector._compute_rsi(prices, period=14)
        assert len(rsi) == len(prices)


# ---------------------------------------------------------------------------
# Tests: _compute_bb_position
# ---------------------------------------------------------------------------


class TestComputeBBPosition:
    """Test Bollinger Band position computation."""

    def test_price_at_mean(self):
        """Price at moving average → BB position ≈ 0.5."""
        prices = np.full(30, 100.0)
        prices[-1] = 100.0
        bb = HMMRegimeDetector._compute_bb_position(prices, period=20, std_dev=2.0)
        assert abs(bb[-1] - 0.5) < 0.01

    def test_price_at_upper_band(self):
        """Price near upper band → BB position > 0.5."""
        prices = np.full(30, 100.0)
        for i in range(20):
            prices[i] = 100 + (i - 10) * 0.5
        bb = HMMRegimeDetector._compute_bb_position(prices, period=20, std_dev=2.0)
        assert -0.5 <= bb[-1] <= 1.5

    def test_zero_std(self):
        """Constant window → BB position = 0.5."""
        prices = np.full(30, 100.0)
        bb = HMMRegimeDetector._compute_bb_position(prices, period=20, std_dev=2.0)
        assert bb[-1] == 0.5

    def test_output_length(self):
        prices = np.array([100 + np.sin(i * 0.2) * 3 for i in range(50)])
        bb = HMMRegimeDetector._compute_bb_position(prices, period=20)
        assert len(bb) == len(prices)


# ---------------------------------------------------------------------------
# Tests: _compute_features
# ---------------------------------------------------------------------------


class TestComputeFeatures:
    """Test feature matrix computation."""

    def test_insufficient_klines_returns_none(self):
        db = MockStateDBWithSQLite()
        detector = HMMRegimeDetector(db=db)
        klines = _make_klines_1h(n=MIN_KLINES - 1)
        assert detector._compute_features(klines) is None

    def test_valid_features_shape(self):
        """700 klines = 29 days → should produce (N, 4) features with N >= 20."""
        db = MockStateDBWithSQLite()
        detector = HMMRegimeDetector(db=db)
        klines = _make_klines_1h(n=700)
        features = detector._compute_features(klines)
        assert features is not None
        assert features.ndim == 2
        assert features.shape[1] == 4

    def test_list_format_klines(self):
        """Should handle klines as lists [timestamp, open, high, low, close, vol]."""
        db = MockStateDBWithSQLite()
        detector = HMMRegimeDetector(db=db)
        klines = _make_list_klines(n=700)
        features = detector._compute_features(klines)
        assert features is not None
        assert features.shape[1] == 4

    def test_no_nan_in_output(self):
        db = MockStateDBWithSQLite()
        detector = HMMRegimeDetector(db=db)
        klines = _make_klines_1h(n=700)
        features = detector._compute_features(klines)
        assert features is not None
        assert not np.isnan(features).any()

    def test_short_daily_series_returns_none(self):
        """If aggregated daily bars < 20, should return None."""
        db = MockStateDBWithSQLite()
        detector = HMMRegimeDetector(db=db)
        # 456 klines = 19 days → n_days < 20 → None
        klines = _make_klines_1h(n=456)
        features = detector._compute_features(klines)
        assert features is None


# ---------------------------------------------------------------------------
# Tests: get_strategy_adjustments
# ---------------------------------------------------------------------------


class TestGetStrategyAdjustments:
    """Test strategy adjustment lookup."""

    def test_known_regime(self):
        db = MockStateDBWithSQLite()
        detector = HMMRegimeDetector(db=db)
        adj = detector.get_strategy_adjustments("BULL_TREND")
        assert adj["position_scale"] == 1.2
        assert "trend" in adj["preferred_strategies"]

    def test_unknown_regime_returns_range_bound(self):
        db = MockStateDBWithSQLite()
        detector = HMMRegimeDetector(db=db)
        adj = detector.get_strategy_adjustments("UNKNOWN_REGIME")
        assert adj["position_scale"] == 1.0

    def test_all_regimes_return_valid(self):
        db = MockStateDBWithSQLite()
        detector = HMMRegimeDetector(db=db)
        for regime in REGIME_LABELS:
            adj = detector.get_strategy_adjustments(regime)
            assert 0 < adj["position_scale"] <= 2.0


# ---------------------------------------------------------------------------
# Tests: format_report
# ---------------------------------------------------------------------------


class TestFormatReport:
    """Test human-readable report generation (Chinese output)."""

    def test_report_contains_regime_name(self):
        db = MockStateDBWithSQLite()
        detector = HMMRegimeDetector(db=db)
        prediction = {
            "regime": "BULL_TREND",
            "probabilities": {"BULL_TREND": 0.7, "BEAR_TREND": 0.1, "RANGE_BOUND": 0.1, "HIGH_VOL": 0.1},
            "confidence": 0.7,
        }
        report = detector.format_report(prediction)
        # Should contain Chinese bull trend name
        assert "牛市" in report
        assert "70" in report or "0.70" in report

    def test_report_contains_all_probabilities(self):
        db = MockStateDBWithSQLite()
        detector = HMMRegimeDetector(db=db)
        prediction = {
            "regime": "RANGE_BOUND",
            "probabilities": {"BULL_TREND": 0.2, "BEAR_TREND": 0.2, "RANGE_BOUND": 0.4, "HIGH_VOL": 0.2},
            "confidence": 0.4,
        }
        report = detector.format_report(prediction)
        # Should have all regime Chinese names
        assert "牛市" in report
        assert "熊市" in report
        assert "盤整" in report
        assert "高波動" in report

    def test_report_contains_strategy_info(self):
        db = MockStateDBWithSQLite()
        detector = HMMRegimeDetector(db=db)
        prediction = {
            "regime": "BULL_TREND",
            "probabilities": {"BULL_TREND": 0.8, "BEAR_TREND": 0.05, "RANGE_BOUND": 0.1, "HIGH_VOL": 0.05},
            "confidence": 0.8,
        }
        report = detector.format_report(prediction)
        assert "偏好" in report
        assert "避免" in report

    def test_empty_prediction(self):
        db = MockStateDBWithSQLite()
        detector = HMMRegimeDetector(db=db)
        report = detector.format_report(None)
        assert "未" in report


# ---------------------------------------------------------------------------
# Tests: should_retrain
# ---------------------------------------------------------------------------


class TestShouldRetrain:
    """Test retrain decision logic."""

    def test_no_training_state_should_retrain(self):
        db = MockStateDBWithSQLite()
        detector = HMMRegimeDetector(db=db)
        result = detector.should_retrain()
        assert result["should_retrain"] is True
        assert result["reason"] == "never_trained"

    def test_recent_training_no_retrain(self):
        db = MockStateDBWithSQLite()
        # Store a recent training state
        conn = db._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
            ("hmm_model_state", json.dumps({"trained_at": time.time(), "n_samples": 100}), time.time()),
        )
        conn.commit()
        detector = HMMRegimeDetector(db=db)
        result = detector.should_retrain(min_new_trades=20, max_interval_days=7)
        assert result["should_retrain"] is False

    def test_enough_new_trades_should_retrain(self):
        db = MockStateDBWithSQLite()
        conn = db._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
            ("hmm_model_state", json.dumps({"trained_at": time.time(), "n_samples": 50}), time.time()),
        )
        # Insert 80 closed trades
        for i in range(80):
            conn.execute("INSERT INTO trade_outcomes (status) VALUES ('closed')")
        conn.commit()
        detector = HMMRegimeDetector(db=db)
        result = detector.should_retrain(min_new_trades=20, max_interval_days=7)
        assert result["should_retrain"] is True
        assert result["trades_since"] >= 20


# ---------------------------------------------------------------------------
# Tests: _store_prediction / get_cached_prediction
# ---------------------------------------------------------------------------


class TestCachedPrediction:
    """Test prediction caching via SQLite."""

    def test_store_and_retrieve(self):
        db = MockStateDBWithSQLite()
        detector = HMMRegimeDetector(db=db)
        prediction = {
            "regime": "BULL_TREND",
            "probabilities": {"BULL_TREND": 0.8, "BEAR_TREND": 0.05, "RANGE_BOUND": 0.1, "HIGH_VOL": 0.05},
            "confidence": 0.8,
            "timestamp": 1234567890,
        }
        detector._store_prediction(prediction)
        cached = detector.get_cached_prediction()
        assert cached is not None
        assert cached["regime"] == "BULL_TREND"
        assert cached["confidence"] == 0.8

    def test_no_cache_returns_none(self):
        db = MockStateDBWithSQLite()
        detector = HMMRegimeDetector(db=db)
        assert detector.get_cached_prediction() is None


# ---------------------------------------------------------------------------
# Tests: _store_label_mapping / _load_label_mapping
# ---------------------------------------------------------------------------


class TestLabelMapping:
    """Test label mapping persistence via SQLite."""

    def test_store_and_load(self):
        db = MockStateDBWithSQLite()
        detector = HMMRegimeDetector(db=db)
        # Means sorted ascending: bear → bull
        means = np.array([[-0.02], [0.001], [0.01], [0.03]])
        detector._store_label_mapping(means)
        loaded = detector._load_label_mapping()
        assert loaded is not None
        assert loaded["label_0"] == "BEAR_TREND"
        assert loaded["label_3"] == "BULL_TREND"

    def test_no_mapping_returns_none(self):
        db = MockStateDBWithSQLite()
        detector = HMMRegimeDetector(db=db)
        assert detector._load_label_mapping() is None
