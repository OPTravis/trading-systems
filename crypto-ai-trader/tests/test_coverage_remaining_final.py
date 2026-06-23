"""
Final coverage tests for remaining uncovered code paths.
"""

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

# ── Backtest: calculate_score specific paths ──────────────────────────


class TestBacktestScorePaths:
    def test_rsi_below_20(self):
        from src.backtest import calculate_score

        score = calculate_score({"rsi": 15, "macd_histogram": 0}, None, None)
        assert score > 40

    def test_rsi_20_30(self):
        from src.backtest import calculate_score

        score = calculate_score({"rsi": 25, "macd_histogram": 0}, None, None)
        assert score > 40

    def test_rsi_30_40(self):
        from src.backtest import calculate_score

        score = calculate_score({"rsi": 35, "macd_histogram": 0}, None, None)
        assert score > 40

    def test_rsi_40_50(self):
        from src.backtest import calculate_score

        score = calculate_score({"rsi": 45, "macd_histogram": 0}, None, None)
        assert score > 40

    def test_rsi_50_60(self):
        from src.backtest import calculate_score

        score = calculate_score({"rsi": 55, "macd_histogram": 0}, None, None)
        assert score >= 40

    def test_rsi_60_70(self):
        from src.backtest import calculate_score

        score = calculate_score({"rsi": 65, "macd_histogram": 0}, None, None)
        assert score > 40

    def test_rsi_above_80(self):
        from src.backtest import calculate_score

        score = calculate_score({"rsi": 85, "macd_histogram": 0}, None, None)
        assert score < 40

    def test_rsi_70_80(self):
        from src.backtest import calculate_score

        score = calculate_score({"rsi": 75, "macd_histogram": 0}, None, None)
        assert score < 40

    def test_macd_positive(self):
        from src.backtest import calculate_score

        score = calculate_score({"rsi": 50, "macd_histogram": 5}, None, None)
        assert score > 40

    def test_macd_negative(self):
        from src.backtest import calculate_score

        score = calculate_score({"rsi": 50, "macd_histogram": -5}, None, None)
        assert score < 40

    def test_volume_surge(self):
        from src.backtest import calculate_score

        score = calculate_score(
            {"rsi": 50, "macd_histogram": 0}, None, None, volume_surge=True
        )
        assert score > 40

    def test_bb_below_lower(self):
        from src.backtest import calculate_score

        score = calculate_score(
            {"rsi": 50, "macd_histogram": 0, "current_price": 90, "bb_lower": 100},
            None,
            None,
        )
        assert score > 40

    def test_bb_near_lower(self):
        from src.backtest import calculate_score

        score = calculate_score(
            {"rsi": 50, "macd_histogram": 0, "current_price": 100, "bb_lower": 100},
            None,
            None,
        )
        assert score > 40

    def test_vwap_above(self):
        from src.backtest import calculate_score

        score = calculate_score(
            {"rsi": 50, "macd_histogram": 0, "current_price": 110, "vwap": 100},
            None,
            None,
        )
        assert score > 40

    def test_ma_alignment(self):
        from src.backtest import calculate_score

        score = calculate_score(
            {"rsi": 50, "macd_histogram": 0, "ma7": 110, "ma25": 105, "ma99": 100},
            None,
            None,
        )
        assert score > 40

    def test_volatility_normal(self):
        from src.backtest import calculate_score

        score = calculate_score(
            {"rsi": 50, "macd_histogram": 0, "volatility_pct": 5}, None, None
        )
        assert isinstance(score, (int, float))

    def test_volatility_high(self):
        from src.backtest import calculate_score

        score = calculate_score(
            {"rsi": 50, "macd_histogram": 0, "volatility_pct": 20}, None, None
        )
        assert isinstance(score, (int, float))

    def test_with_4h_data(self):
        from src.backtest import calculate_score

        score = calculate_score(
            {"rsi": 50, "macd_histogram": 0}, {"macd_histogram": 5}, None
        )
        assert score > 40

    def test_with_1d_data(self):
        from src.backtest import calculate_score

        score = calculate_score(
            {"rsi": 50, "macd_histogram": 0}, None, {"macd_histogram": 5}
        )
        assert score > 40

    def test_score_clamped(self):
        from src.backtest import calculate_score

        score = calculate_score(
            {"rsi": 10, "macd_histogram": 10, "volatility_pct": 5},
            None,
            None,
            volume_surge=True,
        )
        assert 0 <= score <= 100


# ── _detect_volume_surge ──────────────────────────────────────────────


class TestVolumeSurge:
    def test_insufficient_data(self):
        from src.backtest import _detect_volume_surge

        klines = [{"volume": 100}] * 10
        assert _detect_volume_surge(klines) is False

    def test_no_surge(self):
        from src.backtest import _detect_volume_surge

        klines = [{"volume": 100}] * 21
        assert _detect_volume_surge(klines) is False

    def test_surge(self):
        from src.backtest import _detect_volume_surge

        klines = [{"volume": 100}] * 20 + [{"volume": 500}]
        assert _detect_volume_surge(klines) is True


# ── BacktestEngine ────────────────────────────────────────────────────


class TestBacktestEngine:
    def test_import(self):
        from src.backtest import BacktestEngine

        assert BacktestEngine is not None

    def test_has_run(self):
        from src.backtest import BacktestEngine

        assert hasattr(BacktestEngine, "run")


# ── TradeExecutor: specific error paths ───────────────────────────────


class TestTradeExecutorPaths:
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
        assert _check_price_deviation(mock_client, "BTCUSDT", 100.0) is True

    def test_price_deviation_insufficient(self):
        from src.trade_executor import _check_price_deviation

        mock_client = MagicMock()
        mock_client.get_klines.return_value = [{"close": "100.0"}] * 5
        assert _check_price_deviation(mock_client, "BTCUSDT", 100.0) is True

    def test_price_deviation_exception(self):
        from src.trade_executor import _check_price_deviation

        mock_client = MagicMock()
        mock_client.get_klines.side_effect = Exception("API error")
        # P0 fix: fail-closed — exception now blocks trade (returns False)
        assert _check_price_deviation(mock_client, "BTCUSDT", 100.0) is False


# ── RiskManager ───────────────────────────────────────────────────────


class TestRiskManagerPaths:
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


# ── ScanOrchestrator ──────────────────────────────────────────────────


class TestScanOrchestratorPaths:
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


# ── MarketScanner ─────────────────────────────────────────────────────


class TestMarketScannerPaths:
    def test_init(self):
        from src.market_scanner import MarketScanner

        ms = MarketScanner(MagicMock())
        assert ms is not None

    def test_rate_limiter(self):
        from src.market_scanner import _RateLimiter

        rl = _RateLimiter(max_per_second=25)
        rl.wait()
        assert len(rl._timestamps) == 1


# ── Notifier ──────────────────────────────────────────────────────────


class TestNotifierPaths:
    def test_init(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier()
        assert n is not None

    def test_send_text_disabled(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier()
        # P0 fix: send_text delegates to send_message, returns None
        with patch("src.notifier.send_message"):
            result = n.send_text("test")
            assert result is None


# ── Portfolio ─────────────────────────────────────────────────────────


class TestPortfolioPaths:
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


# ── StateDB ───────────────────────────────────────────────────────────


class TestStateDBPaths:
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


# ── Indicators ────────────────────────────────────────────────────────


class TestIndicatorsPaths:
    def test_sma(self):
        from src.indicators import Indicators

        assert Indicators.sma([1, 2, 3, 4, 5], 3) == pytest.approx(4.0)

    def test_ema(self):
        from src.indicators import Indicators

        assert isinstance(Indicators.ema([1, 2, 3, 4, 5], 3), float)

    def test_rsi(self):
        from src.indicators import Indicators

        assert 0 <= Indicators.rsi([100 + i for i in range(30)], 14) <= 100

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
        assert isinstance(Indicators.atr(klines, 14), (float, list))

    def test_obv(self):
        from src.indicators import Indicators

        klines = [{"close": 100 + i, "volume": 1000} for i in range(20)]
        assert isinstance(Indicators.obv(klines), (float, list))

    def test_vwap(self):
        from src.indicators import Indicators

        klines = [{"high": 105, "low": 95, "close": 100, "volume": 1000}] * 20
        assert isinstance(Indicators.vwap(klines), (float, list))


# ── MarketResearcher ──────────────────────────────────────────────────


class TestMarketResearcherPaths:
    def test_init(self):
        from src.market_researcher import MarketResearcher

        mr = MarketResearcher()
        assert mr.CACHE_TTL == 3600

    def test_constants(self):
        from src.market_researcher import MarketResearcher

        assert MarketResearcher.MAX_ADJUSTMENT == 15.0

    def test_cache_hit(self, tmp_path):
        from src.market_researcher import MarketResearcher

        mr = MarketResearcher.__new__(MarketResearcher)
        mr._research_dir = tmp_path / "research"
        mr._research_dir.mkdir()
        mr._cache = {"BTC": {"symbol": "BTCUSDT"}}
        mr._cache_ts = {"BTC": time.time()}
        mr.CACHE_TTL = 3600
        result = mr.research("BTCUSDT")
        assert result["symbol"] == "BTCUSDT"


# ── SaveJson ──────────────────────────────────────────────────────────


class TestSaveJson:
    def test_save_success(self, tmp_path):
        from src.market_researcher import _save_json

        filepath = tmp_path / "test.json"
        assert _save_json(filepath, {"key": "value"}) is True

    def test_save_failure(self, tmp_path):
        from src.market_researcher import _save_json

        assert _save_json(tmp_path / "nonexistent" / "test.json", {}) is False


# ── DimensionScorer ───────────────────────────────────────────────────


class TestDimensionScorerPaths:
    def test_score_all(self):
        from src.dimension_scorer import DimensionScorer

        ds = DimensionScorer(binance_client=MagicMock())
        ds.client.get_ticker_price.return_value = 50000.0
        ds.client.get_klines.return_value = []
        result = ds.score_all()
        assert "resonance" in result


# ── StrategyAdaptor ───────────────────────────────────────────────────


class TestStrategyAdaptorPaths:
    def test_adapt(self):
        from src.strategy_adaptor import StrategyAdaptor

        sa = StrategyAdaptor()
        result = sa.adapt(fear_greed=50, btc_trend="up", btc_price_change_24h=1.5)
        assert isinstance(result, dict)


# ── PositionOptimizer ─────────────────────────────────────────────────


class TestPositionOptimizerPaths:
    def test_init(self):
        from src.position_optimizer import PositionOptimizer

        po = PositionOptimizer(MagicMock(), MagicMock(), MagicMock())
        assert po is not None


# ── BearAnalyst ───────────────────────────────────────────────────────


class TestBearAnalystPaths:
    def test_analyze(self):
        from src.bear_analyst import BearAnalyst

        ba = BearAnalyst()
        result = ba.analyze(
            symbol="BTCUSDT",
            opportunity_data={"score": 80, "technical_score": 70},
            research_data={"sentiment": 0.5},
        )
        assert result is not None


# ── FeeOptimizer ──────────────────────────────────────────────────────


class TestFeeOptimizerPaths:
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


# ── KellySizer ────────────────────────────────────────────────────────


class TestKellySizerPaths:
    def test_init(self):
        from src.kelly_sizer import KellyPositionSizer

        ks = KellyPositionSizer()
        assert ks is not None


# ── CircuitBreaker ────────────────────────────────────────────────────


class TestCircuitBreakerPaths:
    def test_init(self):
        from src.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        assert cb.is_tripped() is False

    def test_record_failure(self):
        from src.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        cb.record_failure()
        assert cb.is_tripped() is False


# ── DailyLossBreaker ──────────────────────────────────────────────────


class TestDailyLossBreakerPaths:
    def test_init(self):
        from src.daily_loss_breaker import DailyLossBreaker

        dlb = DailyLossBreaker()
        assert dlb.should_block_new_trades() is False


# ── DrawdownBreaker ───────────────────────────────────────────────────


class TestDrawdownBreakerPaths:
    pass


# ── PendingConfirmation ───────────────────────────────────────────────


class TestPendingConfirmationPaths:
    def test_save_load_clear(self, tmp_path):
        from src import pending_confirmation

        pending_confirmation.DATA_DIR = tmp_path
        pending_confirmation.save_pending({"symbol": "BTCUSDT"})
        assert pending_confirmation.load_pending() is not None
        pending_confirmation.clear_pending()
        assert pending_confirmation.load_pending() is None


# ── Utils ─────────────────────────────────────────────────────────────


class TestUtilsPaths:
    def test_get_project_root(self):
        from src.utils import get_project_root

        assert get_project_root().exists()


# ── PaperTrader ───────────────────────────────────────────────────────


class TestPaperTraderPaths:
    def test_is_paper_mode(self):
        from src.paper_trader import is_paper_mode

        assert isinstance(is_paper_mode(), bool)

    def test_constants(self):
        from src.paper_trader import PAPER_FEE_RATE, PAPER_SLIPPAGE_PCT

        assert PAPER_SLIPPAGE_PCT > 0
        assert PAPER_FEE_RATE > 0

    def test_binance_sdk_proxy(self):
        from src.paper_trader import _BinanceSDKProxy

        mock_pt = MagicMock()
        mock_pt.get_current_price.return_value = 50000.0
        proxy = _BinanceSDKProxy(mock_pt)
        assert proxy.ticker_price("BTCUSDT")["price"] == "50000.0"


# ── WSUserStream ──────────────────────────────────────────────────────


class TestWSUserStreamPaths:
    def test_connection_stats(self):
        from src.ws_user_stream import ConnectionStats

        stats = ConnectionStats()
        assert stats.total_messages_received == 0

    def test_constants(self):
        from src.ws_user_stream import SPOT_WS_BASE

        assert "wss://" in SPOT_WS_BASE


# ── SectorClassifier ──────────────────────────────────────────────────


class TestSectorClassifierPaths:
    def test_init(self):
        from src.sector_classifier import SectorClassifier

        sc = SectorClassifier()
        assert sc is not None


# ── HMMRegime ─────────────────────────────────────────────────────────


class TestHMMRegimePaths:
    def test_init(self):
        from src.hmm_regime import HMMRegimeDetector

        det = HMMRegimeDetector()
        assert det is not None


# ── ParamOptimizer ────────────────────────────────────────────────────


class TestParamOptimizerPaths:
    def test_init(self):
        from src.param_optimizer import ParamOptimizer

        po = ParamOptimizer()
        assert po is not None


# ── OnlineLearner ─────────────────────────────────────────────────────


class TestOnlineLearnerPaths:
    def test_default_weights(self):
        from src.online_learner import DEFAULT_WEIGHTS

        assert "technical" in DEFAULT_WEIGHTS

    def test_factor_names(self):
        from src.online_learner import FACTOR_NAMES

        assert len(FACTOR_NAMES) > 0


# ── CVaRRisk ──────────────────────────────────────────────────────────


class TestCVaRRiskPaths:
    def test_compute_cvar(self):
        from src.cvar_risk import CVaRRiskManager

        cvar = CVaRRiskManager()
        returns = [-0.05, -0.03, -0.01, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08]
        assert isinstance(cvar.compute_cvar(returns, alpha=0.05), float)

    def test_compute_var(self):
        from src.cvar_risk import CVaRRiskManager

        cvar = CVaRRiskManager()
        returns = [-0.05, -0.03, -0.01, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08]
        assert isinstance(cvar.compute_var(returns, alpha=0.05), float)

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


# ── FeatureStore ──────────────────────────────────────────────────────


class TestFeatureStorePaths:
    def test_init_no_redis(self):
        from src.feature_store import FeatureStore

        fs = FeatureStore.__new__(FeatureStore)
        fs._redis_available = False
        fs._fallback = {}
        fs._r = None
        assert fs._redis_available is False


# ── ConceptDrift ──────────────────────────────────────────────────────


class TestConceptDriftPaths:
    def test_init(self):
        from src.concept_drift import ConceptDriftDetector

        cdd = ConceptDriftDetector()
        assert cdd is not None


# ── DynamicCoinPool ───────────────────────────────────────────────────


class TestDynamicCoinPoolPaths:
    def test_init(self):
        from src.dynamic_coin_pool import DynamicCoinPool

        dcp = DynamicCoinPool(MagicMock())
        assert dcp is not None


# ── MultiTimeframe ────────────────────────────────────────────────────


class TestMultiTimeframePaths:
    def test_init(self):
        from src.multi_timeframe import MultiTimeframeAnalyzer

        mta = MultiTimeframeAnalyzer(MagicMock())
        assert mta is not None


# ── TradeJournal ──────────────────────────────────────────────────────


class TestTradeJournalPaths:
    def test_init(self):
        from src.trade_journal import TradeJournal

        tj = TradeJournal()
        assert tj is not None


# ── SentimentAnalyzer ─────────────────────────────────────────────────


class TestSentimentAnalyzerPaths:
    def test_init(self):
        from src.sentiment import SentimentAnalyzer

        sa = SentimentAnalyzer()
        assert sa is not None


# ── PricePredictor ────────────────────────────────────────────────────


class TestPricePredictorPaths:
    def test_init(self):
        # Mock lightgbm since it's not installed in test env
        mock_lgb = MagicMock()
        # Remove cached module to force re-import with mock
        sys.modules.pop("src.price_predictor", None)
        with patch.dict("sys.modules", {"lightgbm": mock_lgb}):
            from src.price_predictor import PricePredictor

            pp = PricePredictor()
            assert pp.is_ready() is False


# ── DataFeedManager ───────────────────────────────────────────────────


class TestDataFeedManagerPaths:
    def test_init(self):
        from src.data_feed import DataFeedManager

        dfm = DataFeedManager()
        assert dfm is not None


# ── EventBus ──────────────────────────────────────────────────────────


class TestEventBusPaths:
    def test_init(self):
        from src.event_bus import EventBus

        eb = EventBus()
        assert eb is not None


# ── ContextualBandit ──────────────────────────────────────────────────


class TestContextualBanditPaths:
    def test_init(self):
        from src.contextual_bandit import ContextualBandit

        cb = ContextualBandit()
        assert cb is not None


# ── LLMClient ─────────────────────────────────────────────────────────


class TestLLMClientPaths:
    def test_init(self):
        from src.llm_client import LLMClient

        llm = LLMClient()
        assert llm is not None


# ── StrategyRegistry ──────────────────────────────────────────────────


class TestStrategyRegistryPaths:
    def test_init(self):
        from src.strategy_registry import StrategyRegistry

        sr = StrategyRegistry()
        assert sr is not None


# ── StrategyEvolver ───────────────────────────────────────────────────


class TestStrategyEvolverPaths:
    def test_init(self):
        from src.strategy_evolver import StrategyEvolver

        se = StrategyEvolver()
        assert se is not None


# ── CCXTClient ────────────────────────────────────────────────────────


class TestCCXTClientPaths:
    def test_validate_symbol(self):
        from src.ccxt_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
        ):
            with patch("ccxt.binance") as mock_binance:
                mock_binance.return_value = MagicMock()
                client = BinanceClient()
                assert client.validate_symbol("BTCUSDT") is True

    def test_close(self):
        from src.ccxt_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
        ):
            with patch("ccxt.binance") as mock_binance:
                mock_binance.return_value = MagicMock()
                client = BinanceClient()
                client.close()
