"""
Coverage push — targeting every remaining uncovered line.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.portfolio import PortfolioManager

# ── AlpacaClient (49%) ────────────────────────────────────────────────


class TestAlpacaClient:
    def test_stub_methods(self):
# [REMOVED] AlpacaClient deleted — analysis-only refactor

        assert hasattr(AlpacaClient, "connect")
        assert hasattr(AlpacaClient, "disconnect")
        assert hasattr(AlpacaClient, "is_connected")
        assert hasattr(AlpacaClient, "get_market_data")
        assert hasattr(AlpacaClient, "get_historical_bars")
        assert hasattr(AlpacaClient, "get_account")
        assert hasattr(AlpacaClient, "get_positions")
        assert hasattr(AlpacaClient, "get_portfolio")
        assert hasattr(AlpacaClient, "place_order")
        assert hasattr(AlpacaClient, "cancel_order")
        assert hasattr(AlpacaClient, "get_open_orders")
        assert hasattr(AlpacaClient, "get_contract_details")
        assert hasattr(AlpacaClient, "qualify_contract")


# ── StockResearcher (63%) ─────────────────────────────────────────────


class TestStockResearcher:
    def test_gather_technicals_with_data(self):
        from src.research.stock_researcher import StockResearcher

        mock_feed = MagicMock()
        mock_feed.get_historical.return_value = pd.DataFrame(
            {
                "close": [150.0] * 30,
                "high": [155.0] * 30,
                "low": [148.0] * 30,
                "volume": [1e6] * 30,
            },
            index=pd.date_range(end=datetime.now(), periods=30),
        )
        r = StockResearcher(data_feed=mock_feed, xiaomi_key="t", deepseek_key="t")
        result = r._gather_technicals("AAPL")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_gather_technicals_empty(self):
        from src.research.stock_researcher import StockResearcher

        mock_feed = MagicMock()
        mock_feed.get_historical.return_value = pd.DataFrame()
        r = StockResearcher(data_feed=mock_feed, xiaomi_key="t", deepseek_key="t")
        result = r._gather_technicals("AAPL")
        assert isinstance(result, str)

    def test_gather_fundamentals_with_data(self):
        from src.research.stock_researcher import StockResearcher

        r = StockResearcher(data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t")
        r.fundamental_feed = MagicMock()
        r.fundamental_feed.get_key_metrics.return_value = {
            "pe_ratio": 25.0,
            "roe": 0.30,
        }
        r.fundamental_feed.get_company_profile.return_value = {
            "companyName": "Apple",
            "sector": "Tech",
        }
        result = r._gather_fundamentals("AAPL")
        assert isinstance(result, str)

    def test_gather_fundamentals_no_feed(self):
        from src.research.stock_researcher import StockResearcher

        r = StockResearcher(data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t")
        r.fundamental_feed = None
        result = r._gather_fundamentals("AAPL")
        assert isinstance(result, str)

    def test_gather_news_with_data(self):
        from src.research.stock_researcher import StockResearcher

        r = StockResearcher(data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t")
        r.news_feed = MagicMock()
        r.news_feed.get_news.return_value = [
            {"title": "Test", "summary": "Details", "source": "Reuters"}
        ]
        result = r._gather_news("AAPL")
        assert isinstance(result, str)

    def test_gather_news_no_feed(self):
        from src.research.stock_researcher import StockResearcher

        r = StockResearcher(data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t")
        r.news_feed = None
        result = r._gather_news("AAPL")
        assert isinstance(result, str)

    def test_compute_sentiment_with_feed(self):
        from src.research.stock_researcher import StockResearcher

        r = StockResearcher(data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t")
        r.sentiment_feed = MagicMock()
        r.sentiment_feed.analyze_text.return_value = 0.5
        score = r._compute_sentiment("Strong earnings")
        assert score == 0.5

    def test_compute_sentiment_no_feed(self):
        from src.research.stock_researcher import StockResearcher

        r = StockResearcher(data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t")
        r.sentiment_feed = None
        score = r._compute_sentiment("test")
        assert score == 0.0

    def test_compute_sentiment_error(self):
        from src.research.stock_researcher import StockResearcher

        r = StockResearcher(data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t")
        r.sentiment_feed = MagicMock()
        r.sentiment_feed.analyze_text.side_effect = Exception("fail")
        score = r._compute_sentiment("test")
        assert score == 0.0


# ── Momentum (64%) ────────────────────────────────────────────────────


class TestMomentum:
    def test_generate_signals(self, sample_universe):
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        signals = s.generate_signals(sample_universe)
        assert isinstance(signals, list)

    def test_generate_signals_small(self):
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        signals = s.generate_signals(
            {
                "AAPL": pd.DataFrame(
                    {
                        "close": [150.0] * 300,
                        "high": [155.0] * 300,
                        "low": [148.0] * 300,
                        "volume": [1e6] * 300,
                    },
                    index=pd.date_range(end=datetime.now(), periods=300),
                )
            }
        )
        assert isinstance(signals, list)

    def test_calculate_relative_strength(self, sample_universe):
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        rs = s._calculate_relative_strength(sample_universe)
        assert isinstance(rs, dict)

    def test_check_breakout(self, sample_universe):
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        sym = list(sample_universe.keys())[0]
        signal = s._check_breakout(sym, sample_universe[sym], 50.0, datetime.now())
        assert signal is None or hasattr(signal, "strength")

    def test_should_enter_existing(self):
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

    def test_should_exit_min_holding(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=3),
            strategy="Momentum",
            stop_loss=140.0,
            metadata={"current_price": 155.0},
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


# ── MacroAnalyzer (75%) ───────────────────────────────────────────────


class TestMacroAnalyzer:
    def test_build_summary(self):
        from src.research.macro_analyzer import MacroAnalyzer, MacroPhase

        s = MacroAnalyzer._build_summary(
            MacroPhase.EXPANSION, 2.0, 50.0, 3.0, 12.0, 0.9
        )
        assert "EXPANSION" in s

    def test_build_summary_none(self):
        from src.research.macro_analyzer import MacroAnalyzer, MacroPhase

        s = MacroAnalyzer._build_summary(
            MacroPhase.TROUGH, None, None, None, None, None
        )
        assert "TROUGH" in s

    def test_get_macro_state_no_key(self):
        from src.research.macro_analyzer import MacroAnalyzer

        a = MacroAnalyzer(fred_api_key="")
        state = a.get_macro_state()
        assert state.phase.value in ("expansion", "peak", "contraction", "trough")

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


# ── MeanRevert (75%) ─────────────────────────────────────────────────


class TestMeanRevert:
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

    def test_should_exit(self):
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


# ── RegimeDetector (79%) ──────────────────────────────────────────────


class TestRegimeDetector:
    def test_detect_regime_full(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        spy = pd.Series(range(200, 500), dtype=float)
        returns = pd.Series(np.random.normal(0.001, 0.02, 300))
        regime = d.detect_regime(vix=15.0, spy_prices=spy, spy_returns=returns)
        assert regime in ("DEFENSIVE", "NEUTRAL", "AGGRESSIVE")

    def test_vix_signal(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        assert d._vix_signal(10.0) > 0
        assert d._vix_signal(35.0) < 0

    def test_trend_signal(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        assert d._trend_signal(None) == 0.0
        assert d._trend_signal(pd.Series(range(200, 500))) > 0

    def test_credit_signal(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        assert d._credit_spread_signal(None) == 0.0

    def test_hmm_signal(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        assert d._hmm_signal(None) == 0


# ── FeatureStore (78%) ────────────────────────────────────────────────


class TestFeatureStore:
    def test_save_and_get(self, tmp_path):
        from src.data.feature_store import FeatureStore

        db = str(tmp_path / "fs.duckdb")
        store = FeatureStore(db_path=db)
        try:
            df = pd.DataFrame({"symbol": ["AAPL"], "momentum": [75.0]})
            store.save_factor_values("2026-05-28", df)
            result = store.get_factor_values(date="2026-05-28")
            assert not result.empty
        finally:
            store.close()

    def test_get_all_factors(self, tmp_path):
        from src.data.feature_store import FeatureStore

        db = str(tmp_path / "fs.duckdb")
        store = FeatureStore(db_path=db)
        try:
            factors = store.get_all_factors()
            assert isinstance(factors, list)
        finally:
            store.close()

    def test_context_manager(self, tmp_path):
        from src.data.feature_store import FeatureStore

        db = str(tmp_path / "fs.duckdb")
        with FeatureStore(db_path=db) as store:
            assert store is not None


# ── FundamentalFeed (78%) ─────────────────────────────────────────────


class TestFundamentalFeed:
    def test_get_key_metrics(self):
        from src.data.fundamental_feed import FundamentalFeed

        ff = FundamentalFeed(api_key="test")
        with patch.object(ff, "_get") as m:
            m.return_value = [{"peRatio": 25.0}]
            result = ff.get_key_metrics("AAPL")
            assert isinstance(result, (dict, list))

    def test_get_company_profile(self):
        from src.data.fundamental_feed import FundamentalFeed

        ff = FundamentalFeed(api_key="test")
        with patch.object(ff, "_get") as m:
            m.return_value = [{"companyName": "Apple"}]
            result = ff.get_company_profile("AAPL")
            assert isinstance(result, dict)

    def test_cache(self):
        from src.data.fundamental_feed import FundamentalFeed

        ff = FundamentalFeed(api_key="test")
        ff._set_cached("test_key", {"pe": 25.0})
        result = ff._get_cached("test_key")
        assert result == {"pe": 25.0}
        assert ff._get_cached("nonexistent") is None


# ── Portfolio (86%) ───────────────────────────────────────────────────


class TestPortfolio:
    @patch("src.portfolio._get_fx_to_usd", return_value=1.0)
    def test_add_with_sl_tp(self, _):
        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 1_000_000.0
        pm.add_position(
            "AAPL", quantity=100, price=150.0, stop_loss=140.0, take_profit=170.0
        )
        assert pm.get_position("AAPL").stop_loss == 140.0

    @patch("src.portfolio._get_fx_to_usd", return_value=1.0)
    def test_merge_position(self, _):
        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 1_000_000.0
        pm.add_position("AAPL", quantity=100, price=150.0)
        pm.add_position(
            "AAPL", quantity=50, price=160.0, stop_loss=145.0, take_profit=180.0
        )
        pos = pm.get_position("AAPL")
        assert pos.quantity == 150
        assert pos.stop_loss == 145.0

    @patch("src.portfolio._get_fx_to_usd", return_value=1.0)
    def test_reduce_pnl_pct(self, _):
        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 1_000_000.0
        pm.add_position("AAPL", quantity=100, price=150.0)
        result = pm.reduce_position("AAPL", quantity=50, price=160.0)
        assert result["pnl_pct"] > 0

    @patch("src.portfolio._get_fx_to_usd", return_value=1.0)
    def test_sector_exposure(self, _):
        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 1_000_000.0
        pm.add_position("AAPL", quantity=100, price=150.0, sector="Tech")
        sectors = pm.get_sector_exposure()
        assert "Tech" in sectors

    @patch("src.portfolio._get_fx_to_usd", return_value=1.0)
    def test_unsettle_breakdown(self, _):
        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 1_000_000.0
        pm._cash["USD"].record_sell(50_000.0, market="US")
        breakdown = pm.get_unsettle_breakdown("USD")
        assert isinstance(breakdown, dict)

    @patch("src.portfolio._get_fx_to_usd", return_value=1.0)
    def test_save_load(self, _):
        import os
        import tempfile

        from shared.core.state_db import StateDB

        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "test.db")
            db = StateDB(db_path)
            pm = PortfolioManager(db=db)
            pm._cash["USD"].total_cash = 1_000_000.0
            pm.add_position("AAPL", quantity=100, price=150.0)
            # Create new PM with same DB to test load
            pm2 = PortfolioManager(db=db)
            assert pm2.get_position("AAPL") is not None

    @patch("src.portfolio._get_fx_to_usd", return_value=1.0)
    def test_sync_broker(self, _):
        pm = PortfolioManager(db=None)
        broker = MagicMock()
        account = MagicMock()
        account.currency = "USD"
        account.total_cash = 50_000.0
        broker.get_account.return_value = account
        contract = MagicMock()
        contract.symbol = "AAPL"
        contract.exchange = "SMART"
        contract.currency = "USD"
        pos = MagicMock()
        pos.contract = contract
        pos.quantity = 100
        pos.avg_cost = 150.0
        pos.market_value = 15_000.0
        pos.unrealized_pnl = 0.0
        broker.get_portfolio.return_value = [pos]
        assert pm.sync_from_broker(broker) is True


# ── StockScorer (88%) ─────────────────────────────────────────────────


class TestStockScorer:
    def test_score_with_feature_store(self):
        from src.scoring.stock_scorer import StockScorer

        scorer = StockScorer()
        scorer.feature_store = MagicMock()
        scorer.feature_store.get_factor_values.return_value = pd.DataFrame(
            {
                "factor_name": ["momentum", "quality"],
                "value": [75.0, 80.0],
            }
        )
        score = scorer.score_stock("AAPL")
        assert 0 <= score.composite <= 100

    def test_score_quality_none(self):
        from src.scoring.stock_scorer import StockScorer

        scorer = StockScorer()
        assert scorer._score_quality("AAPL") is None

    def test_score_value_none(self):
        from src.scoring.stock_scorer import StockScorer

        scorer = StockScorer()
        assert scorer._score_value("AAPL") is None

    def test_get_weights_allocation(self):
        from src.scoring.stock_scorer import StockScorer

        scorer = StockScorer()
        scorer._strategy_allocation = {
            "AAPL": {"weights": {"technical": 3.0, "momentum": 2.0}}
        }
        weights = scorer._get_weights("AAPL")
        assert sum(weights.values()) == pytest.approx(1.0)

    def test_get_weights_ic(self):
        from src.scoring.stock_scorer import StockScorer

        scorer = StockScorer()
        tracker = MagicMock()
        tracker.get_weights.return_value = {
            "technical": 3.0,
            "momentum": 2.0,
            "fundamental": 1.0,
            "sentiment": 1.0,
            "quality": 1.0,
            "value": 1.0,
        }
        scorer.ic_tracker = tracker
        weights = scorer._get_weights("AAPL")
        assert sum(weights.values()) == pytest.approx(1.0)


# ── PaperClient (84%) ─────────────────────────────────────────────────


class TestPaperClient:
    pass
# ── SentimentFeed (86%) ───────────────────────────────────────────────


class TestSentimentFeed:
    def test_analyze_with_pipeline(self):
        from src.data.sentiment_feed import SentimentFeed

        sf = SentimentFeed()
        sf._pipeline = MagicMock(
            return_value=[
                [
                    {"label": "positive", "score": 0.8},
                    {"label": "negative", "score": 0.1},
                    {"label": "neutral", "score": 0.1},
                ]
            ]
        )
        assert sf.analyze_sentiment("Good") > 0

    def test_analyze_batch(self):
        from src.data.sentiment_feed import SentimentFeed

        sf = SentimentFeed()
        sf._pipeline = MagicMock(
            return_value=[
                [
                    {"label": "positive", "score": 0.8},
                    {"label": "negative", "score": 0.1},
                ],
                [
                    {"label": "negative", "score": 0.7},
                    {"label": "positive", "score": 0.2},
                ],
            ]
        )
        results = sf.analyze_batch(["Good", "Bad"])
        assert len(results) == 2

    def test_score_to_float(self):
        from src.data.sentiment_feed import SentimentFeed

        sf = SentimentFeed()
        assert sf._score_to_float("positive", 0.9) == 0.9
        assert sf._score_to_float("negative", 0.8) == -0.8
        assert sf._score_to_float("neutral", 0.5) == 0.0


# ── TrendStrategy (81%) ───────────────────────────────────────────────


class TestTrendStrategy:
    def test_generate(self, sample_universe):
        from src.strategies.trend_strategy import TrendStrategy

        s = TrendStrategy()
        signals = s.generate_signals(sample_universe)
        assert isinstance(signals, list)

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


# ── MarketHours (80%) ─────────────────────────────────────────────────


class TestMarketHours:
    def test_states(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        for m in [Market.US, Market.HK, Market.CN]:
            state = mh.get_market_state(m)
            assert state in (
                "PRE_MARKET",
                "OPEN",
                "POST_MARKET",
                "CLOSED",
                "LUNCH_BREAK",
            )

    def test_next_close(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        close = mh.next_market_close(Market.US)
        assert isinstance(close, datetime)


# ── SyncIBKRWrapper (90%) ─────────────────────────────────────────────


class TestSyncIBKRWrapper:
    def test_get_market_data(self):
        from src.brokers.sync_ibkr_wrapper import SyncIBKRWrapper

        w = SyncIBKRWrapper()
        w._ib = MagicMock()
        w._connected = True
        mock_ticker = MagicMock()
        mock_ticker.marketPrice.return_value = 150.0
        mock_ticker.close = 150.0
        mock_ticker.open = 149.0
        mock_ticker.bid = 149.5
        mock_ticker.ask = 150.5
        mock_ticker.volume = 1000000
        mock_ticker.high = 152.0
        mock_ticker.low = 148.0
        w._ib.reqTickers.return_value = [mock_ticker]
        w._ib.qualifyContracts = MagicMock()
        data = w.get_market_data("AAPL")
        assert data["price"] == 150.0


# ── CPGClient (86%) ───────────────────────────────────────────────────


class TestCPGClient:
    def test_summary_with_all_fields(self):
        from src.brokers.cpg_client import CPGClient

        cpg = CPGClient(base_url="https://localhost:5000")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "totalcashvalue": {"amount": 50000.0, "currency": "HKD"},
            "netliquidation": {"amount": 150000.0, "currency": "HKD"},
            "buyingpower": {"amount": 300000.0, "currency": "HKD"},
            "availablefunds": {"amount": 50000.0, "currency": "HKD"},
            "grosspositionvalue": {"amount": 100000.0, "currency": "HKD"},
            "unrealizedpnl": {"amount": 5000.0, "currency": "HKD"},
        }
        cpg._session.get = MagicMock(return_value=mock_resp)
        summary = cpg.get_account_summary("U1234567")
        assert summary is not None


# ── SectorData (87%) ──────────────────────────────────────────────────


class TestSectorData:
    def test_rotation(self):
        from src.data.sector_data import SectorData

        sd = SectorData()
        signals = sd.get_sector_rotation_signals()
        assert isinstance(signals, dict)


# ── NewsFeed (89%) ────────────────────────────────────────────────────


class TestNewsFeed:
    def test_sentiment_headlines(self):
        from src.data.news_feed import NewsFeed

        nf = NewsFeed(newsapi_key="test")
        with patch.object(
            nf, "get_news", return_value=[{"title": "Good"}, {"title": ""}]
        ):
            headlines = nf.get_news_for_sentiment("AAPL")
            assert "Good" in headlines


# ── InsiderTrading (80%) ──────────────────────────────────────────────


class TestInsiderTrading:
    def test_cache(self):
        from src.data.insider_trading import InsiderTrading

        it = InsiderTrading(api_key="test")
        it._cache["insider|AAPL|90"] = [{"trade": 1}]
        assert it.get_insider_trades("AAPL") == [{"trade": 1}]

    def test_empty(self):
        from src.data.insider_trading import InsiderTrading

        it = InsiderTrading(api_key="test")
        it._get = MagicMock(return_value=[])
        assert it.get_insider_trades("AAPL") == []
