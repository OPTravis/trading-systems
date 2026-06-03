"""
Deep coverage tests — exercising actual code paths in core modules.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

# ── TradeExecutor: execute_auto_trade (227 missed) ────────────────────


class TestTradeExecutorDeep:

    def test_insufficient_usdt(self):
        from src.trade_executor import execute_auto_trade

        bc = MagicMock()
        bc.get_free_balance.return_value = 5.0
        with patch("src.trade_executor.get_trading_client", return_value=bc), patch(
            "src.trade_executor.FeishuNotifier"
        ), patch.dict(os.environ, {"DCA_CHECK_DISABLED": "1"}):
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
            )
            assert result["success"] is False

    def test_circuit_breaker_tripped(self):
        from src.trade_executor import execute_auto_trade

        bc = MagicMock()
        bc.get_free_balance.return_value = 10000.0
        mock_cb = MagicMock()
        mock_cb.is_tripped.return_value = True
        with patch("src.trade_executor.get_trading_client", return_value=bc), patch(
            "src.trade_executor.FeishuNotifier"
        ), patch(
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
            )
            assert result["success"] is False

    def test_price_deviation_normal(self):
        from src.trade_executor import _check_price_deviation

        mock_client = MagicMock()
        mock_client.get_klines.return_value = [
            {"close": str(100.0 + i)} for i in range(14)
        ]
        result = _check_price_deviation(mock_client, "BTCUSDT", 100.0)
        assert result is True

    def test_price_deviation_insufficient(self):
        from src.trade_executor import _check_price_deviation

        mock_client = MagicMock()
        mock_client.get_klines.return_value = [{"close": "100.0"}] * 5
        result = _check_price_deviation(mock_client, "BTCUSDT", 100.0)
        assert result is True

    def test_price_deviation_exception(self):
        from src.trade_executor import _check_price_deviation

        mock_client = MagicMock()
        mock_client.get_klines.side_effect = Exception("API error")
        result = _check_price_deviation(mock_client, "BTCUSDT", 100.0)
        assert result is True  # fail-open


# ── RiskManager (163 missed) ──────────────────────────────────────────


class TestRiskManagerDeep:
    def test_pre_trade_check_approved(self):
        from src.risk_manager import RiskManager

        rm = RiskManager()
        result = rm.pre_trade_check(
            symbol="BTCUSDT",
            price=50000.0,
            atr=1000.0,
            positions=[],
            score=80.0,
            strategy="test",
        )
        assert result["allowed"] is True

    def test_pre_trade_check_with_positions(self):
        from src.risk_manager import RiskManager

        rm = RiskManager()
        positions = [{"symbol": "BTCUSDT", "quantity": 0.1, "entry_price": 50000.0}]
        result = rm.pre_trade_check(
            symbol="ETHUSDT",
            price=3000.0,
            atr=100.0,
            positions=positions,
            score=70.0,
            strategy="test",
        )
        assert isinstance(result, dict)

    def test_post_trade_update(self):
        from src.risk_manager import RiskManager

        rm = RiskManager()
        rm.post_trade_update("BTCUSDT", 500.0, 0.1)


# ── ScanOrchestrator (150 missed) ─────────────────────────────────────


class TestScanOrchestratorDeep:
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
            cmd_scan(send_notification=False)


# ── MarketScanner (133 missed) ────────────────────────────────────────


class TestMarketScannerDeep:
    def test_init(self):
        from src.market_scanner import MarketScanner

        ms = MarketScanner(MagicMock())
        assert ms is not None

    def test_rate_limiter(self):
        from src.market_scanner import _RateLimiter

        rl = _RateLimiter(max_per_second=25)
        rl.wait()
        assert len(rl._timestamps) == 1


# ── Indicators (48 missed) ────────────────────────────────────────────


class TestIndicatorsDeep:
    def test_sma(self):
        from src.indicators import Indicators

        assert Indicators.sma([1, 2, 3, 4, 5], 3) == pytest.approx(4.0)

    def test_ema(self):
        from src.indicators import Indicators

        assert isinstance(Indicators.ema([1, 2, 3, 4, 5], 3), float)

    def test_rsi(self):
        from src.indicators import Indicators

        rsi = Indicators.rsi([100 + i for i in range(30)], 14)
        assert 0 <= rsi <= 100

    def test_macd(self):
        from src.indicators import Indicators

        result = Indicators.macd([100 + i * 0.5 for i in range(50)])
        assert "macd" in result

    def test_bollinger(self):
        from src.indicators import Indicators

        result = Indicators.bollinger_bands([100 + i for i in range(30)], 20)
        assert "upper" in result

    def test_atr(self):
        from src.indicators import Indicators

        klines = [{"high": 110, "low": 90, "close": 100}] * 20
        result = Indicators.atr(klines, 14)
        assert isinstance(result, (float, list))

    def test_obv(self):
        from src.indicators import Indicators

        klines = [{"close": 100 + i, "volume": 1000} for i in range(20)]
        result = Indicators.obv(klines)
        assert isinstance(result, (float, list))

    def test_vwap(self):
        from src.indicators import Indicators

        klines = [{"high": 105, "low": 95, "close": 100, "volume": 1000}] * 20
        result = Indicators.vwap(klines)
        assert isinstance(result, (float, list))


# ── Portfolio (65 missed) ─────────────────────────────────────────────


class TestPortfolioDeep:
    def test_add_close_summary(self, tmp_path):
        from src.portfolio import PortfolioManager
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        pm = PortfolioManager()
        pm._db = db
        pm.cash_balance = 1_000_000.0
        pm.add_position("BTCUSDT", quantity=0.1, entry_price=50000.0, deduct_cash=False)
        pm.add_position("ETHUSDT", quantity=1.0, entry_price=3000.0, deduct_cash=False)
        assert len(pm.get_all_positions()) == 2
        pm.update_position_price("BTCUSDT", 55000.0)
        result = pm.close_position("BTCUSDT", close_price=55000.0)
        assert result["pnl"] > 0
        summary = pm.get_summary()
        assert "total_value" in summary


# ── StateDB (83 missed) ───────────────────────────────────────────────


class TestStateDBDeep:
    def test_kv_set_get(self, tmp_path):
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        db.kv_set("key1", "val1")
        assert db.kv_get("key1") == "val1"


# ── FeatureStore (123 missed) ─────────────────────────────────────────


class TestFeatureStoreDeep:
    def test_get_training_data(self):
        from src.feature_store import FeatureStore

        fs = FeatureStore.__new__(FeatureStore)
        fs._redis_available = False
        fs._fallback_sorted = {"features:training:BTCUSDT": [(1.0, '{"rsi": 65}')]}
        fs._r = None
        data = fs.get_training_data("BTCUSDT")
        assert len(data) == 1


# ── DimensionScorer (72 missed) ───────────────────────────────────────


class TestDimensionScorerDeep:
    def test_score_all(self):
        from src.dimension_scorer import DimensionScorer

        mock_client = MagicMock()
        mock_client.get_ticker_price.return_value = 50000.0
        mock_client.get_klines.return_value = []
        ds = DimensionScorer(binance_client=mock_client)
        result = ds.score_all()
        assert "resonance" in result
        assert "weighted_score" in result
        assert "dimensions" in result


# ── StrategyAdaptor (90 missed) ───────────────────────────────────────


class TestStrategyAdaptorDeep:
    def test_adapt_fear(self):
        from src.strategy_adaptor import StrategyAdaptor

        sa = StrategyAdaptor()
        result = sa.adapt(fear_greed=20, btc_trend="down", btc_price_change_24h=-5.0)
        assert isinstance(result, dict)

    def test_adapt_greed(self):
        from src.strategy_adaptor import StrategyAdaptor

        sa = StrategyAdaptor()
        result = sa.adapt(fear_greed=80, btc_trend="up", btc_price_change_24h=5.0)
        assert isinstance(result, dict)


# ── PositionOptimizer (92 missed) ─────────────────────────────────────


class TestPositionOptimizerDeep:
    def test_analyze_and_switch(self):
        from src.position_optimizer import PositionOptimizer

        po = PositionOptimizer(MagicMock(), MagicMock(), MagicMock())
        result = po.analyze_and_switch(dry_run=True, opportunities=[])
        assert isinstance(result, list)


# ── Notifier (93 missed) ──────────────────────────────────────────────


class TestNotifierDeep:
    def test_send_text_disabled(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier()
        result = n.send_text("test")
        assert isinstance(result, bool)


# ── PendingConfirmation (53 missed) ───────────────────────────────────


class TestPendingConfirmationDeep:
    def test_save_load_clear(self, tmp_path):
        from src import pending_confirmation

        pending_confirmation.DATA_DIR = tmp_path
        pending_confirmation.save_pending({"symbol": "BTCUSDT"})
        assert pending_confirmation.load_pending() is not None
        pending_confirmation.clear_pending()
        assert pending_confirmation.load_pending() is None
