"""
Coverage boost tests for crypto-ai-trader — targeting major uncovered modules.
"""

from unittest.mock import MagicMock, patch

# ── FeatureStore (0% → target 80%) ────────────────────────────────────


class TestFeatureStore:

    def test_init_with_redis(self):
        from src.feature_store import FeatureStore

        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        with patch("redis.Redis", return_value=mock_redis):
            fs = FeatureStore()
            assert fs._redis_available is True

    def test_get_training_data_fallback(self):
        from src.feature_store import FeatureStore

        fs = FeatureStore.__new__(FeatureStore)
        fs._redis_available = False
        fs._fallback_sorted = {
            "features:training:BTCUSDT": [(1.0, '{"rsi": 65}'), (2.0, '{"rsi": 70}')]
        }
        fs._r = None
        data = fs.get_training_data("BTCUSDT")
        assert len(data) == 2

    def test_get_training_data_empty(self):
        from src.feature_store import FeatureStore

        fs = FeatureStore.__new__(FeatureStore)
        fs._redis_available = False
        fs._fallback_sorted = {}
        fs._r = None
        data = fs.get_training_data("BTCUSDT")
        assert data == []

    def test_online_key(self):
        from src.feature_store import FeatureStore

        fs = FeatureStore.__new__(FeatureStore)
        assert fs._online_key("BTCUSDT") == "features:online:BTCUSDT"

    def test_training_key(self):
        from src.feature_store import FeatureStore

        fs = FeatureStore.__new__(FeatureStore)
        assert fs._training_key("BTCUSDT") == "features:training:BTCUSDT"


# ── DimensionScorer (0% → target 80%) ─────────────────────────────────


class TestDimensionScorer:

    def test_score_all(self):
        from src.dimension_scorer import DimensionScorer

        mock_client = MagicMock()
        mock_client.get_ticker_price.return_value = 50000.0
        mock_client.get_klines.return_value = []
        ds = DimensionScorer(binance_client=mock_client)
        result = ds.score_all()
        assert isinstance(result, dict)
        assert "dimensions" in result
        assert "resonance" in result
        assert "weighted_score" in result

    def test_score_all_returns_valid_resonance(self):
        from src.dimension_scorer import DimensionScorer

        mock_client = MagicMock()
        mock_client.get_ticker_price.return_value = 50000.0
        mock_client.get_klines.return_value = []
        ds = DimensionScorer(binance_client=mock_client)
        result = ds.score_all()
        assert result["resonance"] in (
            "STRONG_BULL",
            "BULL",
            "NEUTRAL",
            "BEAR",
            "STRONG_BEAR",
        )


# ── OnlineLearner (0% → target 60%) ───────────────────────────────────


class TestOnlineLearner:

    def test_default_weights(self):
        from src.online_learner import DEFAULT_WEIGHTS

        assert "technical" in DEFAULT_WEIGHTS
        assert sum(DEFAULT_WEIGHTS.values()) > 0

    # ── HMMRegime (14% → target 50%) ──────────────────────────────────────

    # ── SectorClassifier (38% → target 70%) ───────────────────────────────

    # ── ParamOptimizer (19% → target 50%) ─────────────────────────────────

    # ── CorrelationRisk ───────────────────────────────────────────────────

    # ── StepwiseDrawdown ──────────────────────────────────────────────────

    # ── DrawdownBreaker ───────────────────────────────────────────────────

    # ── CircuitBreaker ────────────────────────────────────────────────────

    def test_record_failure(self):
        from src.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        cb.record_failure()
        assert cb.is_tripped() is False  # Need multiple failures

    # ── DailyLossBreaker ──────────────────────────────────────────────────

    def test_should_block_new_trades(self):
        from src.daily_loss_breaker import DailyLossBreaker

        dlb = DailyLossBreaker()
        assert dlb.should_block_new_trades() is False


# ── KellySizer ────────────────────────────────────────────────────────


# ── FeeOptimizer ──────────────────────────────────────────────────────


class TestFeeOptimizer:

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


# ── TradeJournal ──────────────────────────────────────────────────────


# ── MultiTimeframe ────────────────────────────────────────────────────


# ── DynamicCoinPool ───────────────────────────────────────────────────


# ── PricePredictor ────────────────────────────────────────────────────


# ── ConceptDrift ──────────────────────────────────────────────────────


# ── DataFeedManager ───────────────────────────────────────────────────


# ── Indicators (additional coverage) ──────────────────────────────────


class TestIndicatorsExtra:
    def test_sma_short(self):
        from src.indicators import Indicators

        result = Indicators.sma([100.0], 10)
        assert isinstance(result, float)

    def test_ema_short(self):
        from src.indicators import Indicators

        result = Indicators.ema([100.0], 10)
        assert isinstance(result, float)

    def test_rsi_flat(self):
        from src.indicators import Indicators

        result = Indicators.rsi([100.0] * 30, 14)
        assert isinstance(result, float)

    def test_macd_short(self):
        from src.indicators import Indicators

        result = Indicators.macd([100.0] * 5)
        assert isinstance(result, dict)


# ── RiskManager (additional coverage) ─────────────────────────────────


class TestRiskManagerExtra:
    def test_init_no_client(self):
        from src.risk_manager import RiskManager

        rm = RiskManager()
        assert rm is not None

    def test_pre_trade_check_normal(self):
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


# ── Portfolio (additional coverage) ───────────────────────────────────


class TestPortfolioExtra:

    def test_add_and_close(self, tmp_path):
        from src.portfolio import PortfolioManager
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        pm = PortfolioManager()
        pm._db = db
        pm.cash_balance = 1_000_000.0
        pm.add_position("BTCUSDT", quantity=0.1, entry_price=50000.0, deduct_cash=False)
        pos = pm.get_position("BTCUSDT")
        assert pos is not None
        result = pm.close_position("BTCUSDT", close_price=55000.0)
        assert result["pnl"] > 0


# ── BearAnalyst (additional coverage) ─────────────────────────────────


# ── StrategyAdaptor (additional coverage) ─────────────────────────────


# ── PositionOptimizer (additional coverage) ───────────────────────────


# ── FeishuNotifier (additional coverage) ──────────────────────────────


class TestFeishuNotifierExtra:

    def test_send_text_disabled(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier()
        result = n.send_text("test")
        assert isinstance(result, bool)


# ── PendingConfirmation (additional coverage) ─────────────────────────


class TestPendingConfirmationExtra:
    def test_save_load_clear(self, tmp_path):
        from src import pending_confirmation

        pending_confirmation.DATA_DIR = tmp_path
        pending_confirmation.save_pending({"symbol": "BTCUSDT"})
        data = pending_confirmation.load_pending()
        assert data is not None
        pending_confirmation.clear_pending()
        assert pending_confirmation.load_pending() is None
