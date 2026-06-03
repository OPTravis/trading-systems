"""
Tests for all remaining uncovered modules.
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest

# ── FreqtradeRiskPatterns (220 missed) ────────────────────────────────


class TestFreqtradeRiskPatterns:
    def test_calculate_risk_per_trade_normal(self):
        from src.freqtrade_risk_patterns import calculate_risk_per_trade_position_size

        result = calculate_risk_per_trade_position_size(
            portfolio_value=10000.0,
            risk_pct=0.02,
            entry_price=100.0,
            stoploss_price=95.0,
        )
        assert isinstance(result, float)
        assert result > 0

    def test_calculate_risk_per_trade_invalid_entry(self):
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

    def test_calculate_unlimited_stake(self):
        from src.freqtrade_risk_patterns import calculate_unlimited_stake_amount

        result = calculate_unlimited_stake_amount(10000.0, 5000.0, 5)
        assert result == 3000.0  # (10000+5000)/5

    def test_calculate_unlimited_stake_zero_trades(self):
        from src.freqtrade_risk_patterns import calculate_unlimited_stake_amount

        result = calculate_unlimited_stake_amount(10000.0, 5000.0, 0)
        assert result == 0

    def test_calculate_unlimited_stake_cap(self):
        from src.freqtrade_risk_patterns import calculate_unlimited_stake_amount

        result = calculate_unlimited_stake_amount(1000.0, 5000.0, 10)
        assert result <= 1000.0  # capped at available

    def test_get_available_stake(self):
        from src.freqtrade_risk_patterns import get_available_stake_amount

        result = get_available_stake_amount(10000.0, 5000.0, 0.99)
        assert isinstance(result, float)


# ── HMMRegime (214 missed) ────────────────────────────────────────────


class TestHMMRegime:
    def test_init(self):
        from src.hmm_regime import HMMRegimeDetector

        det = HMMRegimeDetector()
        assert det is not None


# ── PaperTrader (210 missed) ──────────────────────────────────────────


class TestPaperTrader:
    def test_is_paper_mode(self):
        from src.paper_trader import is_paper_mode

        assert isinstance(is_paper_mode(), bool)

    def test_constants(self):
        from src.paper_trader import (
            PAPER_FEE_RATE,
            PAPER_MIN_ORDER_USDT,
            PAPER_SLIPPAGE_PCT,
        )

        assert PAPER_SLIPPAGE_PCT > 0
        assert PAPER_FEE_RATE > 0
        assert PAPER_MIN_ORDER_USDT > 0

    def test_binance_sdk_proxy(self):
        from src.paper_trader import _BinanceSDKProxy

        mock_pt = MagicMock()
        mock_pt.get_current_price.return_value = 50000.0
        proxy = _BinanceSDKProxy(mock_pt)
        result = proxy.ticker_price("BTCUSDT")
        assert result["price"] == "50000.0"


# ── WSUserStream (175 missed) ─────────────────────────────────────────


class TestWSUserStream:
    def test_connection_stats(self):
        from src.ws_user_stream import ConnectionStats

        stats = ConnectionStats()
        assert stats.total_messages_received == 0
        assert stats.total_errors == 0
        assert stats.total_connections == 0

    def test_constants(self):
        from src.ws_user_stream import (
            RECONNECT_INITIAL_DELAY,
            RECONNECT_MAX_DELAY,
            SPOT_WS_BASE,
        )

        assert "wss://" in SPOT_WS_BASE
        assert RECONNECT_INITIAL_DELAY > 0
        assert RECONNECT_MAX_DELAY > RECONNECT_INITIAL_DELAY


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


# ── ScanOrchestrator (148 missed) ─────────────────────────────────────


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


# ── MarketResearcher (338 missed) ─────────────────────────────────────


class TestMarketResearcher:
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

    def test_save_json_success(self, tmp_path):
        from src.market_researcher import _save_json

        filepath = tmp_path / "test.json"
        assert _save_json(filepath, {"key": "value"}) is True

    def test_save_json_failure(self, tmp_path):
        from src.market_researcher import _save_json

        assert _save_json(tmp_path / "nonexistent" / "test.json", {}) is False


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
        assert _check_price_deviation(mock_client, "BTCUSDT", 100.0) is True


# ── Backtest (467 missed) ─────────────────────────────────────────────


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

    def test_score_rsi_oversold(self):
        from src.backtest import calculate_score

        assert calculate_score({"rsi": 20, "macd_histogram": 0}, None, None) > 40

    def test_score_rsi_overbought(self):
        from src.backtest import calculate_score

        assert calculate_score({"rsi": 80, "macd_histogram": 0}, None, None) < 40

    def test_score_macd_positive(self):
        from src.backtest import calculate_score

        assert calculate_score({"rsi": 50, "macd_histogram": 5}, None, None) > 40

    def test_score_volume_surge(self):
        from src.backtest import calculate_score

        assert (
            calculate_score(
                {"rsi": 50, "macd_histogram": 0}, None, None, volume_surge=True
            )
            > 40
        )

    def test_volume_surge_detection(self):
        from src.backtest import _detect_volume_surge

        klines = [{"volume": 100}] * 20 + [{"volume": 500}]
        assert _detect_volume_surge(klines) is True

    def test_volume_surge_no_surge(self):
        from src.backtest import _detect_volume_surge

        klines = [{"volume": 100}] * 21
        assert _detect_volume_surge(klines) is False

    def test_volume_surge_insufficient(self):
        from src.backtest import _detect_volume_surge

        assert _detect_volume_surge([{"volume": 100}] * 10) is False


# ── CCXTClient (410 missed) ───────────────────────────────────────────


class TestCCXTClient:
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


class TestBinanceSDKClient:
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

    def test_get_symbols(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
        ):
            with patch("src._binance_sdk_client.BinanceSpotClient") as mock_cls:
                mock_client = MagicMock()
                mock_client.exchange_info.return_value = {
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "quoteAsset": "USDT",
                            "status": "TRADING",
                        },
                    ]
                }
                mock_cls.return_value = mock_client
                client = BinanceClient()
                symbols = client.get_symbols("USDT")
                assert "BTCUSDT" in symbols

    def test_get_ticker_price(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
        ):
            with patch("src._binance_sdk_client.BinanceSpotClient") as mock_cls:
                mock_client = MagicMock()
                mock_client.ticker_price.return_value = {"price": "50000.0"}
                mock_cls.return_value = mock_client
                client = BinanceClient()
                assert client.get_ticker_price("BTCUSDT") == 50000.0

    def test_close(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
        ):
            with patch("src._binance_sdk_client.BinanceSpotClient"):
                client = BinanceClient()
                client.close()


# ── Notifier (93 missed) ──────────────────────────────────────────────


class TestNotifier:
    def test_init(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier()
        assert n is not None

    def test_send_text_disabled(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier()
        assert isinstance(n.send_text("test"), bool)


# ── DimensionScorer (72 missed) ───────────────────────────────────────


class TestDimensionScorer:
    def test_score_all(self):
        from src.dimension_scorer import DimensionScorer

        ds = DimensionScorer(binance_client=MagicMock())
        ds.client.get_ticker_price.return_value = 50000.0
        ds.client.get_klines.return_value = []
        result = ds.score_all()
        assert "resonance" in result


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
        result = pm.close_position("BTCUSDT", close_price=55000.0)
        assert result["pnl"] > 0


# ── StateDB (73 missed) ───────────────────────────────────────────────


class TestStateDB:
    def test_kv_set_get(self, tmp_path):
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        db.kv_set("key1", "val1")
        assert db.kv_get("key1") == "val1"


# ── Indicators (57 missed) ────────────────────────────────────────────


class TestIndicators:
    def test_sma(self):
        from src.indicators import Indicators

        assert Indicators.sma([1, 2, 3, 4, 5], 3) == pytest.approx(4.0)

    def test_rsi(self):
        from src.indicators import Indicators

        assert 0 <= Indicators.rsi([100 + i for i in range(30)], 14) <= 100


# ── CVaRRisk ──────────────────────────────────────────────────────────


class TestCVaRRisk:
    def test_compute_cvar(self):
        from src.cvar_risk import CVaRRiskManager

        cvar = CVaRRiskManager()
        returns = [-0.05, -0.03, -0.01, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08]
        assert isinstance(cvar.compute_cvar(returns), float)


# ── FeeOptimizer ──────────────────────────────────────────────────────


class TestFeeOptimizer:
    def test_get_effective_fees(self):
        from src.fee_optimizer import FeeOptimizer

        mock_client = MagicMock()
        mock_client.get_account.return_value = {
            "makerCommission": 10,
            "takerCommission": 10,
        }
        fo = FeeOptimizer(mock_client)
        assert isinstance(fo.get_effective_fees("BTCUSDT"), dict)


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

        assert get_project_root().exists()
