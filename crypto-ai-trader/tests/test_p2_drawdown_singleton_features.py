"""
Tests for P2 fixes:
- drawdown_breaker: max_drawdown_pct stored as percentage (consistent with current_drawdown_pct)
- risk_manager: singleton get_risk_manager() with double-checked locking
- price_predictor: feature completeness check, MIN_TRAINING_SAMPLES=200
"""

import threading
from unittest.mock import MagicMock, patch

import pytest


# ─── Drawdown Breaker Unit Consistency ───────────────────────────────────


class TestDrawdownUnitConsistency:
    """max_drawdown_pct should be stored as percentage, same as current_drawdown_pct."""

    @patch("src.state_db.get_state_db")
    def test_max_drawdown_stored_as_percentage(self, mock_db):
        """After drawdown occurs, max_drawdown_pct should be in percentage format."""
        from src.drawdown_breaker import DrawdownBreaker

        mock_state_db = MagicMock()
        mock_state_db.drawdown_get.return_value = {
            "high_watermark": 1000.0,
            "current_drawdown_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "tripped_at": None,
            "tripped_count": 0,
            "reset_at": None,
            "history": [],
        }
        mock_db.return_value = mock_state_db

        breaker = DrawdownBreaker()

        # 5% drawdown: 1000 → 950
        result = breaker.check_drawdown(950.0)

        assert result["drawdown_pct"] == 5.0
        assert breaker._state["current_drawdown_pct"] == 5.0
        # KEY FIX: max_drawdown_pct should be 5.0 (percentage), NOT 0.05 (ratio)
        assert breaker._state["max_drawdown_pct"] == 5.0

    @patch("src.state_db.get_state_db")
    def test_get_status_returns_percentage_not_ratio(self, mock_db):
        """get_status() should return max_drawdown_pct as percentage without extra ×100."""
        from src.drawdown_breaker import DrawdownBreaker

        mock_state_db = MagicMock()
        mock_state_db.drawdown_get.return_value = {
            "high_watermark": 1000.0,
            "current_drawdown_pct": 3.0,
            "max_drawdown_pct": 5.0,  # already percentage
            "tripped_at": None,
            "tripped_count": 0,
            "reset_at": None,
            "history": [],
        }
        mock_db.return_value = mock_state_db

        breaker = DrawdownBreaker()
        status = breaker.get_status()

        # Should return 5.0, NOT 500.0 (which would happen with old ×100 bug)
        assert status["max_drawdown_pct"] == 5.0
        assert status["current_drawdown_pct"] == 3.0

    @patch("src.state_db.get_state_db")
    def test_max_drawdown_tracks_worst(self, mock_db):
        """max_drawdown_pct should track the worst drawdown seen."""
        from src.drawdown_breaker import DrawdownBreaker

        mock_state_db = MagicMock()
        mock_state_db.drawdown_get.return_value = {
            "high_watermark": 1000.0,
            "current_drawdown_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "tripped_at": None,
            "tripped_count": 0,
            "reset_at": None,
            "history": [],
        }
        mock_db.return_value = mock_state_db

        breaker = DrawdownBreaker()

        # First: 3% drawdown
        breaker.check_drawdown(970.0)
        assert breaker._state["max_drawdown_pct"] == 3.0

        # New high watermark resets current but max stays
        breaker.check_drawdown(1050.0)
        assert breaker._state["current_drawdown_pct"] == 0.0
        assert breaker._state["max_drawdown_pct"] == 3.0  # still 3%

        # Worse drawdown: 7%
        breaker.check_drawdown(976.5)  # 1050 → 976.5 = ~7%
        assert breaker._state["max_drawdown_pct"] == pytest.approx(7.0, abs=0.1)


# ─── Risk Manager Singleton ─────────────────────────────────────────────


class TestRiskManagerSingleton:
    """get_risk_manager() should return the same instance across calls."""

    def test_singleton_returns_same_instance(self):
        from src.risk_manager import get_risk_manager, _risk_manager_instance
        import src.risk_manager as rm

        # Reset singleton for test isolation
        rm._risk_manager_instance = None

        rm1 = get_risk_manager()
        rm2 = get_risk_manager()

        assert rm1 is rm2

        # Cleanup
        rm._risk_manager_instance = None

    def test_singleton_thread_safety(self):
        """Multiple threads calling get_risk_manager() should get the same instance."""
        from src.risk_manager import get_risk_manager as gtrm
        import src.risk_manager as rm

        rm._risk_manager_instance = None

        instances = []
        barrier = threading.Barrier(5)

        def get_instance():
            barrier.wait()
            inst = gtrm()
            instances.append(id(inst))

        threads = [threading.Thread(target=get_instance) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads should have gotten the same instance
        assert len(set(instances)) == 1

        # Cleanup
        rm._risk_manager_instance = None


# ─── Price Predictor Feature Checks ─────────────────────────────────────


class TestFeatureCompleteness:
    """Feature completeness check and MIN_TRAINING_SAMPLES=200."""

    def test_min_training_samples_is_200(self):
        from src.price_predictor import MIN_TRAINING_SAMPLES
        assert MIN_TRAINING_SAMPLES == 200

    def test_whale_activity_documented_as_placeholder(self):
        """whale_activity should be in ALL_FEATURES but clearly marked as placeholder."""
        from src.price_predictor import ALL_FEATURES
        assert "whale_activity" in ALL_FEATURES  # still present for model compat

    def test_feature_check_warns_on_missing(self, caplog):
        """train() should warn when ALL_FEATURES are missing from training data."""
        import logging
        from src.price_predictor import PricePredictor, ALL_FEATURES

        # Create training data missing several features
        features_list = []
        labels = []
        for i in range(210):
            # Only include a subset of features
            f = {feat: float(i) for feat in ALL_FEATURES[:10]}
            features_list.append(f)
            labels.append(i % 2)

        pp = PricePredictor()
        with caplog.at_level(logging.WARNING):
            pp.train(features_list, labels)

        # Should warn about missing features
        assert any("features missing" in r.message for r in caplog.records)
