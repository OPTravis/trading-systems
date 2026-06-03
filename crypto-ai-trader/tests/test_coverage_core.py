"""
Core module tests for crypto-ai-trader — targeting highest-impact uncovered code.
"""

from unittest.mock import MagicMock

# ── Indicators ────────────────────────────────────────────────────────


class TestIndicators:
    def test_sma(self):
        from src.indicators import Indicators

        prices = [100.0 + i for i in range(30)]
        result = Indicators.sma(prices, 10)
        assert isinstance(result, float)

    def test_ema(self):
        from src.indicators import Indicators

        prices = [100.0 + i for i in range(30)]
        result = Indicators.ema(prices, 10)
        assert isinstance(result, float)

    def test_rsi(self):
        from src.indicators import Indicators

        prices = [100.0 + i for i in range(30)]
        result = Indicators.rsi(prices, 14)
        assert 0 <= result <= 100

    def test_macd(self):
        from src.indicators import Indicators

        prices = [100.0 + i for i in range(50)]
        result = Indicators.macd(prices)
        assert isinstance(result, dict)

    def test_bollinger_bands(self):
        from src.indicators import Indicators

        prices = [100.0 + i for i in range(30)]
        result = Indicators.bollinger_bands(prices, 20)
        assert isinstance(result, dict)

    def test_atr(self):
        from src.indicators import Indicators

        klines = [
            {"high": 105.0 + i, "low": 95.0 + i, "close": 100.0 + i} for i in range(30)
        ]
        result = Indicators.atr(klines, 14)
        assert isinstance(result, (float, list))

    def test_obv(self):
        from src.indicators import Indicators

        klines = [{"close": 100.0 + i, "volume": 1000000} for i in range(30)]
        result = Indicators.obv(klines)
        assert isinstance(result, (float, list))

    def test_vwap(self):
        from src.indicators import Indicators

        klines = [
            {"high": 105.0, "low": 95.0, "close": 100.0, "volume": 1000000}
            for _ in range(30)
        ]
        result = Indicators.vwap(klines)
        assert isinstance(result, (float, list))


# ── StateDB ───────────────────────────────────────────────────────────


class TestStateDB:
    def test_init(self, tmp_path):
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        assert db is not None

    def test_kv_set_get(self, tmp_path):
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        db.kv_set("test_key", "test_value")
        result = db.kv_get("test_key")
        assert result == "test_value"

    def test_kv_get_missing(self, tmp_path):
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        assert db.kv_get("nonexistent") is None

    def test_portfolio_set_get(self, tmp_path):
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        db.portfolio_set("BTCUSDT", {"quantity": 0.1, "entry_price": 50000.0})
        positions = db.portfolio_get_all()
        assert "BTCUSDT" in positions

    def test_portfolio_remove(self, tmp_path):
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        db.portfolio_set("BTCUSDT", {"quantity": 0.1})
        db.portfolio_remove("BTCUSDT")
        assert db.portfolio_get_all() == {}

    def test_trade_add(self, tmp_path):
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        db.trade_add("BTCUSDT", "BUY", 0.1, 50000.0, 0.0)

    def test_decision_add(self, tmp_path):
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        rowid = db.decision_add("BTCUSDT", "buy", decision="BUY", score=80.0)
        assert rowid is not None

    def test_audit_log(self, tmp_path):
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        db.audit_log("test_action", {"key": "value"})

    def test_transaction(self, tmp_path):
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?)", ("tx_key", "tx_val")
            )
        assert db.kv_get("tx_key") == "tx_val"


# ── Portfolio ─────────────────────────────────────────────────────────


class TestPortfolio:
    def test_init(self):
        from src.portfolio import PortfolioManager

        pm = PortfolioManager()
        assert pm is not None

    def _make_pm(self, tmp_path):
        from src.portfolio import PortfolioManager
        from src.state_db import get_state_db

        db = get_state_db(str(tmp_path / "test.db"))
        pm = PortfolioManager()
        pm._db = db
        pm.cash_balance = 1_000_000.0
        return pm

    def test_add_position(self, tmp_path):
        pm = self._make_pm(tmp_path)
        pm.add_position(
            "BTCUSDT",
            quantity=0.1,
            entry_price=50000.0,
            strategy="test",
            deduct_cash=False,
        )
        pos = pm.get_position("BTCUSDT")
        assert pos is not None
        assert pos["symbol"] == "BTCUSDT"

    def test_get_position(self, tmp_path):
        pm = self._make_pm(tmp_path)
        pm.add_position("BTCUSDT", quantity=0.1, entry_price=50000.0, deduct_cash=False)
        pos = pm.get_position("BTCUSDT")
        assert pos is not None

    def test_get_all_positions(self, tmp_path):
        pm = self._make_pm(tmp_path)
        pm.add_position("BTCUSDT", quantity=0.1, entry_price=50000.0, deduct_cash=False)
        positions = pm.get_all_positions()
        assert len(positions) >= 1

    def test_close_position(self, tmp_path):
        pm = self._make_pm(tmp_path)
        pm.add_position("BTCUSDT", quantity=0.1, entry_price=50000.0, deduct_cash=False)
        result = pm.close_position("BTCUSDT", close_price=55000.0)
        assert result["pnl"] > 0

    def test_update_position_price(self, tmp_path):
        pm = self._make_pm(tmp_path)
        pm.add_position("BTCUSDT", quantity=0.1, entry_price=50000.0, deduct_cash=False)
        pm.update_position_price("BTCUSDT", 55000.0)
        pos = pm.get_position("BTCUSDT")
        assert pos["current_price"] == 55000.0

    def test_get_summary(self):
        from src.portfolio import PortfolioManager

        pm = PortfolioManager()
        summary = pm.get_summary()
        assert isinstance(summary, dict)


# ── RiskManager ───────────────────────────────────────────────────────


class TestRiskManager:
    def test_init(self):
        from src.risk_manager import RiskManager

        rm = RiskManager()
        assert rm is not None

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
        assert isinstance(result, dict)
        assert "allowed" in result

    def test_post_trade_update(self):
        from src.risk_manager import RiskManager

        rm = RiskManager()
        rm.post_trade_update("BTCUSDT", 500.0, 0.1)

    def test_loss_guard_status(self):
        from src.risk_manager import ConsecutiveLossGuard

        guard = ConsecutiveLossGuard()
        status = guard.get_status()
        assert isinstance(status, dict)


# ── BearAnalyst ───────────────────────────────────────────────────────


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


# ── DimensionScorer ───────────────────────────────────────────────────


class TestDimensionScorer:
    def test_init(self):
        from src.dimension_scorer import DimensionScorer

        mock_client = MagicMock()
        ds = DimensionScorer(binance_client=mock_client)
        assert ds is not None

    def test_score_all(self):
        from src.dimension_scorer import DimensionScorer

        mock_client = MagicMock()
        mock_client.get_ticker_price.return_value = 50000.0
        mock_client.get_klines.return_value = []
        ds = DimensionScorer(binance_client=mock_client)
        result = ds.score_all()
        assert isinstance(result, dict)


# ── StrategyAdaptor ───────────────────────────────────────────────────


class TestStrategyAdaptor:
    def test_init(self):
        from src.strategy_adaptor import StrategyAdaptor

        sa = StrategyAdaptor()
        assert sa is not None

    def test_adapt(self):
        from src.strategy_adaptor import StrategyAdaptor

        sa = StrategyAdaptor()
        result = sa.adapt(
            fear_greed=50,
            btc_trend="up",
            btc_price_change_24h=1.5,
        )
        assert isinstance(result, dict)


# ── PositionOptimizer ─────────────────────────────────────────────────


class TestPositionOptimizer:
    def test_init(self):
        from src.position_optimizer import PositionOptimizer

        mock_client = MagicMock()
        mock_portfolio = MagicMock()
        mock_scanner = MagicMock()
        po = PositionOptimizer(mock_client, mock_portfolio, mock_scanner)
        assert po is not None

    def test_analyze_and_switch(self):
        from src.position_optimizer import PositionOptimizer

        mock_client = MagicMock()
        mock_portfolio = MagicMock()
        mock_scanner = MagicMock()
        po = PositionOptimizer(mock_client, mock_portfolio, mock_scanner)
        result = po.analyze_and_switch(
            dry_run=True,
            opportunities=[{"symbol": "BTCUSDT", "score": 80}],
        )
        assert isinstance(result, list)


# ── FeishuNotifier ────────────────────────────────────────────────────


class TestFeishuNotifier:
    def test_init(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier()
        assert n is not None

    def test_send_disabled(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier()
        result = n.send_text("Test message")
        assert isinstance(result, bool)


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


# ── PendingConfirmation ───────────────────────────────────────────────


class TestPendingConfirmation:
    def test_save_load_clear(self, tmp_path):
        from src import pending_confirmation

        pending_confirmation.DATA_DIR = tmp_path
        pending_confirmation.save_pending({"symbol": "BTCUSDT", "action": "BUY"})
        data = pending_confirmation.load_pending()
        assert data is not None
        pending_confirmation.clear_pending()
        assert pending_confirmation.load_pending() is None


# ── Utils ─────────────────────────────────────────────────────────────


class TestUtils:
    def test_get_project_root(self):
        from src.utils import get_project_root

        root = get_project_root()
        assert root.exists()
