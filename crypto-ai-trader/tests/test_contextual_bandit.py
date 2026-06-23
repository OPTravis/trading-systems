"""
Tests for Contextual Thompson Sampling Bandit.

Run: python -m pytest tests/test_contextual_bandit.py -v
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.contextual_bandit import (
    ACTION_MULTIPLIERS,
    DEFAULT_SIZE,
    ContextualBandit,
    _action_index,
    _context_to_index,
    _discretize_fear_greed,
)


# ---------------------------------------------------------------------------
# Helper: mock StateDB
# ---------------------------------------------------------------------------


class MockStateDB:
    """In-memory StateDB mock for bandit tests."""

    def __init__(self):
        self._store = {}

    def kv_get(self, key, default=None):
        return self._store.get(key, default)

    def kv_set(self, key, value):
        self._store[key] = value


# ---------------------------------------------------------------------------
# Tests: helper functions
# ---------------------------------------------------------------------------


class TestDiscretizeFearGreed:
    """Test F&G index → bucket mapping."""

    def test_extreme_fear(self):
        assert _discretize_fear_greed(0) == 0
        assert _discretize_fear_greed(10) == 0
        assert _discretize_fear_greed(20) == 1  # boundary: 20/20=1

    def test_fear(self):
        assert _discretize_fear_greed(21) == 1
        assert _discretize_fear_greed(30) == 1

    def test_neutral(self):
        assert _discretize_fear_greed(41) == 2
        assert _discretize_fear_greed(50) == 2

    def test_greed(self):
        assert _discretize_fear_greed(61) == 3
        assert _discretize_fear_greed(70) == 3

    def test_extreme_greed(self):
        assert _discretize_fear_greed(81) == 4
        assert _discretize_fear_greed(100) == 4

    def test_clamping_negative(self):
        assert _discretize_fear_greed(-5) == 0

    def test_clamping_over_100(self):
        assert _discretize_fear_greed(150) == 4

    def test_float_input(self):
        assert _discretize_fear_greed(55.7) == 2


class TestContextToIndex:
    """Test context dict → integer index mapping."""

    def test_default_context(self):
        """Default context (no keys) should map to sideways/neutral/cold."""
        idx = _context_to_index({})
        assert isinstance(idx, int)
        assert idx >= 0

    def test_bull_context(self):
        idx_bull = _context_to_index(
            {"hmm_regime": "bull_trend", "fear_greed": 70, "btc_trend": "BULLISH", "portfolio_heat": "cold"}
        )
        idx_bear = _context_to_index(
            {"hmm_regime": "bear_trend", "fear_greed": 10, "btc_trend": "BEARISH", "portfolio_heat": "hot"}
        )
        assert idx_bull != idx_bear

    def test_deterministic(self):
        ctx = {"hmm_regime": "high_vol", "fear_greed": 50, "btc_trend": "NEUTRAL", "portfolio_heat": "warm"}
        assert _context_to_index(ctx) == _context_to_index(ctx)

    def test_case_insensitive(self):
        idx1 = _context_to_index({"hmm_regime": "BULL_TREND"})
        idx2 = _context_to_index({"hmm_regime": "bull_trend"})
        assert idx1 == idx2


class TestActionIndex:
    """Test multiplier → action index mapping."""

    def test_exact_match(self):
        for i, m in enumerate(ACTION_MULTIPLIERS):
            assert _action_index(m) == i

    def test_closest_match(self):
        assert _action_index(0.31) == 0  # closest to 0.3
        assert _action_index(0.95) == 3  # closest to 1.0
        assert _action_index(1.15) == 4  # closest to 1.2


# ---------------------------------------------------------------------------
# Tests: ContextualBandit class
# ---------------------------------------------------------------------------


class TestContextualBanditInit:
    """Test initialization and persistence."""

    def test_cold_start_returns_default(self):
        db = MockStateDB()
        bandit = ContextualBandit(db=db)
        size = bandit.recommend_size({"hmm_regime": "bull_trend"})
        assert size == DEFAULT_SIZE

    def test_load_from_db(self):
        db = MockStateDB()
        # Pre-populate priors
        db.kv_set("contextual_bandit:priors", {"0": [[5.0, 1.0], [1.0, 1.0], [1.0, 1.0], [1.0, 1.0], [1.0, 1.0]]})
        bandit = ContextualBandit(db=db)
        assert len(bandit._priors) == 1
        assert bandit._priors[0][0] == [5.0, 1.0]


class TestRecommendSize:
    """Test Thompson Sampling recommendation."""

    def test_returns_valid_multiplier(self):
        db = MockStateDB()
        bandit = ContextualBandit(db=db)
        # Seed some priors
        ctx = {"hmm_regime": "bull_trend", "fear_greed": 70, "btc_trend": "BULLISH", "portfolio_heat": "cold"}
        ctx_idx = _context_to_index(ctx)
        bandit._priors[ctx_idx] = [[10.0, 1.0], [1.0, 1.0], [1.0, 1.0], [1.0, 1.0], [1.0, 1.0]]
        size = bandit.recommend_size(ctx)
        assert size in ACTION_MULTIPLIERS

    def test_strong_prior_dominates(self):
        """If one action has very high alpha, it should almost always be chosen."""
        db = MockStateDB()
        bandit = ContextualBandit(db=db)
        ctx = {"hmm_regime": "bear_trend", "fear_greed": 20}
        ctx_idx = _context_to_index(ctx)
        # Action 0 (0.3x) has overwhelming prior
        bandit._priors[ctx_idx] = [[1000.0, 1.0], [1.0, 1.0], [1.0, 1.0], [1.0, 1.0], [1.0, 1.0]]
        # Should almost always pick 0.3
        choices = [bandit.recommend_size(ctx) for _ in range(100)]
        assert choices.count(0.3) > 90


class TestUpdateFromOutcome:
    """Test learning from trade outcomes."""

    def test_positive_outcome_increases_alpha(self):
        db = MockStateDB()
        bandit = ContextualBandit(db=db)
        ctx = {"hmm_regime": "bull_trend", "fear_greed": 60}
        bandit.update_from_outcome(ctx, action_taken=1.0, pnl_pct=5.0)
        ctx_idx = _context_to_index(ctx)
        priors = bandit._priors[ctx_idx]
        act_idx = _action_index(1.0)
        assert priors[act_idx][0] > 1.0  # alpha increased
        assert priors[act_idx][1] == 1.0  # beta unchanged

    def test_negative_outcome_increases_beta(self):
        db = MockStateDB()
        bandit = ContextualBandit(db=db)
        ctx = {"hmm_regime": "bear_trend", "fear_greed": 30}
        bandit.update_from_outcome(ctx, action_taken=0.5, pnl_pct=-3.0)
        ctx_idx = _context_to_index(ctx)
        priors = bandit._priors[ctx_idx]
        act_idx = _action_index(0.5)
        assert priors[act_idx][0] == 1.0  # alpha unchanged
        assert priors[act_idx][1] > 1.0  # beta increased

    def test_persistence_after_update(self):
        db = MockStateDB()
        bandit = ContextualBandit(db=db)
        ctx = {"hmm_regime": "range_bound"}
        bandit.update_from_outcome(ctx, action_taken=0.8, pnl_pct=2.0)
        # Create new bandit from same DB
        bandit2 = ContextualBandit(db=db)
        ctx_idx = _context_to_index(ctx)
        assert ctx_idx in bandit2._priors

    def test_increment_capped(self):
        """Increment should be capped at 3.0 even for large PnL."""
        db = MockStateDB()
        bandit = ContextualBandit(db=db)
        ctx = {"hmm_regime": "bull_trend"}
        bandit.update_from_outcome(ctx, action_taken=1.2, pnl_pct=100.0)
        ctx_idx = _context_to_index(ctx)
        act_idx = _action_index(1.2)
        # increment = min(3.0, 100/2) = 3.0
        assert bandit._priors[ctx_idx][act_idx][0] == 1.0 + 3.0


class TestGetStats:
    """Test stats reporting."""

    def test_empty_stats(self):
        db = MockStateDB()
        bandit = ContextualBandit(db=db)
        stats = bandit.get_stats()
        assert stats["total_contexts"] == 0
        assert stats["total_updates"] == 0

    def test_stats_after_updates(self):
        db = MockStateDB()
        bandit = ContextualBandit(db=db)
        ctx = {"hmm_regime": "bull_trend", "fear_greed": 60}
        bandit.update_from_outcome(ctx, 1.0, 5.0)
        bandit.update_from_outcome(ctx, 1.0, 3.0)
        stats = bandit.get_stats()
        assert stats["total_contexts"] == 1
        assert stats["total_updates"] > 0


class TestBetaSample:
    """Test internal Beta sampling."""

    def test_high_alpha_skews_high(self):
        db = MockStateDB()
        bandit = ContextualBandit(db=db)
        samples = [bandit._beta_sample(100.0, 1.0) for _ in range(1000)]
        assert np.mean(samples) > 0.9

    def test_high_beta_skews_low(self):
        db = MockStateDB()
        bandit = ContextualBandit(db=db)
        samples = [bandit._beta_sample(1.0, 100.0) for _ in range(1000)]
        assert np.mean(samples) < 0.1

    def test_equal_priors_centered(self):
        db = MockStateDB()
        bandit = ContextualBandit(db=db)
        samples = [bandit._beta_sample(1.0, 1.0) for _ in range(1000)]
        assert 0.3 < np.mean(samples) < 0.7
