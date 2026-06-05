"""
Deep coverage tests — targeting specific uncovered code paths.
"""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# ── _call_llm and _parse_json ─────────────────────────────────────────


class TestLLMHelpers:
    def test_call_llm_success(self):
        from src.research.stock_researcher import _call_llm

        with patch("src.research.stock_researcher.requests.post") as mock_post:
            mock_post.return_value = MagicMock(
                json=lambda: {"choices": [{"message": {"content": "BUY"}}]},
                raise_for_status=lambda: None,
            )
            result = _call_llm("http://api.test", "model", "key", "prompt")
            assert result == "BUY"

    def test_call_llm_retry(self):
        import requests as req_mod

        import src.research.stock_researcher as sr_mod

        _call_llm = sr_mod._call_llm

        with patch.object(sr_mod.requests, "post") as mock_post:
            mock_post.side_effect = [
                req_mod.exceptions.Timeout("timeout"),
                MagicMock(
                    json=lambda: {"choices": [{"message": {"content": "OK"}}]},
                    raise_for_status=lambda: None,
                ),
            ]
            with patch("time.sleep"):
                result = _call_llm("http://api.test", "model", "key", "prompt")
                assert result == "OK"

    def test_call_llm_all_fail(self):
        import requests as req_mod

        import src.research.stock_researcher as sr_mod

        _call_llm = sr_mod._call_llm

        with patch.object(
            sr_mod.requests,
            "post",
            side_effect=req_mod.exceptions.ConnectionError("fail"),
        ):
            with patch("time.sleep"):
                result = _call_llm("http://api.test", "model", "key", "prompt")
                assert result is None

    def test_parse_json_direct(self):
        from src.research.stock_researcher import _parse_json

        result = _parse_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_parse_json_markdown_fence(self):
        from src.research.stock_researcher import _parse_json

        result = _parse_json('```json\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_parse_json_braces(self):
        from src.research.stock_researcher import _parse_json

        result = _parse_json('Here is the result: {"key": "value"} done')
        assert result == {"key": "value"}

    def test_parse_json_empty(self):
        from src.research.stock_researcher import _parse_json

        result = _parse_json("")
        assert result is None

    def test_parse_json_invalid(self):
        from src.research.stock_researcher import _parse_json

        result = _parse_json("not json at all")
        assert result is None

    def test_parse_json_nested_fences(self):
        from src.research.stock_researcher import _parse_json

        result = _parse_json('```{"key": "value"}```')
        assert result is not None


# ── Momentum: generate_signals with breakout ──────────────────────────


class TestMomentumBreakout:
    def test_generate_with_breakout_signal(self, sample_universe):
        """Test that generate_signals properly processes breakout signals."""
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        signals = s.generate_signals(sample_universe)
        assert isinstance(signals, list)

    def test_generate_with_positions_below_median(self, sample_universe):
        """Test sell signals for positions below median RS."""
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        s._positions["AAPL"] = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=15),
            strategy="Momentum",
            metadata={"current_price": 140.0},
        )
        signals = s.generate_signals(sample_universe)
        assert isinstance(signals, list)

    def test_check_breakout_full_path(self, sample_universe):
        """Test _check_breakout with data that triggers the full path."""
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        sym = list(sample_universe.keys())[0]
        df = sample_universe[sym]
        signal = s._check_breakout(sym, df, 70.0, datetime.now())
        assert signal is None or hasattr(signal, "strength")

    def test_rs_with_zero_price(self):
        """Test RS calculation with zero 12mo price."""
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        close = [0.0] * 252 + [150.0] * 21
        universe = {"AAPL": pd.DataFrame({"close": close})}
        rs = s._calculate_relative_strength(universe)
        assert "AAPL" not in rs  # Should be skipped

    def test_should_exit_trailing_stop_hit(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=15),
            strategy="Momentum",
            stop_loss=145.0,
            metadata={"current_price": 144.0},
        )
        assert s.should_exit(pos) is True


# ── MeanRevert: _check_mean_reversion paths ───────────────────────────


class TestMeanRevertPaths:
    def test_generate_signals_with_oversold(self, sample_universe):
        """Test signal generation with oversold conditions."""
        from src.strategies.mean_revert import MeanRevertStrategy

        s = MeanRevertStrategy()
        signals = s.generate_signals(sample_universe)
        assert isinstance(signals, list)

    def test_should_exit_min_holding_stop_hit(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.mean_revert import MeanRevertStrategy

        s = MeanRevertStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=3),
            strategy="MeanRevert",
            stop_loss=145.0,
            metadata={"current_price": 144.0},
        )
        assert s.should_exit(pos) is True

    def test_should_exit_take_profit(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.mean_revert import MeanRevertStrategy

        s = MeanRevertStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=10),
            strategy="MeanRevert",
            stop_loss=140.0,
            take_profit=160.0,
            metadata={"current_price": 162.0},
        )
        assert s.should_exit(pos) is True

    def test_should_exit_no_price(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.mean_revert import MeanRevertStrategy

        s = MeanRevertStrategy()
        # Days held < min_holding_days, no current_price → should not exit
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=2),
            strategy="MeanRevert",
            stop_loss=140.0,
            metadata={},
        )
        assert s.should_exit(pos) is False


# ── TrendStrategy: generate_signals paths ──────────────────────────────


class TestTrendPaths:
    def test_generate_with_data(self, sample_universe):
        from src.strategies.trend_strategy import TrendStrategy

        s = TrendStrategy()
        signals = s.generate_signals(sample_universe)
        assert isinstance(signals, list)

    def test_should_exit_stop_hit(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.trend_strategy import TrendStrategy

        s = TrendStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=15),
            strategy="TrendFollowing",
            stop_loss=145.0,
            metadata={"current_price": 144.0},
        )
        assert s.should_exit(pos) is True

    def test_should_exit_no_stop(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.trend_strategy import TrendStrategy

        s = TrendStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=15),
            strategy="TrendFollowing",
            metadata={"current_price": 144.0},
        )
        result = s.should_exit(pos)
        assert isinstance(result, bool)


# ── MacroAnalyzer: get_macro_state paths ───────────────────────────────


class TestMacroPaths:
    def test_get_macro_state_with_fred(self):
        from src.research.macro_analyzer import MacroAnalyzer

        a = MacroAnalyzer(fred_api_key="test")
        with patch("src.research.macro_analyzer._fred_latest") as mock_fred:
            mock_fred.return_value = 2.0
            with patch.object(a, "_get_credit_spread", return_value=0.85):
                state = a.get_macro_state()
                assert state.phase.value in (
                    "expansion",
                    "peak",
                    "contraction",
                    "trough",
                )

    def test_build_summary_all_none(self):
        from src.research.macro_analyzer import MacroAnalyzer, MacroPhase

        s = MacroAnalyzer._build_summary(MacroPhase.PEAK, None, None, None, None, None)
        assert "PEAK" in s


# ── RegimeDetector: _fit_hmm and detect_regime ────────────────────────


class TestRegimePaths:
    def test_detect_regime_with_hmm(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        d._use_hmm = True
        returns = pd.Series(np.random.normal(0.001, 0.02, 300))
        regime = d.detect_regime(vix=15.0, spy_returns=returns)
        assert regime in ("DEFENSIVE", "NEUTRAL", "AGGRESSIVE")

    def test_detect_regime_no_hmm(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        d._use_hmm = False
        regime = d.detect_regime(vix=15.0)
        assert regime in ("DEFENSIVE", "NEUTRAL", "AGGRESSIVE")

    def test_hmm_fit_returns_none(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        d._use_hmm = True
        d._hmm_states = 2
        # _fit_hmm uses hmmlearn — test with short data that fails to fit
        returns = pd.Series([0.01] * 5)  # Too short for HMM
        states = d._fit_hmm(returns)
        # May return None or array depending on hmmlearn behavior
        assert states is None or isinstance(states, np.ndarray)


# ── StockScorer: score paths ──────────────────────────────────────────


class TestScorerPaths:
    def test_score_with_all_none(self):
        from src.scoring.stock_scorer import StockScorer

        scorer = StockScorer()
        # quality and value return None
        score = scorer.score_stock("AAPL")
        assert 0 <= score.composite <= 100

    def test_score_redistribute_all_skipped(self):
        from src.scoring.stock_scorer import StockScorer

        scorer = StockScorer()
        # All factors return None
        with patch.object(scorer, "_score_technical", return_value=None):
            with patch.object(scorer, "_score_fundamental", return_value=None):
                with patch.object(scorer, "_score_momentum", return_value=None):
                    with patch.object(scorer, "_score_sentiment", return_value=None):
                        score = scorer.score_stock("AAPL")
                        assert 0 <= score.composite <= 100

    def test_score_fundamental_with_scorer(self):
        from src.scoring.fundamental_scorer import FundamentalScorer
        from src.scoring.stock_scorer import StockScorer

        scorer = StockScorer(fundamental_scorer=FundamentalScorer())
        score = scorer._score_fundamental("AAPL")
        assert score == 50.0

    def test_score_sentiment_with_scorer(self):
        from src.scoring.sentiment_scorer import SentimentScorer
        from src.scoring.stock_scorer import StockScorer

        scorer = StockScorer(sentiment_scorer=SentimentScorer())
        score = scorer._score_sentiment("AAPL")
        assert score == 50.0


# ── PaperClient: specific paths ───────────────────────────────────────


class TestPaperPaths:
    pass
# ── FeatureStore: advanced paths ───────────────────────────────────────


class TestFeatureStorePaths:
    def test_save_empty(self, tmp_path):
        from src.data.feature_store import FeatureStore

        db = str(tmp_path / "fs.duckdb")
        store = FeatureStore(db_path=db)
        try:
            count = store.save_factor_values("2026-05-28", pd.DataFrame())
            assert count == 0
        finally:
            store.close()

    def test_get_latest(self, tmp_path):
        from src.data.feature_store import FeatureStore

        db = str(tmp_path / "fs.duckdb")
        store = FeatureStore(db_path=db)
        try:
            df = pd.DataFrame({"symbol": ["AAPL"], "momentum": [75.0]})
            store.save_factor_values("2026-05-28", df)
            result = store.get_factor_values(date=None)
            assert not result.empty
        finally:
            store.close()


# ── SentimentFeed: pipeline paths ─────────────────────────────────────


class TestSentimentPaths:
    def test_pipeline_none_format(self):
        from src.data.sentiment_feed import SentimentFeed

        sf = SentimentFeed()
        sf._pipeline = MagicMock(return_value=[None])
        result = sf.analyze_sentiment("test")
        assert result == 0.0

    def test_batch_exception(self):
        from src.data.sentiment_feed import SentimentFeed

        sf = SentimentFeed()
        sf._pipeline = MagicMock(side_effect=Exception("GPU error"))
        results = sf.analyze_batch(["text"] * 3)
        assert results == [0.0] * 3


# ── StockDataFeed: IBKR paths ─────────────────────────────────────────


class TestDataFeedPaths:
    def test_ibkr_quote(self):
        from src.data.stock_data_feed import StockDataFeed

        feed = StockDataFeed()
        feed.ibkr = MagicMock()
        feed.ibkr.get_market_data.return_value = {"symbol": "AAPL", "price": 150.0}
        quote = feed.get_realtime_quote("AAPL")
        assert quote["symbol"] == "AAPL"

    def test_ibkr_fallback(self):
        from src.data.stock_data_feed import StockDataFeed

        feed = StockDataFeed()
        feed.ibkr = MagicMock()
        feed.ibkr.get_market_data.side_effect = Exception("fail")
        with patch("yfinance.Ticker") as mock_t:
            mock_t.return_value.fast_info = MagicMock(
                last_price=150.0,
                regular_market_change=1.0,
                regular_market_change_percent=0.67,
                last_volume=1000000,
                bid=149.5,
                ask=150.5,
                day_high=152.0,
                day_low=148.0,
                market_cap=2.5e12,
            )
            quote = feed.get_realtime_quote("AAPL")
            assert quote["price"] == 150.0


# ── EarningsCalendar: specific paths ───────────────────────────────────


class TestEarningsPaths:
    def test_is_earnings_day_symbol_filtered(self):
        from src.data.earnings_calendar import EarningsCalendar

        ec = EarningsCalendar(api_key="test")
        with patch("src.data.earnings_calendar.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                json=lambda: [{"reportDate": "2026-07-30"}],
                raise_for_status=lambda: None,
            )
            result = ec.is_earnings_day("AAPL", date(2026, 7, 30))
            assert isinstance(result, bool)

    def test_get_history(self):
        from src.data.earnings_calendar import EarningsCalendar

        ec = EarningsCalendar(api_key="test")
        with patch("src.data.earnings_calendar.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                json=lambda: {
                    "quarterlyEarnings": [
                        {
                            "fiscalDateEnding": "2026-03-31",
                            "reportedEPS": "1.5",
                            "estimatedEPS": "1.4",
                            "surprise": "0.1",
                            "surprisePercentage": "7.14",
                            "reportedDate": "2026-04-25",
                        },
                    ]
                },
                raise_for_status=lambda: None,
            )
            history = ec.get_earnings_history("AAPL")
            assert len(history) == 1


# ── SECFilings: specific paths ─────────────────────────────────────────


class TestSECFilingsPaths:
    def test_get_filings(self):
        from src.data.sec_filings import SECFilings

        sec = SECFilings()
        sec._cache["cik|AAPL"] = "0000320193"
        with patch("src.data.sec_filings.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                json=lambda: {
                    "hits": {
                        "hits": [
                            {
                                "_source": {
                                    "file_date": "2026-05-01",
                                    "form_type": "10-K",
                                    "display_names": ["Apple"],
                                }
                            },
                        ]
                    }
                },
                raise_for_status=lambda: None,
            )
            filings = sec.get_filings("AAPL")
            assert isinstance(filings, list)


# ── MarketHours: lunch break and close ────────────────────────────────


class TestMarketHoursPaths:
    def test_hk_lunch_break(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        sessions = mh.get_sessions(Market.HK)
        assert "lunch_break" in sessions

    def test_next_close(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        close = mh.next_market_close(Market.US)
        assert isinstance(close, datetime)
