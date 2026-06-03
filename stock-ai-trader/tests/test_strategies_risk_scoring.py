"""
Tests for strategies, scoring, and risk modules.
"""

from datetime import date, timedelta
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from src.risk.earnings_blackout import EarningsBlackout
from src.risk.settlement_guard import SettlementGuard
from src.risk.stock_risk_manager import StockRiskManager, TradeSignal
from src.risk.vix_position_scale import VIXPositionScale
from src.risk.vol_target_sizer import VolTargetSizer
from src.scoring.composite_ranker import CompositeRanker
from src.scoring.fundamental_scorer import FundamentalScorer
from src.scoring.sentiment_scorer import SentimentScorer

# ── FundamentalScorer ─────────────────────────────────────────────────


class TestFundamentalScorer:
    @pytest.fixture
    def scorer(self):
        return FundamentalScorer()

    def test_no_metrics_returns_neutral(self, scorer):
        assert scorer.score("AAPL") == 50.0

    def test_empty_metrics_returns_neutral(self, scorer):
        assert scorer.score("AAPL", {}) == 50.0

    def test_good_pe_ratio(self, scorer):
        score = scorer.score("AAPL", {"pe_ratio": 15.0})
        assert score > 50  # Good PE

    def test_bad_pe_ratio(self, scorer):
        score = scorer.score("AAPL", {"pe_ratio": 50.0})
        assert score < 50  # Bad PE

    def test_negative_pe_ratio(self, scorer):
        score = scorer.score("AAPL", {"pe_ratio": -5.0})
        assert score < 50  # Negative PE is bad

    def test_good_roe(self, scorer):
        score = scorer.score("AAPL", {"roe": 25.0})
        assert score > 50

    def test_low_roe(self, scorer):
        score = scorer.score("AAPL", {"roe": 5.0})
        assert score < 50

    def test_multiple_metrics(self, scorer):
        metrics = {"pe_ratio": 15.0, "pb_ratio": 3.0, "roe": 20.0}
        score = scorer.score("AAPL", metrics)
        assert 0 <= score <= 100

    def test_unknown_metric_ignored(self, scorer):
        score = scorer.score("AAPL", {"unknown_metric": 999.0})
        assert score == 50.0  # Neutral for unknown

    def test_score_clamped_0_100(self, scorer):
        # Extreme values should still be clamped
        score = scorer.score("AAPL", {"pe_ratio": 0.001, "roe": 999.0})
        assert 0 <= score <= 100


# ── SentimentScorer ───────────────────────────────────────────────────


class TestSentimentScorer:
    @pytest.fixture
    def scorer(self):
        return SentimentScorer()

    def test_no_data_returns_neutral(self, scorer):
        assert scorer.score("AAPL") == 50.0

    def test_empty_data_returns_neutral(self, scorer):
        assert scorer.score("AAPL", {}) == 50.0

    def test_positive_news(self, scorer):
        data = {"news_sentiment": 0.8}
        score = scorer.score("AAPL", data)
        assert score > 50

    def test_negative_news(self, scorer):
        data = {"news_sentiment": -0.8}
        score = scorer.score("AAPL", data)
        assert score < 50

    def test_analyst_buy_consensus(self, scorer):
        data = {"analyst_ratings": {"buy": 10, "hold": 2, "sell": 1}}
        score = scorer.score("AAPL", data)
        assert score > 50

    def test_analyst_sell_consensus(self, scorer):
        data = {"analyst_ratings": {"buy": 1, "hold": 2, "sell": 10}}
        score = scorer.score("AAPL", data)
        assert score < 50

    def test_analyst_empty_ratings(self, scorer):
        data = {"analyst_ratings": {}}
        score = scorer.score("AAPL", data)
        assert score == 50.0

    def test_insider_buys_positive(self, scorer):
        data = {"insider_trades": [{"type": "buy"}, {"type": "buy"}]}
        score = scorer.score("AAPL", data)
        assert score > 50

    def test_insider_sells_negative(self, scorer):
        data = {"insider_trades": [{"type": "sell"}, {"type": "sell"}]}
        score = scorer.score("AAPL", data)
        assert score < 50

    def test_combined_sentiment(self, scorer):
        data = {
            "news_sentiment": 0.5,
            "analyst_ratings": {"buy": 8, "hold": 2, "sell": 0},
            "insider_trades": [{"type": "buy"}],
        }
        score = scorer.score("AAPL", data)
        assert score > 50

    def test_score_clamped(self, scorer):
        data = {"news_sentiment": 2.0}  # Extreme
        score = scorer.score("AAPL", data)
        assert 0 <= score <= 100


# ── CompositeRanker ───────────────────────────────────────────────────


class TestCompositeRanker:
    @pytest.fixture
    def ranker(self):
        return CompositeRanker()

    @pytest.fixture
    def factor_scores(self):
        return {
            "AAPL": {
                "momentum": 80,
                "value": 60,
                "quality": 70,
                "fundamental": 65,
                "technical": 75,
                "sentiment": 55,
            },
            "MSFT": {
                "momentum": 70,
                "value": 50,
                "quality": 80,
                "fundamental": 70,
                "technical": 60,
                "sentiment": 65,
            },
            "GOOGL": {
                "momentum": 60,
                "value": 70,
                "quality": 60,
                "fundamental": 55,
                "technical": 50,
                "sentiment": 70,
            },
        }

    def test_rank_universe_returns_dataframe(self, ranker, factor_scores):
        result = ranker.rank_universe(list(factor_scores.keys()), factor_scores)
        assert isinstance(result, pd.DataFrame)
        assert "symbol" in result.columns
        assert "composite_score" in result.columns

    def test_rank_universe_top_20_pct(self, ranker, factor_scores):
        result = ranker.rank_universe(list(factor_scores.keys()), factor_scores)
        # 3 symbols → top 20% = 1 symbol
        assert len(result) == max(1, int(len(factor_scores) * 0.20))

    def test_rank_universe_empty(self, ranker):
        result = ranker.rank_universe([], {})
        assert result.empty

    def test_rank_universe_sorted_desc(self, ranker, factor_scores):
        result = ranker.rank_universe(list(factor_scores.keys()), factor_scores)
        scores = result["composite_score"].tolist()
        assert scores == sorted(scores, reverse=True)

    def test_gram_schmidt(self, ranker):
        matrix = np.array([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]])
        orthogonal = ranker._gram_schmidt(matrix)
        # Columns should be orthogonal
        dot = np.dot(orthogonal[:, 0], orthogonal[:, 1])
        assert abs(dot) < 1e-10

    def test_get_ic_weights_no_tracker(self, ranker):
        factors = ["momentum", "value", "quality"]
        weights = ranker._get_ic_weights(factors)
        # Equal weights when no tracker
        assert all(w == pytest.approx(1.0 / 3) for w in weights.values())

    def test_get_ic_weights_with_tracker(self, ranker):
        tracker = MagicMock()
        tracker.get_weights.return_value = {
            "momentum": 2.0,
            "value": 1.0,
            "quality": 1.0,
        }
        ranker.ic_tracker = tracker
        factors = ["momentum", "value", "quality"]
        weights = ranker._get_ic_weights(factors)
        assert weights["momentum"] == pytest.approx(0.5)
        assert weights["value"] == pytest.approx(0.25)


# ── SettlementGuard ───────────────────────────────────────────────────


class TestSettlementGuard:
    @pytest.fixture
    def guard(self):
        return SettlementGuard(total_cash=100_000.0)

    def test_initial_available(self, guard):
        assert guard.get_available_cash() == 100_000.0

    def test_record_sale_unsettled(self, guard):
        guard.record_sale(50_000.0, market="US", sale_date=date(2026, 1, 5))
        # Cash increases but funds are unsettled
        assert guard.total_cash == 150_000.0
        # Available should be less (unsettled portion)
        assert guard.get_available_cash(date(2026, 1, 5)) < 150_000.0

    def test_settlement_t1(self, guard):
        today = date(2026, 1, 5)
        guard.record_sale(50_000.0, market="US", sale_date=today)
        # After T+1, funds settle
        tomorrow = today + timedelta(days=1)
        assert guard.get_available_cash(tomorrow) == 150_000.0

    def test_record_purchase_deducts(self, guard):
        guard.record_purchase(30_000.0)
        assert guard.total_cash == 70_000.0

    def test_set_cash(self, guard):
        guard.set_cash(200_000.0)
        assert guard.total_cash == 200_000.0

    def test_get_unsettle_breakdown(self, guard):
        guard.record_sale(10_000.0, market="US", sale_date=date(2026, 1, 5))
        guard.record_sale(20_000.0, market="HK", sale_date=date(2026, 1, 5))
        breakdown = guard.get_unsettle_breakdown(date(2026, 1, 5))
        assert "US" in breakdown
        assert "HK" in breakdown


# ── EarningsBlackout ──────────────────────────────────────────────────


class TestEarningsBlackout:
    @pytest.fixture
    def eb(self):
        return EarningsBlackout()

    def test_no_earnings_not_blackout(self, eb):
        assert not eb.is_blackout("AAPL")

    def test_blackout_before_earnings(self, eb):
        earnings = date(2026, 1, 10)
        eb.set_earnings_date("AAPL", earnings)
        # 2 days before → blackout
        assert eb.is_blackout("AAPL", date(2026, 1, 8))
        assert eb.is_blackout("AAPL", date(2026, 1, 9))
        assert eb.is_blackout("AAPL", earnings)

    def test_not_blackout_after_earnings(self, eb):
        earnings = date(2026, 1, 10)
        eb.set_earnings_date("AAPL", earnings)
        assert not eb.is_blackout("AAPL", date(2026, 1, 11))

    def test_not_blackout_far_before(self, eb):
        earnings = date(2026, 1, 10)
        eb.set_earnings_date("AAPL", earnings)
        assert not eb.is_blackout("AAPL", date(2026, 1, 5))

    def test_get_blackout_symbols(self, eb):
        eb.set_earnings_date("AAPL", date(2026, 1, 10))
        eb.set_earnings_date("MSFT", date(2026, 2, 10))
        symbols = eb.get_blackout_symbols(date(2026, 1, 9))
        assert "AAPL" in symbols
        assert "MSFT" not in symbols

    def test_days_until_earnings(self, eb):
        eb.set_earnings_date("AAPL", date(2026, 1, 15))
        days = eb.days_until_earnings("AAPL", date(2026, 1, 10))
        assert days == 5

    def test_days_until_earnings_past(self, eb):
        eb.set_earnings_date("AAPL", date(2026, 1, 5))
        days = eb.days_until_earnings("AAPL", date(2026, 1, 10))
        assert days is None

    def test_auto_prune(self, eb):
        eb.set_earnings_date("AAPL", date(2026, 1, 1))
        eb._auto_prune(date(2026, 1, 10))
        assert eb.get_next_earnings("AAPL") is None


# ── VIXPositionScale ──────────────────────────────────────────────────


class TestVIXPositionScale:
    @pytest.fixture
    def scale(self):
        return VIXPositionScale()

    def test_low_vix_full_size(self, scale):
        assert scale.get_multiplier(12.0) == 1.0

    def test_normal_vix(self, scale):
        mult = scale.get_multiplier(20.0)
        assert 0.5 <= mult <= 1.0

    def test_high_vix_reduced(self, scale):
        mult = scale.get_multiplier(35.0)
        assert mult <= 0.5

    def test_extreme_vix_minimal(self, scale):
        mult = scale.get_multiplier(50.0)
        assert mult <= 0.2


# ── VolTargetSizer ────────────────────────────────────────────────────


class TestVolTargetSizer:
    @pytest.fixture
    def sizer(self):
        return VolTargetSizer()

    def test_calculate_basic(self, sizer):
        portfolio = {"total_value": 100_000.0, "n_positions": 10}
        frac = sizer.calculate("AAPL", portfolio, current_vol=0.25)
        assert 0.01 <= frac <= 0.20

    def test_calculate_low_vol_larger_position(self, sizer):
        portfolio = {"total_value": 100_000.0, "n_positions": 10}
        low_vol = sizer.calculate("AAPL", portfolio, current_vol=0.10)
        high_vol = sizer.calculate("AAPL", portfolio, current_vol=0.50)
        assert low_vol > high_vol

    def test_calculate_dollar(self, sizer):
        portfolio = {"total_value": 100_000.0, "n_positions": 10}
        dollar = sizer.calculate_dollar("AAPL", portfolio, current_vol=0.25)
        assert dollar > 0

    def test_get_portfolio_vol_estimate(self, sizer):
        positions = {"AAPL": 50_000.0, "MSFT": 50_000.0}
        vols = {"AAPL": 0.20, "MSFT": 0.25}
        vol = sizer.get_portfolio_vol_estimate(positions, vols)
        assert 0 < vol < 0.5

    def test_portfolio_vol_empty(self, sizer):
        assert sizer.get_portfolio_vol_estimate({}, {}) == 0.0


# ── StockRiskManager ──────────────────────────────────────────────────


class TestStockRiskManager:
    @pytest.fixture
    def rm(self):
        rm = StockRiskManager(account_value=100_000.0, initial_cash=1_000_000.0)
        return rm

    def _make_signal(self, **kwargs):
        defaults = {"symbol": "AAPL", "side": "buy", "quantity": 100, "price": 150.0}
        defaults.update(kwargs)
        return TradeSignal(**defaults)

    def test_approve_normal_trade(self, rm):
        decision = rm.pre_trade_check(self._make_signal())
        assert decision.approved

    def test_pdt_blocked(self, rm):
        signal = self._make_signal(is_day_trade=True, quantity=1, price=1.0)
        rm.account_value = 10_000.0  # Under 25K
        # Use up day trades
        for _ in range(3):
            rm.pdt_guard.record_day_trade()
        decision = rm.pre_trade_check(signal)
        assert not decision.approved
        assert "PDT" in decision.reason

    def test_earnings_blackout_blocked(self, rm):
        rm.earnings_blackout.set_earnings_date("AAPL", date.today())
        decision = rm.pre_trade_check(self._make_signal())
        assert not decision.approved
        assert "blackout" in decision.reason.lower()

    def test_insufficient_settled_funds(self, rm):
        rm.settlement_guard.set_cash(100.0)
        signal = self._make_signal(quantity=100, price=150.0)  # $15k needed
        decision = rm.pre_trade_check(signal)
        assert not decision.approved
        assert "Insufficient" in decision.reason

    def test_vix_scaling(self, rm):
        decision = rm.pre_trade_check(self._make_signal(), vix=35.0)
        assert decision.approved
        assert decision.position_multiplier < 1.0

    def test_sector_concentration_blocked(self, rm):
        rm.max_sector_concentration = 0.20
        rm.sector_exposure["Technology"] = 15_000.0
        signal = self._make_signal(quantity=100, price=150.0, sector="Technology")
        decision = rm.pre_trade_check(signal)
        assert not decision.approved
        assert "concentration" in decision.reason.lower()

    def test_update_account_value(self, rm):
        rm.update_account_value(200_000.0)
        assert rm.account_value == 200_000.0

    def test_update_sector_exposure(self, rm):
        rm.update_sector_exposure("Tech", 10_000.0)
        rm.update_sector_exposure("Tech", 5_000.0)
        assert rm.sector_exposure["Tech"] == 15_000.0


# ── MacroAnalyzer._build_summary ──────────────────────────────────────


class TestMacroAnalyzer:
    def test_build_summary_expansion(self):
        from src.research.macro_analyzer import MacroAnalyzer, MacroPhase

        summary = MacroAnalyzer._build_summary(
            MacroPhase.EXPANSION,
            fed_rate=2.0,
            spread=50.0,
            gdp=3.0,
            vix=12.0,
            credit=0.9,
        )
        assert "EXPANSION" in summary
        assert "2.00%" in summary

    def test_build_summary_contraction(self):
        from src.research.macro_analyzer import MacroAnalyzer, MacroPhase

        summary = MacroAnalyzer._build_summary(
            MacroPhase.CONTRACTION,
            fed_rate=5.5,
            spread=-30.0,
            gdp=-1.0,
            vix=35.0,
            credit=0.7,
        )
        assert "CONTRACTION" in summary
        assert "INVERTED" in summary

    def test_build_summary_none_values(self):
        from src.research.macro_analyzer import MacroAnalyzer, MacroPhase

        summary = MacroAnalyzer._build_summary(
            MacroPhase.TROUGH,
            fed_rate=None,
            spread=None,
            gdp=None,
            vix=None,
            credit=None,
        )
        assert "TROUGH" in summary
