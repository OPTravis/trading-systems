"""
Comprehensive tests for scan_orchestrator.py

Covers:
- load_universe(): sp500, non-existent universe, empty universe, missing file
- ScanOrchestrator.__init__: default config, custom config
- _phase1_sync_and_regime(): with/without broker, regime detection
- _phase2_score_and_rank(): with/without scorer, scoring failure fallback
- _phase3_research(): with/without researcher, timeout, parallel research
- _phase4_risk_check(): score filter, risk rejection, position sizing
- _phase5_execute(): with/without executor, zero position skip
- run(): full pipeline, empty universe, auto_execute
- analyze_symbol(): single symbol analysis
"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.scan_orchestrator import (
    ScanOrchestrator,
    ScanResult,
    TradeSignal,
    load_universe,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _make_score(
    composite=75.0,
    technical=70.0,
    fundamental=65.0,
    momentum=80.0,
    sentiment=60.0,
    quality=72.0,
    value=68.0,
    atr_pct=0.02,
    weights=None,
):
    """Create a mock score object returned by scorer.score_stock()."""
    score = SimpleNamespace(
        composite=composite,
        technical=technical,
        fundamental=fundamental,
        momentum=momentum,
        sentiment=sentiment,
        quality=quality,
        value=value,
        atr_pct=atr_pct,
        weights=weights
        or {
            "technical": 0.2,
            "fundamental": 0.2,
            "momentum": 0.2,
            "sentiment": 0.1,
            "quality": 0.15,
            "value": 0.15,
        },
    )
    return score


def _make_risk_decision(
    approved=True, warnings=None, reason="", position_multiplier=1.0
):
    """Create a mock RiskDecision object."""
    return SimpleNamespace(
        approved=approved,
        warnings=warnings or [],
        reason=reason,
        position_multiplier=position_multiplier,
    )


def _make_research_report(
    recommendation="BUY",
    confidence="high",
    summary="Good stock",
    catalysts=None,
    sentiment_score=0.5,
):
    """Create a mock ResearchReport."""
    return SimpleNamespace(
        recommendation=SimpleNamespace(value=recommendation),
        confidence=confidence,
        summary=summary,
        catalysts=catalysts or ["earnings beat"],
        sentiment_score=sentiment_score,
    )


def _write_temp_yaml(data: dict) -> str:
    """Write a dict to a temp YAML file and return the directory path."""
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "universes.yaml")
    with open(path, "w") as f:
        yaml.dump(data, f)
    return tmpdir


def _make_orchestrator(**overrides):
    """Create a ScanOrchestrator with sensible mock defaults."""
    defaults = dict(
        broker=MagicMock(),
        portfolio=MagicMock(),
        stock_data_feed=MagicMock(),
        stock_scorer=MagicMock(),
        composite_ranker=None,
        risk_manager=MagicMock(),
        regime_detector=MagicMock(),
        stock_researcher=MagicMock(),
        position_sizer=MagicMock(),
        trade_executor=MagicMock(),
        feature_store=MagicMock(),
        config={"system": {"mode": "paper"}},
    )
    defaults.update(overrides)
    return ScanOrchestrator(**defaults)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. load_universe()
# ═══════════════════════════════════════════════════════════════════════════════


class TestLoadUniverse:
    """Tests for load_universe()."""

    def test_load_sp500(self):
        """load_universe('sp500') returns a non-empty list of tickers."""
        symbols = load_universe("sp500")
        assert isinstance(symbols, list)
        assert len(symbols) > 0
        assert "AAPL" in symbols
        assert "MSFT" in symbols

    def test_load_sp500_has_no_duplicates(self):
        """AMZN appears in multiple sectors; output should be deduplicated."""
        symbols = load_universe("sp500")
        assert len(symbols) == len(set(symbols))

    def test_load_nonexistent_universe(self):
        """A universe name not in the YAML returns an empty list."""
        symbols = load_universe("definitely_not_real_universe_xyz")
        assert symbols == []

    def test_load_from_temp_file(self):
        """load_universe reads from a custom config_dir and deduplicates."""
        data = {
            "universes": {
                "mini": {
                    "sectors": {
                        "tech": ["AAA", "BBB"],
                        "finance": ["CCC", "AAA"],  # AAA duplicated
                    }
                }
            }
        }
        tmpdir = _write_temp_yaml(data)
        symbols = load_universe("mini", config_dir=tmpdir)
        # All 3 unique symbols present, no duplicates
        assert len(symbols) == 3
        assert set(symbols) == {"AAA", "BBB", "CCC"}
        # Each symbol appears exactly once
        assert len(set(symbols)) == len(symbols)

    def test_load_missing_file(self):
        """If universes.yaml doesn't exist, returns empty list."""
        symbols = load_universe("sp500", config_dir="/tmp/nonexistent_dir_12345")
        assert symbols == []

    def test_load_empty_sectors(self):
        """A universe with empty sectors returns an empty list."""
        data = {"universes": {"empty": {"sectors": {}}}}
        tmpdir = _write_temp_yaml(data)
        symbols = load_universe("empty", config_dir=tmpdir)
        assert symbols == []


# ═══════════════════════════════════════════════════════════════════════════════
# 2. ScanOrchestrator.__init__
# ═══════════════════════════════════════════════════════════════════════════════


class TestOrchestratorInit:
    """Tests for ScanOrchestrator.__init__."""

    def test_default_init_all_none(self):
        """With no args, all components are None and config loads from file."""
        orch = ScanOrchestrator()
        assert orch.broker is None
        assert orch.portfolio is None
        assert orch.data_feed is None
        assert orch.scorer is None
        assert orch.ranker is None
        assert orch.risk_mgr is None
        assert orch.regime_detector is None
        assert orch.researcher is None
        assert orch.sizer is None
        assert orch.executor is None
        assert orch.feature_store is None
        # config should be a dict (loaded from YAML or empty)
        assert isinstance(orch.config, dict)

    def test_custom_config(self):
        """Passing config= overrides _load_config."""
        custom = {"trading": {"max_positions": 5}}
        orch = ScanOrchestrator(config=custom)
        assert orch.config == custom

    def test_components_assigned(self):
        """All component kwargs are stored as attributes."""
        broker = MagicMock()
        portfolio = MagicMock()
        orch = ScanOrchestrator(broker=broker, portfolio=portfolio, config={})
        assert orch.broker is broker
        assert orch.portfolio is portfolio

    def test_load_config_reads_yaml(self):
        """_load_config returns a dict from config/config.yaml when it exists."""
        orch = ScanOrchestrator(config={"override": True})
        # Manually call _load_config to verify it reads the real file
        real_config = orch._load_config()
        assert isinstance(real_config, dict)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. _phase1_sync_and_regime()
# ═══════════════════════════════════════════════════════════════════════════════


class TestPhase1SyncAndRegime:
    """Tests for _phase1_sync_and_regime()."""

    def test_no_broker_no_detector(self):
        """Without broker or regime_detector, returns NEUTRAL."""
        orch = _make_orchestrator(broker=None, portfolio=None, regime_detector=None)
        assert orch._phase1_sync_and_regime() == "NEUTRAL"

    def test_portfolio_sync_called(self):
        """When broker and portfolio exist, sync_from_broker is called."""
        orch = _make_orchestrator()
        orch.portfolio.sync_from_broker = MagicMock()
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 18.0})
        orch.regime_detector.detect_regime = MagicMock(return_value="BULL")
        regime = orch._phase1_sync_and_regime()
        orch.portfolio.sync_from_broker.assert_called_once_with(orch.broker)
        assert isinstance(regime, str)

    def test_portfolio_sync_exception(self):
        """If sync_from_broker raises, phase1 still returns a regime."""
        orch = _make_orchestrator()
        orch.portfolio.sync_from_broker = MagicMock(
            side_effect=RuntimeError("sync fail")
        )
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 18.0})
        orch.regime_detector.detect_regime = MagicMock(return_value="NEUTRAL")
        regime = orch._phase1_sync_and_regime()
        assert isinstance(regime, str)

    def test_regime_detector_called(self):
        """When regime_detector exists, detect_regime is called with VIX."""
        orch = _make_orchestrator()
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 18.5})
        orch.regime_detector.detect_regime = MagicMock(return_value="LOW_VOL")
        regime = orch._phase1_sync_and_regime()
        orch.regime_detector.detect_regime.assert_called_once_with(vix=18.5)
        assert regime == "LOW_VOL"

    def test_regime_detector_no_data_feed(self):
        """Without data_feed, regime_detector gets vix=None."""
        orch = _make_orchestrator(stock_data_feed=None)
        orch.regime_detector.detect_regime = MagicMock(return_value="RISK_OFF")
        regime = orch._phase1_sync_and_regime()
        orch.regime_detector.detect_regime.assert_called_once_with(vix=None)
        assert regime == "RISK_OFF"

    def test_vix_quote_fails(self):
        """If VIX quote raises, regime_detector still called with vix=None."""
        orch = _make_orchestrator()
        orch.data_feed.get_realtime_quote = MagicMock(side_effect=Exception("no quote"))
        orch.regime_detector.detect_regime = MagicMock(return_value="UNKNOWN")
        regime = orch._phase1_sync_and_regime()
        orch.regime_detector.detect_regime.assert_called_once_with(vix=None)
        assert regime == "UNKNOWN"

    def test_regime_detector_exception(self):
        """If detect_regime raises, returns NEUTRAL."""
        orch = _make_orchestrator()
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 20.0})
        orch.regime_detector.detect_regime = MagicMock(side_effect=Exception("boom"))
        regime = orch._phase1_sync_and_regime()
        assert regime == "NEUTRAL"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. _phase2_score_and_rank()
# ═══════════════════════════════════════════════════════════════════════════════


class TestPhase2ScoreAndRank:
    """Tests for _phase2_score_and_rank()."""

    def test_no_scorer(self):
        """Without scorer, returns empty results."""
        orch = _make_orchestrator(stock_scorer=None)
        ranked, scores = orch._phase2_score_and_rank(["AAPL", "MSFT"])
        assert ranked == []
        assert scores == {}

    def test_scores_all_symbols(self):
        """Scores each symbol in the universe."""
        orch = _make_orchestrator()
        orch.data_feed.get_multiple_quotes = MagicMock(
            return_value={"AAPL": {"price": 150}, "MSFT": {"price": 300}}
        )
        orch.scorer.score_stock = MagicMock(side_effect=lambda sym, md: _make_score())

        ranked, scores = orch._phase2_score_and_rank(["AAPL", "MSFT"])
        assert orch.scorer.score_stock.call_count == 2
        assert len(scores) == 2
        assert "AAPL" in scores
        assert "MSFT" in scores

    def test_ranked_by_composite_fallback(self):
        """Without ranker, symbols sorted by composite score descending."""
        orch = _make_orchestrator(composite_ranker=None)
        orch.data_feed.get_multiple_quotes = MagicMock(return_value={})

        def score_side_effect(sym, md):
            if sym == "AAPL":
                return _make_score(composite=80)
            return _make_score(composite=60)

        orch.scorer.score_stock = MagicMock(side_effect=score_side_effect)
        ranked, _ = orch._phase2_score_and_rank(["MSFT", "AAPL"])
        assert ranked[0] == "AAPL"
        assert ranked[1] == "MSFT"

    def test_ranker_used_when_available(self):
        """When ranker is set, rank_universe is called."""
        import pandas as pd

        orch = _make_orchestrator()
        orch.data_feed.get_multiple_quotes = MagicMock(return_value={})
        orch.scorer.score_stock = MagicMock(return_value=_make_score())

        mock_ranker = MagicMock()
        mock_ranker.rank_universe = MagicMock(
            return_value=pd.DataFrame({"symbol": ["MSFT", "AAPL"]})
        )
        orch.ranker = mock_ranker

        ranked, _ = orch._phase2_score_and_rank(["AAPL", "MSFT"])
        mock_ranker.rank_universe.assert_called_once()
        assert ranked == ["MSFT", "AAPL"]

    def test_ranker_fails_fallback_to_sort(self):
        """If ranker raises, the code has a bug: ranked_symbols is unset after except.

        The except block logs a warning but does not set ranked_symbols, so the
        subsequent logger.info line raises UnboundLocalError. This test documents
        that known bug.
        """
        orch = _make_orchestrator()
        orch.data_feed.get_multiple_quotes = MagicMock(return_value={})

        orch.scorer.score_stock = MagicMock(return_value=_make_score(composite=70))

        mock_ranker = MagicMock()
        mock_ranker.rank_universe = MagicMock(side_effect=Exception("ranker broken"))
        orch.ranker = mock_ranker

        # Bug fix: ranker failure should fall back to composite sort, not crash
        ranked, _ = orch._phase2_score_and_rank(["AAPL", "MSFT"])
        assert len(ranked) == 2  # Falls back to sorted by composite score

    def test_scoring_failure_for_one_symbol(self):
        """If scoring fails for one symbol, the other still gets scored."""
        orch = _make_orchestrator()
        orch.data_feed.get_multiple_quotes = MagicMock(return_value={})

        def side_effect(sym, md):
            if sym == "BAD":
                raise Exception("no data")
            return _make_score(composite=75)

        orch.scorer.score_stock = MagicMock(side_effect=side_effect)
        ranked, scores = orch._phase2_score_and_rank(["AAPL", "BAD"])
        assert "AAPL" in scores
        assert "BAD" not in scores

    def test_batch_quote_fails_fallback_individual(self):
        """If get_multiple_quotes fails, falls back to individual quotes."""
        orch = _make_orchestrator()
        orch.data_feed.get_multiple_quotes = MagicMock(
            side_effect=Exception("batch fail")
        )
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 150})
        orch.scorer.score_stock = MagicMock(return_value=_make_score())

        ranked, scores = orch._phase2_score_and_rank(["AAPL", "MSFT"])
        assert orch.data_feed.get_realtime_quote.call_count == 2
        assert len(scores) == 2

    def test_individual_quote_also_fails(self):
        """If both batch and individual quotes fail, scoring still proceeds with empty data."""
        orch = _make_orchestrator()
        orch.data_feed.get_multiple_quotes = MagicMock(
            side_effect=Exception("batch fail")
        )
        orch.data_feed.get_realtime_quote = MagicMock(
            side_effect=Exception("quote fail")
        )
        orch.scorer.score_stock = MagicMock(return_value=_make_score())

        ranked, scores = orch._phase2_score_and_rank(["AAPL"])
        assert len(scores) == 1

    def test_no_data_feed(self):
        """Without data_feed, scoring proceeds with empty market_data."""
        orch = _make_orchestrator(stock_data_feed=None)
        orch.scorer.score_stock = MagicMock(return_value=_make_score())

        ranked, scores = orch._phase2_score_and_rank(["AAPL"])
        orch.scorer.score_stock.assert_called_once_with("AAPL", {})
        assert len(scores) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 5. _phase3_research()
# ═══════════════════════════════════════════════════════════════════════════════


class TestPhase3Research:
    """Tests for _phase3_research()."""

    def test_no_researcher_pass_through(self):
        """Without researcher, each candidate gets a pass-through result."""
        orch = _make_orchestrator(stock_researcher=None)
        factor_scores = {"AAPL": {"composite": 80}, "MSFT": {"composite": 70}}
        results = orch._phase3_research(["AAPL", "MSFT"], factor_scores)
        assert len(results) == 2
        for r in results:
            assert r["score_adjustment"] == 0.0
            assert r["confidence"] == "none"
            assert "No researcher" in r["summary"]

    def test_empty_candidates(self):
        """Empty candidate list returns empty results."""
        orch = _make_orchestrator(stock_researcher=None)
        results = orch._phase3_research([], {})
        assert results == []

    def test_researcher_called_for_each(self):
        """researcher.analyze_stock called for each candidate."""
        orch = _make_orchestrator()
        orch.researcher.analyze_stock = MagicMock(
            side_effect=lambda sym: _make_research_report()
        )

        results = orch._phase3_research(["AAPL", "MSFT", "GOOGL"], {})
        assert orch.researcher.analyze_stock.call_count == 3
        assert len(results) == 3

    def test_strong_buy_adjustment(self):
        """STRONG_BUY recommendation yields +20 score adjustment."""
        orch = _make_orchestrator()
        orch.researcher.analyze_stock = MagicMock(
            return_value=_make_research_report(recommendation="STRONG_BUY")
        )
        results = orch._phase3_research(["AAPL"], {})
        assert results[0]["score_adjustment"] == 20

    def test_sell_adjustment(self):
        """SELL recommendation yields -10 score adjustment."""
        orch = _make_orchestrator()
        orch.researcher.analyze_stock = MagicMock(
            return_value=_make_research_report(recommendation="SELL")
        )
        results = orch._phase3_research(["AAPL"], {})
        assert results[0]["score_adjustment"] == -10

    def test_strong_sell_adjustment(self):
        """STRONG_SELL recommendation yields -20 score adjustment."""
        orch = _make_orchestrator()
        orch.researcher.analyze_stock = MagicMock(
            return_value=_make_research_report(recommendation="STRONG_SELL")
        )
        results = orch._phase3_research(["AAPL"], {})
        assert results[0]["score_adjustment"] == -20

    def test_unknown_recommendation_yields_zero(self):
        """Unknown recommendation value defaults to 0 adjustment."""
        orch = _make_orchestrator()
        orch.researcher.analyze_stock = MagicMock(
            return_value=_make_research_report(recommendation="SPECULATIVE_BUY")
        )
        results = orch._phase3_research(["AAPL"], {})
        assert results[0]["score_adjustment"] == 0

    def test_research_failure_for_one_symbol(self):
        """If research fails for one symbol, the rest still succeed."""
        orch = _make_orchestrator()

        def side_effect(sym):
            if sym == "BAD":
                raise Exception("LLM error")
            return _make_research_report()

        orch.researcher.analyze_stock = MagicMock(side_effect=side_effect)
        results = orch._phase3_research(["AAPL", "BAD", "MSFT"], {})
        # BAD fails, AAPL and MSFT succeed
        assert len(results) == 2
        symbols = [r["symbol"] for r in results]
        assert "BAD" not in symbols

    def test_research_result_has_summary_and_sentiment(self):
        """Research result carries over summary and sentiment from report."""
        orch = _make_orchestrator()
        orch.researcher.analyze_stock = MagicMock(
            return_value=_make_research_report(
                summary="Strong earnings",
                sentiment_score=0.85,
                catalysts=["new product launch"],
            )
        )
        results = orch._phase3_research(["AAPL"], {})
        assert results[0]["summary"] == "Strong earnings"
        assert results[0]["sentiment"] == 0.85
        assert results[0]["news"] == ["new product launch"]


# ═══════════════════════════════════════════════════════════════════════════════
# 6. _phase4_risk_check()
# ═══════════════════════════════════════════════════════════════════════════════


class TestPhase4RiskCheck:
    """Tests for _phase4_risk_check()."""

    def _base_research_results(self, symbols=None):
        symbols = symbols or ["AAPL"]
        return [
            {"symbol": sym, "score_adjustment": 0.0, "summary": "ok"} for sym in symbols
        ]

    def test_score_below_min_blocked(self):
        """Candidates below min_score are blocked."""
        orch = _make_orchestrator(risk_manager=None, position_sizer=None)
        factor_scores = {"AAPL": {"composite": 40.0}}
        research = self._base_research_results()

        approved, blocked = orch._phase4_risk_check(
            research, factor_scores, min_score=60.0
        )
        assert len(approved) == 0
        assert len(blocked) == 1
        assert "Score" in blocked[0]["reason"]

    def test_score_above_min_approved(self):
        """Candidates above min_score with valid price are approved."""
        orch = _make_orchestrator(risk_manager=None, position_sizer=None)
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 150.0})
        factor_scores = {"AAPL": {"composite": 80.0, "atr_pct": 0.02}}
        research = self._base_research_results()

        approved, blocked = orch._phase4_risk_check(
            research, factor_scores, min_score=60.0
        )
        assert len(approved) == 1
        assert approved[0].symbol == "AAPL"
        assert approved[0].side == "BUY"
        assert approved[0].price == 150.0

    def test_no_price_data_blocked(self):
        """If price is 0 or unavailable, candidate is blocked."""
        orch = _make_orchestrator(risk_manager=None, position_sizer=None)
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 0})
        factor_scores = {"AAPL": {"composite": 80.0}}
        research = self._base_research_results()

        approved, blocked = orch._phase4_risk_check(
            research, factor_scores, min_score=60.0
        )
        assert len(approved) == 0
        assert any("No price" in b["reason"] for b in blocked)

    def test_price_quote_exception_blocked(self):
        """If quote raises, price stays 0 and candidate is blocked."""
        orch = _make_orchestrator(risk_manager=None, position_sizer=None)
        orch.data_feed.get_realtime_quote = MagicMock(side_effect=Exception("fail"))
        factor_scores = {"AAPL": {"composite": 80.0}}
        research = self._base_research_results()

        approved, blocked = orch._phase4_risk_check(
            research, factor_scores, min_score=60.0
        )
        assert len(approved) == 0

    def test_risk_rejection(self):
        """Risk manager can reject a candidate."""
        orch = _make_orchestrator(position_sizer=None)
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 150.0})
        orch.risk_mgr.pre_trade_check = MagicMock(
            return_value=_make_risk_decision(
                approved=False, reason="Concentration limit"
            )
        )
        factor_scores = {"AAPL": {"composite": 80.0}}
        research = self._base_research_results()

        approved, blocked = orch._phase4_risk_check(
            research, factor_scores, min_score=60.0
        )
        assert len(approved) == 0
        assert blocked[0]["reason"] == "Concentration limit"

    def test_risk_approved_with_warnings(self):
        """Risk approval can include warnings."""
        orch = _make_orchestrator(position_sizer=None)
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 150.0})
        orch.risk_mgr.pre_trade_check = MagicMock(
            return_value=_make_risk_decision(approved=True, warnings=["Near earnings"])
        )
        factor_scores = {"AAPL": {"composite": 80.0, "atr_pct": 0.02}}
        research = self._base_research_results()

        approved, blocked = orch._phase4_risk_check(
            research, factor_scores, min_score=60.0
        )
        assert len(approved) == 1
        assert "Near earnings" in approved[0].risk_warnings

    def test_position_sizing(self):
        """Position sizer calculates position_size_usd from NAV."""
        orch = _make_orchestrator(risk_manager=None)
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 150.0})
        orch.portfolio.get_nav = MagicMock(return_value=100_000.0)
        orch.portfolio.position_count = 5
        orch.sizer.calculate = MagicMock(return_value=0.02)  # 2% of NAV

        factor_scores = {"AAPL": {"composite": 80.0, "atr_pct": 0.02}}
        research = self._base_research_results()

        approved, _ = orch._phase4_risk_check(research, factor_scores, min_score=60.0)
        assert len(approved) == 1
        assert approved[0].position_size_usd == pytest.approx(100_000 * 0.02 * 1.0)

    def test_position_multiplier_from_risk(self):
        """Risk decision position_multiplier scales the position size."""
        orch = _make_orchestrator()
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 150.0})
        orch.risk_mgr.pre_trade_check = MagicMock(
            return_value=_make_risk_decision(approved=True, position_multiplier=0.5)
        )
        orch.portfolio.get_nav = MagicMock(return_value=100_000.0)
        orch.portfolio.position_count = 5
        orch.sizer.calculate = MagicMock(return_value=0.04)

        factor_scores = {"AAPL": {"composite": 80.0, "atr_pct": 0.02}}
        research = self._base_research_results()

        approved, _ = orch._phase4_risk_check(research, factor_scores, min_score=60.0)
        assert approved[0].position_size_usd == pytest.approx(100_000 * 0.04 * 0.5)

    def test_stop_loss_and_take_profit_with_atr(self):
        """Stop loss and take profit are ATR-based when atr_pct > 0."""
        orch = _make_orchestrator(risk_manager=None, position_sizer=None)
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 100.0})
        factor_scores = {"AAPL": {"composite": 80.0, "atr_pct": 0.03}}
        research = self._base_research_results()

        approved, _ = orch._phase4_risk_check(research, factor_scores, min_score=60.0)
        sig = approved[0]
        # stop = 100 * (1 - max(0.03*2, 0.03)) = 100 * 0.94 = 94.0
        assert sig.stop_loss == pytest.approx(100.0 * (1 - max(0.03 * 2, 0.03)))
        # tp = 100 * (1 + max(0.03*3, 0.05)) = 100 * 1.09 = 109.0
        assert sig.take_profit == pytest.approx(100.0 * (1 + max(0.03 * 3, 0.05)))

    def test_stop_loss_fallback_when_atr_zero(self):
        """Fallback to 5%/10% when atr_pct is 0."""
        orch = _make_orchestrator(risk_manager=None, position_sizer=None)
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 100.0})
        factor_scores = {"AAPL": {"composite": 80.0, "atr_pct": 0.0}}
        research = self._base_research_results()

        approved, _ = orch._phase4_risk_check(research, factor_scores, min_score=60.0)
        sig = approved[0]
        assert sig.stop_loss == pytest.approx(95.0)
        assert sig.take_profit == pytest.approx(110.0)

    def test_results_sorted_by_score(self):
        """Approved signals are sorted by score descending."""
        orch = _make_orchestrator(risk_manager=None, position_sizer=None)
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 100.0})
        factor_scores = {
            "AAPL": {"composite": 60.0, "atr_pct": 0.02},
            "MSFT": {"composite": 90.0, "atr_pct": 0.02},
            "GOOGL": {"composite": 75.0, "atr_pct": 0.02},
        }
        research = [
            {"symbol": "AAPL", "score_adjustment": 0.0},
            {"symbol": "MSFT", "score_adjustment": 0.0},
            {"symbol": "GOOGL", "score_adjustment": 0.0},
        ]

        approved, _ = orch._phase4_risk_check(research, factor_scores, min_score=50.0)
        scores = [s.score for s in approved]
        assert scores == sorted(scores, reverse=True)

    def test_no_data_feed_price_stays_zero(self):
        """Without data_feed, price is 0 and candidate is blocked."""
        orch = _make_orchestrator(
            stock_data_feed=None, risk_manager=None, position_sizer=None
        )
        factor_scores = {"AAPL": {"composite": 80.0}}
        research = self._base_research_results()

        approved, blocked = orch._phase4_risk_check(
            research, factor_scores, min_score=60.0
        )
        assert len(approved) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 7. _phase5_execute()
# ═══════════════════════════════════════════════════════════════════════════════


class TestPhase5Execute:
    """Tests for _phase5_execute()."""

    def _make_signal(
        self, symbol="AAPL", price=150.0, position_size_usd=3000.0, side="BUY"
    ):
        return TradeSignal(
            symbol=symbol,
            side=side,
            price=price,
            score=80.0,
            position_size_usd=position_size_usd,
            stop_loss=price * 0.95,
            take_profit=price * 1.10,
        )

    def test_no_executor(self):
        """Without executor, nothing happens (no crash)."""
        orch = _make_orchestrator(trade_executor=None)
        signals = [self._make_signal()]
        orch._phase5_execute(signals)  # should not raise

    def test_executor_called(self):
        """Executor.execute is called with correct args for valid signals."""
        orch = _make_orchestrator()
        orch.executor.execute = MagicMock(return_value={"success": True})

        signal = self._make_signal(price=100.0, position_size_usd=2000.0)
        orch._phase5_execute([signal])

        orch.executor.execute.assert_called_once()
        call_kwargs = orch.executor.execute.call_args
        assert call_kwargs[1]["symbol"] == "AAPL"
        assert call_kwargs[1]["side"] == "BUY"
        assert call_kwargs[1]["order_type"] == "LMT"
        assert call_kwargs[1]["quantity"] == pytest.approx(20.0)

    def test_zero_position_skipped(self):
        """Signals with position_size_usd <= 0 are skipped."""
        orch = _make_orchestrator()
        orch.executor.execute = MagicMock()

        signal = self._make_signal(position_size_usd=0.0)
        orch._phase5_execute([signal])

        orch.executor.execute.assert_not_called()

    def test_zero_price_skipped(self):
        """Signals with price <= 0 result in quantity=0 and are skipped."""
        orch = _make_orchestrator()
        orch.executor.execute = MagicMock()

        signal = self._make_signal(price=0.0, position_size_usd=1000.0)
        orch._phase5_execute([signal])

        orch.executor.execute.assert_not_called()

    def test_buy_slippage(self):
        """BUY orders use limit_price = price * 1.002."""
        orch = _make_orchestrator()
        orch.executor.execute = MagicMock(return_value={"success": True})

        signal = self._make_signal(price=100.0, side="BUY", position_size_usd=1000.0)
        orch._phase5_execute([signal])

        call_kwargs = orch.executor.execute.call_args[1]
        assert call_kwargs["price"] == pytest.approx(100.2)

    def test_sell_slippage(self):
        """SELL orders use limit_price = price * 0.998."""
        orch = _make_orchestrator()
        orch.executor.execute = MagicMock(return_value={"success": True})

        signal = self._make_signal(price=100.0, side="SELL", position_size_usd=1000.0)
        orch._phase5_execute([signal])

        call_kwargs = orch.executor.execute.call_args[1]
        assert call_kwargs["price"] == pytest.approx(99.8)

    def test_execution_failure_logged(self):
        """Execution failure result doesn't raise."""
        orch = _make_orchestrator()
        orch.executor.execute = MagicMock(
            return_value={"success": False, "error": "rejected"}
        )

        signal = self._make_signal(price=100.0, position_size_usd=1000.0)
        orch._phase5_execute([signal])  # should not raise

    def test_execution_exception(self):
        """Exception in executor.execute is caught."""
        orch = _make_orchestrator()
        orch.executor.execute = MagicMock(side_effect=Exception("connection lost"))

        signal = self._make_signal(price=100.0, position_size_usd=1000.0)
        orch._phase5_execute([signal])  # should not raise

    def test_empty_signals(self):
        """Empty signals list does nothing."""
        orch = _make_orchestrator()
        orch.executor.execute = MagicMock()
        orch._phase5_execute([])
        orch.executor.execute.assert_not_called()

    def test_multiple_signals(self):
        """Multiple signals are all executed."""
        orch = _make_orchestrator()
        orch.executor.execute = MagicMock(return_value={"success": True})

        signals = [
            self._make_signal(symbol="AAPL", position_size_usd=1000),
            self._make_signal(symbol="MSFT", position_size_usd=2000),
            self._make_signal(symbol="GOOGL", position_size_usd=3000),
        ]
        orch._phase5_execute(signals)
        assert orch.executor.execute.call_count == 3

    def test_slippage_and_stop_take_forwarded(self):
        """stop_loss and take_profit are forwarded to executor."""
        orch = _make_orchestrator()
        orch.executor.execute = MagicMock(return_value={"success": True})

        signal = self._make_signal(price=100.0, position_size_usd=2000.0)
        signal.stop_loss = 95.0
        signal.take_profit = 110.0
        orch._phase5_execute([signal])

        call_kwargs = orch.executor.execute.call_args[1]
        assert call_kwargs["stop_loss"] == 95.0
        assert call_kwargs["take_profit"] == 110.0


# ═══════════════════════════════════════════════════════════════════════════════
# 8. run() — Full pipeline
# ═══════════════════════════════════════════════════════════════════════════════


class TestRunPipeline:
    """Tests for the run() method — full pipeline end-to-end."""

    def test_empty_universe(self):
        """Empty universe returns early with 0 signals."""
        orch = _make_orchestrator()
        orch.regime_detector.detect_regime = MagicMock(return_value="NEUTRAL")

        with patch("src.scan_orchestrator.load_universe", return_value=[]):
            result = orch.run(universe_name="empty_universe")

        assert isinstance(result, ScanResult)
        assert result.universe_size == 0
        assert result.signals == []
        assert result.blocked == []

    def test_full_pipeline_no_execute(self):
        """Full pipeline with auto_execute=False doesn't call executor."""
        orch = _make_orchestrator()
        orch.regime_detector.detect_regime = MagicMock(return_value="BULL")
        orch.data_feed.get_multiple_quotes = MagicMock(
            return_value={"AAPL": {"price": 150}}
        )
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 150})
        orch.scorer.score_stock = MagicMock(return_value=_make_score(composite=80))
        orch.researcher.analyze_stock = MagicMock(
            return_value=_make_research_report(recommendation="BUY")
        )
        orch.risk_mgr.pre_trade_check = MagicMock(
            return_value=_make_risk_decision(approved=True)
        )
        orch.executor.execute = MagicMock(return_value={"success": True})

        with patch("src.scan_orchestrator.load_universe", return_value=["AAPL"]):
            result = orch.run(universe_name="sp500", auto_execute=False, min_score=60.0)

        assert result.universe_size == 1
        assert result.candidates_scored == 1
        assert result.regime == "BULL"
        orch.executor.execute.assert_not_called()

    def test_full_pipeline_auto_execute(self):
        """Full pipeline with auto_execute=True calls executor."""
        orch = _make_orchestrator()
        orch.regime_detector.detect_regime = MagicMock(return_value="BULL")
        orch.data_feed.get_multiple_quotes = MagicMock(
            return_value={"AAPL": {"price": 150}}
        )
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 150})
        orch.scorer.score_stock = MagicMock(return_value=_make_score(composite=80))
        orch.researcher.analyze_stock = MagicMock(
            return_value=_make_research_report(recommendation="BUY")
        )
        orch.risk_mgr.pre_trade_check = MagicMock(
            return_value=_make_risk_decision(approved=True)
        )
        orch.portfolio.get_nav = MagicMock(return_value=100_000.0)
        orch.portfolio.position_count = 5
        orch.sizer.calculate = MagicMock(return_value=0.02)
        orch.executor.execute = MagicMock(return_value={"success": True})

        with patch("src.scan_orchestrator.load_universe", return_value=["AAPL"]):
            result = orch.run(universe_name="sp500", auto_execute=True, min_score=60.0)

        assert result.universe_size == 1
        orch.executor.execute.assert_called_once()

    def test_auto_execute_env_var(self):
        """AUTO_EXECUTE=true env var triggers execution."""
        orch = _make_orchestrator()
        orch.regime_detector.detect_regime = MagicMock(return_value="BULL")
        orch.data_feed.get_multiple_quotes = MagicMock(
            return_value={"AAPL": {"price": 150}}
        )
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 150})
        orch.scorer.score_stock = MagicMock(return_value=_make_score(composite=80))
        orch.researcher.analyze_stock = MagicMock(return_value=_make_research_report())
        orch.risk_mgr.pre_trade_check = MagicMock(
            return_value=_make_risk_decision(approved=True)
        )
        orch.portfolio.get_nav = MagicMock(return_value=100_000.0)
        orch.portfolio.position_count = 5
        orch.sizer.calculate = MagicMock(return_value=0.02)
        orch.executor.execute = MagicMock(return_value={"success": True})

        with patch("src.scan_orchestrator.load_universe", return_value=["AAPL"]):
            with patch.dict(os.environ, {"AUTO_EXECUTE": "true"}):
                orch.run(universe_name="sp500", auto_execute=False)

        orch.executor.execute.assert_called_once()

    def test_feature_store_cleanup(self):
        """feature_store.close() is called at the end of run()."""
        orch = _make_orchestrator()
        orch.regime_detector.detect_regime = MagicMock(return_value="NEUTRAL")
        orch.data_feed.get_multiple_quotes = MagicMock(return_value={})
        orch.scorer.score_stock = MagicMock(return_value=_make_score(composite=30))

        with patch("src.scan_orchestrator.load_universe", return_value=["AAPL"]):
            orch.run(universe_name="test", min_score=90.0)

        orch.feature_store.close.assert_called_once()

    def test_feature_store_close_exception_swallowed(self):
        """Exception in feature_store.close() is caught."""
        orch = _make_orchestrator()
        orch.feature_store.close = MagicMock(side_effect=Exception("close fail"))
        orch.regime_detector.detect_regime = MagicMock(return_value="NEUTRAL")
        orch.data_feed.get_multiple_quotes = MagicMock(return_value={})
        orch.scorer.score_stock = MagicMock(return_value=_make_score(composite=30))

        with patch("src.scan_orchestrator.load_universe", return_value=["AAPL"]):
            orch.run(universe_name="test", min_score=90.0)  # should not raise

    def test_no_feature_store(self):
        """Run works fine without feature_store."""
        orch = _make_orchestrator(feature_store=None)
        orch.regime_detector.detect_regime = MagicMock(return_value="NEUTRAL")

        with patch("src.scan_orchestrator.load_universe", return_value=[]):
            result = orch.run(universe_name="empty")
        assert isinstance(result, ScanResult)

    def test_result_contains_all_fields(self):
        """ScanResult has all expected fields populated."""
        orch = _make_orchestrator()
        orch.regime_detector.detect_regime = MagicMock(return_value="BULL")
        orch.data_feed.get_multiple_quotes = MagicMock(return_value={})
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 100})
        orch.scorer.score_stock = MagicMock(return_value=_make_score(composite=40))

        with patch("src.scan_orchestrator.load_universe", return_value=["AAPL"]):
            result = orch.run(universe_name="sp500", min_score=60.0)

        assert isinstance(result.timestamp, str)
        assert result.regime == "BULL"
        assert result.universe_size == 1
        assert result.candidates_scored == 1
        assert isinstance(result.duration_sec, float)
        assert result.duration_sec >= 0

    def test_top_n_research_limit(self):
        """Only top_n_research candidates are researched."""
        orch = _make_orchestrator()
        orch.regime_detector.detect_regime = MagicMock(return_value="NEUTRAL")
        orch.data_feed.get_multiple_quotes = MagicMock(return_value={})
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 100})
        orch.scorer.score_stock = MagicMock(return_value=_make_score(composite=80))
        orch.researcher.analyze_stock = MagicMock(return_value=_make_research_report())

        universe = ["A", "B", "C", "D", "E", "F", "G"]
        with patch("src.scan_orchestrator.load_universe", return_value=universe):
            orch.run(universe_name="test", top_n_research=3, min_score=0)

        # Only 3 should be researched (top_n_research=3)
        assert orch.researcher.analyze_stock.call_count == 3

    def test_all_blocked_by_score(self):
        """All candidates blocked by min_score yields empty signals."""
        orch = _make_orchestrator()
        orch.regime_detector.detect_regime = MagicMock(return_value="NEUTRAL")
        orch.data_feed.get_multiple_quotes = MagicMock(return_value={})
        orch.scorer.score_stock = MagicMock(return_value=_make_score(composite=30))

        with patch("src.scan_orchestrator.load_universe", return_value=["AAPL"]):
            result = orch.run(universe_name="test", min_score=90.0)

        assert result.signals == []
        assert len(result.blocked) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# 9. analyze_symbol()
# ═══════════════════════════════════════════════════════════════════════════════


class TestAnalyzeSymbol:
    """Tests for analyze_symbol()."""

    def test_basic_analysis(self):
        """analyze_symbol returns a dict with all expected keys."""
        orch = _make_orchestrator()
        orch.data_feed.get_realtime_quote = MagicMock(
            return_value={"price": 150, "volume": 1e6}
        )
        orch.scorer.score_stock = MagicMock(return_value=_make_score())
        orch.researcher.analyze_stock = MagicMock(return_value=_make_research_report())

        result = orch.analyze_symbol("AAPL")
        assert result["symbol"] == "AAPL"
        assert "timestamp" in result
        assert result["quote"]["price"] == 150
        assert result["factor_scores"]["composite"] == 75.0
        assert result["research"].summary == "Good stock"

    def test_no_data_feed(self):
        """Without data_feed, quote is empty dict."""
        orch = _make_orchestrator(
            stock_data_feed=None, stock_scorer=None, stock_researcher=None
        )
        result = orch.analyze_symbol("AAPL")
        assert result["quote"] == {}
        assert result["factor_scores"] == {}

    def test_quote_failure(self):
        """If quote fails, quote key stays empty dict."""
        orch = _make_orchestrator(stock_scorer=None, stock_researcher=None)
        orch.data_feed.get_realtime_quote = MagicMock(side_effect=Exception("fail"))
        result = orch.analyze_symbol("AAPL")
        assert result["quote"] == {}

    def test_scoring_failure(self):
        """If scoring fails, factor_scores stays empty dict."""
        orch = _make_orchestrator(stock_researcher=None)
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 150})
        orch.scorer.score_stock = MagicMock(side_effect=Exception("no factors"))
        result = orch.analyze_symbol("AAPL")
        assert result["factor_scores"] == {}

    def test_research_failure(self):
        """If research fails, research stays empty dict."""
        orch = _make_orchestrator()
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 150})
        orch.scorer.score_stock = MagicMock(return_value=_make_score())
        orch.researcher.analyze_stock = MagicMock(side_effect=Exception("LLM down"))
        result = orch.analyze_symbol("AAPL")
        assert result["research"] == {}

    def test_factor_scores_include_all_fields(self):
        """factor_scores includes all factor dimensions and weights."""
        orch = _make_orchestrator(stock_researcher=None)
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 150})
        orch.scorer.score_stock = MagicMock(
            return_value=_make_score(
                composite=82.0,
                technical=78.0,
                fundamental=70.0,
                momentum=85.0,
                sentiment=65.0,
                quality=75.0,
                value=72.0,
                weights={"technical": 0.25, "fundamental": 0.25},
            )
        )

        result = orch.analyze_symbol("AAPL")
        fs = result["factor_scores"]
        assert fs["composite"] == 82.0
        assert fs["technical"] == 78.0
        assert fs["fundamental"] == 70.0
        assert fs["momentum"] == 85.0
        assert fs["sentiment"] == 65.0
        assert fs["quality"] == 75.0
        assert fs["value"] == 72.0
        assert "weights" in fs

    def test_no_researcher(self):
        """Without researcher, research stays empty dict."""
        orch = _make_orchestrator(stock_researcher=None)
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 150})
        orch.scorer.score_stock = MagicMock(return_value=_make_score())

        result = orch.analyze_symbol("AAPL")
        assert result["research"] == {}


# ═══════════════════════════════════════════════════════════════════════════════
# Data model tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestDataModels:
    """Tests for TradeSignal and ScanResult dataclasses."""

    def test_trade_signal_defaults(self):
        """TradeSignal has sensible defaults."""
        sig = TradeSignal(symbol="AAPL", side="BUY")
        assert sig.quantity == 0.0
        assert sig.price == 0.0
        assert sig.score == 0.0
        assert sig.currency == "USD"
        assert sig.market == "US"
        assert sig.risk_approved is False
        assert sig.risk_warnings == []
        assert sig.factor_scores == {}

    def test_scan_result_creation(self):
        """ScanResult can be created with all fields."""
        sig = TradeSignal(symbol="AAPL", side="BUY")
        result = ScanResult(
            timestamp="2026-01-01T00:00:00",
            regime="BULL",
            universe_size=100,
            candidates_scored=50,
            research_completed=5,
            signals=[sig],
            blocked=[{"symbol": "MSFT", "reason": "low score"}],
            duration_sec=1.5,
        )
        assert result.regime == "BULL"
        assert len(result.signals) == 1
        assert len(result.blocked) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Edge cases / integration-style
# ═══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Edge cases and integration-style tests."""

    def test_risk_check_exception_doesnt_crash(self):
        """If risk_mgr.pre_trade_check throws, candidate is still processed (no crash)."""
        orch = _make_orchestrator(position_sizer=None)
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 150.0})
        orch.risk_mgr.pre_trade_check = MagicMock(side_effect=Exception("risk timeout"))

        factor_scores = {"AAPL": {"composite": 80.0, "atr_pct": 0.02}}
        research = [{"symbol": "AAPL", "score_adjustment": 0.0, "summary": "ok"}]

        approved, blocked = orch._phase4_risk_check(
            research, factor_scores, min_score=60.0
        )
        # Exception in risk check means risk_approved stays True (default)
        assert len(approved) == 1

    def test_position_sizing_failure(self):
        """If position sizer fails, position_size_usd stays 0."""
        orch = _make_orchestrator(risk_manager=None)
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 150.0})
        orch.portfolio.get_nav = MagicMock(side_effect=Exception("nav error"))
        orch.sizer.calculate = MagicMock(return_value=0.02)

        factor_scores = {"AAPL": {"composite": 80.0, "atr_pct": 0.02}}
        research = [{"symbol": "AAPL", "score_adjustment": 0.0}]

        approved, _ = orch._phase4_risk_check(research, factor_scores, min_score=60.0)
        assert approved[0].position_size_usd == 0.0

    def test_score_adjustment_pushes_above_min(self):
        """Score adjustment can push a borderline candidate above min_score."""
        orch = _make_orchestrator(risk_manager=None, position_sizer=None)
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 100.0})

        factor_scores = {"AAPL": {"composite": 55.0, "atr_pct": 0.02}}
        research = [{"symbol": "AAPL", "score_adjustment": 10.0}]  # adj_score = 65

        approved, blocked = orch._phase4_risk_check(
            research, factor_scores, min_score=60.0
        )
        assert len(approved) == 1
        assert approved[0].score == 65.0

    def test_score_adjustment_pushes_below_min(self):
        """Negative score adjustment can push a candidate below min_score."""
        orch = _make_orchestrator(risk_manager=None, position_sizer=None)
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 100.0})

        factor_scores = {"AAPL": {"composite": 65.0, "atr_pct": 0.02}}
        research = [{"symbol": "AAPL", "score_adjustment": -10.0}]  # adj_score = 55

        approved, blocked = orch._phase4_risk_check(
            research, factor_scores, min_score=60.0
        )
        assert len(approved) == 0
        assert len(blocked) == 1

    def test_no_scorer_no_researcher_pipeline(self):
        """Pipeline works end-to-end even without scorer or researcher."""
        orch = _make_orchestrator(
            stock_scorer=None,
            stock_researcher=None,
            risk_manager=None,
            position_sizer=None,
        )
        orch.regime_detector.detect_regime = MagicMock(return_value="NEUTRAL")

        with patch("src.scan_orchestrator.load_universe", return_value=["AAPL"]):
            result = orch.run(universe_name="test", min_score=0)

        assert result.candidates_scored == 0
        assert result.research_completed == 0
        assert result.signals == []

    def test_empty_research_results(self):
        """If research returns empty, risk check gets empty list."""
        orch = _make_orchestrator()
        orch.regime_detector.detect_regime = MagicMock(return_value="NEUTRAL")
        orch.data_feed.get_multiple_quotes = MagicMock(return_value={})
        orch.scorer.score_stock = MagicMock(return_value=_make_score(composite=80))
        orch.researcher.analyze_stock = MagicMock(side_effect=Exception("all fail"))

        with patch("src.scan_orchestrator.load_universe", return_value=["AAPL"]):
            result = orch.run(universe_name="test", min_score=60.0)

        assert result.research_completed == 0
        assert result.signals == []

    def test_sector_resolved_from_factor_scores(self):
        """Sector is resolved from factor_scores if present."""
        orch = _make_orchestrator(risk_manager=None, position_sizer=None)
        orch.data_feed.get_realtime_quote = MagicMock(return_value={"price": 100.0})

        factor_scores = {
            "AAPL": {"composite": 80.0, "sector": "Technology", "atr_pct": 0.02}
        }
        research = [{"symbol": "AAPL", "score_adjustment": 0.0}]

        approved, _ = orch._phase4_risk_check(research, factor_scores, min_score=60.0)
        assert approved[0].sector == "Technology"
