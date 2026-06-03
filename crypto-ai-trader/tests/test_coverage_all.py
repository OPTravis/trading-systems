"""
Comprehensive coverage tests for all remaining uncovered modules.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

# ── FeatureStore (123 missed) ─────────────────────────────────────────


class TestFeatureStore:
    def test_init_no_redis(self):
        from src.feature_store import FeatureStore

        fs = FeatureStore.__new__(FeatureStore)
        fs._redis_available = False
        fs._fallback = {}
        fs._r = None
        assert fs._redis_available is False
        assert fs._r is None

    def test_init_with_redis(self):
        from src.feature_store import FeatureStore

        mock_r = MagicMock()
        mock_r.ping.return_value = True
        with patch("redis.Redis", return_value=mock_r):
            fs = FeatureStore()
            assert fs._redis_available is True

    def test_store_features_fallback(self):
        from src.feature_store import FeatureStore

        fs = FeatureStore.__new__(FeatureStore)
        fs._redis_available = False
        fs._fallback = {}
        fs._r = None
        result = fs.store_features("BTCUSDT", {"rsi": 65.0}, namespace="online")
        assert result is True
        assert "features:online:BTCUSDT" in fs._fallback

    def test_store_features_redis(self):
        from src.feature_store import FeatureStore

        mock_r = MagicMock()
        mock_pipe = MagicMock()
        mock_r.pipeline.return_value = mock_pipe
        fs = FeatureStore.__new__(FeatureStore)
        fs._redis_available = True
        fs._r = mock_r
        fs._fallback = {}
        result = fs.store_features("BTCUSDT", {"rsi": 65.0}, namespace="online")
        assert result is True

    def test_get_features_fallback(self):
        from src.feature_store import FeatureStore

        fs = FeatureStore.__new__(FeatureStore)
        fs._redis_available = False
        fs._fallback = {"features:online:BTCUSDT": {"rsi": "65.0"}}
        fs._r = None
        result = fs.get_features("BTCUSDT", namespace="online")
        assert result is not None

    def test_get_features_missing(self):
        from src.feature_store import FeatureStore

        fs = FeatureStore.__new__(FeatureStore)
        fs._redis_available = False
        fs._fallback = {}
        fs._r = None
        result = fs.get_features("BTCUSDT", namespace="online")
        assert result is None

    def test_get_training_data(self):
        from src.feature_store import FeatureStore

        fs = FeatureStore.__new__(FeatureStore)
        fs._redis_available = False
        fs._fallback_sorted = {"features:training:BTCUSDT": [(1.0, '{"rsi": 65}')]}
        fs._r = None
        data = fs.get_training_data("BTCUSDT")
        assert len(data) == 1


# ── OnlineLearner (146 missed) ────────────────────────────────────────


class TestOnlineLearner:
    def test_default_weights(self):
        from src.online_learner import DEFAULT_WEIGHTS

        assert "technical" in DEFAULT_WEIGHTS
        assert sum(DEFAULT_WEIGHTS.values()) > 0

    def test_factor_names(self):
        from src.online_learner import FACTOR_NAMES

        assert len(FACTOR_NAMES) > 0
        assert "technical" in FACTOR_NAMES

    def test_class_exists(self):
        from src.online_learner import OnlineLearner

        assert OnlineLearner is not None


# ── CVaRRisk (72 missed) ──────────────────────────────────────────────


class TestCVaRRisk:
    def test_compute_cvar(self):
        from src.cvar_risk import CVaRRiskManager

        cvar = CVaRRiskManager()
        returns = [-0.05, -0.03, -0.01, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08]
        result = cvar.compute_cvar(returns, alpha=0.05)
        assert isinstance(result, float)

    def test_compute_cvar_empty(self):
        from src.cvar_risk import CVaRRiskManager

        cvar = CVaRRiskManager()
        assert cvar.compute_cvar([]) == 0.0
        assert cvar.compute_cvar([1, 2, 3]) == 0.0  # < 10 items

    def test_compute_var(self):
        from src.cvar_risk import CVaRRiskManager

        cvar = CVaRRiskManager()
        returns = [-0.05, -0.03, -0.01, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08]
        result = cvar.compute_var(returns, alpha=0.05)
        assert isinstance(result, float)

    def test_compute_var_empty(self):
        from src.cvar_risk import CVaRRiskManager

        cvar = CVaRRiskManager()
        assert cvar.compute_var([]) == 0.0

    def test_compute_portfolio_risk(self):
        from src.cvar_risk import CVaRRiskManager

        cvar = CVaRRiskManager()
        positions = [
            {
                "symbol": "BTCUSDT",
                "entry_price": 50000,
                "current_price": 55000,
                "quantity": 0.1,
                "historical_returns": [
                    -0.05,
                    -0.03,
                    -0.01,
                    0.01,
                    0.02,
                    0.03,
                    0.04,
                    0.05,
                    0.06,
                    0.07,
                    0.08,
                ],
            },
        ]
        result = cvar.compute_portfolio_risk(positions)
        assert "portfolio_cvar_95" in result
        assert "risk_level" in result

    def test_init(self):
        from src.cvar_risk import CVaRRiskManager

        cvar = CVaRRiskManager()
        assert cvar is not None


# ── StepwiseDrawdown (32 missed) ──────────────────────────────────────


class TestCircuitBreaker:
    def test_init(self):
        from src.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        assert cb.is_tripped() is False

    def test_record_failure(self):
        from src.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        cb.record_failure()
        assert cb.is_tripped() is False

    def test_record_success(self):
        from src.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        cb.record_success()
        assert cb.is_tripped() is False


# ── DailyLossBreaker (38 missed) ──────────────────────────────────────


class TestDailyLossBreaker:
    def test_init(self):
        from src.daily_loss_breaker import DailyLossBreaker

        dlb = DailyLossBreaker()
        assert dlb.should_block_new_trades() is False


# ── DrawdownBreaker (12 missed) ───────────────────────────────────────


class TestKellySizer:
    def test_init(self):
        from src.kelly_sizer import KellyPositionSizer

        ks = KellyPositionSizer()
        assert ks is not None


# ── FeeOptimizer (51 missed) ──────────────────────────────────────────


class TestFeeOptimizer:
    def test_init(self):
        from src.fee_optimizer import FeeOptimizer

        fo = FeeOptimizer(MagicMock())
        assert fo is not None

    def test_get_effective_fees(self):
        from src.fee_optimizer import FeeOptimizer

        mock_client = MagicMock()
        mock_client.get_account.return_value = {
            "makerCommission": 10,
            "takerCommission": 10,
        }
        fo = FeeOptimizer(mock_client)
        result = fo.get_effective_fees("BTCUSDT")
        assert isinstance(result, dict)


# ── DimensionScorer (72 missed) ───────────────────────────────────────


class TestDimensionScorer:
    def test_score_all(self):
        from src.dimension_scorer import DimensionScorer

        mock_client = MagicMock()
        mock_client.get_ticker_price.return_value = 50000.0
        mock_client.get_klines.return_value = []
        ds = DimensionScorer(binance_client=mock_client)
        result = ds.score_all()
        assert "resonance" in result
        assert "weighted_score" in result


# ── StrategyAdaptor (82 missed) ───────────────────────────────────────


class TestStrategyAdaptor:
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

    def test_adapt_neutral(self):
        from src.strategy_adaptor import StrategyAdaptor

        sa = StrategyAdaptor()
        result = sa.adapt(fear_greed=50, btc_trend="flat", btc_price_change_24h=0.5)
        assert isinstance(result, dict)


# ── PositionOptimizer (92 missed) ─────────────────────────────────────


class TestPositionOptimizer:
    def test_init(self):
        from src.position_optimizer import PositionOptimizer

        po = PositionOptimizer(MagicMock(), MagicMock(), MagicMock())
        assert po is not None

    def test_analyze_and_switch(self):
        from src.position_optimizer import PositionOptimizer

        po = PositionOptimizer(MagicMock(), MagicMock(), MagicMock())
        result = po.analyze_and_switch(dry_run=True, opportunities=[])
        assert isinstance(result, list)


# ── BearAnalyst (59 missed) ───────────────────────────────────────────


class TestBearAnalyst:
    def test_init(self):
        from src.bear_analyst import BearAnalyst

        ba = BearAnalyst()
        assert ba is not None

    def test_analyze(self):
        from src.bear_analyst import BearAnalyst

        ba = BearAnalyst()
        result = ba.analyze(
            symbol="BTCUSDT",
            opportunity_data={"score": 80, "technical_score": 70},
            research_data={"sentiment": 0.5},
        )
        assert result is not None
        assert hasattr(result, "bear_score")


# ── Notifier (93 missed) ──────────────────────────────────────────────


class TestNotifier:
    def test_init(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier()
        assert n is not None

    def test_send_text_disabled(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier()
        result = n.send_text("test")
        assert isinstance(result, bool)


# ── Portfolio (65 missed) ─────────────────────────────────────────────


class TestPortfolio:
    def test_add_close(self, tmp_path):
        from src.portfolio import PortfolioManager
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        pm = PortfolioManager()
        pm._db = db
        pm.cash_balance = 1_000_000.0
        pm.add_position("BTCUSDT", quantity=0.1, entry_price=50000.0, deduct_cash=False)
        assert pm.get_position("BTCUSDT") is not None
        result = pm.close_position("BTCUSDT", close_price=55000.0)
        assert result["pnl"] > 0

    def test_summary(self, tmp_path):
        from src.portfolio import PortfolioManager
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        pm = PortfolioManager()
        pm._db = db
        pm.cash_balance = 1_000_000.0
        summary = pm.get_summary()
        assert "total_value" in summary


# ── StateDB (73 missed) ───────────────────────────────────────────────


class TestStateDB:
    def test_kv_set_get(self, tmp_path):
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        db.kv_set("key1", "val1")
        assert db.kv_get("key1") == "val1"

    def test_portfolio_set_get(self, tmp_path):
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        db.portfolio_set("BTCUSDT", {"qty": 0.1})
        assert "BTCUSDT" in db.portfolio_get_all()

    def test_trade_add(self, tmp_path):
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        db.trade_add("BTCUSDT", "BUY", 0.1, 50000.0, 100.0)

    def test_decision_add(self, tmp_path):
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        db.decision_add("BTCUSDT", "buy", decision="BUY", score=80.0)

    def test_audit_log(self, tmp_path):
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        db.audit_log("test", {"key": "value"})


# ── Indicators (53 missed) ────────────────────────────────────────────


class TestIndicators:
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


# ── MarketScanner (135 missed) ────────────────────────────────────────


class TestMarketScanner:
    def test_init(self):
        from src.market_scanner import MarketScanner

        ms = MarketScanner(MagicMock())
        assert ms is not None

    def test_rate_limiter(self):
        from src.market_scanner import _RateLimiter

        rl = _RateLimiter(max_per_second=25)
        rl.wait()
        assert len(rl._timestamps) == 1


# ── ScanOrchestrator (150 missed) ─────────────────────────────────────


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
            cmd_scan(send_notification=False)


# ── RiskManager (163 missed) ──────────────────────────────────────────


class TestRiskManager:
    def test_pre_trade_check(self):
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
        assert "allowed" in result

    def test_post_trade_update(self):
        from src.risk_manager import RiskManager

        rm = RiskManager()
        rm.post_trade_update("BTCUSDT", 500.0, 0.1)


# ── TradeExecutor (224 missed) ────────────────────────────────────────


class TestTradeExecutor:
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
        assert result is True


# ── PendingConfirmation ───────────────────────────────────────────────


class TestPendingConfirmation:
    def test_save_load_clear(self, tmp_path):
        from src import pending_confirmation

        pending_confirmation.DATA_DIR = tmp_path
        pending_confirmation.save_pending({"symbol": "BTCUSDT"})
        assert pending_confirmation.load_pending() is not None
        pending_confirmation.clear_pending()
        assert pending_confirmation.load_pending() is None


# ── Utils ─────────────────────────────────────────────────────────────


class TestUtils:
    def test_get_project_root(self):
        from src.utils import get_project_root

        root = get_project_root()
        assert root.exists()
