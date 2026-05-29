"""
Tests for specialist agent modules.

Run with:  python -m pytest tests/test_agents.py -v
Or:        python tests/test_agents.py
"""

import sys
import os

# Ensure project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.agents.base import SpecialistResult
from src.agents.technical_agent import TechnicalAgent
from src.agents.trend_agent import TrendAgent
from src.agents.volume_agent import VolumeAgent
from src.agents.sentiment_agent import SentimentAgent
from src.agents.onchain_agent import OnChainAgent
from src.agents.market_sentiment_agent import MarketSentimentAgent
from src.agents.prepump_agent import PrePumpAgent


# ---------------------------------------------------------------------------
# Helpers — generate mock klines
# ---------------------------------------------------------------------------

def _make_klines_1h(n=50, base_price=100.0, volatility=2.0):
    """Generate n 1h kline dicts with realistic structure."""
    import random
    klines = []
    price = base_price
    for i in range(n):
        change = random.uniform(-volatility, volatility)
        open_ = price
        close = price + change
        high = max(open_, close) + abs(change) * 0.5
        low = min(open_, close) - abs(change) * 0.5
        volume = random.uniform(100, 1000)
        klines.append({
            'open': open_,
            'high': high,
            'low': low,
            'close': close,
            'volume': volume,
            'timestamp': i * 3600000,
        })
        price = close
    return klines


def _make_klines_4h(n=80, base_price=100.0):
    return _make_klines_1h(n=n, base_price=base_price, volatility=3.0)


def _mock_tf_1h(overrides=None):
    """Pre-computed 1h analysis dict."""
    d = {
        'rsi': 35,
        'macd_histogram': 1.5,
        'bb_lower': 95.0,
        'bb_upper': 110.0,
        'current_price': 100.0,
        'vwap': 99.0,
        'ma7': 101.0,
        'ma25': 100.0,
        'ma99': 98.0,
        'volatility_pct': 4.0,
        'momentum': 2.5,
        'trend': 'weak_up',
    }
    if overrides:
        d.update(overrides)
    return d


# ===================================================================
# Test SpecialistResult (base)
# ===================================================================

def test_specialist_result_defaults():
    r = SpecialistResult()
    assert r.score == 0.0
    assert r.signals == []
    assert r.data == {}
    assert r.confidence == 'medium'


def test_specialist_result_custom():
    r = SpecialistResult(score=75.5, signals=['test'], data={'k': 'v'}, confidence='high')
    assert r.score == 75.5
    assert r.signals == ['test']
    assert r.data == {'k': 'v'}
    assert r.confidence == 'high'


# ===================================================================
# Test TechnicalAgent
# ===================================================================

def test_technical_agent_with_precomputed():
    agent = TechnicalAgent()
    result = agent.analyze(tf_1h=_mock_tf_1h())
    assert 0 <= result.score <= 100
    assert isinstance(result.signals, list)
    assert isinstance(result.data, dict)
    print(f"  [technical/precomputed] score={result.score}, signals={len(result.signals)}")


def test_technical_agent_with_klines():
    agent = TechnicalAgent()
    try:
        klines = _make_klines_1h(60)
        result = agent.analyze(klines_1h=klines)
        assert 0 <= result.score <= 100
        assert isinstance(result.data, dict)
        print(f"  [technical/klines] score={result.score}, signals={len(result.signals)}")
    except ImportError as e:
        print(f"  [technical/klines] SKIPPED (missing dep: {e})")


def test_technical_agent_empty():
    agent = TechnicalAgent()
    result = agent.analyze()
    assert 0 <= result.score <= 100
    print(f"  [technical/empty] score={result.score}")


def test_technical_agent_bullish():
    agent = TechnicalAgent()
    tf = _mock_tf_1h({
        'rsi': 25, 'macd_histogram': 5.0, 'current_price': 90.0,
        'bb_lower': 95.0, 'vwap': 88.0, 'ma7': 95, 'ma25': 93, 'ma99': 90,
    })
    result = agent.analyze(tf_1h=tf)
    assert result.score > 50  # bullish setup should score higher
    print(f"  [technical/bullish] score={result.score}")


def test_technical_agent_factor_technical_subscores():
    # RSI oversold
    assert TechnicalAgent._factor_technical({'rsi': 15}) == 25  # only RSI contribution
    # RSI overbought
    assert TechnicalAgent._factor_technical({'rsi': 85}) == 3
    # MACD positive (no other indicators present → rsi defaults to 50 → +10)
    assert TechnicalAgent._factor_technical({'macd_histogram': 1.0}) == 35  # 10 (RSI default 50) + 25 (MACD)
    # MACD negative (no RSI default contribution)
    assert TechnicalAgent._factor_technical({'macd_histogram': -1.0}) == 0.0


def test_technical_agent_factor_price_action_subscores():
    # Ideal volatility
    assert TechnicalAgent._factor_price_action({'volatility_pct': 5.0, 'momentum': 3.0}) == 100
    # No momentum
    assert TechnicalAgent._factor_price_action({'volatility_pct': 5.0, 'momentum': 0.0}) == 80


def test_technical_agent_factor_bb_squeeze():
    assert TechnicalAgent._factor_bb_squeeze(None) == 30.0
    assert TechnicalAgent._factor_bb_squeeze({'squeezing': True, 'percentile': 10}) == 80.0
    assert TechnicalAgent._factor_bb_squeeze({'squeezing': False, 'percentile': 50}) == 30.0


def test_technical_agent_factor_rsi_divergence():
    assert TechnicalAgent._factor_rsi_divergence(None) == 30.0
    assert TechnicalAgent._factor_rsi_divergence({'detected': True, 'strength': 50}) == 85.0
    assert TechnicalAgent._factor_rsi_divergence({'detected': False, 'rsi_current': 25}) == 50.0


# ===================================================================
# Test TrendAgent
# ===================================================================

def test_trend_agent_bullish():
    agent = TrendAgent()
    mtf = {'trend_score': 80, 'trend_alignment': 'bullish', 'entry_signal': 'long',
           'tf_1h': {}, 'tf_4h': {}, 'tf_15m': {}}
    result = agent.analyze(mtf)
    assert 0 <= result.score <= 100
    assert result.score == 80.0
    assert any('Bullish' in s or 'Entry' in s for s in result.signals)
    print(f"  [trend/bullish] score={result.score}, signals={result.signals}")


def test_trend_agent_bearish():
    agent = TrendAgent()
    mtf = {'trend_score': 15, 'trend_alignment': 'bearish'}
    result = agent.analyze(mtf)
    assert result.score == 15.0
    assert any('Bearish' in s for s in result.signals)


def test_trend_agent_empty():
    agent = TrendAgent()
    result = agent.analyze({})
    assert result.score == 50.0
    assert isinstance(result.signals, list)


def test_trend_agent_clamping():
    agent = TrendAgent()
    result = agent.analyze({'trend_score': 150})
    assert result.score == 100.0
    result = agent.analyze({'trend_score': -10})
    assert result.score == 0.0


# ===================================================================
# Test VolumeAgent
# ===================================================================

def test_volume_agent_top_rank():
    agent = VolumeAgent()
    result = agent.analyze(coin_data={'rank': 5, 'price_change_24h': 8.0, 'volume_surge': True})
    assert 0 <= result.score <= 100
    assert result.score == 30 + 30 + 40  # 100
    assert result.score == 100
    print(f"  [volume/top] score={result.score}")


def test_volume_agent_low_rank_no_surge():
    agent = VolumeAgent()
    result = agent.analyze(coin_data={'rank': 50, 'price_change_24h': -2.0})
    assert 0 <= result.score <= 100
    assert result.score == 5 + 10  # 15
    print(f"  [volume/low] score={result.score}")


def test_volume_agent_empty():
    agent = VolumeAgent()
    result = agent.analyze()
    assert 0 <= result.score <= 100
    print(f"  [volume/empty] score={result.score}")


def test_volume_agent_subscore_rank():
    # price_change defaults to 0 → +10 (range -5..0)
    assert VolumeAgent._factor_volume_momentum({'rank': 5}) == 40    # 30 + 10
    assert VolumeAgent._factor_volume_momentum({'rank': 15}) == 30   # 20 + 10
    assert VolumeAgent._factor_volume_momentum({'rank': 25}) == 20   # 10 + 10
    assert VolumeAgent._factor_volume_momentum({'rank': 50}) == 15   # 5 + 10


def test_volume_agent_subscore_price_change():
    # rank defaults to 999 → +5
    assert VolumeAgent._factor_volume_momentum({'price_change_24h': 3.0}) == 25    # 5 + 20
    assert VolumeAgent._factor_volume_momentum({'price_change_24h': 10.0}) == 35   # 5 + 30
    assert VolumeAgent._factor_volume_momentum({'price_change_24h': 20.0}) == 20   # 5 + 15
    assert VolumeAgent._factor_volume_momentum({'price_change_24h': -1.0}) == 15   # 5 + 10


def test_volume_agent_surge_bonus():
    base = VolumeAgent._factor_volume_momentum({'rank': 50, 'price_change_24h': 0})
    with_surge = VolumeAgent._factor_volume_momentum({'rank': 50, 'price_change_24h': 0, 'volume_surge': True})
    assert with_surge == base + 40


# ===================================================================
# Test SentimentAgent
# ===================================================================

def test_sentiment_agent_positive():
    agent = SentimentAgent()
    result = agent.analyze(funding_data={'sentiment_score': 10, 'funding_rate': -0.02, 'oi_change_pct': 15})
    assert 0 <= result.score <= 100
    assert result.score > 50
    assert any('Negative Funding' in s for s in result.signals)
    print(f"  [sentiment/positive] score={result.score}, signals={result.signals}")


def test_sentiment_agent_negative():
    agent = SentimentAgent()
    result = agent.analyze(funding_data={'sentiment_score': -10, 'funding_rate': 0.05, 'oi_change_pct': -15})
    assert 0 <= result.score <= 100
    assert result.score < 50
    assert any('High Funding' in s for s in result.signals)
    print(f"  [sentiment/negative] score={result.score}, signals={result.signals}")


def test_sentiment_agent_neutral():
    agent = SentimentAgent()
    result = agent.analyze(funding_data={'sentiment_score': 0, 'funding_rate': 0.01, 'oi_change_pct': 0})
    assert result.score == 50.0


def test_sentiment_agent_none():
    agent = SentimentAgent()
    result = agent.analyze()
    assert result.score == 50.0
    assert result.confidence == 'none'


def test_sentiment_agent_subscore_mapping():
    # +15 → 50 + 15*3.33 = 99.95
    s = SentimentAgent._factor_sentiment({'sentiment_score': 15})
    assert abs(s - 99.95) < 0.1
    # -15 → 50 - 15*3.33 = 0.05
    s = SentimentAgent._factor_sentiment({'sentiment_score': -15})
    assert abs(s - 0.05) < 0.1
    # None → 50
    assert SentimentAgent._factor_sentiment(None) == 50.0


# ===================================================================
# Test OnChainAgent
# ===================================================================

def test_onchain_agent_high():
    agent = OnChainAgent()
    result = agent.analyze(onchain_score=80)
    assert result.score == 80
    assert any('Strong' in s for s in result.signals)
    print(f"  [onchain/high] score={result.score}")


def test_onchain_agent_low():
    agent = OnChainAgent()
    result = agent.analyze(onchain_score=20)
    assert result.score == 20
    assert any('Weak' in s or 'Declining' in s for s in result.signals)


def test_onchain_agent_neutral():
    agent = OnChainAgent()
    result = agent.analyze(onchain_score=50)
    assert result.score == 50


def test_onchain_agent_clamp():
    agent = OnChainAgent()
    assert agent.analyze(onchain_score=150).score == 100
    assert agent.analyze(onchain_score=-10).score == 0


# ===================================================================
# Test MarketSentimentAgent
# ===================================================================

def test_fng_extreme_fear():
    agent = MarketSentimentAgent()
    result = agent.analyze(fng_value=10)
    # contrarian: 90 + (20-10)*0.5 = 95
    assert result.score == 95
    assert any('Extreme Fear' in s for s in result.signals)
    print(f"  [fng/extreme_fear] score={result.score}")


def test_fng_extreme_greed():
    agent = MarketSentimentAgent()
    result = agent.analyze(fng_value=90)
    # contrarian: 10 + (100-90)*0.5 = 15
    assert result.score == 15
    assert any('Extreme Greed' in s or 'Greed' in s for s in result.signals)


def test_fng_neutral():
    agent = MarketSentimentAgent()
    result = agent.analyze(fng_value=50)
    assert result.score == 60  # 50 + (60-50)*1.0
    assert any('Neutral' in s for s in result.signals)


def test_fng_contrarian_mapping_all_ranges():
    # Verify piecewise mapping matches market_scanner exactly
    cases = [
        (0,  100.0),   # 90 + 20*0.5 = 100
        (20,  90.0),   # 90 + 0
        (25,  85.0),   # 70 + (40-25) = 85
        (40,  70.0),   # 70 + 0
        (50,  60.0),   # 50 + (60-50) = 60
        (60,  50.0),   # 50 + 0
        (70,  40.0),   # 30 + (80-70) = 40
        (80,  30.0),   # 30 + 0
        (90,  15.0),   # 10 + (100-90)*0.5 = 15
        (100, 10.0),   # 10 + 0
    ]
    for fng, expected in cases:
        actual = MarketSentimentAgent._contrarian_score(fng)
        assert abs(actual - expected) < 0.01, f"F&G={fng}: expected {expected}, got {actual}"


# ===================================================================
# Test PrePumpAgent
# ===================================================================

def test_prepump_obv_divergence_detected():
    agent = PrePumpAgent()
    result = agent.analyze(obv_div_data={'detected': True, 'strength': 60, 'obv_trend': 'rising'})
    assert 0 <= result.score <= 100
    assert any('OBV' in s for s in result.signals)
    print(f"  [prepump/obv_div] score={result.score}, signals={result.signals}")


def test_prepump_consolidation_breakout():
    agent = PrePumpAgent()
    result = agent.analyze(
        consolidation_data={'breaking_out': True, 'volume_confirmed': True, 'days_in_range': 45}
    )
    assert 0 <= result.score <= 100
    assert any('Breakout' in s for s in result.signals)
    print(f"  [prepump/breakout] score={result.score}, signals={result.signals}")


def test_prepump_both():
    agent = PrePumpAgent()
    result = agent.analyze(
        obv_div_data={'detected': True, 'strength': 80, 'obv_trend': 'rising'},
        consolidation_data={'breaking_out': False, 'in_consolidation': True, 'days_in_range': 35, 'range_pct': 12}
    )
    assert 0 <= result.score <= 100
    print(f"  [prepump/both] score={result.score}, signals={result.signals}")


def test_prepump_none():
    agent = PrePumpAgent()
    result = agent.analyze()
    # Both default to 50 → blended 50
    assert result.score == 50.0


def test_prepump_subscore_obv_divergence():
    assert PrePumpAgent._factor_obv_divergence(None) == 30.0
    assert PrePumpAgent._factor_obv_divergence({'detected': True, 'strength': 100, 'obv_trend': 'rising'}) == 100.0
    assert PrePumpAgent._factor_obv_divergence({'detected': False, 'obv_trend': 'rising'}) == 50.0
    assert PrePumpAgent._factor_obv_divergence({'detected': False, 'obv_trend': 'falling'}) == 30.0


def test_prepump_subscore_consolidation():
    assert PrePumpAgent._factor_consolidation(None) == 30.0
    # Breaking out with volume
    assert PrePumpAgent._factor_consolidation({'breaking_out': True, 'volume_confirmed': True}) == 95.0
    # Breaking out without volume
    assert PrePumpAgent._factor_consolidation({'breaking_out': True, 'volume_confirmed': False}) == 80.0
    # In consolidation, long and tight
    assert PrePumpAgent._factor_consolidation({'in_consolidation': True, 'days_in_range': 45, 'range_pct': 10}) == 75.0
    # In consolidation, short
    assert PrePumpAgent._factor_consolidation({'in_consolidation': True, 'days_in_range': 20, 'range_pct': 20}) == 50.0


# ===================================================================
# Test cross-agent integration
# ===================================================================

def test_all_agents_return_specialist_result():
    """Every agent must return a SpecialistResult."""
    agents_and_args = [
        (TechnicalAgent(), {'tf_1h': _mock_tf_1h()}),
        (TrendAgent(), {'mtf_data': {'trend_score': 50}}),
        (VolumeAgent(), {'coin_data': {'rank': 25, 'price_change_24h': 3}}),
        (SentimentAgent(), {'funding_data': {'sentiment_score': 5}}),
        (OnChainAgent(), {'onchain_score': 55}),
        (MarketSentimentAgent(), {'fng_value': 45}),
        (PrePumpAgent(), {}),
    ]
    for agent, kwargs in agents_and_args:
        result = agent.analyze(**kwargs)
        assert isinstance(result, SpecialistResult)
        assert 0 <= result.score <= 100
        assert isinstance(result.signals, list)
        assert isinstance(result.data, dict)


def test_combined_weighted_score():
    """Verify that combining all agents reproduces a score in 0-100 range."""
    tf_1h = _mock_tf_1h()
    klines_1h = _make_klines_1h(60)
    klines_4h = _make_klines_4h(80)

    r_tech = TechnicalAgent().analyze(tf_1h=tf_1h)
    r_trend = TrendAgent().analyze({'trend_score': 70, 'trend_alignment': 'bullish'})
    r_vol = VolumeAgent().analyze(coin_data={'rank': 10, 'price_change_24h': 5, 'volume_surge': True})
    r_sent = SentimentAgent().analyze({'sentiment_score': 5, 'funding_rate': -0.01, 'oi_change_pct': 8})
    r_onchain = OnChainAgent().analyze(onchain_score=60)
    r_fng = MarketSentimentAgent().analyze(fng_value=30)
    r_prepump = PrePumpAgent().analyze()

    # Weighted sum using overall factor weights
    total = (
        0.15 * r_tech.score     # technical (15%)
        + 0.15 * r_trend.score  # trend (15%)
        + 0.10 * r_vol.score    # volume (10%)
        + 0.08 * r_sent.score   # sentiment (8%)
        + 0.08 * 50.0           # price_action is in technical agent (use neutral)
        + 0.08 * r_prepump.score  # OBV+consolidation (16% of total, but each 8%)
        + 0.04 * 50.0           # bb_squeeze in technical (neutral)
        + 0.04 * 50.0           # rsi_div in technical (neutral)
        + 0.10 * r_onchain.score  # onchain (10%)
        + 0.10 * r_fng.score      # market sentiment (10%)
    )
    assert 0 <= total <= 100, f"Combined score out of range: {total}"
    print(f"\n  [integration] Combined weighted score: {total:.2f}")


# ===================================================================
# Run all tests
# ===================================================================

if __name__ == '__main__':
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            passed += 1
            print(f"  ✓ {test_fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {test_fn.__name__}: {e}")
            traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)} tests")
    if failed:
        sys.exit(1)
    else:
        print("All tests passed! ✓")
