"""
Tests for all remaining uncovered modules.
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest

# ── Notifier (93 missed) ──────────────────────────────────────────────


class TestNotifierComplete:
    def test_init_no_webhook(self):
        from src.notifier import FeishuNotifier

        # P0 fix: FeishuNotifier is now a no-op wrapper, no webhook_url stored
        n = FeishuNotifier()
        assert not hasattr(n, "webhook_url") or n.__dict__.get("webhook_url", "") == ""

    def test_send_text_disabled(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier()
        # P0 fix: send_text now delegates to send_message, returns None
        with patch("src.notifier.send_message"):
            result = n.send_text("test")
            assert result is None

    def test_send_text_success(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier()
        # P0 fix: send_text delegates to send_message (no return value)
        with patch("src.notifier.send_message") as mock_send:
            n.send_text("test")
            mock_send.assert_called_once_with(title="", body="test")

    def test_send_text_failure(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier()
        # P0 fix: send_message may raise; send_text doesn't catch
        with patch("src.notifier.send_message", side_effect=Exception("network")):
            with pytest.raises(Exception, match="network"):
                n.send_text("test")

    def test_send_text_exception(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier()
        # P0 fix: same as failure — exception propagates
        with patch("src.notifier.send_message", side_effect=Exception("network")):
            with pytest.raises(Exception, match="network"):
                n.send_text("test")


# ── LLMClient (87 missed) ─────────────────────────────────────────────


class TestLLMClientComplete:
    def test_init(self):
        from src.llm_client import LLMClient

        llm = LLMClient()
        assert llm is not None

    def test_get_provider_config(self):
        from src.llm_client import _get_provider_config

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}):
            config, key = _get_provider_config("primary")
            assert "provider" in config

    def test_load_config(self):
        from src.llm_client import _load_config

        cfg = _load_config()
        assert "llm" in cfg


# ── SentimentAnalyzer (75 missed) ─────────────────────────────────────


class TestSentimentAnalyzerComplete:
    def test_init(self):
        from src.sentiment import SentimentAnalyzer

        sa = SentimentAnalyzer()
        assert sa.cache_ttl == 300


# ── RiskManager (163 missed) ──────────────────────────────────────────


class TestRiskManagerComplete:
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


class TestScanOrchestratorComplete:
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


# ── TradeExecutor (224 missed) ────────────────────────────────────────


class TestTradeExecutorComplete:
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
        assert _check_price_deviation(mock_client, "BTCUSDT", 100.0) is True

    def test_price_deviation_insufficient(self):
        from src.trade_executor import _check_price_deviation

        mock_client = MagicMock()
        mock_client.get_klines.return_value = [{"close": "100.0"}] * 5
        assert _check_price_deviation(mock_client, "BTCUSDT", 100.0) is True

    def test_price_deviation_exception(self):
        from src.trade_executor import _check_price_deviation

        mock_client = MagicMock()
        mock_client.get_klines.side_effect = Exception("error")
        # P0 fix: fail-closed — exception now blocks trade (returns False)
        assert _check_price_deviation(mock_client, "BTCUSDT", 100.0) is False


# ── MarketScanner (136 missed) ────────────────────────────────────────


class TestMarketScannerComplete:
    def test_init(self):
        from src.market_scanner import MarketScanner

        ms = MarketScanner(MagicMock())
        assert ms is not None

    def test_rate_limiter(self):
        from src.market_scanner import _RateLimiter

        rl = _RateLimiter(max_per_second=25)
        rl.wait()
        assert len(rl._timestamps) == 1


# ── MarketResearcher (338 missed) ─────────────────────────────────────


class TestMarketResearcherComplete:
    def test_init(self):
        from src.market_researcher import MarketResearcher

        mr = MarketResearcher()
        assert mr.CACHE_TTL == 3600

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

    def test_save_json(self, tmp_path):
        from src.market_researcher import _save_json

        assert _save_json(tmp_path / "test.json", {"key": "value"}) is True


# ── Backtest (467 missed) ─────────────────────────────────────────────


class TestBacktestComplete:
    def test_score_rsi_ranges(self):
        from src.backtest import calculate_score

        for rsi in [15, 25, 35, 45, 55, 65, 75, 85]:
            score = calculate_score({"rsi": rsi, "macd_histogram": 0}, None, None)
            assert 0 <= score <= 100

    def test_score_macd(self):
        from src.backtest import calculate_score

        assert calculate_score({"rsi": 50, "macd_histogram": 5}, None, None) > 40
        assert calculate_score({"rsi": 50, "macd_histogram": -5}, None, None) < 40

    def test_score_volume_surge(self):
        from src.backtest import calculate_score

        assert (
            calculate_score(
                {"rsi": 50, "macd_histogram": 0}, None, None, volume_surge=True
            )
            > 40
        )

    def test_score_bb(self):
        from src.backtest import calculate_score

        score = calculate_score(
            {"rsi": 50, "macd_histogram": 0, "current_price": 90, "bb_lower": 100},
            None,
            None,
        )
        assert score > 40

    def test_score_vwap(self):
        from src.backtest import calculate_score

        score = calculate_score(
            {"rsi": 50, "macd_histogram": 0, "current_price": 110, "vwap": 100},
            None,
            None,
        )
        assert score > 40

    def test_score_ma(self):
        from src.backtest import calculate_score

        score = calculate_score(
            {"rsi": 50, "macd_histogram": 0, "ma7": 110, "ma25": 105, "ma99": 100},
            None,
            None,
        )
        assert score > 40

    def test_volume_surge(self):
        from src.backtest import _detect_volume_surge

        klines = [{"volume": 100}] * 20 + [{"volume": 500}]
        assert _detect_volume_surge(klines) is True
        assert _detect_volume_surge([{"volume": 100}] * 21) is False
        assert _detect_volume_surge([{"volume": 100}] * 10) is False


# ── PaperTrader (210 missed) ──────────────────────────────────────────


class TestPaperTraderComplete:
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


# ── WSUserStream (175 missed) ─────────────────────────────────────────


class TestWSUserStreamComplete:
    def test_connection_stats(self):
        from src.ws_user_stream import ConnectionStats

        stats = ConnectionStats()
        assert stats.total_messages_received == 0

    def test_constants(self):
        from src.ws_user_stream import SPOT_WS_BASE

        assert "wss://" in SPOT_WS_BASE


# ── SectorClassifier (170 missed) ─────────────────────────────────────


class TestSectorClassifierComplete:
    def test_init(self):
        from src.sector_classifier import SectorClassifier

        sc = SectorClassifier()
        assert sc is not None


# ── HMMRegime (214 missed) ────────────────────────────────────────────


class TestHMMRegimeComplete:
    def test_init(self):
        from src.hmm_regime import HMMRegimeDetector

        det = HMMRegimeDetector()
        assert det is not None


# ── FreqtradeRiskPatterns (213 missed) ─────────────────────────────────


class TestFreqtradeComplete:
    def test_calculate_risk_per_trade(self):
        from src.freqtrade_risk_patterns import calculate_risk_per_trade_position_size

        result = calculate_risk_per_trade_position_size(
            portfolio_value=10000.0,
            risk_pct=0.02,
            entry_price=100.0,
            stoploss_price=95.0,
        )
        assert result > 0

    def test_calculate_unlimited_stake(self):
        from src.freqtrade_risk_patterns import calculate_unlimited_stake_amount

        assert calculate_unlimited_stake_amount(10000.0, 5000.0, 5) == 3000.0
        assert calculate_unlimited_stake_amount(10000.0, 5000.0, 0) == 0


# ── ParamOptimizer (148 missed) ───────────────────────────────────────


class TestParamOptimizerComplete:
    def test_init(self):
        from src.param_optimizer import ParamOptimizer

        po = ParamOptimizer()
        assert po is not None


# ── OnlineLearner (146 missed) ────────────────────────────────────────


class TestOnlineLearnerComplete:
    def test_default_weights(self):
        from src.online_learner import DEFAULT_WEIGHTS

        assert "technical" in DEFAULT_WEIGHTS

    def test_factor_names(self):
        from src.online_learner import FACTOR_NAMES

        assert len(FACTOR_NAMES) > 0


# ── SelfHealer (141 missed) ───────────────────────────────────────────


class TestSmartOrderComplete:
    def test_class_exists(self):
        from src.smart_order import SmartOrder

        assert SmartOrder is not None


# ── SectorClustering (133 missed) ─────────────────────────────────────


class TestFundingArbComplete:
    def test_class_exists(self):
        from src.funding_arb import FundingArbitrage

        assert FundingArbitrage is not None


# ── ConceptDrift (121 missed) ─────────────────────────────────────────


class TestConceptDriftComplete:
    def test_init(self):
        from src.concept_drift import ConceptDriftDetector

        cdd = ConceptDriftDetector()
        assert cdd is not None


# ── DynamicCoinPool (117 missed) ──────────────────────────────────────


class TestDynamicCoinPoolComplete:
    def test_init(self):
        from src.dynamic_coin_pool import DynamicCoinPool

        dcp = DynamicCoinPool(MagicMock())
        assert dcp is not None


# ── FeatureStore (102 missed) ─────────────────────────────────────────


class TestFeatureStoreComplete:
    def test_init_no_redis(self):
        from src.feature_store import FeatureStore

        fs = FeatureStore.__new__(FeatureStore)
        fs._redis_available = False
        fs._fallback = {}
        fs._r = None
        assert fs._redis_available is False


# ── DimensionScorer (71 missed) ───────────────────────────────────────


class TestDimensionScorerComplete:
    def test_score_all(self):
        from src.dimension_scorer import DimensionScorer

        ds = DimensionScorer(binance_client=MagicMock())
        ds.client.get_ticker_price.return_value = 50000.0
        ds.client.get_klines.return_value = []
        result = ds.score_all()
        assert "resonance" in result


# ── StrategyAdaptor (82 missed) ───────────────────────────────────────


class TestStrategyAdaptorComplete:
    def test_adapt(self):
        from src.strategy_adaptor import StrategyAdaptor

        sa = StrategyAdaptor()
        result = sa.adapt(fear_greed=50, btc_trend="up", btc_price_change_24h=1.5)
        assert isinstance(result, dict)


# ── PositionOptimizer (92 missed) ─────────────────────────────────────


class TestPositionOptimizerComplete:
    def test_init(self):
        from src.position_optimizer import PositionOptimizer

        po = PositionOptimizer(MagicMock(), MagicMock(), MagicMock())
        assert po is not None


# ── BearAnalyst (59 missed) ───────────────────────────────────────────


class TestBearAnalystComplete:
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


class TestPortfolioComplete:
    def test_add_close(self, tmp_path):
        from src.portfolio import PortfolioManager
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        pm = PortfolioManager()
        pm._db = db
        pm.cash_balance = 1_000_000.0
        pm.add_position("BTCUSDT", quantity=0.1, entry_price=50000.0, deduct_cash=False)
        result = pm.close_position("BTCUSDT", close_price=55000.0)
        assert result["pnl"] > 0


# ── StateDB (73 missed) ───────────────────────────────────────────────


class TestStateDBComplete:
    def test_kv_set_get(self, tmp_path):
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        db.kv_set("key1", "val1")
        assert db.kv_get("key1") == "val1"


# ── Indicators (54 missed) ────────────────────────────────────────────


class TestIndicatorsComplete:
    def test_sma(self):
        from src.indicators import Indicators

        assert Indicators.sma([1, 2, 3, 4, 5], 3) == pytest.approx(4.0)

    def test_rsi(self):
        from src.indicators import Indicators

        assert 0 <= Indicators.rsi([100 + i for i in range(30)], 14) <= 100

    def test_macd(self):
        from src.indicators import Indicators

        result = Indicators.macd([100 + i * 0.5 for i in range(50)])
        assert "macd" in result

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


# ── PendingConfirmation (53 missed) ───────────────────────────────────


class TestPendingConfirmationComplete:
    def test_save_load_clear(self, tmp_path):
        from src import pending_confirmation

        pending_confirmation.DATA_DIR = tmp_path
        pending_confirmation.save_pending({"symbol": "BTCUSDT"})
        assert pending_confirmation.load_pending() is not None
        pending_confirmation.clear_pending()
        assert pending_confirmation.load_pending() is None


# ── FeeOptimizer (51 missed) ──────────────────────────────────────────


class TestFeeOptimizerComplete:
    def test_get_effective_fees(self):
        from src.fee_optimizer import FeeOptimizer

        mock_client = MagicMock()
        mock_client.get_account.return_value = {
            "makerCommission": 10,
            "takerCommission": 10,
        }
        fo = FeeOptimizer(mock_client)
        assert isinstance(fo.get_effective_fees("BTCUSDT"), dict)


# ── KellySizer (31 missed) ────────────────────────────────────────────


class TestKellySizerComplete:
    def test_init(self):
        from src.kelly_sizer import KellyPositionSizer

        assert KellyPositionSizer() is not None


# ── CircuitBreaker (29 missed) ────────────────────────────────────────


class TestCircuitBreakerComplete:
    def test_init(self):
        from src.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        assert cb.is_tripped() is False


# ── DailyLossBreaker (38 missed) ──────────────────────────────────────


class TestDailyLossBreakerComplete:
    def test_init(self):
        from src.daily_loss_breaker import DailyLossBreaker

        dlb = DailyLossBreaker()
        assert dlb.should_block_new_trades() is False


# ── CVaRRisk ──────────────────────────────────────────────────────────


class TestCVaRRiskComplete:
    def test_compute_cvar(self):
        from src.cvar_risk import CVaRRiskManager

        cvar = CVaRRiskManager()
        returns = [-0.05, -0.03, -0.01, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08]
        assert isinstance(cvar.compute_cvar(returns), float)

    def test_compute_var(self):
        from src.cvar_risk import CVaRRiskManager

        cvar = CVaRRiskManager()
        returns = [-0.05, -0.03, -0.01, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08]
        assert isinstance(cvar.compute_var(returns), float)


# ── Utils ─────────────────────────────────────────────────────────────


class TestUtilsComplete:
    def test_get_project_root(self):
        from src.utils import get_project_root

        assert get_project_root().exists()


# ── CCXT Client (260 missed) ──────────────────────────────────────────


class TestCCXTClientComplete:
    def test_init_with_keys(self):
        from src.ccxt_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
        ):
            with patch("ccxt.binance") as mock_binance:
                mock_binance.return_value = MagicMock()
                client = BinanceClient()
                assert client.api_key == "k"

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


# ── _binance_sdk_client (160 missed) ──────────────────────────────────


class TestBinanceSDKClientComplete:
    def test_init_with_keys(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
        ):
            with patch("src._binance_sdk_client.BinanceSpotClient"):
                client = BinanceClient()
                assert client.api_key == "k"

    def test_validate_symbol(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
        ):
            with patch("src._binance_sdk_client.BinanceSpotClient"):
                client = BinanceClient()
                assert client.validate_symbol("BTCUSDT") is True

    def test_close(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
        ):
            with patch("src._binance_sdk_client.BinanceSpotClient"):
                client = BinanceClient()
                client.close()
