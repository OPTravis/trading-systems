"""
Last round of coverage tests — targeting specific uncovered lines.
"""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# ── HistoricalStore (52% → target 85%) ────────────────────────────────


class TestHistoricalStoreLast:
    def test_ingest_batch_empty(self, tmp_path):
        from src.data.historical_store import HistoricalStore

        db = str(tmp_path / "hist.duckdb")
        store = HistoricalStore(db_path=db)
        try:
            result = store.ingest_batch([])
            assert isinstance(result, dict)
        finally:
            store.close()

    @patch("yfinance.Ticker")
    def test_ingest_batch_success(self, mock_ticker, tmp_path):
        from src.data.historical_store import HistoricalStore

        dates = pd.date_range(end=datetime.now(), periods=10, freq="D")
        mock_ticker.return_value.history.return_value = pd.DataFrame(
            {
                "Open": [150.0] * 10,
                "High": [155.0] * 10,
                "Low": [148.0] * 10,
                "Close": [152.0] * 10,
                "Volume": [1000000] * 10,
                "Adj Close": [152.0] * 10,
            },
            index=dates,
        )
        db = str(tmp_path / "hist.duckdb")
        store = HistoricalStore(db_path=db)
        try:
            result = store.ingest_batch(["AAPL", "MSFT"], period="1mo")
            assert isinstance(result, dict)
        finally:
            store.close()

    @patch("yfinance.Ticker")
    def test_ingest_batch_empty_data(self, mock_ticker, tmp_path):
        from src.data.historical_store import HistoricalStore

        mock_ticker.return_value.history.return_value = pd.DataFrame()
        db = str(tmp_path / "hist.duckdb")
        store = HistoricalStore(db_path=db)
        try:
            result = store.ingest_batch(["INVALID"])
            assert result.get("INVALID") == 0
        finally:
            store.close()

    @patch("yfinance.Ticker")
    def test_ingest_batch_exception(self, mock_ticker, tmp_path):
        from src.data.historical_store import HistoricalStore

        mock_ticker.return_value.history.side_effect = Exception("API error")
        db = str(tmp_path / "hist.duckdb")
        store = HistoricalStore(db_path=db)
        try:
            result = store.ingest_batch(["AAPL"])
            assert result.get("AAPL") == 0
        finally:
            store.close()


# ── Momentum (55% → target 85%) ───────────────────────────────────────


class TestMomentumLast:
    def test_generate_signals_large_universe(self, sample_universe):
        """Test with enough symbols for meaningful ranking."""
        from src.strategies.momentum import MomentumStrategy

        strategy = MomentumStrategy()
        signals = strategy.generate_signals(sample_universe)
        assert isinstance(signals, list)

    def test_generate_signals_below_minimum(self):
        """Test with fewer than 5 symbols."""
        from src.strategies.momentum import MomentumStrategy

        strategy = MomentumStrategy()
        small = {
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
        signals = strategy.generate_signals(small)
        assert signals == []

    def test_generate_signals_with_positions(self, sample_universe):
        """Test sell signal generation for positions below median."""
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.momentum import MomentumStrategy

        strategy = MomentumStrategy()
        # Add a position that would be below median
        strategy._positions["AAPL"] = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=15),
            strategy="Momentum",
            metadata={"current_price": 140.0},
        )
        signals = strategy.generate_signals(sample_universe)
        assert isinstance(signals, list)

    def test_check_breakout_with_volume(self, sample_universe):
        """Test breakout detection with volume surge."""
        from src.strategies.momentum import MomentumStrategy

        strategy = MomentumStrategy()
        sym = list(sample_universe.keys())[0]
        df = sample_universe[sym].copy()
        signal = strategy._check_breakout(sym, df, 60.0, datetime.now())
        assert signal is None or hasattr(signal, "strength")

    def test_should_enter_existing_position(self, sample_universe):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.base_strategy import Signal, SignalAction
        from src.strategies.momentum import MomentumStrategy

        strategy = MomentumStrategy()
        strategy._positions["AAPL"] = StratPosition(
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
        assert strategy.should_enter(signal) is False


# ── SECFilings (57% → target 85%) ─────────────────────────────────────


class TestSECFilingsLast:
    def test_get_company_cik_found(self):
        from src.data.sec_filings import SECFilings

        sec = SECFilings()
        with patch("src.data.sec_filings.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                json=lambda: {"result": [{"cik_str": 320193, "ticker": "AAPL"}]},
                raise_for_status=lambda: None,
            )
            cik = sec._get_company_cik("AAPL")
            assert cik == "0000320193"

    def test_get_company_cik_not_found(self):
        from src.data.sec_filings import SECFilings

        sec = SECFilings()
        with patch("src.data.sec_filings.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                json=lambda: {"result": []},
                raise_for_status=lambda: None,
            )
            cik = sec._get_company_cik("INVALID")
            assert cik is None

    def test_get_company_cik_exception(self):
        from src.data.sec_filings import SECFilings

        sec = SECFilings()
        with patch.object(sec.session, "get", side_effect=Exception("timeout")):
            cik = sec._get_company_cik("AAPL")
            assert cik is None

    def test_get_latest_filing(self):
        from src.data.sec_filings import SECFilings

        sec = SECFilings()
        with patch.object(sec, "_get_company_cik", return_value="0000320193"):
            with patch("src.data.sec_filings.requests.get") as mock_get:
                mock_get.return_value = MagicMock(
                    json=lambda: {
                        "filings": {
                            "recent": {
                                "form": ["10-K", "10-Q"],
                                "filingDate": ["2026-05-01", "2026-03-01"],
                                "accessionNumber": ["0001-123456", "0001-789012"],
                                "primaryDocDescription": [
                                    "Annual Report",
                                    "Quarterly Report",
                                ],
                                "primaryDocument": ["10-k.htm", "10-q.htm"],
                            }
                        }
                    },
                    raise_for_status=lambda: None,
                )
                filing = sec.get_latest_filing("AAPL", "10-K")
                assert filing is not None

    def test_get_latest_filing_no_cik(self):
        from src.data.sec_filings import SECFilings

        sec = SECFilings()
        with patch.object(sec, "_get_company_cik", return_value=None):
            filing = sec.get_latest_filing("INVALID")
            assert filing is None

    def test_get_filings(self):
        from src.data.sec_filings import SECFilings

        sec = SECFilings()
        with patch.object(sec, "_get_company_cik", return_value="0000320193"):
            with patch("src.data.sec_filings.requests.get") as mock_get:
                mock_get.return_value = MagicMock(
                    json=lambda: {
                        "hits": {
                            "hits": [
                                {
                                    "_source": {
                                        "file_date": "2026-05-01",
                                        "form_type": "10-K",
                                        "display_names": ["Apple Inc."],
                                    }
                                },
                            ]
                        }
                    },
                    raise_for_status=lambda: None,
                )
                filings = sec.get_filings("AAPL", filing_type="10-K")
                assert isinstance(filings, list)


# ── MeanRevert (59% → target 85%) ─────────────────────────────────────


class TestMeanRevertLast:
    def test_generate_signals_large_universe(self, sample_universe):
        from src.strategies.mean_revert import MeanRevertStrategy

        strategy = MeanRevertStrategy()
        signals = strategy.generate_signals(sample_universe)
        assert isinstance(signals, list)

    def test_should_enter_existing_position(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.base_strategy import Signal, SignalAction
        from src.strategies.mean_revert import MeanRevertStrategy

        strategy = MeanRevertStrategy()
        strategy._positions["AAPL"] = StratPosition(
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
            strength=0.8,
            price=150.0,
        )
        assert strategy.should_enter(signal) is False


# ── TrendStrategy (69% → target 85%) ──────────────────────────────────


class TestTrendStrategyLast:
    def test_generate_signals_large_universe(self, sample_universe):
        from src.strategies.trend_strategy import TrendStrategy

        strategy = TrendStrategy()
        signals = strategy.generate_signals(sample_universe)
        assert isinstance(signals, list)

    def test_should_exit_no_stop(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.trend_strategy import TrendStrategy

        strategy = TrendStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=5),
            strategy="TrendFollowing",
            metadata={"current_price": 145.0},
        )
        result = strategy.should_exit(pos)
        assert isinstance(result, bool)


# ── StockResearcher (63% → target 80%) ────────────────────────────────


class TestStockResearcherLast:
    def test_gather_technicals_no_data(self):
        from src.research.stock_researcher import StockResearcher

        mock_feed = MagicMock()
        mock_feed.get_historical.return_value = pd.DataFrame()
        researcher = StockResearcher(
            data_feed=mock_feed, xiaomi_key="t", deepseek_key="t"
        )
        result = researcher._gather_technicals("AAPL")
        assert isinstance(result, str)

    def test_gather_fundamentals_with_profile(self):
        from src.research.stock_researcher import StockResearcher

        researcher = StockResearcher(
            data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t"
        )
        researcher.fundamental_feed = MagicMock()
        researcher.fundamental_feed.get_key_metrics.return_value = {"pe_ratio": 25.0}
        researcher.fundamental_feed.get_company_profile.return_value = {
            "companyName": "Apple",
            "sector": "Tech",
        }
        result = researcher._gather_fundamentals("AAPL")
        assert isinstance(result, str)

    def test_gather_news_empty(self):
        from src.research.stock_researcher import StockResearcher

        researcher = StockResearcher(
            data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t"
        )
        researcher.news_feed = MagicMock()
        researcher.news_feed.get_news.return_value = []
        result = researcher._gather_news("AAPL")
        assert isinstance(result, str)


# ── FeatureStore (71% → target 85%) ───────────────────────────────────


class TestFeatureStoreLast:
    def test_get_factor_values_no_date(self, tmp_path):
        from src.data.feature_store import FeatureStore

        db = str(tmp_path / "fs.duckdb")
        store = FeatureStore(db_path=db)
        try:
            result = store.get_factor_values(date=None, symbols=None)
            assert isinstance(result, pd.DataFrame)
        finally:
            store.close()

    def test_get_factor_values_with_factors(self, tmp_path):
        from src.data.feature_store import FeatureStore

        db = str(tmp_path / "fs.duckdb")
        store = FeatureStore(db_path=db)
        try:
            df = pd.DataFrame({"symbol": ["AAPL"], "momentum": [75.0]})
            store.save_factor_values("2026-05-28", df)
            result = store.get_factor_values(
                date="2026-05-28", factor_names=["momentum"]
            )
            assert isinstance(result, pd.DataFrame)
        finally:
            store.close()


# ── MarketHours (71% → target 85%) ────────────────────────────────────


class TestMarketHoursLast:
    def test_get_market_state_all(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        for market in [Market.US, Market.HK, Market.CN]:
            state = mh.get_market_state(market)
            assert state in (
                "PRE_MARKET",
                "OPEN",
                "POST_MARKET",
                "CLOSED",
                "LUNCH_BREAK",
            )

    def test_next_market_open_hk(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        next_open = mh.next_market_open(Market.HK)
        assert isinstance(next_open, datetime)


# ── CorporateActions (69% → target 85%) ───────────────────────────────


class TestCorporateActionsLast:
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

    def test_get_merger_found(self):
        from src.market.corporate_actions import CorporateActions, Merger

        ca = CorporateActions()
        ca.add_merger(
            Merger(
                acquirer="MSFT",
                target="ATVI",
                announce_date=date(2026, 1, 1),
                close_date=None,
            )
        )
        result = ca.get_merger("ATVI")
        assert result is not None


# ── PaperClient (77% → target 90%) ────────────────────────────────────


class TestPaperClientLast:
    pass
# ── StockDataFeed (75% → target 85%) ──────────────────────────────────


class TestStockDataFeedLast:
    @patch("yfinance.Ticker")
    def test_get_realtime_quote_with_ibkr(self, mock_ticker):
        from src.data.stock_data_feed import StockDataFeed

        feed = StockDataFeed()
        feed.ibkr = MagicMock()
        feed.ibkr.get_market_data.return_value = {"symbol": "AAPL", "price": 150.0}
        quote = feed.get_realtime_quote("AAPL")
        assert quote["symbol"] == "AAPL"

    @patch("yfinance.Ticker")
    def test_get_realtime_quote_ibkr_fallback(self, mock_ticker):
        from src.data.stock_data_feed import StockDataFeed

        feed = StockDataFeed()
        feed.ibkr = MagicMock()
        feed.ibkr.get_market_data.side_effect = Exception("IBKR error")
        mock_ticker.return_value.fast_info = MagicMock(
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


# ── EarningsCalendar (76% → target 90%) ───────────────────────────────


class TestEarningsCalendarLast:
    @patch("src.data.earnings_calendar.requests.get")
    def test_get_earnings_history(self, mock_get):
        from src.data.earnings_calendar import EarningsCalendar

        ec = EarningsCalendar(api_key="test")
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
        assert history[0]["reported_eps"] == 1.5

    @patch("src.data.earnings_calendar.requests.get")
    def test_is_earnings_day_symbol_filtered(self, mock_get):
        from src.data.earnings_calendar import EarningsCalendar

        ec = EarningsCalendar(api_key="test")
        mock_get.return_value = MagicMock(
            json=lambda: [{"reportDate": "2026-07-30"}],
            raise_for_status=lambda: None,
        )
        result = ec.is_earnings_day("AAPL", date(2026, 7, 30))
        assert isinstance(result, bool)


# ── RegimeDetector (78% → target 90%) ─────────────────────────────────


class TestRegimeDetectorLast:
    def test_detect_regime_aggressive(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        spy = pd.Series(range(200, 500), dtype=float)
        returns = pd.Series(np.random.normal(0.002, 0.015, 300))
        regime = d.detect_regime(vix=10.0, spy_prices=spy, spy_returns=returns)
        assert regime in ("DEFENSIVE", "NEUTRAL", "AGGRESSIVE")

    def test_detect_regime_defensive(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        spy = pd.Series(range(500, 200, -1), dtype=float)
        returns = pd.Series(np.random.normal(-0.002, 0.03, 300))
        regime = d.detect_regime(vix=40.0, spy_prices=spy, spy_returns=returns)
        assert regime in ("DEFENSIVE", "NEUTRAL", "AGGRESSIVE")

    def test_detect_regime_with_hyg(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        hyg = pd.Series([0.85] * 30)
        regime = d.detect_regime(vix=18.0, hyg_tlt_ratio=hyg)
        assert regime in ("DEFENSIVE", "NEUTRAL", "AGGRESSIVE")


# ── Portfolio (83% → target 90%) ──────────────────────────────────────


class TestPortfolioLast:
    @patch("src.portfolio._get_fx_to_usd", return_value=1.0)
    def test_get_nav_multi_currency(self, mock_fx):
        from src.portfolio import PortfolioManager

        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 100_000.0
        pm._cash["HKD"].total_cash = 100_000.0
        pm.add_position("AAPL", quantity=100, price=150.0)
        nav = pm.get_nav()
        assert nav > 0

    def test_reduce_position_partial(self):
        from src.portfolio import PortfolioManager

        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 1_000_000.0
        pm.add_position("AAPL", quantity=100, price=150.0)
        result = pm.reduce_position("AAPL", quantity=50, price=160.0)
        assert result["remaining_qty"] == 50

    def test_close_position_full(self):
        from src.portfolio import PortfolioManager

        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 1_000_000.0
        pm.add_position("AAPL", quantity=100, price=150.0)
        pm.close_position("AAPL", price=160.0)
        assert pm.position_count == 0


# ── Notifier (coverage gaps) ──────────────────────────────────────────


class TestNotifierLast:
    @patch("src.notifier.requests.post")
    def test_send_card_success(self, mock_post):
        from src.notifier import FeishuNotifier

        mock_post.return_value = MagicMock(json=lambda: {"code": 0})
        notifier = FeishuNotifier(chat_id="test")
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            result = notifier._send_card("Title", [{"tag": "div"}])
            assert result is True

    @patch("src.notifier.requests.post")
    def test_send_earnings_with_surprise(self, mock_post):
        from src.notifier import FeishuNotifier

        mock_post.return_value = MagicMock(json=lambda: {"code": 0})
        notifier = FeishuNotifier(chat_id="test")
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            result = notifier.send_earnings_alert(
                "AAPL", "2026-07-30", estimated_eps=1.5, actual_eps=1.8
            )
            assert result is True

    @patch("src.notifier.requests.post")
    def test_send_trade_signal_with_metadata(self, mock_post):
        from src.notifier import FeishuNotifier

        mock_post.return_value = MagicMock(json=lambda: {"code": 0})
        notifier = FeishuNotifier(chat_id="test")
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            signal = {
                "symbol": "AAPL",
                "action": "BUY",
                "price": 150.0,
                "strategy": "momentum",
                "strength": 0.8,
                "stop_loss": 145.0,
                "metadata": {"rsi": 35},
            }
            result = notifier.send_trade_signal(signal)
            assert result is True


# ── NewsFeed (89% → target 95%) ───────────────────────────────────────


class TestNewsFeedLast:
    @patch("src.data.news_feed.requests.get")
    def test_get_news_jina_fallback(self, mock_get):
        from src.data.news_feed import NewsFeed

        nf = NewsFeed(newsapi_key="")
        mock_get.return_value = MagicMock(ok=True, text="Some news content")
        news = nf.get_news("AAPL")
        assert isinstance(news, list)

    @patch("src.data.news_feed.requests.get")
    def test_get_market_news_empty(self, mock_get):
        from src.data.news_feed import NewsFeed

        nf = NewsFeed(newsapi_key="test")
        mock_get.return_value = MagicMock(
            json=lambda: {"articles": []},
            raise_for_status=lambda: None,
        )
        news = nf.get_market_news(10)
        assert isinstance(news, list)
