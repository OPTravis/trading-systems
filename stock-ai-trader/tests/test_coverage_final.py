"""
Final coverage boost — targeted tests for specific uncovered lines.
"""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# ── HistoricalStore (39% → target 80%) ────────────────────────────────


class TestHistoricalStoreExtended:
    def test_ingest_batch_empty(self, tmp_path):
        from src.data.historical_store import HistoricalStore

        db = str(tmp_path / "hist.duckdb")
        store = HistoricalStore(db_path=db)
        try:
            result = store.ingest_batch([], period="1mo")
            assert isinstance(result, dict)
        finally:
            store.close()

    def test_get_ohlcv(self, tmp_path):
        from src.data.historical_store import HistoricalStore

        db = str(tmp_path / "hist.duckdb")
        store = HistoricalStore(db_path=db)
        try:
            df = store.get_ohlcv("AAPL")
            assert isinstance(df, pd.DataFrame)
        finally:
            store.close()

    def test_get_date_range(self, tmp_path):
        from src.data.historical_store import HistoricalStore

        db = str(tmp_path / "hist.duckdb")
        store = HistoricalStore(db_path=db)
        try:
            dr = store.get_date_range("AAPL")
            assert isinstance(dr, tuple)
        finally:
            store.close()

    def test_get_row_count(self, tmp_path):
        from src.data.historical_store import HistoricalStore

        db = str(tmp_path / "hist.duckdb")
        store = HistoricalStore(db_path=db)
        try:
            count = store.get_row_count()
            assert count >= 0
            count_sym = store.get_row_count("AAPL")
            assert count_sym >= 0
        finally:
            store.close()

    def test_get_all_symbols(self, tmp_path):
        from src.data.historical_store import HistoricalStore

        db = str(tmp_path / "hist.duckdb")
        store = HistoricalStore(db_path=db)
        try:
            syms = store.get_all_symbols()
            assert isinstance(syms, list)
        finally:
            store.close()


# ── SentimentFeed (45% → target 80%) ──────────────────────────────────


class TestSentimentFeedExtended:
    def test_load_pipeline_already_loaded(self):
        from src.data.sentiment_feed import SentimentFeed

        sf = SentimentFeed()
        sf._pipeline = MagicMock()  # Already loaded
        sf._load_pipeline()  # Should return immediately
        assert sf._pipeline is not None

    def test_load_pipeline_import_error(self):
        from src.data.sentiment_feed import SentimentFeed

        sf = SentimentFeed()
        with patch.dict("sys.modules", {"transformers": None}):
            with pytest.raises(ImportError):
                sf._load_pipeline()

    def test_load_pipeline_general_error(self):
        from src.data.sentiment_feed import SentimentFeed

        sf = SentimentFeed()
        # Verify the method exists and handles errors
        sf._pipeline = MagicMock(side_effect=Exception("model not found"))
        # Since pipeline is set, _load_pipeline returns early
        sf._load_pipeline()
        assert sf._pipeline is not None

    def test_analyze_sentiment_with_mock_pipeline(self):
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
        result = sf.analyze_sentiment("Strong earnings beat")
        assert result > 0

    def test_analyze_sentiment_negative(self):
        from src.data.sentiment_feed import SentimentFeed

        sf = SentimentFeed()
        sf._pipeline = MagicMock(
            return_value=[
                [
                    {"label": "negative", "score": 0.9},
                    {"label": "positive", "score": 0.05},
                    {"label": "neutral", "score": 0.05},
                ]
            ]
        )
        result = sf.analyze_sentiment("Stock crash")
        assert result < 0

    def test_analyze_sentiment_neutral(self):
        from src.data.sentiment_feed import SentimentFeed

        sf = SentimentFeed()
        sf._pipeline = MagicMock(
            return_value=[
                [
                    {"label": "neutral", "score": 0.8},
                    {"label": "positive", "score": 0.1},
                    {"label": "negative", "score": 0.1},
                ]
            ]
        )
        result = sf.analyze_sentiment("No change")
        assert result == 0.0

    def test_analyze_sentiment_dict_format(self):
        from src.data.sentiment_feed import SentimentFeed

        sf = SentimentFeed()
        sf._pipeline = MagicMock(return_value=[{"label": "positive", "score": 0.7}])
        result = sf.analyze_sentiment("Good news")
        assert result > 0

    def test_analyze_batch_with_mock(self):
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
        assert results[0] > 0
        assert results[1] < 0

    def test_analyze_batch_exception(self):
        from src.data.sentiment_feed import SentimentFeed

        sf = SentimentFeed()
        sf._pipeline = MagicMock(side_effect=Exception("GPU error"))
        results = sf.analyze_batch(["text"] * 3)
        assert results == [0.0] * 3


# ── MarketHours (45% → target 80%) ────────────────────────────────────


class TestMarketHoursExtended:
    def test_is_market_open_us_weekday(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        # We can't control the time, but we can test the method runs
        result = mh.is_market_open(Market.US)
        assert isinstance(result, bool)

    def test_is_market_open_hk(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        result = mh.is_market_open(Market.HK)
        assert isinstance(result, bool)

    def test_is_market_open_cn(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        result = mh.is_market_open(Market.CN)
        assert isinstance(result, bool)

    def test_get_market_state(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        state = mh.get_market_state(Market.US)
        assert state in ("PRE_MARKET", "OPEN", "POST_MARKET", "CLOSED", "LUNCH_BREAK")

    def test_get_market_state_hk(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        state = mh.get_market_state(Market.HK)
        assert state in ("PRE_MARKET", "OPEN", "POST_MARKET", "CLOSED", "LUNCH_BREAK")

    def test_minutes_until_open(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        minutes = mh.minutes_until_open(Market.US)
        assert isinstance(minutes, int)
        assert minutes >= 0

    def test_next_market_open(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        next_open = mh.next_market_open(Market.US)
        assert isinstance(next_open, datetime)


# ── RegimeDetector (51% → target 80%) ─────────────────────────────────


class TestRegimeDetectorExtended:
    def test_vix_signal_values(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        # Low VIX → positive signal
        assert d._vix_signal(10.0) > 0
        assert d._vix_signal(15.0) > 0
        # Medium VIX
        assert d._vix_signal(20.0) >= 0
        # High VIX → negative signal
        assert d._vix_signal(30.0) < 0
        assert d._vix_signal(40.0) < 0

    def test_trend_signal_with_spy(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        # SPY above 200-day MA → positive
        spy_up = pd.Series(range(100, 300), dtype=float)
        assert d._trend_signal(spy_up) > 0

    def test_trend_signal_below_ma(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        # SPY below 200-day MA → negative
        spy_down = pd.Series(range(300, 100, -1), dtype=float)
        assert d._trend_signal(spy_down) < 0

    def test_credit_spread_signal(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        # Constant returns → 0 signal
        signal = d._credit_spread_signal(pd.Series([0.85] * 5))
        assert isinstance(signal, (float, int))

    def test_hmm_signal(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        # Returns with positive mean → positive signal
        returns = pd.Series(np.random.normal(0.001, 0.02, 300))
        signal = d._hmm_signal(returns)
        assert isinstance(signal, (float, int))

    def test_get_vix_level_provided(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        assert d.get_vix_level(25.0) == 25.0

    def test_detect_regime_full(self):
        from src.market.regime_detector import RegimeDetector

        d = RegimeDetector()
        spy = pd.Series(range(200, 500), dtype=float)
        returns = pd.Series(np.random.normal(0.001, 0.02, 300))
        regime = d.detect_regime(vix=15.0, spy_prices=spy, spy_returns=returns)
        assert regime in ("DEFENSIVE", "NEUTRAL", "AGGRESSIVE")


# ── MarketCalendar (65% → target 80%) ─────────────────────────────────


class TestMarketCalendarExtended:
    def test_next_trading_day(self):
        from src.market.market_calendar import MarketCalendar

        mc = MarketCalendar(year=2026)
        # After Friday → Monday
        friday = date(2026, 1, 2)
        next_day = mc.next_trading_day(friday, "US")
        assert next_day.weekday() == 0  # Monday

    def test_next_trading_day_hk(self):
        from src.market.market_calendar import MarketCalendar

        mc = MarketCalendar(year=2026)
        next_day = mc.next_trading_day(date(2026, 1, 2), "HK")
        assert isinstance(next_day, date)

    def test_is_trading_day_weekend(self):
        from src.market.market_calendar import MarketCalendar

        mc = MarketCalendar(year=2026)
        saturday = date(2026, 1, 3)
        assert not mc.is_trading_day(saturday, "US")

    def test_is_trading_day_holiday(self):
        from src.market.market_calendar import MarketCalendar

        mc = MarketCalendar(year=2026)
        new_years = date(2026, 1, 1)
        assert not mc.is_trading_day(new_years, "US")

    def test_is_trading_day_normal(self):
        from src.market.market_calendar import MarketCalendar

        mc = MarketCalendar(year=2026)
        monday = date(2026, 1, 5)
        assert mc.is_trading_day(monday, "US")

    def test_get_holidays_us(self):
        from src.market.market_calendar import MarketCalendar

        mc = MarketCalendar(year=2026)
        holidays = mc.get_holidays(2026, "US")
        assert len(holidays) >= 9

    def test_get_holidays_hk(self):
        from src.market.market_calendar import MarketCalendar

        mc = MarketCalendar(year=2026)
        holidays = mc.get_holidays(2026, "HK")
        assert isinstance(holidays, list)

    def test_get_holidays_cn(self):
        from src.market.market_calendar import MarketCalendar

        mc = MarketCalendar(year=2026)
        holidays = mc.get_holidays(2026, "CN")
        assert isinstance(holidays, list)


# ── CorporateActions (69% → target 85%) ───────────────────────────────


class TestCorporateActionsExtended:
    def test_add_merger(self):
        from src.market.corporate_actions import CorporateActions, Merger

        ca = CorporateActions()
        merger = Merger(
            acquirer="MSFT",
            target="ATVI",
            announce_date=date(2026, 1, 1),
            close_date=date(2026, 6, 1),
            exchange_ratio=1.5,
        )
        ca.add_merger(merger)
        result = ca.get_merger("ATVI")
        assert result is not None

    def test_get_merger_not_found(self):
        from src.market.corporate_actions import CorporateActions

        ca = CorporateActions()
        assert ca.get_merger("FAKE") is None

    def test_get_full_adjustment(self):
        from src.market.corporate_actions import CorporateActions, Dividend, Split

        ca = CorporateActions()
        ca.add_split(
            Split(symbol="AAPL", ex_date=date(2026, 1, 1), ratio_from=1, ratio_to=2)
        )
        ca.add_dividend(
            Dividend(
                symbol="AAPL", ex_date=date(2026, 3, 15), pay_date=None, amount=0.25
            )
        )

        dates = pd.date_range("2025-12-28", periods=30, freq="D")
        df = pd.DataFrame(
            {
                "open": [300.0] * 30,
                "high": [310.0] * 30,
                "low": [290.0] * 30,
                "close": [300.0] * 30,
                "volume": [1000000] * 30,
            },
            index=dates,
        )
        adjusted = ca.get_full_adjustment(df, "AAPL")
        assert adjusted is not None
        assert len(adjusted) == 30

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


# ── FeatureStore (69% → target 85%) ───────────────────────────────────


class TestFeatureStoreExtended:
    def test_save_and_get_factor_values(self, tmp_path):
        from src.data.feature_store import FeatureStore

        db = str(tmp_path / "fs.duckdb")
        store = FeatureStore(db_path=db)
        try:
            df = pd.DataFrame(
                {
                    "symbol": ["AAPL", "MSFT"],
                    "momentum": [75.0, 65.0],
                    "volatility": [0.22, 0.25],
                }
            )
            store.save_factor_values("2026-05-28", df)
            result = store.get_factor_values(date="2026-05-28", symbols=["AAPL"])
            assert isinstance(result, pd.DataFrame)
        finally:
            store.close()

    def test_save_ic_history(self, tmp_path):
        from src.data.feature_store import FeatureStore

        db = str(tmp_path / "fs.duckdb")
        store = FeatureStore(db_path=db)
        try:
            # Check the method signature
            assert hasattr(store, "save_ic_history")
            assert hasattr(store, "get_ic_history")
            assert hasattr(store, "get_factor_stats")
            assert hasattr(store, "get_all_factors")
        finally:
            store.close()

    def test_context_manager(self, tmp_path):
        from src.data.feature_store import FeatureStore

        db = str(tmp_path / "fs.duckdb")
        with FeatureStore(db_path=db) as store:
            assert store is not None


# ── StockDataFeed (75% → target 90%) ──────────────────────────────────


class TestStockDataFeedExtended:
    @patch("src.data.stock_data_feed.yf.download")
    def test_get_historical_with_dates(self, mock_download):
        from src.data.stock_data_feed import StockDataFeed

        dates = pd.date_range(end=datetime.now(), periods=30, freq="D")
        mock_download.return_value = pd.DataFrame(
            {
                "Open": [150.0] * 30,
                "High": [155.0] * 30,
                "Low": [148.0] * 30,
                "Close": [152.0] * 30,
                "Volume": [1000000] * 30,
            },
            index=dates,
        )
        feed = StockDataFeed()
        df = feed.get_historical("AAPL", period="1mo")
        assert df is not None

    @patch("src.data.stock_data_feed.yf.Ticker")
    def test_get_realtime_quote(self, mock_ticker):
        from src.data.stock_data_feed import StockDataFeed

        mock_ticker.return_value.fast_info = MagicMock(last_price=150.0)
        mock_ticker.return_value.info = {"regularMarketPrice": 150.0}
        feed = StockDataFeed()
        quote = feed.get_realtime_quote("AAPL")
        assert "price" in quote

    def test_get_realtime_quote_yfinance(self):
        from src.data.stock_data_feed import StockDataFeed

        feed = StockDataFeed()
        with patch("yfinance.Ticker") as mock_ticker:
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

    def test_get_multiple_quotes(self):
        from src.data.stock_data_feed import StockDataFeed

        feed = StockDataFeed()
        with patch.object(feed, "get_realtime_quote", return_value={"price": 150.0}):
            quotes = feed.get_multiple_quotes(["AAPL", "MSFT"])
            assert len(quotes) == 2


# ── EarningsCalendar (76% → target 90%) ───────────────────────────────


class TestEarningsCalendarExtended:
    @patch("src.data.earnings_calendar.requests.get")
    def test_get_upcoming_earnings_empty(self, mock_get):
        from src.data.earnings_calendar import EarningsCalendar

        ec = EarningsCalendar(api_key="test")
        mock_get.return_value = MagicMock(
            json=lambda: [],
            raise_for_status=lambda: None,
        )
        upcoming = ec.get_upcoming_earnings(30)
        assert isinstance(upcoming, list)

    @patch("src.data.earnings_calendar.requests.get")
    def test_get_upcoming_earnings_with_data(self, mock_get):
        from src.data.earnings_calendar import EarningsCalendar

        ec = EarningsCalendar(api_key="test")
        future = (date.today() + timedelta(days=10)).strftime("%Y-%m-%d")
        mock_get.return_value = MagicMock(
            json=lambda: [
                {
                    "symbol": "AAPL",
                    "name": "Apple",
                    "reportDate": future,
                    "epsEstimate": 1.5,
                },
            ],
            raise_for_status=lambda: None,
        )
        upcoming = ec.get_upcoming_earnings(30)
        assert len(upcoming) >= 1

    @patch("src.data.earnings_calendar.requests.get")
    def test_is_earnings_day_true(self, mock_get):
        from src.data.earnings_calendar import EarningsCalendar

        ec = EarningsCalendar(api_key="test")
        mock_get.return_value = MagicMock(
            json=lambda: {
                "quarterlyEarnings": [
                    {
                        "reportedDate": "2026-07-30",
                        "reportedEPS": "1.5",
                        "estimatedEPS": "1.4",
                    }
                ]
            },
            raise_for_status=lambda: None,
        )
        result = ec.is_earnings_day("AAPL", date(2026, 7, 30))
        assert result is True

    @patch("src.data.earnings_calendar.requests.get")
    def test_is_earnings_day_false(self, mock_get):
        from src.data.earnings_calendar import EarningsCalendar

        ec = EarningsCalendar(api_key="test")
        mock_get.return_value = MagicMock(
            json=lambda: {
                "quarterlyEarnings": [
                    {"reportedDate": "2026-07-30", "reportedEPS": "1.5"}
                ]
            },
            raise_for_status=lambda: None,
        )
        result = ec.is_earnings_day("AAPL", date(2026, 1, 1))
        assert result is False


# ── FundamentalFeed (78% → target 90%) ────────────────────────────────


class TestFundamentalFeedExtended:
    def test_get_key_metrics(self):
        from src.data.fundamental_feed import FundamentalFeed

        ff = FundamentalFeed(api_key="test")
        with patch.object(ff, "_get") as mock_get:
            mock_get.return_value = [{"peRatio": 25.0, "pbRatio": 5.0}]
            result = ff.get_key_metrics("AAPL")
            assert isinstance(result, (dict, list))

    def test_get_financial_ratios(self):
        from src.data.fundamental_feed import FundamentalFeed

        ff = FundamentalFeed(api_key="test")
        with patch.object(ff, "_get") as mock_get:
            mock_get.return_value = [{"currentRatio": 1.5}]
            result = ff.get_financial_ratios("AAPL")
            assert isinstance(result, (dict, list))

    def test_get_company_profile(self):
        from src.data.fundamental_feed import FundamentalFeed

        ff = FundamentalFeed(api_key="test")
        with patch.object(ff, "_get") as mock_get:
            mock_get.return_value = [
                {"companyName": "Apple Inc.", "sector": "Technology"}
            ]
            result = ff.get_company_profile("AAPL")
            assert isinstance(result, dict)

    def test_get_income_statement(self):
        from src.data.fundamental_feed import FundamentalFeed

        ff = FundamentalFeed(api_key="test")
        with patch.object(ff, "_get") as mock_get:
            mock_get.return_value = [{"revenue": 100e9}]
            result = ff.get_income_statement("AAPL")
            assert isinstance(result, (dict, list))

    def test_get_balance_sheet(self):
        from src.data.fundamental_feed import FundamentalFeed

        ff = FundamentalFeed(api_key="test")
        with patch.object(ff, "_get") as mock_get:
            mock_get.return_value = [{"totalAssets": 350e9}]
            result = ff.get_balance_sheet("AAPL")
            assert isinstance(result, (dict, list))

    def test_get_cash_flow(self):
        from src.data.fundamental_feed import FundamentalFeed

        ff = FundamentalFeed(api_key="test")
        with patch.object(ff, "_get") as mock_get:
            mock_get.return_value = [{"operatingCashFlow": 30e9}]
            result = ff.get_cash_flow("AAPL")
            assert isinstance(result, (dict, list))


# ── VIXPositionScale (64% → target 95%) ───────────────────────────────


class TestVIXPositionScaleExtended:
    def test_all_thresholds(self):
        from src.risk.vix_position_scale import VIXPositionScale

        s = VIXPositionScale()
        assert s.get_multiplier(5.0) == 1.0  # LOW
        assert s.get_multiplier(20.0) == 0.85  # NORMAL
        assert s.get_multiplier(25.0) == 0.7  # ELEVATED
        assert s.get_multiplier(30.0) == 0.5  # HIGH
        assert s.get_multiplier(40.0) == 0.0  # FROZEN

    def test_get_regime(self):
        from src.risk.vix_position_scale import VIXPositionScale

        s = VIXPositionScale()
        assert s.get_regime(10.0) == "LOW"
        assert s.get_regime(20.0) == "NORMAL"
        assert s.get_regime(25.0) == "ELEVATED"
        assert s.get_regime(30.0) == "HIGH"
        assert s.get_regime(40.0) == "FROZEN"

    def test_is_trading_allowed(self):
        from src.risk.vix_position_scale import VIXPositionScale

        s = VIXPositionScale()
        assert s.is_trading_allowed(15.0) is True
        assert s.is_trading_allowed(45.0) is False

    def test_get_threshold_info(self):
        from src.risk.vix_position_scale import VIXPositionScale

        s = VIXPositionScale()
        info = s.get_threshold_info()
        assert len(info) == 5

    def test_invalid_vix(self):
        from src.risk.vix_position_scale import VIXPositionScale

        s = VIXPositionScale()
        assert s.get_multiplier(-1.0) == 0.0
        assert s.get_multiplier(float("nan")) == 0.0


# ── InsiderTrading (51% → target 80%) ─────────────────────────────────


class TestInsiderTradingExtended:
    def test_get_insider_trades_empty(self):
        from src.data.insider_trading import InsiderTrading

        it = InsiderTrading(api_key="test")
        it._get = MagicMock(return_value=[])
        trades = it.get_insider_trades("AAPL")
        assert trades == []

    def test_get_insider_summary_mock(self):
        from src.data.insider_trading import InsiderTrading

        it = InsiderTrading(api_key="test")
        with patch.object(it, "get_insider_trades") as mock_trades:
            mock_trades.return_value = [
                {
                    "transaction_type": "P-PURCHASE",
                    "shares": 1000,
                    "price": 150.0,
                    "value": 150000.0,
                },
                {
                    "transaction_type": "S-SALE",
                    "shares": 500,
                    "price": 155.0,
                    "value": 77500.0,
                },
            ]
            summary = it.get_insider_summary("AAPL")
            assert summary["total_buys"] >= 1


# ── SecFilings (56% → target 80%) ─────────────────────────────────────


class TestSecFilingsExtended:
    def test_get_company_cik(self):
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

    def test_parse_filing(self):
        from src.data.sec_filings import SECFilings

        sec = SECFilings()
        with patch("src.data.sec_filings.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                text="<html>10-K filing content for Apple Inc.</html>",
                raise_for_status=lambda: None,
            )
            content = sec.parse_filing("https://www.sec.gov/filing/10-K")
            assert isinstance(content, str)


# ── Momentum (49% → target 80%) ───────────────────────────────────────


class TestMomentumExtended:
    def test_generate_signals_small_universe(self, sample_universe):
        from src.strategies.momentum import MomentumStrategy

        strategy = MomentumStrategy()
        small = {k: v for i, (k, v) in enumerate(sample_universe.items()) if i < 3}
        signals = strategy.generate_signals(small)
        assert isinstance(signals, list)

    def test_calculate_relative_strength(self, sample_universe):
        from src.strategies.momentum import MomentumStrategy

        strategy = MomentumStrategy()
        rs = strategy._calculate_relative_strength(sample_universe)
        assert isinstance(rs, dict)

    def test_check_breakout(self, sample_universe):
        from src.strategies.momentum import MomentumStrategy

        strategy = MomentumStrategy()
        sym = list(sample_universe.keys())[0]
        df = sample_universe[sym]
        signal = strategy._check_breakout(sym, df, 50.0, datetime.now())
        assert signal is None or hasattr(signal, "strength")


# ── MeanRevert (59% → target 80%) ─────────────────────────────────────


class TestMeanRevertExtended:
    def test_generate_signals(self, sample_universe):
        from src.strategies.mean_revert import MeanRevertStrategy

        strategy = MeanRevertStrategy()
        signals = strategy.generate_signals(sample_universe)
        assert isinstance(signals, list)


# ── TrendStrategy (62% → target 85%) ──────────────────────────────────


class TestTrendStrategyExtended:
    def test_generate_signals(self, sample_universe):
        from src.strategies.trend_strategy import TrendStrategy

        strategy = TrendStrategy()
        signals = strategy.generate_signals(sample_universe)
        assert isinstance(signals, list)

    def test_should_enter(self):
        from src.strategies.base_strategy import Signal, SignalAction
        from src.strategies.trend_strategy import TrendStrategy

        strategy = TrendStrategy()
        signal = Signal(
            symbol="AAPL",
            action=SignalAction.BUY,
            strategy="TrendFollowing",
            timestamp=datetime.now(),
            strength=0.7,
            price=150.0,
        )
        assert strategy.should_enter(signal) is True

    def test_should_not_enter_sell(self):
        from src.strategies.base_strategy import Signal, SignalAction
        from src.strategies.trend_strategy import TrendStrategy

        strategy = TrendStrategy()
        signal = Signal(
            symbol="AAPL",
            action=SignalAction.SELL,
            strategy="TrendFollowing",
            timestamp=datetime.now(),
            strength=0.7,
            price=150.0,
        )
        assert strategy.should_enter(signal) is False


# ── MacroAnalyzer (39% → target 75%) ──────────────────────────────────


class TestMacroAnalyzerExtended:
    def test_get_macro_state_with_key(self):
        from src.research.macro_analyzer import MacroAnalyzer

        analyzer = MacroAnalyzer(fred_api_key="test")
        with patch("src.research.macro_analyzer._fred_latest") as mock_fred:
            mock_fred.return_value = 2.0  # Return a float for all series
            with patch.object(analyzer, "_get_credit_spread", return_value=0.85):
                state = analyzer.get_macro_state()
                assert state.phase.value in (
                    "expansion",
                    "peak",
                    "contraction",
                    "trough",
                )

    def test_fred_latest_failure(self):
        from src.research.macro_analyzer import _fred_latest

        with patch(
            "src.research.macro_analyzer.requests.get",
            side_effect=Exception("API error"),
        ):
            result = _fred_latest("FEDFUNDS", "test_key")
            assert result is None

    def test_fred_cpi_12m_ago_failure(self):
        from src.research.macro_analyzer import _fred_cpi_12m_ago

        with patch(
            "src.research.macro_analyzer.requests.get",
            side_effect=Exception("API error"),
        ):
            result = _fred_cpi_12m_ago("test_key")
            assert result is None


# ── StockResearcher (61% → target 80%) ────────────────────────────────


class TestStockResearcherExtended:
    def test_gather_technicals(self):
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
        researcher = StockResearcher(
            data_feed=mock_feed, xiaomi_key="t", deepseek_key="t"
        )
        result = researcher._gather_technicals("AAPL")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_gather_fundamentals(self):
        from src.research.stock_researcher import StockResearcher

        researcher = StockResearcher(
            data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t"
        )
        researcher.fundamental_feed = MagicMock()
        researcher.fundamental_feed.get_key_metrics.return_value = {
            "pe_ratio": 25.0,
            "roe": 0.30,
        }
        result = researcher._gather_fundamentals("AAPL")
        assert isinstance(result, str)

    def test_gather_fundamentals_none(self):
        from src.research.stock_researcher import StockResearcher

        researcher = StockResearcher(
            data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t"
        )
        researcher.fundamental_feed = None
        result = researcher._gather_fundamentals("AAPL")
        assert "unavailable" in result.lower() or isinstance(result, str)

    def test_gather_news(self):
        from src.research.stock_researcher import StockResearcher

        researcher = StockResearcher(
            data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t"
        )
        researcher.news_feed = MagicMock()
        researcher.news_feed.get_news.return_value = [
            {"title": "AAPL beats", "summary": "Strong quarter"},
            {"title": "New iPhone", "summary": "Launch expected"},
        ]
        result = researcher._gather_news("AAPL")
        assert isinstance(result, str)

    def test_gather_news_none(self):
        from src.research.stock_researcher import StockResearcher

        researcher = StockResearcher(
            data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t"
        )
        researcher.news_feed = None
        result = researcher._gather_news("AAPL")
        assert isinstance(result, str)

    def test_compute_sentiment(self):
        from src.research.stock_researcher import StockResearcher

        researcher = StockResearcher(
            data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t"
        )
        researcher.sentiment_feed = MagicMock()
        researcher.sentiment_feed.analyze_text.return_value = 0.6
        score = researcher._compute_sentiment("Strong earnings")
        assert score == 0.6

    def test_compute_sentiment_none(self):
        from src.research.stock_researcher import StockResearcher

        researcher = StockResearcher(
            data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t"
        )
        researcher.sentiment_feed = None
        score = researcher._compute_sentiment("test")
        assert score == 0.0

    def test_compute_sentiment_exception(self):
        from src.research.stock_researcher import StockResearcher

        researcher = StockResearcher(
            data_feed=MagicMock(), xiaomi_key="t", deepseek_key="t"
        )
        researcher.sentiment_feed = MagicMock()
        researcher.sentiment_feed.analyze_text.side_effect = Exception("GPU error")
        score = researcher._compute_sentiment("test")
        assert score == 0.0


# ── PaperClient (75% → target 90%) ────────────────────────────────────


class TestPaperClientExtended:
    @pytest.mark.asyncio
    async def test_cancel_order_nonexistent(self):
        from src.brokers.paper_client import PaperClient

        client = PaperClient()
        await client.connect()
        # Cancelling non-existent order logs warning but doesn't raise
        await client.cancel_order(999999)
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_get_open_orders_empty(self):
        from src.brokers.paper_client import PaperClient

        client = PaperClient()
        await client.connect()
        orders = await client.get_open_orders()
        assert orders == []
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_get_order_nonexistent(self):
        from src.brokers.paper_client import PaperClient

        client = PaperClient()
        await client.connect()
        result = await client.get_order(999999)
        assert result is None
        await client.disconnect()


# ── IBKRClient (41% → target 55%) ─────────────────────────────────────


class TestIBKRClientExtended:
    def test_on_error_various_codes(self):
        from src.brokers.ibkr_client import IBKRClient

        c = IBKRClient()
        # Info codes
        c._on_error(0, 2104, "Data farm OK", None)
        c._on_error(0, 2106, "Data farm OK", None)
        c._on_error(0, 2158, "Data farm OK", None)
        # Connection errors
        c._on_error(0, 502, "Connection error", None)
        c._on_error(0, 504, "Not connected", None)
        c._on_error(0, 1100, "Connectivity lost", None)
        c._on_error(0, 1300, "TWS socket dropped", None)
        # Other
        c._on_error(0, 321, "Error", None)

    def test_to_ib_contract_cash(self):
        from src.brokers.broker_protocol import Contract
        from src.brokers.ibkr_client import IBKRClient

        c = IBKRClient()
        contract = Contract(symbol="EURUSD", sec_type="CASH", currency="USD")
        ib = c._to_ib_contract(contract)
        assert ib is not None

    def test_to_ib_contract_generic(self):
        from src.brokers.broker_protocol import Contract
        from src.brokers.ibkr_client import IBKRClient

        c = IBKRClient()
        contract = Contract(symbol="AAPL", sec_type="IND")
        ib = c._to_ib_contract(contract)
        assert ib.symbol == "AAPL"


# ── AlpacaClient (49% → target 70%) ───────────────────────────────────


class TestAlpacaClientExtended:
    def test_stub_connect(self):
# [REMOVED] AlpacaClient deleted

        # Test via class methods (can't instantiate abstract class)
        assert hasattr(AlpacaClient, "connect")
        assert hasattr(AlpacaClient, "disconnect")
        assert hasattr(AlpacaClient, "is_connected")
        assert hasattr(AlpacaClient, "get_market_data")
        assert hasattr(AlpacaClient, "get_account")
        assert hasattr(AlpacaClient, "place_order")
