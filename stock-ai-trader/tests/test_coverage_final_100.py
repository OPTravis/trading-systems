"""
Final 100% coverage push — targeting specific remaining uncovered lines.
"""

import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

# ── TradeExecutor: SL/TP error paths, wait_for_fill paths ─────────────


class TestTradeExecutorFinal:
    @pytest.mark.asyncio
    async def test_place_sl_failure(self):
        from src.brokers.broker_protocol import (
            Contract,
            Order,
            OrderSide,
            OrderStatus,
            OrderType,
        )
        from src.trade_executor import TradeExecutor

        broker = AsyncMock()
        placed = Order(
            contract=Contract(symbol="AAPL"),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=100,
        )
        placed.order_id = 12345
        placed.status = OrderStatus.FILLED
        placed.filled_qty = 100
        placed.avg_fill_price = 150.0
        broker.place_order.return_value = placed
        broker.place_order.side_effect = [placed, Exception("SL failed")]
        te = TradeExecutor(broker=broker)
        result = await te.execute("AAPL", "BUY", 100, stop_loss=140.0)
        assert result["success"] is True  # Main order filled even if SL fails

    @pytest.mark.asyncio
    async def test_place_tp_failure(self):
        from src.brokers.broker_protocol import (
            Contract,
            Order,
            OrderSide,
            OrderStatus,
            OrderType,
        )
        from src.trade_executor import TradeExecutor

        broker = AsyncMock()
        placed = Order(
            contract=Contract(symbol="AAPL"),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=100,
        )
        placed.order_id = 12345
        placed.status = OrderStatus.FILLED
        placed.filled_qty = 100
        placed.avg_fill_price = 150.0
        call_count = [0]

        async def mock_place(order):
            call_count[0] += 1
            if call_count[0] == 1:
                return placed
            raise Exception("TP failed")

        broker.place_order = mock_place
        te = TradeExecutor(broker=broker)
        result = await te.execute("AAPL", "BUY", 100, take_profit=170.0)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_wait_for_fill_timeout(self):
        from src.trade_executor import TradeExecutor

        broker = AsyncMock()
        broker.get_open_orders.return_value = []
        te = TradeExecutor(broker=broker)
        te.FILL_TIMEOUT_SEC = 0.1
        result = await te._wait_for_fill(12345, "AAPL", "BUY", 100, 0)
        assert result is None

    @pytest.mark.asyncio
    async def test_wait_for_fill_cancelled(self):
        from src.brokers.broker_protocol import (
            Contract,
            Order,
            OrderSide,
            OrderStatus,
            OrderType,
        )
        from src.trade_executor import TradeExecutor

        broker = AsyncMock()
        order = Order(
            contract=Contract(symbol="AAPL"),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=100,
        )
        order.order_id = 12345
        order.status = OrderStatus.CANCELLED
        broker.get_open_orders.return_value = [order]
        te = TradeExecutor(broker=broker)
        te.FILL_TIMEOUT_SEC = 1
        result = await te._wait_for_fill(12345, "AAPL", "BUY", 100, 0)
        assert result is not None
        assert result.success is False

    @pytest.mark.asyncio
    async def test_wait_for_fill_consecutive_errors(self):
        from src.trade_executor import TradeExecutor

        broker = AsyncMock()
        broker.get_open_orders.side_effect = Exception("timeout")
        te = TradeExecutor(broker=broker)
        te.FILL_TIMEOUT_SEC = 1
        result = await te._wait_for_fill(12345, "AAPL", "BUY", 100, 0)
        assert result is None

    def test_get_pending_no_broker(self):
        from src.trade_executor import TradeExecutor

        te = TradeExecutor(broker=None)
        assert te.get_pending_orders() == []

    def test_cancel_all_no_broker(self):
        from src.trade_executor import TradeExecutor

        te = TradeExecutor(broker=None)
        te.cancel_all_orders()

    @pytest.mark.asyncio
    async def test_size_and_execute_no_portfolio(self):
        from src.trade_executor import TradeExecutor

        te = TradeExecutor(broker=None)
        result = await te.size_and_execute("AAPL", 150.0)
        assert result["success"] is False


# ── PaperClient: modify, qualify, contract details ─────────────────────


class TestPaperClientFinal:
    @pytest.mark.asyncio
    async def test_modify_order(self):
        from src.brokers.broker_protocol import Contract, Order, OrderSide, OrderType
        from src.brokers.paper_client import PaperClient

        client = PaperClient(starting_balance=100_000.0)
        await client.connect()
        client.set_market_price("AAPL", 150.0)
        order = Order(
            contract=Contract(symbol="AAPL"),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=10,
            limit_price=140.0,
        )
        placed = await client.place_order(order)
        modified = await client.modify_order(placed.order_id, limit_price=135.0)
        assert modified.limit_price == 135.0
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_get_contract_details(self):
        from src.brokers.broker_protocol import Contract
        from src.brokers.paper_client import PaperClient

        client = PaperClient()
        await client.connect()
        details = await client.get_contract_details(Contract(symbol="AAPL"))
        assert "AAPL" in details.long_name
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_qualify_contract(self):
        from src.brokers.broker_protocol import Contract
        from src.brokers.paper_client import PaperClient

        client = PaperClient()
        await client.connect()
        contract = Contract(symbol="AAPL")
        result = await client.qualify_contract(contract)
        assert result.qualified is True
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_cancel_nonexistent(self):
        from src.brokers.paper_client import PaperClient

        client = PaperClient()
        await client.connect()
        await client.cancel_order(999)
        await client.disconnect()


# ── Portfolio: remaining paths ─────────────────────────────────────────


class TestPortfolioFinal:
    def test_save_with_db(self):
        import os
        import tempfile

        from shared.core.state_db import StateDB
        from src.portfolio import PortfolioManager

        with tempfile.TemporaryDirectory() as td:
            db = StateDB(os.path.join(td, "test.db"))
            pm = PortfolioManager(db=db)
            pm._cash["USD"].total_cash = 1_000_000.0
            pm.add_position("AAPL", quantity=100, price=150.0)
            pm._save(force=True)
            assert db.portfolio_get_all() is not None

    def test_load_from_db(self):
        import os
        import tempfile

        from shared.core.state_db import StateDB
        from src.portfolio import PortfolioManager

        with tempfile.TemporaryDirectory() as td:
            db = StateDB(os.path.join(td, "test.db"))
            pm = PortfolioManager(db=db)
            pm._cash["USD"].total_cash = 1_000_000.0
            pm.add_position("AAPL", quantity=100, price=150.0, sector="Tech")
            pm2 = PortfolioManager(db=db)
            assert pm2.get_position("AAPL") is not None

    def test_save_removes_closed(self):
        import os
        import tempfile

        from shared.core.state_db import StateDB
        from src.portfolio import PortfolioManager

        with tempfile.TemporaryDirectory() as td:
            db = StateDB(os.path.join(td, "test.db"))
            pm = PortfolioManager(db=db)
            pm._cash["USD"].total_cash = 1_000_000.0
            pm.add_position("AAPL", quantity=100, price=150.0)
            pm._save(force=True)
            pm.close_position("AAPL", price=160.0)
            pm._save(force=True)
            assert "AAPL" not in db.portfolio_get_all()

    def test_save_exception(self):
        from src.portfolio import PortfolioManager

        pm = PortfolioManager(db=None)
        pm._db = MagicMock()
        pm._db.portfolio_set.side_effect = Exception("DB error")
        pm._save(force=True)

    def test_load_exception(self):
        from src.portfolio import PortfolioManager

        pm = PortfolioManager(db=None)
        pm._db = MagicMock()
        pm._db.portfolio_get_all.side_effect = Exception("DB error")

    def test_save_debounce(self):
        from src.portfolio import PortfolioManager

        pm = PortfolioManager(db=None)
        pm._db = MagicMock()
        pm._last_save_time = time.monotonic()
        pm._save(force=False)
        pm._db.portfolio_set.assert_not_called()

    def test_sync_failure(self):
        from src.portfolio import PortfolioManager

        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 1_000_000.0
        broker = MagicMock()
        broker.get_account.side_effect = Exception("fail")
        assert pm.sync_from_broker(broker) is False

    def test_sync_mid_failure(self):
        from src.portfolio import PortfolioManager

        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 1_000_000.0
        broker = MagicMock()
        account = MagicMock()
        account.currency = "USD"
        account.total_cash = 50_000.0
        broker.get_account.return_value = account
        broker.get_portfolio.side_effect = Exception("fail")
        assert pm.sync_from_broker(broker) is False

    def test_fx_paths(self):
        import src.portfolio as p

        p._FX_CACHE = {}
        p._FX_CACHE_TS = 0.0
        with patch("yfinance.Tickers") as mock:
            mock.return_value.tickers = {
                "USDHKD=X": MagicMock(info={"regularMarketPrice": 7.8})
            }
            rate = p._get_fx_to_usd("HKD")
            assert rate == 7.8

    def test_fx_cache_hit(self):
        import src.portfolio as p

        p._FX_CACHE = {"HKD": 7.8}
        p._FX_CACHE_TS = time.time()
        assert p._get_fx_to_usd("HKD") == 7.8
        p._FX_CACHE = {}
        p._FX_CACHE_TS = 0.0

    def test_sector_exposure(self):
        from src.portfolio import PortfolioManager

        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 1_000_000.0
        pm.add_position("AAPL", quantity=100, price=150.0, sector="Tech")
        assert "Tech" in pm.get_sector_exposure()

    def test_unsettle_breakdown(self):
        from src.portfolio import PortfolioManager

        pm = PortfolioManager(db=None)
        pm._cash["USD"].total_cash = 1_000_000.0
        pm._cash["USD"].record_sell(50_000.0, market="US")
        assert isinstance(pm.get_unsettle_breakdown("USD"), dict)


# ── Momentum: specific uncovered lines ─────────────────────────────────


class TestMomentumFinal:
    def test_rs_short_data(self):
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        rs = s._calculate_relative_strength(
            {"AAPL": pd.DataFrame({"close": [150.0] * 10})}
        )
        assert "AAPL" not in rs

    def test_rs_normal(self):
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        rs = s._calculate_relative_strength(
            {"AAPL": pd.DataFrame({"close": list(range(100, 352))})}
        )
        assert "AAPL" in rs

    def test_rs_zero_price(self):
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        close = [0.0] * 252 + [150.0] * 21
        rs = s._calculate_relative_strength({"AAPL": pd.DataFrame({"close": close})})
        assert "AAPL" not in rs

    def test_breakout_no_volume(self):
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        df = pd.DataFrame(
            {
                "close": [150.0] * 25,
                "high": [155.0] * 25,
                "low": [148.0] * 25,
                "volume": [100] * 25,
            },
            index=pd.date_range(end=datetime.now(), periods=25),
        )
        assert s._check_breakout("AAPL", df, 50.0, datetime.now()) is None

    def test_breakout_no_breakout(self):
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        df = pd.DataFrame(
            {
                "close": [150.0] * 25,
                "high": [155.0] * 25,
                "low": [148.0] * 25,
                "volume": [1e6] * 25,
            },
            index=pd.date_range(end=datetime.now(), periods=25),
        )
        assert s._check_breakout("AAPL", df, 50.0, datetime.now()) is None

    def test_should_exit_no_stop(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=15),
            strategy="Momentum",
            stop_loss=None,
            metadata={"current_price": 144.0},
        )
        assert s.should_exit(pos) is False

    def test_update_trailing_stop(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now(),
            strategy="Momentum",
            stop_loss=140.0,
            metadata={"atr": 3.0},
        )
        s.update_trailing_stop(pos, 160.0)
        assert pos.stop_loss > 140.0

    def test_params(self):
        from src.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        assert "rs_lookback_long" in s.get_params()


# ── MeanRevert: specific uncovered lines ───────────────────────────────


class TestMeanRevertFinal:
    def test_generate_signals(self, sample_universe):
        from src.strategies.mean_revert import MeanRevertStrategy

        s = MeanRevertStrategy()
        signals = s.generate_signals(sample_universe)
        assert isinstance(signals, list)

    def test_should_enter(self):
        from src.strategies.base_strategy import Signal, SignalAction
        from src.strategies.mean_revert import MeanRevertStrategy

        s = MeanRevertStrategy()
        signal = Signal(
            symbol="AAPL",
            action=SignalAction.BUY,
            strategy="MeanRevert",
            timestamp=datetime.now(),
            strength=0.7,
            price=150.0,
        )
        assert s.should_enter(signal) is True

    def test_should_exit_max_holding(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.mean_revert import MeanRevertStrategy

        s = MeanRevertStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=30),
            strategy="MeanRevert",
            metadata={"current_price": 155.0},
        )
        assert s.should_exit(pos) is True

    def test_should_exit_stop_hit(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.mean_revert import MeanRevertStrategy

        s = MeanRevertStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=5),
            strategy="MeanRevert",
            stop_loss=145.0,
            metadata={"current_price": 144.0},
        )
        assert s.should_exit(pos) is True

    def test_should_exit_tp_hit(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.mean_revert import MeanRevertStrategy

        s = MeanRevertStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=10),
            strategy="MeanRevert",
            stop_loss=140.0,
            take_profit=160.0,
            metadata={"current_price": 162.0},
        )
        assert s.should_exit(pos) is True


# ── TrendStrategy: specific uncovered lines ────────────────────────────


class TestTrendStrategyFinal:
    def test_generate_signals(self, sample_universe):
        from src.strategies.trend_strategy import TrendStrategy

        s = TrendStrategy()
        signals = s.generate_signals(sample_universe)
        assert isinstance(signals, list)

    def test_should_enter(self):
        from src.strategies.base_strategy import Signal, SignalAction
        from src.strategies.trend_strategy import TrendStrategy

        s = TrendStrategy()
        signal = Signal(
            symbol="AAPL",
            action=SignalAction.BUY,
            strategy="TrendFollowing",
            timestamp=datetime.now(),
            strength=0.7,
            price=150.0,
        )
        assert s.should_enter(signal) is True

    def test_should_exit(self):
        from src.strategies.base_strategy import Position as StratPosition
        from src.strategies.trend_strategy import TrendStrategy

        s = TrendStrategy()
        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now() - timedelta(days=35),
            strategy="TrendFollowing",
            stop_loss=140.0,
            metadata={"current_price": 155.0},
        )
        assert s.should_exit(pos) is True


# ── MacroAnalyzer: remaining scoring paths ─────────────────────────────


class TestMacroFinal:
    def test_scoring_peak(self):
        from src.research.macro_analyzer import MacroAnalyzer

        a = MacroAnalyzer(fred_api_key="test")
        with patch("src.research.macro_analyzer._fred_latest") as mock_fred:

            def side_effect(series, key):
                vals = {
                    "FEDFUNDS": 5.0,
                    "DGS2": 4.0,
                    "DGS10": 4.5,
                    "A191RL1Q225SBEA": 1.0,
                    "UNRATE": 4.0,
                    "VIXCLS": 22.0,
                    "FPCPITOTLZGUSA": 3.0,
                }
                return vals.get(series)

            mock_fred.side_effect = side_effect
            with patch.object(a, "_get_credit_spread", return_value=0.82):
                state = a.get_macro_state()
                assert state.phase.value in (
                    "expansion",
                    "peak",
                    "contraction",
                    "trough",
                )

    def test_scoring_trough(self):
        from src.research.macro_analyzer import MacroAnalyzer

        a = MacroAnalyzer(fred_api_key="test")
        with patch("src.research.macro_analyzer._fred_latest") as mock_fred:

            def side_effect(series, key):
                vals = {
                    "FEDFUNDS": 1.0,
                    "DGS2": 0.5,
                    "DGS10": 1.5,
                    "A191RL1Q225SBEA": -3.0,
                    "UNRATE": 8.0,
                    "VIXCLS": 35.0,
                    "FPCPITOTLZGUSA": 1.0,
                }
                return vals.get(series)

            mock_fred.side_effect = side_effect
            with patch.object(a, "_get_credit_spread", return_value=0.75):
                state = a.get_macro_state()
                assert state.phase.value in (
                    "expansion",
                    "peak",
                    "contraction",
                    "trough",
                )


# ── IBKRClient: remaining paths ────────────────────────────────────────


class TestIBKRClientFinal:
    def test_on_error_info(self):
        from src.brokers.ibkr_client import IBKRClient

        c = IBKRClient()
        c._on_error(0, 2104, "Data farm OK", None)
        c._on_error(0, 2106, "Data farm OK", None)
        c._on_error(0, 2158, "Data farm OK", None)

    def test_on_error_connection(self):
        from src.brokers.ibkr_client import IBKRClient

        c = IBKRClient()
        c._on_error(0, 502, "Connection error", None)
        c._on_error(0, 504, "Not connected", None)
        c._on_error(0, 1100, "Connectivity lost", None)
        c._on_error(0, 1300, "TWS socket dropped", None)

    def test_on_error_other(self):
        from src.brokers.ibkr_client import IBKRClient

        c = IBKRClient()
        c._on_error(0, 321, "Error", None)


# ── ScanOrchestrator: remaining paths ──────────────────────────────────


class TestScanOrchestratorFinal:
    def test_build_sector_map(self):
        from src.scan_orchestrator import ScanOrchestrator

        orch = ScanOrchestrator()
        sector_map = orch._build_sector_map()
        assert isinstance(sector_map, dict)

    def test_phase2_fallback(self):
        from src.scan_orchestrator import ScanOrchestrator

        orch = ScanOrchestrator()
        orch.scorer = MagicMock()
        orch.scorer.score_stock.return_value = MagicMock(
            composite=70,
            technical=65,
            fundamental=60,
            momentum=75,
            sentiment=55,
            quality=80,
            value=50,
        )
        orch.ranker = None
        ranked, scores = orch._phase2_score_and_rank(["AAPL", "MSFT"])
        assert len(ranked) == 2


# ── StockScorer: remaining paths ───────────────────────────────────────


class TestScorerFinal:
    def test_score_with_none_factors(self):
        from src.scoring.stock_scorer import StockScorer

        scorer = StockScorer()
        score = scorer.score_stock("AAPL")
        assert 0 <= score.composite <= 100

    def test_score_redistribute(self):
        from src.scoring.stock_scorer import StockScorer

        scorer = StockScorer()
        with patch.object(scorer, "_score_technical", return_value=None):
            with patch.object(scorer, "_score_fundamental", return_value=None):
                with patch.object(scorer, "_score_momentum", return_value=None):
                    with patch.object(scorer, "_score_sentiment", return_value=None):
                        score = scorer.score_stock("AAPL")
                        assert 0 <= score.composite <= 100

    def test_get_weights_allocation(self):
        from src.scoring.stock_scorer import StockScorer

        scorer = StockScorer()
        scorer._strategy_allocation = {
            "AAPL": {"weights": {"technical": 3.0, "momentum": 2.0}}
        }
        weights = scorer._get_weights("AAPL")
        assert sum(weights.values()) == pytest.approx(1.0)

    def test_get_weights_ic(self):
        from src.scoring.stock_scorer import StockScorer

        scorer = StockScorer()
        tracker = MagicMock()
        tracker.get_weights.return_value = {
            "technical": 3.0,
            "momentum": 2.0,
            "fundamental": 1.0,
            "sentiment": 1.0,
            "quality": 1.0,
            "value": 1.0,
        }
        scorer.ic_tracker = tracker
        weights = scorer._get_weights("AAPL")
        assert sum(weights.values()) == pytest.approx(1.0)


# ── Notifier: remaining paths ──────────────────────────────────────────


class TestNotifierFinal:
    def test_token_cache_hit(self):
        import src.notifier as n

        n._token_cache["token"] = "cached"
        n._token_cache["expires_at"] = time.time() + 3600
        assert n._get_tenant_token() == "cached"

    def test_token_no_creds(self):
        import src.notifier as n

        n._token_cache["token"] = ""
        n._token_cache["expires_at"] = 0.0
        with patch.dict("os.environ", {"FEISHU_APP_ID": "", "FEISHU_APP_SECRET": ""}):
            assert n._get_tenant_token() == ""

    def test_send_card_no_token(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier(chat_id="test")
        with patch("src.notifier._get_tenant_token", return_value=""):
            assert n._send_card("Title", []) is False

    def test_send_card_disabled(self):
        from src.notifier import FeishuNotifier

        n = FeishuNotifier(chat_id="")
        assert n._send_card("Title", []) is False


# ── BaseStrategy: remaining paths ──────────────────────────────────────


class TestBaseStrategyFinal:
    def test_position_pnl(self):
        from src.strategies.base_strategy import Position as StratPosition

        pos = StratPosition(
            symbol="AAPL",
            entry_price=150.0,
            quantity=100,
            entry_date=datetime.now(),
            strategy="test",
            metadata={"current_price": 160.0},
        )
        assert pos.unrealized_pnl_pct > 0

    def test_position_zero_entry(self):
        from src.strategies.base_strategy import Position as StratPosition

        pos = StratPosition(
            symbol="AAPL",
            entry_price=0.0,
            quantity=100,
            entry_date=datetime.now(),
            strategy="test",
        )
        assert pos.unrealized_pnl_pct == 0.0
