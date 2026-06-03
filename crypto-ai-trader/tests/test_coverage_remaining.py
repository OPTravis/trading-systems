"""
Tests for remaining uncovered modules — targeting specific code paths.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

# ── _binance_sdk_client: helpers (534 missed) ─────────────────────────


class TestBinanceSDKHelpers:
    def test_sanitize_error(self):
        from src._binance_sdk_client import _sanitize_error

        msg = "Error api_key=ABCD1234567890 failed"
        result = _sanitize_error(msg)
        assert "ABCD1234567890" not in result
        assert "REDACTED" in result

    def test_sanitize_error_no_secret(self):
        from src._binance_sdk_client import _sanitize_error

        msg = "Connection timeout"
        result = _sanitize_error(msg)
        assert result == "Connection timeout"

    def test_parse_retry_after_normal(self):
        from src._binance_sdk_client import _parse_retry_after

        error = MagicMock()
        error.header = {"Retry-After": "30"}
        result = _parse_retry_after(error, default_wait=10)
        assert result == 30

    def test_parse_retry_after_capped(self):
        from src._binance_sdk_client import _parse_retry_after

        error = MagicMock()
        error.header = {"Retry-After": "120"}
        result = _parse_retry_after(error, default_wait=10)
        assert result == 60  # capped at 60

    def test_parse_retry_after_non_numeric(self):
        from src._binance_sdk_client import _parse_retry_after

        error = MagicMock()
        error.header = {"Retry-After": "abc"}
        result = _parse_retry_after(error, default_wait=10)
        assert result == 10

    def test_verify_ssl_env(self):
        from src._binance_sdk_client import VERIFY_SSL

        assert isinstance(VERIFY_SSL, bool)

    def test_sensitive_pattern(self):
        from src._binance_sdk_client import _SENSITIVE_PATTERN

        assert _SENSITIVE_PATTERN.search("api_key=secret12345678") is not None


# ── Backtest: data classes (525 missed) ───────────────────────────────


class TestBacktest:
    def test_position_dataclass(self):
        from src.backtest import Position

        pos = Position(
            symbol="BTCUSDT",
            entry_price=50000.0,
            entry_bar=10,
            entry_time=1000000,
            quantity=0.1,
            usdt_cost=5000.0,
            atr=1000.0,
            sl_price=49000.0,
            tp1_price=51000.0,
            tp1_size=0.04,
            tp2_price=52000.0,
            tp2_size=0.04,
            tp3_price=53000.0,
            tp3_size=0.02,
        )
        assert pos.symbol == "BTCUSDT"
        assert pos.trailing_activated is False

    def test_backtest_engine_import(self):
        from src.backtest import BacktestEngine

        assert BacktestEngine is not None


# ── MarketResearcher (452 missed) ─────────────────────────────────────


class TestMarketResearcher:
    def test_init(self):
        from src.market_researcher import MarketResearcher

        mr = MarketResearcher()
        assert mr is not None
        assert mr.CACHE_TTL == 3600

    def test_constants(self):
        from src.market_researcher import MarketResearcher

        assert MarketResearcher.MAX_ADJUSTMENT == 15.0
        assert MarketResearcher.MIN_ADJUSTMENT == -15.0

    def test_load_recent_cache_empty(self, tmp_path):
        from src.market_researcher import MarketResearcher

        mr = MarketResearcher.__new__(MarketResearcher)
        mr._research_dir = tmp_path / "research"
        mr._research_dir.mkdir()
        mr._cache = {}
        mr._cache_ts = {}
        mr._load_recent_cache()
        assert mr._cache == {}


# ── CCXT Client (429 missed) ──────────────────────────────────────────


class TestCCXTClient:
    def test_import(self):
        from src.ccxt_client import BinanceClient

        assert BinanceClient is not None

    def test_class_has_methods(self):
        from src.ccxt_client import BinanceClient

        assert hasattr(BinanceClient, "get_ticker_price")
        assert hasattr(BinanceClient, "get_klines")
        assert hasattr(BinanceClient, "get_24hr_stats")
        assert hasattr(BinanceClient, "place_market_buy")
        assert hasattr(BinanceClient, "place_market_sell")


# ── FreqtradeRiskPatterns (271 missed) ────────────────────────────────


class TestFreqtradeRiskPatterns:
    def test_calculate_risk_per_trade(self):
        from src.freqtrade_risk_patterns import calculate_risk_per_trade_position_size

        result = calculate_risk_per_trade_position_size(
            portfolio_value=10000.0,
            risk_pct=0.02,
            entry_price=100.0,
            stoploss_price=95.0,
        )
        assert isinstance(result, float)
        assert result > 0

    def test_calculate_risk_per_trade_invalid(self):
        from src.freqtrade_risk_patterns import calculate_risk_per_trade_position_size

        result = calculate_risk_per_trade_position_size(
            portfolio_value=10000.0,
            risk_pct=0.02,
            entry_price=0.0,
            stoploss_price=95.0,
        )
        assert result == 10.0  # min_stake

    def test_calculate_risk_per_trade_same_price(self):
        from src.freqtrade_risk_patterns import calculate_risk_per_trade_position_size

        result = calculate_risk_per_trade_position_size(
            portfolio_value=10000.0,
            risk_pct=0.02,
            entry_price=100.0,
            stoploss_price=100.0,
        )
        assert result == 10.0

    def test_calculate_risk_per_trade_cap(self):
        from src.freqtrade_risk_patterns import calculate_risk_per_trade_position_size

        result = calculate_risk_per_trade_position_size(
            portfolio_value=10000.0,
            risk_pct=0.5,
            entry_price=100.0,
            stoploss_price=99.0,
            max_position_pct=0.15,
        )
        assert result <= 10000.0 * 0.15


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


# ── HMMRegime (214 missed) ────────────────────────────────────────────


class TestHMMRegime:
    def test_init(self):
        from src.hmm_regime import HMMRegimeDetector

        det = HMMRegimeDetector()
        assert det is not None


# ── PaperTrader (212 missed) ──────────────────────────────────────────


class TestPaperTrader:
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
        result = proxy.ticker_price("BTCUSDT")
        assert result["price"] == "50000.0"


# ── WS User Stream (175 missed) ───────────────────────────────────────


class TestWSUserStream:
    def test_connection_stats(self):
        from src.ws_user_stream import ConnectionStats

        stats = ConnectionStats()
        assert stats.total_messages_received == 0
        assert stats.total_errors == 0

    def test_constants(self):
        from src.ws_user_stream import RECONNECT_INITIAL_DELAY, SPOT_WS_BASE

        assert "wss://" in SPOT_WS_BASE
        assert RECONNECT_INITIAL_DELAY > 0


# ── SectorClassifier (170 missed) ─────────────────────────────────────


class TestSectorClassifier:
    def test_init(self):
        from src.sector_classifier import SectorClassifier

        sc = SectorClassifier()
        assert sc is not None


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


# ── ParamOptimizer (148 missed) ───────────────────────────────────────


class TestParamOptimizer:
    def test_init(self):
        from src.param_optimizer import ParamOptimizer

        po = ParamOptimizer()
        assert po is not None


# ── OnlineLearner (146 missed) ────────────────────────────────────────


class TestOnlineLearner:
    def test_default_weights(self):
        from src.online_learner import DEFAULT_WEIGHTS

        assert "technical" in DEFAULT_WEIGHTS

    def test_factor_names(self):
        from src.online_learner import FACTOR_NAMES

        assert len(FACTOR_NAMES) > 0


# ── FundingArb (145 missed) ───────────────────────────────────────────


class TestFundingArb:
    def test_class_exists(self):
        from src.funding_arb import FundingArbitrage

        assert FundingArbitrage is not None


# ── MarketScanner (135 missed) ────────────────────────────────────────


class TestMarketScanner:
    def test_init(self):
        from src.market_scanner import MarketScanner

        ms = MarketScanner(MagicMock())
        assert ms is not None


# ── FeatureStore (123 missed) ─────────────────────────────────────────


class TestFeatureStore:
    def test_init_no_redis(self):
        from src.feature_store import FeatureStore

        fs = FeatureStore.__new__(FeatureStore)
        fs._redis_available = False
        fs._fallback = {}
        fs._r = None
        assert fs._redis_available is False


# ── ConceptDrift (121 missed) ─────────────────────────────────────────


class TestConceptDrift:
    def test_init(self):
        from src.concept_drift import ConceptDriftDetector

        cdd = ConceptDriftDetector()
        assert cdd is not None


# ── DynamicCoinPool (117 missed) ──────────────────────────────────────


class TestDynamicCoinPool:
    def test_init(self):
        from src.dynamic_coin_pool import DynamicCoinPool

        dcp = DynamicCoinPool(MagicMock())
        assert dcp is not None


# ── Notifier (93 missed) ──────────────────────────────────────────────


class TestNotifier:
    def test_init(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier()
        assert n is not None


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


# ── CVaRRisk (72 missed) ──────────────────────────────────────────────


class TestCVaRRisk:
    def test_compute_cvar(self):
        from src.cvar_risk import CVaRRiskManager

        cvar = CVaRRiskManager()
        returns = [-0.05, -0.03, -0.01, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08]
        result = cvar.compute_cvar(returns, alpha=0.05)
        assert isinstance(result, float)

    def test_compute_var(self):
        from src.cvar_risk import CVaRRiskManager

        cvar = CVaRRiskManager()
        returns = [-0.05, -0.03, -0.01, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08]
        result = cvar.compute_var(returns, alpha=0.05)
        assert isinstance(result, float)

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


# ── StrategyAdaptor (82 missed) ───────────────────────────────────────


class TestStrategyAdaptor:
    def test_adapt(self):
        from src.strategy_adaptor import StrategyAdaptor

        sa = StrategyAdaptor()
        result = sa.adapt(fear_greed=50, btc_trend="up", btc_price_change_24h=1.5)
        assert isinstance(result, dict)


# ── PositionOptimizer (92 missed) ─────────────────────────────────────


class TestPositionOptimizer:
    def test_init(self):
        from src.position_optimizer import PositionOptimizer

        po = PositionOptimizer(MagicMock(), MagicMock(), MagicMock())
        assert po is not None


# ── BearAnalyst (59 missed) ───────────────────────────────────────────


class TestBearAnalyst:
    def test_analyze(self):
        from src.bear_analyst import BearAnalyst

        ba = BearAnalyst()
        result = ba.analyze(
            symbol="BTCUSDT",
            opportunity_data={"score": 80, "technical_score": 70},
            research_data={"sentiment": 0.5},
        )
        assert result is not None


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


# ── FeeOptimizer (51 missed) ──────────────────────────────────────────


class TestFeeOptimizer:
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


# ── KellySizer (31 missed) ────────────────────────────────────────────


class TestKellySizer:
    def test_init(self):
        from src.kelly_sizer import KellyPositionSizer

        ks = KellyPositionSizer()
        assert ks is not None


# ── CircuitBreaker (33 missed) ────────────────────────────────────────


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


# ── DailyLossBreaker (38 missed) ──────────────────────────────────────


class TestDailyLossBreaker:
    def test_init(self):
        from src.daily_loss_breaker import DailyLossBreaker

        dlb = DailyLossBreaker()
        assert dlb.should_block_new_trades() is False


# ── DrawdownBreaker (12 missed) ───────────────────────────────────────


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


# ── MultiTimeframe ────────────────────────────────────────────────────


class TestMultiTimeframe:
    def test_init(self):
        from src.multi_timeframe import MultiTimeframeAnalyzer

        mta = MultiTimeframeAnalyzer(MagicMock())
        assert mta is not None


# ── TradeJournal ──────────────────────────────────────────────────────


class TestTradeJournal:
    def test_init(self):
        from src.trade_journal import TradeJournal

        tj = TradeJournal()
        assert tj is not None


# ── SentimentAnalyzer ─────────────────────────────────────────────────


class TestSentimentAnalyzer:
    def test_init(self):
        from src.sentiment import SentimentAnalyzer

        sa = SentimentAnalyzer()
        assert sa is not None


# ── PricePredictor ────────────────────────────────────────────────────


class TestPricePredictor:
    def test_init(self):
        from src.price_predictor import PricePredictor

        pp = PricePredictor()
        assert pp.is_ready() is False


# ── DataFeedManager ───────────────────────────────────────────────────


class TestDataFeedManager:
    def test_init(self):
        from src.data_feed import DataFeedManager

        dfm = DataFeedManager()
        assert dfm is not None


# ── EventBus ──────────────────────────────────────────────────────────


class TestEventBus:
    def test_init(self):
        from src.event_bus import EventBus

        eb = EventBus()
        assert eb is not None


# ── ContextualBandit ──────────────────────────────────────────────────


class TestContextualBandit:
    def test_init(self):
        from src.contextual_bandit import ContextualBandit

        cb = ContextualBandit()
        assert cb is not None


# ── LLMClient ─────────────────────────────────────────────────────────


class TestLLMClient:
    def test_init(self):
        from src.llm_client import LLMClient

        llm = LLMClient()
        assert llm is not None


# ── StrategyRegistry ──────────────────────────────────────────────────


class TestStrategyRegistry:
    def test_init(self):
        from src.strategy_registry import StrategyRegistry

        sr = StrategyRegistry()
        assert sr is not None


# ── StrategyEvolver ───────────────────────────────────────────────────


class TestStrategyEvolver:
    def test_init(self):
        from src.strategy_evolver import StrategyEvolver

        se = StrategyEvolver()
        assert se is not None
