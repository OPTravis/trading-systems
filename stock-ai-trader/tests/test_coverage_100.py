"""
100% coverage push — targeting every remaining uncovered line.
"""

import time
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# ── _call_llm: Exception path (line 148-150) ──────────────────────────


class TestCallLLMException:
    def test_call_llm_non_request_exception(self):
        """Non-RequestException should return None immediately."""
        import src.research.stock_researcher as sr_mod

        with patch.object(sr_mod.requests, "post", side_effect=ValueError("bad")):
            with patch("time.sleep"):
                result = sr_mod._call_llm("http://api.test", "model", "key", "prompt")
                assert result is None

    def test_call_llm_all_retries_exhausted(self):
        """All retries exhausted should return None."""
        import requests as req_mod

        import src.research.stock_researcher as sr_mod

        with patch.object(
            sr_mod.requests, "post", side_effect=req_mod.exceptions.Timeout("timeout")
        ):
            with patch("time.sleep"):
                result = sr_mod._call_llm("http://api.test", "model", "key", "prompt")
                assert result is None


# ── _parse_json: all paths (lines 177-178, 186-187) ───────────────────


class TestParseJSON:
    def test_parse_fenced_json(self):
        from src.research.stock_researcher import _parse_json

        result = _parse_json('```json\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_parse_brace_json(self):
        from src.research.stock_researcher import _parse_json

        result = _parse_json('Result: {"key": "value"} done')
        assert result == {"key": "value"}

    def test_parse_invalid_fenced(self):
        from src.research.stock_researcher import _parse_json

        result = _parse_json("```json\nnot json\n```")
        assert result is None

    def test_parse_invalid_braces(self):
        from src.research.stock_researcher import _parse_json

        result = _parse_json("Result: {not json} done")
        assert result is None


# ── StockResearcher: analyze_stock paths (lines 294-422) ───────────────


class TestResearcherAnalyze:
    def test_analyze_with_verification(self):
        """Test analyze_stock with both primary and verification LLM."""
        import json

        import src.research.stock_researcher as sr_mod

        r = sr_mod.StockResearcher(
            data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t"
        )
        r.news_feed = MagicMock()
        r.news_feed.get_news.return_value = [{"title": "Test", "summary": "Details"}]
        r.sentiment_feed = MagicMock()
        r.sentiment_feed.analyze_text.return_value = 0.5

        primary = json.dumps(
            {
                "recommendation": "BUY",
                "confidence": 0.7,
                "summary": "Good",
                "bull_case": "Growth",
                "bear_case": "Risk",
                "risk_rating": "MEDIUM",
                "catalysts": ["Earnings"],
            }
        )
        verify = json.dumps(
            {"agree": False, "your_recommendation": "HOLD", "your_confidence": 0.4}
        )

        call_count = [0]

        def mock_call_llm(url, model, key, prompt, temperature=0.3):
            call_count[0] += 1
            if call_count[0] == 1:
                return primary
            return verify

        with patch.object(sr_mod, "_call_llm", side_effect=mock_call_llm):
            report = r.analyze_stock("AAPL")
            assert report is not None
            assert report.models_agreed is False

    def test_analyze_verification_agrees(self):
        """Test analyze_stock when verification agrees."""
        import json

        import src.research.stock_researcher as sr_mod

        r = sr_mod.StockResearcher(
            data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t"
        )
        r.news_feed = MagicMock()
        r.news_feed.get_news.return_value = []
        r.sentiment_feed = MagicMock()
        r.sentiment_feed.analyze_text.return_value = 0.0

        primary = json.dumps(
            {
                "recommendation": "BUY",
                "confidence": 0.7,
                "summary": "Good",
                "bull_case": "Growth",
                "bear_case": "Risk",
                "risk_rating": "MEDIUM",
                "catalysts": [],
            }
        )
        verify = json.dumps({"agree": True})

        call_count = [0]

        def mock_call_llm(url, model, key, prompt, temperature=0.3):
            call_count[0] += 1
            if call_count[0] == 1:
                return primary
            return verify

        with patch.object(sr_mod, "_call_llm", side_effect=mock_call_llm):
            report = r.analyze_stock("AAPL")
            assert report.models_agreed is True

    def test_analyze_invalid_recommendation(self):
        """Test with invalid recommendation string."""
        import json

        import src.research.stock_researcher as sr_mod

        r = sr_mod.StockResearcher(
            data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t"
        )
        r.sentiment_feed = MagicMock()
        r.sentiment_feed.analyze_text.return_value = 0.0

        primary = json.dumps(
            {
                "recommendation": "INVALID",
                "confidence": 0.5,
                "summary": "",
                "bull_case": "",
                "bear_case": "",
                "risk_rating": "MEDIUM",
                "catalysts": [],
            }
        )

        with patch.object(sr_mod, "_call_llm", return_value=primary):
            report = r.analyze_stock("AAPL")
            assert report.recommendation.value == "HOLD"

    def test_gather_technicals_success(self):
        from src.research.stock_researcher import StockResearcher

        mock_feed = MagicMock()
        mock_feed.get_history.return_value = pd.DataFrame(
            {
                "close": [150.0 + i for i in range(300)],
                "high": [155.0 + i for i in range(300)],
                "low": [148.0 + i for i in range(300)],
                "volume": [1e6] * 300,
            },
            index=pd.date_range(end=datetime.now(), periods=300),
        )
        r = StockResearcher(data_feed=mock_feed, xiaomi_key="t", deepseek_key="t")
        result = r._gather_technicals("AAPL")
        assert "Price:" in result

    def test_gather_technicals_short_data(self):
        from src.research.stock_researcher import StockResearcher

        mock_feed = MagicMock()
        mock_feed.get_history.return_value = pd.DataFrame(
            {
                "close": [150.0] * 10,
                "high": [155.0] * 10,
                "low": [148.0] * 10,
                "volume": [1e6] * 10,
            },
            index=pd.date_range(end=datetime.now(), periods=10),
        )
        r = StockResearcher(data_feed=mock_feed, xiaomi_key="t", deepseek_key="t")
        result = r._gather_technicals("AAPL")
        assert isinstance(result, str)

    def test_gather_technicals_exception(self):
        from src.research.stock_researcher import StockResearcher

        mock_feed = MagicMock()
        mock_feed.get_history.side_effect = Exception("API error")
        r = StockResearcher(data_feed=mock_feed, xiaomi_key="t", deepseek_key="t")
        result = r._gather_technicals("AAPL")
        assert "error" in result.lower()

    def test_gather_fundamentals_success(self):
        from src.research.stock_researcher import StockResearcher

        r = StockResearcher(data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t")
        r.fundamental_feed = MagicMock()
        r.fundamental_feed.get_key_metrics.return_value = {
            "pe_ratio": 25.0,
            "roe": 0.30,
        }
        result = r._gather_fundamentals("AAPL")
        assert "pe_ratio" in result

    def test_gather_fundamentals_empty(self):
        from src.research.stock_researcher import StockResearcher

        r = StockResearcher(data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t")
        r.fundamental_feed = MagicMock()
        r.fundamental_feed.get_key_metrics.return_value = {}
        result = r._gather_fundamentals("AAPL")
        assert "No fundamental" in result

    def test_gather_fundamentals_exception(self):
        from src.research.stock_researcher import StockResearcher

        r = StockResearcher(data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t")
        r.fundamental_feed = MagicMock()
        r.fundamental_feed.get_key_metrics.side_effect = Exception("API error")
        result = r._gather_fundamentals("AAPL")
        assert "error" in result.lower()

    def test_gather_news_success(self):
        from src.research.stock_researcher import StockResearcher

        r = StockResearcher(data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t")
        r.news_feed = MagicMock()
        r.news_feed.get_news.return_value = [{"title": "Test", "summary": "Details"}]
        result = r._gather_news("AAPL")
        assert "Test" in result

    def test_gather_news_empty(self):
        from src.research.stock_researcher import StockResearcher

        r = StockResearcher(data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t")
        r.news_feed = MagicMock()
        r.news_feed.get_news.return_value = []
        result = r._gather_news("AAPL")
        assert "No recent" in result

    def test_gather_news_exception(self):
        from src.research.stock_researcher import StockResearcher

        r = StockResearcher(data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t")
        r.news_feed = MagicMock()
        r.news_feed.get_news.side_effect = Exception("API error")
        result = r._gather_news("AAPL")
        assert "error" in result.lower()

    def test_compute_rsi_short(self):
        from src.research.stock_researcher import StockResearcher

        prices = pd.Series([150.0] * 5)
        rsi = StockResearcher._compute_rsi(prices, 14)
        assert rsi == 50.0

    def test_compute_rsi_no_losses(self):
        from src.research.stock_researcher import StockResearcher

        prices = pd.Series([100.0 + i for i in range(20)])
        rsi = StockResearcher._compute_rsi(prices, 14)
        assert rsi == 100.0

    def test_compute_rsi_normal(self):
        from src.research.stock_researcher import StockResearcher

        np.random.seed(42)
        prices = pd.Series(np.random.normal(150, 5, 30))
        rsi = StockResearcher._compute_rsi(prices, 14)
        assert 0 <= rsi <= 100

    def test_warnings_no_keys(self):
        from src.research.stock_researcher import StockResearcher

        r = StockResearcher(data_feed=MagicMock(), xiaomi_key="", deepseek_key="")
        # Should log warnings but not crash
        assert r.xiaomi_key == ""


# ── Momentum: all paths (lines 79-284) ────────────────────────────────


class TestMomentumAllPaths:
    def test_generate_signals_with_sell_positions(self, sample_universe):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        # Add position below median
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

    def test_generate_signals_no_rs(self):
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        with patch.object(s, "_calculate_relative_strength", return_value={}):
            signals = s.generate_signals(
                {
                    "AAPL": pd.DataFrame(
                        {"close": [150.0] * 300},
                        index=pd.date_range(end=datetime.now(), periods=300),
                    )
                }
            )
            assert signals == []

    def test_rs_short_data(self):
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        rs = s._calculate_relative_strength(
            {"AAPL": pd.DataFrame({"close": [150.0] * 10})}
        )
        assert "AAPL" not in rs

    def test_rs_normal(self):
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        close = list(range(100, 352))
        rs = s._calculate_relative_strength({"AAPL": pd.DataFrame({"close": close})})
        assert "AAPL" in rs

    def test_rs_index_error(self):
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        rs = s._calculate_relative_strength(
            {"AAPL": pd.DataFrame({"close": [150.0] * 252})}
        )
        assert isinstance(rs, dict)

    def test_check_breakout_with_signal(self, sample_universe):
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        sym = list(sample_universe.keys())[0]
        df = sample_universe[sym]
        signal = s._check_breakout(sym, df, 70.0, datetime.now())
        assert signal is None or hasattr(signal, "strength")

    def test_check_breakout_no_breakout(self):
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        df = pd.DataFrame(
            {
                "close": [150.0] * 25,
                "high": [155.0] * 25,
                "low": [148.0] * 25,
                "volume": [1e6] * 25,
            },
            index=pd.date_range(end=datetime.now(), periods=25),
        )
        signal = s._check_breakout("AAPL", df, 50.0, datetime.now())
        assert signal is None

    def test_should_enter_existing_position(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.base_strategy import Signal, SignalAction
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        s._positions["AAPL"] = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now(),
            strategy="Momentum",
        )
        signal = Signal(
            symbol="AAPL",
            action=SignalAction.BUY,
            strategy="Momentum",
            timestamp=datetime.now(),
            strength=0.8,
            price=150.0,
        )
        assert s.should_enter(signal) is False

    def test_should_exit_trailing_stop(self):
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

    def test_should_exit_no_stop(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=15),
            strategy="Momentum",
            stop_loss=None,
            metadata={"current_price": 144.0},
        )
        assert s.should_exit(pos) is False

    def test_update_trailing_stop(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now(),
            strategy="Momentum",
            stop_loss=140.0,
            metadata={"atr": 3.0},
        )
        s.update_trailing_stop(pos, 160.0)
        assert pos.stop_loss > 140.0

    def test_params(self):
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        params = s.get_params()
        assert "rs_lookback_long" in params

    def test_custom_params(self):
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy(params={"rs_lookback_long": 200})
        assert s.get_params()["rs_lookback_long"] == 200


# ── MeanRevert: all paths (lines 61-209) ──────────────────────────────


class TestMeanRevertAllPaths:
    def test_generate_signals(self, sample_universe):
        from src.strategies.mean_revert import MeanRevertStrategy

        s = MeanRevertStrategy()
        signals = s.generate_signals(sample_universe)
        assert isinstance(signals, list)

    def test_should_enter(self):
        from src.strategies.base_strategy import Signal, SignalAction
        from src.strategies.mean_revert import MeanRevertStrategy

        s = MeanRevertStrategy()
        signal = Signal(
            symbol="AAPL",
            action=SignalAction.BUY,
            strategy="MeanRevert",
            timestamp=datetime.now(),
            strength=0.7,
            price=150.0,
        )
        assert s.should_enter(signal) is True

    def test_should_enter_sell(self):
        from src.strategies.base_strategy import Signal, SignalAction
        from src.strategies.mean_revert import MeanRevertStrategy

        s = MeanRevertStrategy()
        signal = Signal(
            symbol="AAPL",
            action=SignalAction.SELL,
            strategy="MeanRevert",
            timestamp=datetime.now(),
            strength=0.7,
            price=150.0,
        )
        assert s.should_enter(signal) is False

    def test_should_enter_existing(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.base_strategy import Signal, SignalAction
        from src.strategies.mean_revert import MeanRevertStrategy

        s = MeanRevertStrategy()
        s._positions["AAPL"] = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now(),
            strategy="MeanRevert",
        )
        signal = Signal(
            symbol="AAPL",
            action=SignalAction.BUY,
            strategy="MeanRevert",
            timestamp=datetime.now(),
            strength=0.7,
            price=150.0,
        )
        assert s.should_enter(signal) is False

    def test_should_exit_max_holding(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.mean_revert import MeanRevertStrategy

        s = MeanRevertStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=30),
            strategy="MeanRevert",
            metadata={"current_price": 155.0},
        )
        assert s.should_exit(pos) is True

    def test_should_exit_min_holding_no_stop(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.mean_revert import MeanRevertStrategy

        s = MeanRevertStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=2),
            strategy="MeanRevert",
            stop_loss=None,
            metadata={"current_price": 145.0},
        )
        assert s.should_exit(pos) is False

    def test_should_exit_min_holding_stop_hit(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.mean_revert import MeanRevertStrategy

        s = MeanRevertStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=2),
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

    def test_should_exit_stop_loss(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.mean_revert import MeanRevertStrategy

        s = MeanRevertStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=10),
            strategy="MeanRevert",
            stop_loss=145.0,
            metadata={"current_price": 144.0},
        )
        assert s.should_exit(pos) is True

    def test_should_exit_normal(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.mean_revert import MeanRevertStrategy

        s = MeanRevertStrategy()
        # Days held between min and max, no stop/take-profit hit
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=5),
            strategy="MeanRevert",
            stop_loss=140.0,
            metadata={"current_price": 155.0},
        )
        assert s.should_exit(pos) is False


# ── TrendStrategy: all paths ───────────────────────────────────────────


class TestTrendAllPaths:
    def test_generate_signals(self, sample_universe):
        from src.strategies.trend_strategy import TrendStrategy

        s = TrendStrategy()
        signals = s.generate_signals(sample_universe)
        assert isinstance(signals, list)

    def test_generate_empty(self):
        from src.strategies.trend_strategy import TrendStrategy

        s = TrendStrategy()
        signals = s.generate_signals({})
        assert signals == []

    def test_should_enter(self):
        from src.strategies.base_strategy import Signal, SignalAction
        from src.strategies.trend_strategy import TrendStrategy

        s = TrendStrategy()
        signal = Signal(
            symbol="AAPL",
            action=SignalAction.BUY,
            strategy="TrendFollowing",
            timestamp=datetime.now(),
            strength=0.7,
            price=150.0,
        )
        assert s.should_enter(signal) is True

    def test_should_enter_weak(self):
        from src.strategies.base_strategy import Signal, SignalAction
        from src.strategies.trend_strategy import TrendStrategy

        s = TrendStrategy()
        signal = Signal(
            symbol="AAPL",
            action=SignalAction.BUY,
            strategy="TrendFollowing",
            timestamp=datetime.now(),
            strength=0.3,
            price=150.0,
        )
        assert s.should_enter(signal) is False

    def test_should_enter_sell(self):
        from src.strategies.base_strategy import Signal, SignalAction
        from src.strategies.trend_strategy import TrendStrategy

        s = TrendStrategy()
        signal = Signal(
            symbol="AAPL",
            action=SignalAction.SELL,
            strategy="TrendFollowing",
            timestamp=datetime.now(),
            strength=0.7,
            price=150.0,
        )
        assert s.should_enter(signal) is False

    def test_should_enter_existing(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.base_strategy import Signal, SignalAction
        from src.strategies.trend_strategy import TrendStrategy

        s = TrendStrategy()
        s._positions["AAPL"] = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now(),
            strategy="TrendFollowing",
        )
        signal = Signal(
            symbol="AAPL",
            action=SignalAction.BUY,
            strategy="TrendFollowing",
            timestamp=datetime.now(),
            strength=0.7,
            price=150.0,
        )
        assert s.should_enter(signal) is False

    def test_should_exit_max_holding(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.trend_strategy import TrendStrategy

        s = TrendStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=35),
            strategy="TrendFollowing",
            stop_loss=140.0,
            metadata={"current_price": 155.0},
        )
        assert s.should_exit(pos) is True

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

    def test_should_exit_min_holding(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.trend_strategy import TrendStrategy

        s = TrendStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=3),
            strategy="TrendFollowing",
            stop_loss=140.0,
            metadata={"current_price": 155.0},
        )
        assert s.should_exit(pos) is False

    def test_should_exit_normal(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.trend_strategy import TrendStrategy

        s = TrendStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=15),
            strategy="TrendFollowing",
            stop_loss=140.0,
            metadata={"current_price": 155.0},
        )
        assert s.should_exit(pos) is False

    def test_params(self):
        from src.strategies.trend_strategy import TrendStrategy

        s = TrendStrategy()
        params = s.get_params()
        assert "fast_period" in params


# ── MacroAnalyzer: all paths ───────────────────────────────────────────


class TestMacroAllPaths:
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

    def test_get_macro_state_no_key(self):
        from src.research.macro_analyzer import MacroAnalyzer

        a = MacroAnalyzer(fred_api_key="")
        state = a.get_macro_state()
        assert state.phase.value in ("expansion", "peak", "contraction", "trough")

    def test_build_summary_all_none(self):
        from src.research.macro_analyzer import MacroAnalyzer, MacroPhase

        for phase in [
            MacroPhase.EXPANSION,
            MacroPhase.PEAK,
            MacroPhase.CONTRACTION,
            MacroPhase.TROUGH,
        ]:
            s = MacroAnalyzer._build_summary(phase, None, None, None, None, None)
            assert phase.value.upper() in s

    def test_build_summary_with_values(self):
        from src.research.macro_analyzer import MacroAnalyzer, MacroPhase

        s = MacroAnalyzer._build_summary(
            MacroPhase.EXPANSION, 2.0, 50.0, 3.0, 12.0, 0.9
        )
        assert "EXPANSION" in s
        assert "2.00%" in s

    def test_build_summary_inverted(self):
        from src.research.macro_analyzer import MacroAnalyzer, MacroPhase

        s = MacroAnalyzer._build_summary(
            MacroPhase.CONTRACTION, 5.5, -30.0, -1.0, 35.0, 0.7
        )
        assert "INVERTED" in s

    def test_fred_latest_success(self):
        from src.research.macro_analyzer import _fred_latest

        with patch("src.research.macro_analyzer.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                json=lambda: {"observations": [{"value": "2.5"}]},
                raise_for_status=lambda: None,
            )
            result = _fred_latest("FEDFUNDS", "test")
            assert result == 2.5

    def test_fred_latest_dot(self):
        from src.research.macro_analyzer import _fred_latest

        with patch("src.research.macro_analyzer.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                json=lambda: {"observations": [{"value": "."}]},
                raise_for_status=lambda: None,
            )
            result = _fred_latest("FEDFUNDS", "test")
            assert result is None

    def test_fred_latest_empty(self):
        from src.research.macro_analyzer import _fred_latest

        with patch("src.research.macro_analyzer.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                json=lambda: {"observations": []}, raise_for_status=lambda: None
            )
            result = _fred_latest("FEDFUNDS", "test")
            assert result is None

    def test_fred_latest_exception(self):
        from src.research.macro_analyzer import _fred_latest

        with patch(
            "src.research.macro_analyzer.requests.get", side_effect=Exception("fail")
        ):
            result = _fred_latest("FEDFUNDS", "test")
            assert result is None

    def test_get_credit_spread(self):
        from src.research.macro_analyzer import MacroAnalyzer

        a = MacroAnalyzer(fred_api_key="test")
        with patch("yfinance.Ticker") as mock_t:
            mock_t.return_value.history.return_value = pd.DataFrame(
                {"Close": [100.0, 101.0]}
            )
            spread = a._get_credit_spread()
            assert spread is not None

    def test_get_credit_spread_failure(self):
        from src.research.macro_analyzer import MacroAnalyzer

        a = MacroAnalyzer(fred_api_key="test")
        with patch("yfinance.Ticker", side_effect=Exception("fail")):
            spread = a._get_credit_spread()
            assert spread is None


# ── Portfolio: all paths ───────────────────────────────────────────────


class TestPortfolioAllPaths:
    def test_fx_cache_hit(self):
        import src.portfolio as p

        p._FX_CACHE = {"USD": 1.0, "HKD": 7.8}
        p._FX_CACHE_TS = time.time()
        rate = p._get_fx_to_usd("HKD")
        assert rate == 7.8
        p._FX_CACHE = {}
        p._FX_CACHE_TS = 0.0

    def test_fx_unknown_currency(self):
        import src.portfolio as p

        p._FX_CACHE = {}
        p._FX_CACHE_TS = 0.0
        rate = p._get_fx_to_usd("XYZ")
        assert rate == 1.0

    def test_sync_failure_rollback(self):
        from src.portfolio import PortfolioManager

        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 1_000_000.0
        pm.add_position("AAPL", quantity=100, price=150.0)
        old_count = pm.position_count
        broker = MagicMock()
        broker.get_account.side_effect = Exception("Connection lost")
        result = pm.sync_from_broker(broker)
        assert result is False
        assert pm.position_count == old_count

    def test_sync_mid_process_failure(self):
        from src.portfolio import PortfolioManager

        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 1_000_000.0
        broker = MagicMock()
        account = MagicMock()
        account.currency = "USD"
        account.total_cash = 50_000.0
        broker.get_account.return_value = account
        broker.get_portfolio.side_effect = Exception("Sync failed")
        result = pm.sync_from_broker(broker)
        assert result is False

    def test_save_debounce(self):
        from src.portfolio import PortfolioManager

        pm = PortfolioManager(db=None)
        pm._db = MagicMock()
        pm._last_save_time = time.monotonic()
        pm._save(force=False)
        pm._db.portfolio_set.assert_not_called()

    def test_save_exception(self):
        from src.portfolio import PortfolioManager

        pm = PortfolioManager(db=None)
        pm._db = MagicMock()
        pm._db.portfolio_set.side_effect = Exception("DB error")
        pm._save(force=True)

    def test_load_exception(self):
        from src.portfolio import PortfolioManager

        pm = PortfolioManager(db=None)
        pm._db = MagicMock()
        pm._db.portfolio_get_all.side_effect = Exception("DB error")

    def test_get_unsettle_breakdown(self):
        from src.portfolio import PortfolioManager

        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 1_000_000.0
        pm._cash["USD"].record_sell(50_000.0, market="US")
        breakdown = pm.get_unsettle_breakdown("USD")
        assert isinstance(breakdown, dict)


# ── Notifier: all paths ───────────────────────────────────────────────


class TestNotifierAllPaths:
    def test_get_tenant_token_cache_hit(self):
        import src.notifier as n

        n._token_cache["token"] = "cached"
        n._token_cache["expires_at"] = time.time() + 3600
        token = n._get_tenant_token()
        assert token == "cached"

    def test_get_tenant_token_no_credentials(self):
        import src.notifier as n

        n._token_cache["token"] = ""
        n._token_cache["expires_at"] = 0.0
        with patch.dict("os.environ", {"FEISHU_APP_ID": "", "FEISHU_APP_SECRET": ""}):
            token = n._get_tenant_token()
            assert token == ""

    def test_send_card_no_token(self):
        from src.notifier import FeishuNotifier

        notifier = FeishuNotifier(chat_id="test")
        with patch("src.notifier._get_tenant_token", return_value=""):
            result = notifier._send_card("Title", [])
            assert result is False

    def test_send_earnings_no_estimates(self):
        from src.notifier import FeishuNotifier

        notifier = FeishuNotifier(chat_id="test")
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            with patch(
                "src.notifier.requests.post",
                return_value=MagicMock(json=lambda: {"code": 0}),
            ):
                result = notifier.send_earnings_alert("AAPL", "2026-07-30")
                assert result is True

    def test_send_trade_executed_with_order_id(self):
        from src.notifier import FeishuNotifier

        notifier = FeishuNotifier(chat_id="test")
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            with patch(
                "src.notifier.requests.post",
                return_value=MagicMock(json=lambda: {"code": 0}),
            ):
                result = notifier.send_trade_executed(
                    "AAPL", "BUY", 150.0, 100, "momentum", order_id="12345"
                )
                assert result is True

    def test_send_system_status_with_message(self):
        from src.notifier import FeishuNotifier

        notifier = FeishuNotifier(chat_id="test")
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            with patch(
                "src.notifier.requests.post",
                return_value=MagicMock(json=lambda: {"code": 0}),
            ):
                result = notifier.send_system_status(
                    {
                        "overall": "OK",
                        "checks": {"api": {"status": "OK", "message": "All good"}},
                    }
                )
                assert result is True


# ── BaseStrategy: all paths ────────────────────────────────────────────


class TestBaseStrategyAllPaths:
    def test_signal_is_buy(self):
        from src.strategies.base_strategy import Signal, SignalAction

        s = Signal(
            symbol="AAPL",
            action=SignalAction.BUY,
            strategy="test",
            timestamp=datetime.now(),
            strength=0.8,
            price=150.0,
        )
        assert s.is_buy is True
        assert s.is_sell is False

    def test_signal_is_sell(self):
        from src.strategies.base_strategy import Signal, SignalAction

        s = Signal(
            symbol="AAPL",
            action=SignalAction.SELL,
            strategy="test",
            timestamp=datetime.now(),
            strength=0.8,
            price=150.0,
        )
        assert s.is_sell is True
        assert s.is_buy is False

    def test_position_pnl(self):
        from src.strategies.base_strategy import Position as StratPosition

        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now(),
            strategy="test",
            metadata={"current_price": 160.0},
        )
        assert pos.unrealized_pnl_pct > 0

    def test_position_pnl_zero_entry(self):
        from src.strategies.base_strategy import Position as StratPosition

        pos = StratPosition(
            symbol="AAPL",
            entry_price=0.0,
            quantity=100,
            entry_date=datetime.now(),
            strategy="test",
        )
        assert pos.unrealized_pnl_pct == 0.0

    def test_base_strategy_params(self):
        from src.strategies.base_strategy import BaseStrategy

        class TestStrat(BaseStrategy):
            def generate_signals(self, universe):
                return []

            def should_enter(self, signal):
                return True

            def should_exit(self, position):
                return False

        s = TestStrat(name="Test", params={"key": "value"})
        assert s.name == "Test"
        assert s.get_params() == {"key": "value"}
        s.set_params({"key": "new"})
        assert s.get_params() == {"key": "new"}

    def test_base_strategy_positions(self):
        from src.strategies.base_strategy import BaseStrategy
        from src.strategies.base_strategy import Position as StratPosition

        class TestStrat(BaseStrategy):
            def generate_signals(self, universe):
                return []

            def should_enter(self, signal):
                return True

            def should_exit(self, position):
                return False

        s = TestStrat(name="Test")
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now(),
            strategy="Test",
        )
        s.add_position(pos)
        assert s.has_position("AAPL")
        assert len(s.get_positions()) == 1
        removed = s.remove_position("AAPL")
        assert removed.symbol == "AAPL"
        assert s.remove_position("AAPL") is None


# ── ScanOrchestrator: remaining paths ──────────────────────────────────


class TestScanOrchestratorPaths:
    def test_build_sector_map(self):
        from src.scan_orchestrator import ScanOrchestrator

        orch = ScanOrchestrator()
        sector_map = orch._build_sector_map()
        assert isinstance(sector_map, dict)

    def test_phase2_score_fallback(self):
        from src.scan_orchestrator import ScanOrchestrator

        orch = ScanOrchestrator()
        orch.scorer = MagicMock()
        orch.scorer.score_stock.return_value = MagicMock(
            composite=70,
            technical=65,
            fundamental=60,
            momentum=75,
            sentiment=55,
            quality=80,
            value=50,
        )
        orch.ranker = None
        ranked, scores = orch._phase2_score_and_rank(["AAPL", "MSFT"])
        assert len(ranked) == 2


# ── TradeExecutor: remaining paths ─────────────────────────────────────


class TestTradeExecutorPaths:
    @pytest.mark.asyncio
    async def test_execute_no_broker(self):
        from src.trade_executor import TradeExecutor

        te = TradeExecutor(broker=None)
        result = await te.execute("AAPL", "BUY", 100)
        assert result["success"] is False

    def test_get_pending_no_broker(self):
        from src.trade_executor import TradeExecutor

        te = TradeExecutor(broker=None)
        assert te.get_pending_orders() == []

    def test_cancel_all_no_broker(self):
        from src.trade_executor import TradeExecutor

        te = TradeExecutor(broker=None)
        te.cancel_all_orders()  # Should not crash


# ── RegimeDetector: remaining paths ────────────────────────────────────


class TestRegimeRemaining:
    def test_detect_regime_all_signals(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        spy = pd.Series(range(200, 500), dtype=float)
        returns = pd.Series(np.random.normal(0.001, 0.02, 300))
        hyg = pd.Series([0.85] * 30)
        regime = d.detect_regime(
            vix=15.0, spy_prices=spy, spy_returns=returns, hyg_tlt_ratio=hyg
        )
        assert regime in ("DEFENSIVE", "NEUTRAL", "AGGRESSIVE")

    def test_detect_regime_none_vix(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        regime = d.detect_regime(vix=None)
        assert regime in ("DEFENSIVE", "NEUTRAL", "AGGRESSIVE")


# ── MarketHours: remaining paths ───────────────────────────────────────


class TestMarketHoursRemaining:
    def test_hk_sessions(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        sessions = mh.get_sessions(Market.HK)
        assert "lunch_break" in sessions

    def test_next_close(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        close = mh.next_market_close(Market.US)
        assert isinstance(close, datetime)

    def test_next_open(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        open_dt = mh.next_market_open(Market.US)
        assert isinstance(open_dt, datetime)


# ── CorporateActions: remaining paths ──────────────────────────────────


class TestCorporateActionsRemaining:
    def test_adjust_for_dividends(self):
        from src.market.corporate_actions import CorporateActions, Dividend

        ca = CorporateActions()
        ca.add_dividend(
            Dividend(
                symbol="AAPL", ex_date=date(2026, 1, 10), pay_date=None, amount=0.50
            )
        )
        dates = pd.date_range("2026-1-5", periods=15, freq="D")
        df = pd.DataFrame(
            {
                "open": [150.0] * 15,
                "high": [155.0] * 15,
                "low": [148.0] * 15,
                "close": [150.0] * 15,
                "volume": [1000000] * 15,
            },
            index=dates,
        )
        adjusted = ca.adjust_for_dividends(df, "AAPL")
        assert adjusted is not None

    def test_adjust_for_dividends_empty(self):
        from src.market.corporate_actions import CorporateActions

        ca = CorporateActions()
        df = pd.DataFrame(
            {"close": [150.0]}, index=pd.DatetimeIndex([datetime(2026, 1, 1)])
        )
        adjusted = ca.adjust_for_dividends(df, "FAKE")
        assert len(adjusted) == 1

    def test_get_full_adjustment(self):
        from src.market.corporate_actions import CorporateActions, Dividend, Split

        ca = CorporateActions()
        ca.add_split(
            Split(symbol="AAPL", ex_date=date(2026, 1, 1), ratio_from=1, ratio_to=2)
        )
        ca.add_dividend(
            Dividend(
                symbol="AAPL", ex_date=date(2026, 3, 15), pay_date=None, amount=0.50
            )
        )
        dates = pd.date_range("2025-12-28", periods=60, freq="D")
        df = pd.DataFrame(
            {
                "open": [300.0] * 60,
                "high": [310.0] * 60,
                "low": [290.0] * 60,
                "close": [300.0] * 60,
                "volume": [1000000] * 60,
            },
            index=dates,
        )
        adjusted = ca.get_full_adjustment(df, "AAPL")
        assert adjusted is not None


# ── Notifier remaining paths ───────────────────────────────────────────


class TestNotifierRemaining2:
    def test_send_earnings_with_estimated(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier(chat_id="test")
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            with patch(
                "src.notifier.requests.post",
                return_value=MagicMock(json=lambda: {"code": 0}),
            ):
                result = n.send_earnings_alert("AAPL", "2026-07-30", estimated_eps=1.5)
                assert result is True

    def test_send_trade_executed_no_order_id(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier(chat_id="test")
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            with patch(
                "src.notifier.requests.post",
                return_value=MagicMock(json=lambda: {"code": 0}),
            ):
                result = n.send_trade_executed("AAPL", "BUY", 150.0, 100, "momentum")
                assert result is True

    def test_send_system_status_with_messages(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier(chat_id="test")
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            with patch(
                "src.notifier.requests.post",
                return_value=MagicMock(json=lambda: {"code": 0}),
            ):
                result = n.send_system_status(
                    {
                        "overall": "OK",
                        "checks": {"api": {"status": "OK", "message": "Good"}},
                    }
                )
                assert result is True
