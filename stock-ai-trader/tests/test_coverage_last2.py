"""
Final coverage push — targeting specific uncovered lines.
"""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.portfolio import PortfolioManager

# ── Portfolio (85% → target 95%) ──────────────────────────────────────


class TestPortfolioLast2:
    @patch("src.portfolio._get_fx_to_usd", return_value=1.0)
    def test_add_position_with_stop_loss(self, mock_fx):
        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 1_000_000.0
        pm.add_position(
            "AAPL", quantity=100, price=150.0, stop_loss=140.0, take_profit=170.0
        )
        pos = pm.get_position("AAPL")
        assert pos.stop_loss == 140.0
        assert pos.take_profit == 170.0

    @patch("src.portfolio._get_fx_to_usd", return_value=1.0)
    def test_add_position_merge_with_sl_tp(self, mock_fx):
        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 1_000_000.0
        pm.add_position("AAPL", quantity=100, price=150.0, stop_loss=140.0)
        pm.add_position(
            "AAPL", quantity=50, price=160.0, stop_loss=145.0, take_profit=180.0
        )
        pos = pm.get_position("AAPL")
        assert pos.stop_loss == 145.0
        assert pos.take_profit == 180.0

    @patch("src.portfolio._get_fx_to_usd", return_value=1.0)
    def test_reduce_position_pnl_pct(self, mock_fx):
        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 1_000_000.0
        pm.add_position("AAPL", quantity=100, price=150.0)
        result = pm.reduce_position("AAPL", quantity=50, price=160.0)
        assert result["pnl_pct"] > 0

    @patch("src.portfolio._get_fx_to_usd", return_value=1.0)
    def test_close_nonexistent(self, mock_fx):
        pm = PortfolioManager(db=None)
        with pytest.raises(ValueError):
            pm.close_position("FAKE", 150.0)

    @patch("src.portfolio._get_fx_to_usd", return_value=1.0)
    def test_get_nav_with_fx(self, mock_fx):
        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 100_000.0
        pm._cash["HKD"].total_cash = 780_000.0
        nav = pm.get_nav()
        assert nav > 0

    @patch("src.portfolio._get_fx_to_usd", return_value=1.0)
    def test_get_cash_balance(self, mock_fx):
        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 100_000.0
        assert pm.get_cash_balance("USD") == 100_000.0
        assert pm.get_available_cash("USD") == 100_000.0

    def test_add_position_with_sector_strategy(self):
        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 1_000_000.0
        pm.add_position(
            "AAPL", quantity=100, price=150.0, sector="Technology", strategy="momentum"
        )
        pos = pm.get_position("AAPL")
        assert pos.sector == "Technology"
        assert pos.strategy == "momentum"


# ── StockScorer (82% → target 95%) ────────────────────────────────────


class TestStockScorerLast2:
    def test_get_feature_store_scores(self):
        from src.scoring.stock_scorer import StockScorer

        scorer = StockScorer()
        scorer.feature_store = MagicMock()
        scorer.feature_store.get_factor_values.return_value = pd.DataFrame(
            {
                "factor_name": ["momentum", "value"],
                "value": [75.0, 60.0],
            }
        )
        scores = scorer._get_feature_store_scores("AAPL")
        assert "momentum" in scores

    def test_get_feature_store_empty(self):
        from src.scoring.stock_scorer import StockScorer

        scorer = StockScorer()
        scorer.feature_store = MagicMock()
        scorer.feature_store.get_factor_values.return_value = pd.DataFrame()
        scores = scorer._get_feature_store_scores("AAPL")
        assert scores == {}

    def test_get_feature_store_none(self):
        from src.scoring.stock_scorer import StockScorer

        scorer = StockScorer()
        scorer.feature_store = None
        scores = scorer._get_feature_store_scores("AAPL")
        assert scores == {}

    def test_get_feature_store_exception(self):
        from src.scoring.stock_scorer import StockScorer

        scorer = StockScorer()
        scorer.feature_store = MagicMock()
        scorer.feature_store.get_factor_values.side_effect = Exception("DB error")
        scores = scorer._get_feature_store_scores("AAPL")
        assert scores == {}

    def test_score_stock_with_feature_store(self):
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

    def test_get_weights_with_allocation(self):
        from src.scoring.stock_scorer import StockScorer

        scorer = StockScorer()
        scorer._strategy_allocation = {
            "AAPL": {"weights": {"technical": 3.0, "momentum": 2.0}}
        }
        weights = scorer._get_weights("AAPL")
        assert sum(weights.values()) == pytest.approx(1.0)

    def test_get_weights_ic_tracker_zero_total(self):
        from src.scoring.stock_scorer import StockScorer

        scorer = StockScorer()
        tracker = MagicMock()
        tracker.get_weights.return_value = {"technical": 0.0, "momentum": 0.0}
        scorer.ic_tracker = tracker
        weights = scorer._get_weights("AAPL")
        # Should fall back to defaults
        assert sum(weights.values()) > 0


# ── Momentum (64% → target 85%) ───────────────────────────────────────


class TestMomentumLast2:
    def test_generate_signals_no_rs(self, sample_universe):
        """Test when relative strength calculation returns empty."""
        from src.strategies.momentum import MomentumStrategy

        strategy = MomentumStrategy()
        with patch.object(strategy, "_calculate_relative_strength", return_value={}):
            signals = strategy.generate_signals(sample_universe)
            assert signals == []

    def test_generate_signals_below_minimum(self):
        from src.strategies.momentum import MomentumStrategy

        strategy = MomentumStrategy()
        signals = strategy.generate_signals({"AAPL": pd.DataFrame()})
        assert signals == []

    def test_check_breakout_no_volume(self, sample_universe):
        from src.strategies.momentum import MomentumStrategy

        strategy = MomentumStrategy()
        sym = list(sample_universe.keys())[0]
        df = sample_universe[sym].copy()
        # Set very low volume
        df["volume"] = 100
        signal = strategy._check_breakout(sym, df, 50.0, datetime.now())
        assert signal is None

    def test_check_breakout_below_high(self, sample_universe):
        from src.strategies.momentum import MomentumStrategy

        strategy = MomentumStrategy()
        sym = list(sample_universe.keys())[0]
        df = sample_universe[sym].copy()
        # Set close below high
        df.iloc[-1, df.columns.get_loc("close")] = df["high"].min() * 0.9
        signal = strategy._check_breakout(sym, df, 50.0, datetime.now())
        assert signal is None


# ── MeanRevert (66% → target 85%) ─────────────────────────────────────


class TestMeanRevertLast2:
    def test_generate_signals_small_universe(self):
        from src.strategies.mean_revert import MeanRevertStrategy

        strategy = MeanRevertStrategy()
        small = {"AAPL": pd.DataFrame({"close": [150.0] * 50})}
        signals = strategy.generate_signals(small)
        assert isinstance(signals, list)

    def test_should_enter_sell_signal(self):
        from src.strategies.base_strategy import Signal, SignalAction
        from src.strategies.mean_revert import MeanRevertStrategy

        strategy = MeanRevertStrategy()
        signal = Signal(
            symbol="AAPL",
            action=SignalAction.SELL,
            strategy="MeanRevert",
            timestamp=datetime.now(),
            strength=0.8,
            price=150.0,
        )
        assert strategy.should_enter(signal) is False

    def test_should_exit_stop_loss(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.mean_revert import MeanRevertStrategy

        strategy = MeanRevertStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=5),
            strategy="MeanRevert",
            stop_loss=140.0,
            metadata={"current_price": 139.0},
        )
        assert strategy.should_exit(pos) is True


# ── TrendStrategy (79% → target 90%) ──────────────────────────────────


class TestTrendStrategyLast2:
    def test_generate_signals_empty(self):
        from src.strategies.trend_strategy import TrendStrategy

        strategy = TrendStrategy()
        signals = strategy.generate_signals({})
        assert signals == []

    def test_should_enter_weak(self):
        from src.strategies.base_strategy import Signal, SignalAction
        from src.strategies.trend_strategy import TrendStrategy

        strategy = TrendStrategy()
        signal = Signal(
            symbol="AAPL",
            action=SignalAction.BUY,
            strategy="TrendFollowing",
            timestamp=datetime.now(),
            strength=0.3,
            price=150.0,
        )
        assert strategy.should_enter(signal) is False


# ── RegimeDetector (79% → target 90%) ─────────────────────────────────


class TestRegimeDetectorLast2:
    def test_hmm_signal_with_returns(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        d._use_hmm = True
        d._hmm_states = 2
        returns = pd.Series(np.random.normal(0.001, 0.02, 300))
        signal = d._hmm_signal(returns)
        assert signal in (-1, 0, 1)

    def test_detect_regime_with_all_signals(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        spy = pd.Series(range(200, 500), dtype=float)
        returns = pd.Series(np.random.normal(0.001, 0.02, 300))
        hyg = pd.Series([0.85] * 30)
        regime = d.detect_regime(
            vix=15.0, spy_prices=spy, spy_returns=returns, hyg_tlt_ratio=hyg
        )
        assert regime in ("DEFENSIVE", "NEUTRAL", "AGGRESSIVE")


# ── MarketHours (80% → target 90%) ────────────────────────────────────


class TestMarketHoursLast2:
    def test_get_sessions_hk(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        sessions = mh.get_sessions(Market.HK)
        assert "lunch_break" in sessions

    def test_is_market_open_returns_bool(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        for market in [Market.US, Market.HK, Market.CN]:
            result = mh.is_market_open(market)
            assert isinstance(result, bool)


# ── InsiderTrading (65%) ──────────────────────────────────────────────


class TestInsiderTradingLast2:
    def test_get_insider_trades_api(self):
        from src.data.insider_trading import InsiderTrading

        it = InsiderTrading(api_key="test")
        recent = datetime.now().strftime("%Y-%m-%d")
        # Cache hit
        it._cache["insider|AAPL|90"] = [{"date": recent}]
        trades = it.get_insider_trades("AAPL")
        assert len(trades) >= 1

    def test_get_insider_trades_skip_old(self):
        from src.data.insider_trading import InsiderTrading

        it = InsiderTrading(api_key="test")
        it._get = MagicMock(
            return_value=[
                {
                    "transactionDate": "2020-01-01",
                    "reportingName": "CEO",
                    "typeOfOwner": "Officer",
                    "transactionType": "P-PURCHASE",
                    "securitiesTransacted": 1000,
                    "price": 150.0,
                    "securityName": "Common Stock",
                },
            ]
        )
        trades = it.get_insider_trades("AAPL")
        assert len(trades) == 0


# ── SECFilings (82%) ──────────────────────────────────────────────────


class TestSECFilingsLast2:
    def test_get_filings_with_cache(self):
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


# ── SentimentFeed (83%) ───────────────────────────────────────────────


class TestSentimentFeedLast2:
    def test_analyze_batch_dict_format(self):
        from src.data.sentiment_feed import SentimentFeed

        sf = SentimentFeed()
        sf._pipeline = MagicMock(
            return_value=[
                {"label": "positive", "score": 0.8},
                {"label": "negative", "score": 0.7},
            ]
        )
        results = sf.analyze_batch(["Good", "Bad"])
        assert len(results) == 2


# ── EarningsCalendar (83%) ────────────────────────────────────────────


class TestEarningsCalendarLast2:
    def test_is_earnings_day_no_match(self):
        from src.data.earnings_calendar import EarningsCalendar

        ec = EarningsCalendar(api_key="test")
        with patch("src.data.earnings_calendar.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                json=lambda: {
                    "quarterlyEarnings": [
                        {"reportedDate": "2026-01-01", "reportedEPS": "1.0"}
                    ]
                },
                raise_for_status=lambda: None,
            )
            result = ec.is_earnings_day("AAPL", date(2026, 7, 30))
            assert result is False


# ── HistoricalStore (83%) ─────────────────────────────────────────────


class TestHistoricalStoreLast2:
    @patch("yfinance.Ticker")
    def test_ingest_batch_with_adj_close(self, mock_ticker, tmp_path):
        from src.data.historical_store import HistoricalStore

        dates = pd.date_range(end=datetime.now(), periods=5, freq="D")
        df = pd.DataFrame(
            {
                "Open": [150.0] * 5,
                "High": [155.0] * 5,
                "Low": [148.0] * 5,
                "Close": [152.0] * 5,
                "Volume": [1000000] * 5,
                "Adj Close": [152.0] * 5,
            },
            index=pd.DatetimeIndex(dates, name="Date"),
        )
        mock_ticker.return_value.history.return_value = df
        db = str(tmp_path / "hist.duckdb")
        store = HistoricalStore(db_path=db)
        try:
            result = store.ingest_batch(["AAPL"])
            assert result["AAPL"] > 0
        finally:
            store.close()


# ── PaperClient (84%) ─────────────────────────────────────────────────


class TestPaperClientLast2:
    @pytest.mark.asyncio
    async def test_place_order_buy_updates_position(self):
        from src.brokers.broker_protocol import Contract, Order, OrderSide, OrderType
        from src.brokers.paper_client import PaperClient

        client = PaperClient(starting_balance=100_000.0)
        await client.connect()
        client.set_market_price("AAPL", 150.0)
        order = Order(
            contract=Contract(symbol="AAPL"),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=100,
        )
        await client.place_order(order)
        positions = await client.get_positions()
        assert len(positions) == 1
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_place_order_sell_updates_balance(self):
        from src.brokers.broker_protocol import Contract, Order, OrderSide, OrderType
        from src.brokers.paper_client import PaperClient

        client = PaperClient(starting_balance=100_000.0)
        await client.connect()
        client.set_market_price("AAPL", 150.0)
        buy = Order(
            contract=Contract(symbol="AAPL"),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=100,
        )
        await client.place_order(buy)
        initial_balance = client._balance
        sell = Order(
            contract=Contract(symbol="AAPL"),
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=100,
        )
        await client.place_order(sell)
        assert client._balance > initial_balance
        await client.disconnect()


# ── SectorData (87%) ──────────────────────────────────────────────────


class TestSectorDataLast2:
    def test_get_sector_rotation(self):
        from src.data.sector_data import SectorData

        sd = SectorData()
        signals = sd.get_sector_rotation_signals()
        assert isinstance(signals, dict)


# ── NewsFeed (89%) ────────────────────────────────────────────────────


class TestNewsFeedLast2:
    def test_get_news_no_key(self):
        from src.data.news_feed import NewsFeed

        nf = NewsFeed(newsapi_key="")
        with patch("src.data.news_feed.requests.get") as mock_get:
            mock_get.return_value = MagicMock(ok=True, text="Content")
            news = nf.get_news("AAPL")
            assert isinstance(news, list)


# ── SyncIBKRWrapper (90%) ─────────────────────────────────────────────


class TestSyncIBKRWrapperLast2:
    def test_get_market_data_with_ib(self):
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


# ── FeatureStore (78%) ────────────────────────────────────────────────


class TestFeatureStoreLast2:
    def test_get_factor_values_with_data(self, tmp_path):
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
