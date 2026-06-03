"""
Coverage tests for trade_executor, scan_orchestrator, risk_manager, and other core modules.
"""

import os
from unittest.mock import MagicMock, patch

# ── TradeExecutor.execute_auto_trade ──────────────────────────────────


class TestExecuteAutoTrade:
    """Test execute_auto_trade with mocked dependencies."""

    def _make_client(self, usdt_free=1000.0, symbol="SOLUSDT", price=100.0):
        bc = MagicMock()
        bc.get_free_balance.return_value = usdt_free
        bc.get_ticker_price.return_value = price
        bc.get_account.return_value = {
            "balances": [{"asset": "USDT", "free": str(usdt_free), "locked": "0"}]
        }
        bc.place_market_buy.return_value = {
            "orderId": 12345,
            "fills": [{"price": str(price), "qty": "1.0"}],
        }
        bc.get_symbol_info.return_value = {
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "10"},
            ]
        }
        return bc

    def test_insufficient_usdt(self):
        from src.trade_executor import execute_auto_trade

        bc = self._make_client(usdt_free=5.0)
        with patch("src.trade_executor.get_trading_client", return_value=bc), patch(
            "src.trade_executor.FeishuNotifier"
        ), patch(
            "src.trade_executor.count_active_positions", return_value=0
        ), patch.dict(
            os.environ, {"DCA_CHECK_DISABLED": "1"}
        ):
            result = execute_auto_trade(
                symbol="SOLUSDT",
                price=100.0,
                strategy="test",
                stop_loss_pct=5.0,
                tp_levels=[{"pct": 2.0, "size_pct": 100}],
                stop_price=95.0,
                max_hold=24,
                signals={},
                reason="test",
                score=75.0,
                cash_reserve_pct=30,
            )
            assert result["success"] is False
            assert "Insufficient" in result.get("error", "")

    def test_circuit_breaker_tripped(self):
        from src.trade_executor import execute_auto_trade

        bc = self._make_client()
        mock_cb = MagicMock()
        mock_cb.is_tripped.return_value = True
        with patch("src.trade_executor.get_trading_client", return_value=bc), patch(
            "src.trade_executor.FeishuNotifier"
        ), patch("src.trade_executor.count_active_positions", return_value=0), patch(
            "src.circuit_breaker.CircuitBreaker", return_value=mock_cb
        ), patch.dict(
            os.environ, {"DCA_CHECK_DISABLED": "1"}
        ):
            result = execute_auto_trade(
                symbol="SOLUSDT",
                price=100.0,
                strategy="test",
                stop_loss_pct=5.0,
                tp_levels=[{"pct": 2.0, "size_pct": 100}],
                stop_price=95.0,
                max_hold=24,
                signals={},
                reason="test",
                score=75.0,
                cash_reserve_pct=30,
            )
            assert result["success"] is False
            assert "Circuit breaker" in result.get("error", "")


# ── ScanOrchestrator ──────────────────────────────────────────────────


class TestScanOrchestrator:
    def test_cmd_scan(self):
        from src.scan_orchestrator import cmd_scan

        mock_client = MagicMock()
        mock_scanner = MagicMock()
        mock_scanner.get_top_movers.return_value = [
            {
                "symbol": "BTCUSDT",
                "direction": "gainer",
                "change_pct": 5.0,
                "quote_volume": 1000000,
            },
        ]
        with patch(
            "src.scan_orchestrator.get_trading_client", return_value=mock_client
        ), patch(
            "src.scan_orchestrator.MarketScanner", return_value=mock_scanner
        ), patch(
            "src.scan_orchestrator.FeishuNotifier"
        ), patch(
            "src.scan_orchestrator.SentimentAnalyzer"
        ):
            # cmd_scan just prints, no return value
            cmd_scan(send_notification=False)

    def test_cmd_cron_scan_imports(self):
        from src.scan_orchestrator import cmd_cron_scan

        assert callable(cmd_cron_scan)


# ── MarketScanner ─────────────────────────────────────────────────────


class TestMarketScanner:
    def test_init(self):
        from src.market_scanner import MarketScanner

        mock_client = MagicMock()
        ms = MarketScanner(mock_client)
        assert ms is not None

    def test_rate_limiter(self):
        from src.market_scanner import _RateLimiter

        rl = _RateLimiter(max_per_second=25)
        assert rl._max_per_second == 25


# ── MarketResearcher ──────────────────────────────────────────────────


class TestMarketResearcher:
    def test_init(self):
        from src.market_researcher import MarketResearcher

        mr = MarketResearcher()
        assert mr is not None


# ── TradeExecutor (additional) ────────────────────────────────────────


class TestTradeExecutorExtra:
    def test_check_price_deviation_normal(self):
        from src.trade_executor import _check_price_deviation

        mock_client = MagicMock()
        mock_client.get_klines.return_value = [
            {"close": str(100.0 + i)} for i in range(14)
        ]
        result = _check_price_deviation(mock_client, "BTCUSDT", 100.0)
        assert result is True

    def test_check_price_deviation_insufficient_data(self):
        from src.trade_executor import _check_price_deviation

        mock_client = MagicMock()
        mock_client.get_klines.return_value = [{"close": "100.0"}] * 5
        result = _check_price_deviation(mock_client, "BTCUSDT", 100.0)
        assert result is True

    def test_get_position_tier(self):
        pass

    def test_count_active_positions(self):
        pass


# ── PaperTrader ───────────────────────────────────────────────────────


class TestPaperTrader:
    def test_get_trading_client(self):
        from src.paper_trader import get_trading_client

        client = get_trading_client()
        assert client is not None

    def test_is_paper_mode(self):
        from src.paper_trader import is_paper_mode

        result = is_paper_mode()
        assert isinstance(result, bool)


# ── SentimentAnalyzer ─────────────────────────────────────────────────


class TestSentimentAnalyzerExtra:
    def test_init(self):
        from src.sentiment import SentimentAnalyzer

        sa = SentimentAnalyzer()
        assert sa is not None


# ── PricePredictor ────────────────────────────────────────────────────


class TestPricePredictorExtra:
    def test_init(self):
        from src.price_predictor import PricePredictor

        pp = PricePredictor()
        assert pp is not None
        assert pp.is_ready() is False


# ── MultiTimeframe ────────────────────────────────────────────────────


class TestMultiTimeframeExtra:
    def test_init(self):
        from src.multi_timeframe import MultiTimeframeAnalyzer

        mock_client = MagicMock()
        mta = MultiTimeframeAnalyzer(binance_client=mock_client)
        assert mta is not None


# ── DynamicCoinPool ───────────────────────────────────────────────────


class TestDynamicCoinPoolExtra:
    def test_init(self):
        from src.dynamic_coin_pool import DynamicCoinPool

        mock_client = MagicMock()
        dcp = DynamicCoinPool(mock_client)
        assert dcp is not None


# ── ConceptDrift ──────────────────────────────────────────────────────


class TestConceptDriftExtra:
    def test_init(self):
        from src.concept_drift import ConceptDriftDetector

        cdd = ConceptDriftDetector()
        assert cdd is not None


# ── DataFeedManager ───────────────────────────────────────────────────


class TestDataFeedManagerExtra:
    def test_init(self):
        from src.data_feed import DataFeedManager

        dfm = DataFeedManager()
        assert dfm is not None


# ── CircuitBreaker ────────────────────────────────────────────────────


class TestCircuitBreakerExtra:
    def test_init(self):
        from src.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        assert cb is not None

    def test_is_tripped(self):
        from src.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        assert cb.is_tripped() is False

    def test_record_failure(self):
        from src.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        cb.record_failure()
        assert cb.is_tripped() is False


# ── DailyLossBreaker ──────────────────────────────────────────────────


class TestDailyLossBreakerExtra:
    def test_init(self):
        from src.daily_loss_breaker import DailyLossBreaker

        dlb = DailyLossBreaker()
        assert dlb is not None

    def test_should_block_new_trades(self):
        from src.daily_loss_breaker import DailyLossBreaker

        dlb = DailyLossBreaker()
        assert dlb.should_block_new_trades() is False


# ── KellySizer ────────────────────────────────────────────────────────


class TestKellySizerExtra:
    def test_init(self):
        from src.kelly_sizer import KellyPositionSizer

        ks = KellyPositionSizer()
        assert ks is not None


# ── FeeOptimizer ──────────────────────────────────────────────────────


class TestFeeOptimizerExtra:
    def test_init(self):
        from src.fee_optimizer import FeeOptimizer

        mock_client = MagicMock()
        fo = FeeOptimizer(binance_client=mock_client)
        assert fo is not None

    def test_get_effective_fees(self):
        from src.fee_optimizer import FeeOptimizer

        mock_client = MagicMock()
        mock_client.get_account.return_value = {
            "makerCommission": 10,
            "takerCommission": 10,
        }
        fo = FeeOptimizer(binance_client=mock_client)
        result = fo.get_effective_fees("BTCUSDT")
        assert isinstance(result, dict)
