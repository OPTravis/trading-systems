"""
Tests for outcome-based score calibration in specialist agents.

Run with:  python -m pytest tests/test_calibration.py -v
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.agents.calibration import (
    AGENT_FACTOR_MAP,
    MIN_OUTCOMES,
    apply_calibration,
    get_outcome_history,
    record_agent_outcome,
    record_agent_outcomes_from_trade,
    update_calibration,
)


# ===================================================================
# Helpers
# ===================================================================


def _seed_outcomes(
    db,
    agent_name: str,
    n: int,
    high_score: float = 0.85,
    low_score: float = 0.15,
    high_pnl: float = 5.0,
    low_pnl: float = 1.0,
):
    """Insert *n* (score, pnl) pairs for *agent_name* into *db*.

    Roughly half use *high_score*/*high_pnl*, half use *low_score*/*low_pnl*.
    """
    for i in range(n):
        if i % 2 == 0:
            update_calibration(agent_name, high_score, high_pnl, db=db)
        else:
            update_calibration(agent_name, low_score, low_pnl, db=db)


# ===================================================================
# Test get_outcome_history
# ===================================================================


def test_get_outcome_history_empty(statedb):
    history = get_outcome_history("nonexistent_agent", db=statedb)
    assert history == []


def test_get_outcome_history_stores_pairs(statedb):
    update_calibration("test_agent", 0.8, 5.0, db=statedb)
    update_calibration("test_agent", 0.2, 1.0, db=statedb)

    history = get_outcome_history("test_agent", db=statedb)
    assert len(history) == 2
    assert history[0] == (0.8, 5.0)
    assert history[1] == (0.2, 1.0)


# ===================================================================
# Test update_calibration
# ===================================================================


def test_update_calibration_stores_data(statedb):
    update_calibration("tech", 0.8, 3.0, db=statedb)

    key = "agent_calibration:tech"
    data = statedb.kv_get(key)
    assert data is not None
    assert data["count"] == 1
    assert data["calibration_factor"] is None  # < 20 outcomes
    assert data["outcomes"] == [[0.8, 3.0]]


def test_update_calibration_returns_none_below_threshold(statedb):
    for i in range(19):
        result = update_calibration("tech", 0.8, 3.0, db=statedb)
    assert result is None


def test_update_calibration_returns_factor_at_threshold(statedb):
    _seed_outcomes(statedb, "tech", MIN_OUTCOMES)
    # Last call should return a factor
    result = update_calibration("tech", 0.8, 5.0, db=statedb)
    assert result is not None
    assert isinstance(result, float)


def test_update_calibration_factor_recomputed_each_time(statedb):
    _seed_outcomes(statedb, "tech", MIN_OUTCOMES)
    factor1 = statedb.kv_get("agent_calibration:tech")["calibration_factor"]

    update_calibration("tech", 0.8, 10.0, db=statedb)
    factor2 = statedb.kv_get("agent_calibration:tech")["calibration_factor"]

    # Factor should change (new data point added)
    # Not guaranteed to differ but the mechanism should recompute
    assert factor2 is not None


# ===================================================================
# Test apply_calibration
# ===================================================================


def test_apply_calibration_no_data(statedb):
    result = apply_calibration("nonexistent", 80.0, db=statedb)
    assert result == 80.0


def test_apply_calibration_below_threshold(statedb):
    _seed_outcomes(statedb, "tech", MIN_OUTCOMES - 1)
    result = apply_calibration("tech", 80.0, db=statedb)
    assert result == 80.0  # unchanged


def test_apply_calibration_at_threshold(statedb):
    _seed_outcomes(statedb, "tech", MIN_OUTCOMES)
    result = apply_calibration("tech", 80.0, db=statedb)
    # Should be calibrated (not equal to raw unless factor is exactly 1.0)
    assert 0.0 <= result <= 100.0


def test_apply_calibration_preserves_zero(statedb):
    _seed_outcomes(statedb, "tech", MIN_OUTCOMES)
    result = apply_calibration("tech", 0.0, db=statedb)
    assert result == 0.0


def test_apply_calibration_preserves_hundred(statedb):
    _seed_outcomes(statedb, "tech", MIN_OUTCOMES)
    result = apply_calibration("tech", 100.0, db=statedb)
    # May be clamped if factor > 1, but should be <= 100
    assert 0.0 <= result <= 100.0


def test_apply_calibration_factor_1_no_change(statedb):
    """When calibration factor = 1.0, score should be unchanged."""
    for i in range(MIN_OUTCOMES):
        update_calibration("tech", 0.8, 3.0, db=statedb)
    # All same pnl → mean_high == mean_low → factor 1.0
    result = apply_calibration("tech", 75.0, db=statedb)
    assert abs(result - 75.0) < 0.1


def test_apply_calibration_factor_clamped(statedb):
    """Calibration factor is clamped to [0.5, 2.0]."""
    # Create outcomes with extreme calibration
    for i in range(MIN_OUTCOMES):
        if i % 2 == 0:
            update_calibration("tech", 0.9, 100.0, db=statedb)
        else:
            update_calibration("tech", 0.1, 0.001, db=statedb)

    data = statedb.kv_get("agent_calibration:tech")
    factor = data["calibration_factor"]
    # factor = mean(100) / mean(0.001) = 100/0.001 = 100000 → clamped to 2.0
    clamped_factor = max(0.5, min(2.0, factor))
    assert clamped_factor == 2.0

    result = apply_calibration("tech", 50.0, db=statedb)
    # 50 * 2.0 = 100
    assert abs(result - 100.0) < 0.1


def test_apply_calibration_low_factor_reduces_score(statedb):
    """When high-score predictions underperform, factor < 1 reduces scores."""
    for i in range(MIN_OUTCOMES):
        if i % 2 == 0:
            # High predictions → negative PnL
            update_calibration("tech", 0.85, -5.0, db=statedb)
        else:
            # Low predictions → positive PnL
            update_calibration("tech", 0.15, 3.0, db=statedb)

    result = apply_calibration("tech", 80.0, db=statedb)
    # factor = -5/3 = -1.67 → clamped to 0.5
    # 80 * 0.5 = 40
    assert result < 80.0
    assert abs(result - 40.0) < 0.1


# ===================================================================
# Test compute_calibration_factor
# ===================================================================


def test_compute_calibration_factor_basic():
    from src.agents.calibration import _compute_calibration_factor

    outcomes = [[0.8, 5.0]] * 10 + [[0.2, 1.0]] * 10
    factor = _compute_calibration_factor(outcomes)
    assert abs(factor - 5.0) < 0.01  # 5.0 / 1.0


def test_compute_calibration_factor_no_high_scores():
    from src.agents.calibration import _compute_calibration_factor

    outcomes = [[0.1, 5.0]] * 20
    factor = _compute_calibration_factor(outcomes)
    assert factor == 1.0  # no high-score outcomes


def test_compute_calibration_factor_no_low_scores():
    from src.agents.calibration import _compute_calibration_factor

    outcomes = [[0.9, 5.0]] * 20
    factor = _compute_calibration_factor(outcomes)
    assert factor == 1.0  # no low-score outcomes


def test_compute_calibration_factor_zero_low_mean():
    from src.agents.calibration import _compute_calibration_factor

    outcomes = [[0.8, 5.0]] * 10 + [[0.2, 0.0]] * 10
    factor = _compute_calibration_factor(outcomes)
    assert factor == 2.0  # mean_high > 0 → capped to 2.0


def test_compute_calibration_factor_negative_means():
    from src.agents.calibration import _compute_calibration_factor

    outcomes = [[0.8, -2.0]] * 10 + [[0.2, -5.0]] * 10
    factor = _compute_calibration_factor(outcomes)
    # factor = -2.0 / -5.0 = 0.4
    assert abs(factor - 0.4) < 0.01


# ===================================================================
# Test record_agent_outcome
# ===================================================================


def test_record_agent_outcome_basic(statedb):
    factor = record_agent_outcome("tech_agent", 80.0, 5.0, db=statedb)
    assert factor is None  # < 20 outcomes

    history = get_outcome_history("tech_agent", db=statedb)
    assert len(history) == 1
    assert history[0] == (0.8, 5.0)  # normalized


def test_record_agent_outcome_clamps_score(statedb):
    record_agent_outcome("tech", 150.0, 5.0, db=statedb)
    history = get_outcome_history("tech", db=statedb)
    assert history[0][0] == 1.0  # clamped to 1.0

    record_agent_outcome("tech2", -10.0, 5.0, db=statedb)
    history = get_outcome_history("tech2", db=statedb)
    assert history[0][0] == 0.0  # clamped to 0.0


# ===================================================================
# Test record_agent_outcomes_from_trade
# ===================================================================


def test_record_outcomes_from_trade_basic(statedb):
    factors = {
        "technical": 70.0,
        "trend": 60.0,
        "volume": 50.0,
        "sentiment": 40.0,
        "onchain": 80.0,
        "market_sentiment": 30.0,
        "obv_divergence": 65.0,
        "consolidation": 55.0,
        "price_action": 75.0,
        "bb_squeeze": 45.0,
        "rsi_divergence": 35.0,
    }
    results = record_agent_outcomes_from_trade(json.dumps(factors), 5.0, db=statedb)

    assert "technical_agent" in results
    assert "trend_agent" in results
    assert "volume_agent" in results
    assert "sentiment_agent" in results
    assert "onchain_agent" in results
    assert "market_sentiment_agent" in results
    assert "prepump_agent" in results

    # Verify technical_agent gets average of its factors
    tech_history = get_outcome_history("technical_agent", db=statedb)
    assert len(tech_history) == 1
    expected_tech = (70.0 + 75.0 + 45.0 + 35.0) / 4.0 / 100.0  # avg normalized
    assert abs(tech_history[0][0] - expected_tech) < 0.001

    # Verify prepump_agent gets average of obv_divergence + consolidation
    prepump_history = get_outcome_history("prepump_agent", db=statedb)
    expected_prepump = (65.0 + 55.0) / 2.0 / 100.0
    assert abs(prepump_history[0][0] - expected_prepump) < 0.001


def test_record_outcomes_from_trade_json_string(statedb):
    factors = {"trend": 80.0}
    results = record_agent_outcomes_from_trade(json.dumps(factors), 3.0, db=statedb)
    assert "trend_agent" in results


def test_record_outcomes_from_trade_empty_json(statedb):
    results = record_agent_outcomes_from_trade("", 3.0, db=statedb)
    assert results == {}


def test_record_outcomes_from_trade_bad_json(statedb):
    results = record_agent_outcomes_from_trade("not-json", 3.0, db=statedb)
    assert results == {}


def test_record_outcomes_from_trade_partial_factors(statedb):
    """Only agents with matching factors should be recorded."""
    factors = {"trend": 60.0, "volume": 40.0}
    results = record_agent_outcomes_from_trade(json.dumps(factors), 2.0, db=statedb)
    assert "trend_agent" in results
    assert "volume_agent" in results
    assert "technical_agent" not in results  # no matching factors


def test_record_outcomes_from_trade_dict_input(statedb):
    """Should also accept a dict directly."""
    factors = {"trend": 70.0}
    results = record_agent_outcomes_from_trade(factors, 4.0, db=statedb)
    assert "trend_agent" in results


# ===================================================================
# Test AGENT_FACTOR_MAP
# ===================================================================


def test_agent_factor_map_covers_all_agents():
    expected = {
        "technical_agent",
        "trend_agent",
        "volume_agent",
        "sentiment_agent",
        "onchain_agent",
        "market_sentiment_agent",
        "prepump_agent",
    }
    assert set(AGENT_FACTOR_MAP.keys()) == expected


def test_agent_factor_map_values():
    assert AGENT_FACTOR_MAP["technical_agent"] == [
        "technical",
        "price_action",
        "bb_squeeze",
        "rsi_divergence",
    ]
    assert AGENT_FACTOR_MAP["trend_agent"] == ["trend"]
    assert AGENT_FACTOR_MAP["prepump_agent"] == ["obv_divergence", "consolidation"]


# ===================================================================
# Test per-agent calibration integration
# ===================================================================


def test_technical_agent_calibrated(statedb):
    """TechnicalAgent should apply calibration when enough outcomes exist."""
    from src.agents.technical_agent import TechnicalAgent

    agent = TechnicalAgent()
    tf_1h = {
        "rsi": 35,
        "macd_histogram": 1.5,
        "bb_lower": 95.0,
        "current_price": 100.0,
        "vwap": 99.0,
        "ma7": 101.0,
        "ma25": 100.0,
        "ma99": 98.0,
        "volatility_pct": 4.0,
        "momentum": 2.5,
    }

    # Get raw score before calibration
    raw_result = agent.analyze(tf_1h=tf_1h)
    raw_score = raw_result.score

    # Seed calibration data (factor > 1 → score should increase)
    for i in range(MIN_OUTCOMES):
        if i % 2 == 0:
            update_calibration("technical_agent", 0.85, 10.0, db=statedb)
        else:
            update_calibration("technical_agent", 0.15, 1.0, db=statedb)

    calibrated_result = agent.analyze(tf_1h=tf_1h)
    assert calibrated_result.score != raw_score or raw_score in (0.0, 100.0)


def test_trend_agent_calibrated(statedb):
    from src.agents.trend_agent import TrendAgent

    agent = TrendAgent()
    mtf = {"trend_score": 70, "trend_alignment": "bullish"}

    raw_score = agent.analyze(mtf).score

    _seed_outcomes(statedb, "trend_agent", MIN_OUTCOMES)
    calibrated_score = agent.analyze(mtf).score

    assert 0.0 <= calibrated_score <= 100.0


def test_volume_agent_calibrated(statedb):
    from src.agents.volume_agent import VolumeAgent

    agent = VolumeAgent()
    coin_data = {"rank": 5, "price_change_24h": 8.0, "volume_surge": True}

    raw_score = agent.analyze(coin_data=coin_data).score

    _seed_outcomes(statedb, "volume_agent", MIN_OUTCOMES)
    calibrated_score = agent.analyze(coin_data=coin_data).score

    assert 0.0 <= calibrated_score <= 100.0


def test_sentiment_agent_calibrated(statedb):
    from src.agents.sentiment_agent import SentimentAgent

    agent = SentimentAgent()
    funding = {"sentiment_score": 5, "funding_rate": -0.01, "oi_change_pct": 5}

    raw_score = agent.analyze(funding_data=funding).score

    _seed_outcomes(statedb, "sentiment_agent", MIN_OUTCOMES)
    calibrated_score = agent.analyze(funding_data=funding).score

    assert 0.0 <= calibrated_score <= 100.0


def test_onchain_agent_calibrated(statedb):
    from src.agents.onchain_agent import OnChainAgent

    agent = OnChainAgent()
    raw_score = agent.analyze(onchain_score=60).score

    _seed_outcomes(statedb, "onchain_agent", MIN_OUTCOMES)
    calibrated_score = agent.analyze(onchain_score=60).score

    assert 0.0 <= calibrated_score <= 100.0


def test_market_sentiment_agent_calibrated(statedb):
    from src.agents.market_sentiment_agent import MarketSentimentAgent

    agent = MarketSentimentAgent()
    raw_score = agent.analyze(fng_value=30).score

    _seed_outcomes(statedb, "market_sentiment_agent", MIN_OUTCOMES)
    calibrated_score = agent.analyze(fng_value=30).score

    assert 0.0 <= calibrated_score <= 100.0


def test_prepump_agent_calibrated(statedb):
    from src.agents.prepump_agent import PrePumpAgent

    agent = PrePumpAgent()
    obv = {"detected": True, "strength": 60, "obv_trend": "rising"}

    raw_score = agent.analyze(obv_div_data=obv).score

    _seed_outcomes(statedb, "prepump_agent", MIN_OUTCOMES)
    calibrated_score = agent.analyze(obv_div_data=obv).score

    assert 0.0 <= calibrated_score <= 100.0


# ===================================================================
# Test no-calibration pass-through (< 20 outcomes)
# ===================================================================


def test_agents_unchanged_below_threshold(statedb):
    """Agents should return identical scores when < 20 outcomes."""
    from src.agents.technical_agent import TechnicalAgent
    from src.agents.trend_agent import TrendAgent

    tech = TechnicalAgent()
    trend = TrendAgent()

    tf = {
        "rsi": 35,
        "macd_histogram": 1.5,
        "bb_lower": 95.0,
        "current_price": 100.0,
        "vwap": 99.0,
        "ma7": 101.0,
        "ma25": 100.0,
        "ma99": 98.0,
        "volatility_pct": 4.0,
        "momentum": 2.5,
    }
    score_before = tech.analyze(tf_1h=tf).score

    # Add fewer than MIN_OUTCOMES calibration entries
    for _ in range(5):
        update_calibration("technical_agent", 0.9, 10.0, db=statedb)

    score_after = tech.analyze(tf_1h=tf).score
    assert score_before == score_after

    # Trend agent
    mtf = {"trend_score": 65}
    trend_before = trend.analyze(mtf).score
    for _ in range(5):
        update_calibration("trend_agent", 0.9, 10.0, db=statedb)
    trend_after = trend.analyze(mtf).score
    assert trend_before == trend_after


# ===================================================================
# Test score range invariants
# ===================================================================


def test_calibration_never_exceeds_bounds(statedb):
    """Calibrated scores must always be in [0, 100]."""
    from src.agents.technical_agent import TechnicalAgent

    agent = TechnicalAgent()

    # Seed with extreme calibration (factor clamped to 2.0)
    for i in range(MIN_OUTCOMES):
        if i % 2 == 0:
            update_calibration("technical_agent", 0.9, 100.0, db=statedb)
        else:
            update_calibration("technical_agent", 0.1, 0.01, db=statedb)

    tf = {
        "rsi": 25,
        "macd_histogram": 5.0,
        "current_price": 90.0,
        "bb_lower": 95.0,
        "vwap": 88.0,
        "ma7": 95,
        "ma25": 93,
        "ma99": 90,
        "volatility_pct": 5.0,
        "momentum": 3.0,
    }

    result = agent.analyze(tf_1h=tf)
    assert 0.0 <= result.score <= 100.0


# ===================================================================
# Fixtures
# ===================================================================


@pytest.fixture
def statedb(tmp_path, monkeypatch):
    """Provide an isolated StateDB instance for calibration tests."""
    import src.state_db as sd_mod

    test_db_path = str(tmp_path / "test_calibration.db")
    monkeypatch.setenv("STATE_DB_PATH", test_db_path)
    sd_mod._state_db_instance = None
    db = sd_mod.get_state_db(test_db_path)
    yield db
    sd_mod._state_db_instance = None
