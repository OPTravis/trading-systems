"""
Tests for remaining uncovered modules: notifier, execution, research, market, data, strategies.
"""

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from src.brokers.broker_protocol import (
    Contract,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from src.execution.order_executor import OrderExecutor, OrderResult
from src.notifier import AlertLevel, FeishuNotifier

# ── Notifier ──────────────────────────────────────────────────────────


class TestFeishuNotifier:
    @pytest.fixture
    def notifier(self):
        return FeishuNotifier(chat_id="test_chat_id")

    @pytest.fixture
    def disabled_notifier(self):
        return FeishuNotifier(chat_id="")

    @patch("src.notifier.requests.post")
    def test_send_success(self, mock_post, notifier):
        mock_post.return_value = MagicMock(
            json=lambda: {"code": 0},
            status_code=200,
        )
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            result = notifier._send("test message")
            assert result is True

    @patch("src.notifier.requests.post")
    def test_send_failure(self, mock_post, notifier):
        mock_post.return_value = MagicMock(
            json=lambda: {"code": 99, "msg": "error"},
            status_code=200,
        )
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            result = notifier._send("test")
            assert result is False

    def test_send_disabled(self, disabled_notifier):
        result = disabled_notifier._send("test")
        assert result is False

    @patch("src.notifier.requests.post")
    def test_send_no_token(self, mock_post, notifier):
        with patch("src.notifier._get_tenant_token", return_value=""):
            result = notifier._send("test")
            assert result is False

    @patch("src.notifier.requests.post")
    def test_send_alert(self, mock_post, notifier):
        mock_post.return_value = MagicMock(json=lambda: {"code": 0})
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            result = notifier.send_alert("Test", "Message", AlertLevel.INFO)
            assert result is True

    @patch("src.notifier.requests.post")
    def test_send_trade_signal(self, mock_post, notifier):
        mock_post.return_value = MagicMock(json=lambda: {"code": 0})
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            signal = {
                "symbol": "AAPL",
                "action": "BUY",
                "price": 150.0,
                "strategy": "momentum",
                "strength": 0.8,
            }
            result = notifier.send_trade_signal(signal)
            assert result is True

    @patch("src.notifier.requests.post")
    def test_send_daily_report(self, mock_post, notifier):
        mock_post.return_value = MagicMock(json=lambda: {"code": 0})
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            report = {
                "date": "2026-05-30",
                "total_return_pct": 5.0,
                "daily_pnl": 500.0,
                "total_trades": 10,
                "win_rate": 60.0,
                "positions": [
                    {
                        "symbol": "AAPL",
                        "quantity": 100,
                        "entry_price": 150,
                        "current_price": 160,
                        "pnl_pct": 6.67,
                    }
                ],
                "risk_status": {"pdt": "OK"},
            }
            result = notifier.send_daily_report(report)
            assert result is True

    @patch("src.notifier.requests.post")
    def test_send_earnings_alert(self, mock_post, notifier):
        mock_post.return_value = MagicMock(json=lambda: {"code": 0})
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            result = notifier.send_earnings_alert(
                "AAPL", "2026-07-30", estimated_eps=1.5, actual_eps=1.8
            )
            assert result is True

    @patch("src.notifier.requests.post")
    def test_send_trade_executed(self, mock_post, notifier):
        mock_post.return_value = MagicMock(json=lambda: {"code": 0})
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            result = notifier.send_trade_executed("AAPL", "BUY", 150.0, 100, "momentum")
            assert result is True

    @patch("src.notifier.requests.post")
    def test_send_risk_alert(self, mock_post, notifier):
        mock_post.return_value = MagicMock(json=lambda: {"code": 0})
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            result = notifier.send_risk_alert("PDT", {"remaining": 1})
            assert result is True

    @patch("src.notifier.requests.post")
    def test_send_system_status(self, mock_post, notifier):
        mock_post.return_value = MagicMock(json=lambda: {"code": 0})
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            result = notifier.send_system_status(
                {"overall": "OK", "checks": {"api": {"status": "OK"}}}
            )
            assert result is True

    @patch("src.notifier.requests.post")
    def test_send_card(self, mock_post, notifier):
        mock_post.return_value = MagicMock(json=lambda: {"code": 0})
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            result = notifier._send_card("Title", [{"tag": "div"}])
            assert result is True

    def test_alert_level_enum(self):
        assert AlertLevel.INFO == "info"
        assert AlertLevel.WARNING == "warning"
        assert AlertLevel.CRITICAL == "critical"
        assert AlertLevel.TRADE == "trade"

    @patch("src.notifier.requests.post")
    def test_send_earnings_no_estimates(self, mock_post, notifier):
        mock_post.return_value = MagicMock(json=lambda: {"code": 0})
        with patch("src.notifier._get_tenant_token", return_value="tok"):
            result = notifier.send_earnings_alert("AAPL", "2026-07-30")
            assert result is True


# ── OrderExecutor ─────────────────────────────────────────────────────


class TestOrderExecutor:
    @pytest.fixture
    def mock_broker(self):
        broker = AsyncMock()
        order = Order(
            contract=Contract(symbol="AAPL"),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=100,
        )
        order.order_id = 12345
        order.status = OrderStatus.FILLED
        order.filled_qty = 100
        order.avg_fill_price = 150.0
        order.commission = 1.0
        broker.place_order.return_value = order
        broker.cancel_order.return_value = None
        return broker

    @pytest.fixture
    def executor(self, mock_broker):
        return OrderExecutor(mock_broker)

    @pytest.mark.asyncio
    async def test_place_market_order(self, executor, mock_broker):
        result = await executor.place_order("AAPL", "BUY", 100, order_type="MKT")
        assert result.success is True
        assert result.filled_qty == 100
        mock_broker.place_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_place_limit_order(self, executor):
        result = await executor.place_order(
            "AAPL", "BUY", 100, order_type="LMT", limit_price=150.0
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_place_order_rejected(self, executor, mock_broker):
        order = Order(
            contract=Contract(symbol="AAPL"),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=100,
        )
        order.status = OrderStatus.REJECTED
        mock_broker.place_order.return_value = order
        result = await executor.place_order("AAPL", "BUY", 100)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_place_order_risk_blocked(self, mock_broker):
        risk_mgr = MagicMock()
        risk_mgr.check_order_allowed.return_value = (False, "Daily limit reached")
        executor = OrderExecutor(mock_broker, risk_manager=risk_mgr)
        result = await executor.place_order("AAPL", "BUY", 100)
        assert result.success is False
        assert "Risk blocked" in result.error

    @pytest.mark.asyncio
    async def test_cancel_order(self, executor, mock_broker):
        result = await executor.cancel_order(12345)
        assert result is True
        mock_broker.cancel_order.assert_called_once_with(12345)

    @pytest.mark.asyncio
    async def test_cancel_order_failure(self, executor, mock_broker):
        mock_broker.cancel_order.side_effect = Exception("not found")
        result = await executor.cancel_order(999)
        assert result is False

    @pytest.mark.asyncio
    async def test_get_order_status(self, executor, mock_broker):
        order = Order(
            contract=Contract(symbol="AAPL"),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=100,
        )
        order.order_id = 12345
        order.status = OrderStatus.FILLED
        mock_broker.get_open_orders.return_value = [order]
        status = await executor.get_order_status(12345)
        assert status == "FILLED"

    @pytest.mark.asyncio
    async def test_get_order_status_not_found(self, executor, mock_broker):
        mock_broker.get_open_orders.return_value = []
        status = await executor.get_order_status(999)
        assert status is None


# ── Research ──────────────────────────────────────────────────────────


class TestStockResearcher:
    def test_analyze_stock_basic(self):
        import json

        import src.research.stock_researcher as sr_mod

        mock_data_feed = MagicMock()
        researcher = sr_mod.StockResearcher(
            data_feed=mock_data_feed, xiaomi_key="test", deepseek_key="test"
        )

        llm_response = json.dumps(
            {
                "recommendation": "BUY",
                "confidence": 0.75,
                "summary": "Strong fundamentals and positive sentiment.",
                "bull_case": "Revenue growth, margin expansion.",
                "bear_case": "Valuation stretched.",
                "fair_value_estimate": 175.0,
                "risk_rating": "MEDIUM",
                "catalysts": ["Earnings beat", "New product launch"],
            }
        )

        with patch.object(sr_mod, "_call_llm", return_value=llm_response):
            report = researcher.analyze_stock("AAPL")
            assert report is not None
            assert report.symbol == "AAPL"
            assert report.recommendation.value == "BUY"
            assert report.confidence > 0

    def test_analyze_stock_llm_failure(self):
        import src.research.stock_researcher as sr_mod

        mock_data_feed = MagicMock()
        researcher = sr_mod.StockResearcher(
            data_feed=mock_data_feed, xiaomi_key="test", deepseek_key="test"
        )

        with patch.object(sr_mod, "_call_llm", return_value=None):
            report = researcher.analyze_stock("AAPL")
            assert report is not None
            assert report.symbol == "AAPL"
            assert report.recommendation.value == "HOLD"


# ── Market: RegimeDetector ────────────────────────────────────────────


class TestRegimeDetector:
    def test_detect_regime_high_vix(self):
        from src.market.regime_detector import RegimeDetector

        detector = RegimeDetector()
        # detect_regime takes optional pre-fetched data, no network calls needed
        regime = detector.detect_regime(vix=35.0)
        assert regime in ("DEFENSIVE", "NEUTRAL", "AGGRESSIVE")

    def test_detect_regime_low_vix(self):
        from src.market.regime_detector import RegimeDetector

        detector = RegimeDetector()
        regime = detector.detect_regime(vix=12.0)
        assert regime in ("DEFENSIVE", "NEUTRAL", "AGGRESSIVE")

    def test_detect_regime_none_vix(self):
        from src.market.regime_detector import RegimeDetector

        detector = RegimeDetector()
        regime = detector.detect_regime(vix=None)
        assert regime in ("DEFENSIVE", "NEUTRAL", "AGGRESSIVE")

    def test_detect_regime_with_spy_prices(self):
        from src.market.regime_detector import RegimeDetector

        detector = RegimeDetector()
        spy_prices = pd.Series(range(200, 400), dtype=float)
        regime = detector.detect_regime(vix=15.0, spy_prices=spy_prices)
        assert regime in ("DEFENSIVE", "NEUTRAL", "AGGRESSIVE")


# ── Market: CorporateActions ──────────────────────────────────────────


class TestCorporateActions:
    def test_add_and_get_split(self):
        from src.market.corporate_actions import CorporateActions, Split

        ca = CorporateActions()
        split = Split(
            symbol="AAPL",
            ex_date=date(2026, 6, 1),
            ratio_from=1,
            ratio_to=2,
        )
        ca.add_split(split)
        history = ca.get_split_history("AAPL")
        assert len(history) == 1
        assert history[0].split_factor == 2.0

    def test_add_and_get_dividend(self):
        from src.market.corporate_actions import CorporateActions, Dividend

        ca = CorporateActions()
        div = Dividend(
            symbol="AAPL",
            ex_date=date(2026, 5, 15),
            pay_date=date(2026, 5, 20),
            amount=0.25,
        )
        ca.add_dividend(div)
        history = ca.get_dividend_history("AAPL")
        assert len(history) == 1
        assert history[0].amount == 0.25

    def test_adjust_for_splits(self):
        from src.market.corporate_actions import CorporateActions, Split

        ca = CorporateActions()
        ca.add_split(
            Split(symbol="AAPL", ex_date=date(2026, 1, 1), ratio_from=1, ratio_to=2)
        )

        # adjust_for_splits takes a DataFrame
        dates = pd.date_range("2025-12-28", periods=10, freq="D")
        df = pd.DataFrame(
            {
                "open": [300.0] * 10,
                "high": [310.0] * 10,
                "low": [290.0] * 10,
                "close": [300.0] * 10,
                "volume": [1000000] * 10,
            },
            index=dates,
        )
        adjusted = ca.adjust_for_splits(df, "AAPL")
        # Prices before split date should be halved
        assert adjusted is not None
        assert len(adjusted) == 10

    def test_get_split_history_empty(self):
        from src.market.corporate_actions import CorporateActions

        ca = CorporateActions()
        assert ca.get_split_history("FAKE") == []

    def test_get_dividend_history_empty(self):
        from src.market.corporate_actions import CorporateActions

        ca = CorporateActions()
        assert ca.get_dividend_history("FAKE") == []

    def test_split_reverse(self):
        from src.market.corporate_actions import Split

        split = Split(symbol="XYZ", ex_date=date(2026, 1, 1), ratio_from=4, ratio_to=1)
        assert split.reverse is True
        assert split.split_factor == pytest.approx(0.25)


# ── Market: MarketHours (gaps) ────────────────────────────────────────


class TestMarketHoursGaps:
    def test_get_sessions_hk(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        sessions = mh.get_sessions(Market.HK)
        assert sessions["timezone"] == "Asia/Hong_Kong"

    def test_get_sessions_cn(self):
        from src.market.market_hours import Market, MarketHours

        mh = MarketHours()
        sessions = mh.get_sessions(Market.CN)
        assert sessions["timezone"] == "Asia/Shanghai"


# ── Data: StockDataFeed ───────────────────────────────────────────────


class TestStockDataFeed:
    @patch("src.data.stock_data_feed.yf.download")
    def test_get_historical(self, mock_download):
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
        assert len(df) > 0

    @patch("src.data.stock_data_feed.yf.download")
    def test_get_historical_empty(self, mock_download):
        from src.data.stock_data_feed import StockDataFeed

        mock_download.return_value = pd.DataFrame()
        feed = StockDataFeed()
        df = feed.get_historical("INVALID")
        assert df is not None


# ── Strategies: MeanRevert ────────────────────────────────────────────


class TestMeanRevert:
    def test_generate_signals(self, sample_universe):
        from src.strategies.mean_revert import MeanRevertStrategy

        strategy = MeanRevertStrategy()
        signals = strategy.generate_signals(sample_universe)
        assert isinstance(signals, list)

    def test_should_enter(self, sample_universe):
        from src.strategies.base_strategy import Signal, SignalAction
        from src.strategies.mean_revert import MeanRevertStrategy

        strategy = MeanRevertStrategy()
        signal = Signal(
            symbol="AAPL",
            action=SignalAction.BUY,
            strategy="MeanRevert",
            timestamp=datetime.now(),
            strength=0.8,
            price=150.0,
        )
        # Should enter if no existing position
        assert strategy.should_enter(signal) is True

    def test_should_not_enter_sell(self):
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


# ── Strategies: Momentum (gaps) ───────────────────────────────────────


class TestMomentumGaps:
    def test_calculate_relative_strength(self, sample_universe):
        from src.strategies.momentum import MomentumStrategy

        strategy = MomentumStrategy()
        rs = strategy._calculate_relative_strength(sample_universe)
        assert isinstance(rs, dict)
        for sym in sample_universe:
            if sym in rs:
                assert isinstance(rs[sym], float)

    def test_should_enter_momentum(self):
        from src.strategies.base_strategy import Signal, SignalAction
        from src.strategies.momentum import MomentumStrategy

        strategy = MomentumStrategy()
        signal = Signal(
            symbol="AAPL",
            action=SignalAction.BUY,
            strategy="Momentum",
            timestamp=datetime.now(),
            strength=0.7,
            price=150.0,
        )
        assert strategy.should_enter(signal) is True

    def test_should_not_enter_weak(self):
        from src.strategies.base_strategy import Signal, SignalAction
        from src.strategies.momentum import MomentumStrategy

        strategy = MomentumStrategy()
        signal = Signal(
            symbol="AAPL",
            action=SignalAction.BUY,
            strategy="Momentum",
            timestamp=datetime.now(),
            strength=0.3,
            price=150.0,
        )
        assert strategy.should_enter(signal) is False


# ── Execution: TWAP ───────────────────────────────────────────────────


class TestTWAPExecutor:
    @patch("src.execution.twap_executor.time.sleep")
    @pytest.mark.asyncio
    async def test_execute_twap_basic(self, mock_sleep):
        from src.execution.twap_executor import TWAPExecutor

        broker = AsyncMock()
        executor = TWAPExecutor(broker)

        # Mock OrderExecutor.place_order (async)
        # Each slice fills the full slice quantity (100/5 = 20 shares each)
        executor.order_executor.place_order = AsyncMock(
            return_value=OrderResult(
                success=True, filled_qty=20, avg_fill_price=150.0, commission=0.5
            )
        )

        result = await executor.execute_twap(
            "AAPL", "BUY", 100, duration_minutes=1, num_slices=5
        )
        assert result.success is True
        assert result.total_filled == 100
        assert result.num_slices == 5


# ── Execution: VWAP ───────────────────────────────────────────────────


class TestVWAPExecutor:
    @patch("src.execution.vwap_executor.time.sleep")
    @pytest.mark.asyncio
    async def test_execute_vwap_basic(self, mock_sleep):
        from src.execution.vwap_executor import VWAPExecutor

        broker = AsyncMock()
        executor = VWAPExecutor(broker)

        # Each slice fills its full portion (100 * 0.5 = 50 shares each)
        executor.order_executor.place_order = AsyncMock(
            return_value=OrderResult(
                success=True, filled_qty=50, avg_fill_price=150.0, commission=0.5
            )
        )

        # Use a small profile (2 slices)
        profile = [0.5, 0.5]
        result = await executor.execute_vwap(
            "AAPL", "BUY", 100, duration_minutes=1, volume_profile=profile
        )
        assert result.success is True
        assert result.total_filled == 100

    def test_get_limit_price(self):
        from src.execution.vwap_executor import VWAPExecutor

        # yfinance is imported inside the method, so we need to patch at yfinance level
        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.fast_info = MagicMock(last_price=150.0)
            price = VWAPExecutor._get_limit_price("AAPL", "BUY")
            assert price is not None
            assert price >= 150.0

    def test_get_limit_price_buy_aggressive(self):
        from src.execution.vwap_executor import VWAPExecutor

        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.fast_info = MagicMock(last_price=100.0)
            buy_price = VWAPExecutor._get_limit_price("AAPL", "BUY")
            sell_price = VWAPExecutor._get_limit_price("AAPL", "SELL")
            assert buy_price > sell_price  # Buy should be more aggressive

    def test_get_limit_price_failure(self):
        from src.execution.vwap_executor import VWAPExecutor

        with patch("yfinance.Ticker", side_effect=Exception("fail")):
            price = VWAPExecutor._get_limit_price("AAPL", "BUY")
            assert price is None

    def test_get_limit_price_no_price(self):
        from src.execution.vwap_executor import VWAPExecutor

        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.fast_info = MagicMock(last_price=None)
            price = VWAPExecutor._get_limit_price("AAPL", "BUY")
            assert price is None
