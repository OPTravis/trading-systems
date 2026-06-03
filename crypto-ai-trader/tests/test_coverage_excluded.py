"""
Tests for previously-excluded modules: backtest, ws_user_stream, ccxt_client, _binance_sdk_client.
"""

# ── Backtest: data classes and helpers ─────────────────────────────────


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
        assert pos.entry_price == 50000.0
        assert pos.trailing_activated is False

    def test_position_defaults(self):
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
        assert pos.trailing_activated is False
        assert pos.trailing_sl == 0.0
        assert pos.highest_price == 0.0
        assert pos.tp1_hit is False
        assert pos.tp2_hit is False
        assert pos.tp3_hit is False

    def test_backtest_engine_import(self):
        from src.backtest import BacktestEngine

        assert BacktestEngine is not None

    def test_backtest_engine_has_run(self):
        from src.backtest import BacktestEngine

        assert hasattr(BacktestEngine, "run")


# ── WebSocket User Stream: ConnectionStats and helpers ─────────────────


class TestWSUserStream:
    def test_connection_stats_init(self):
        from src.ws_user_stream import ConnectionStats

        stats = ConnectionStats()
        assert stats.total_messages_received == 0
        assert stats.total_errors == 0
        assert stats.total_connections == 0

    def test_user_data_stream_class(self):
        from src.ws_user_stream import UserDataStream

        assert UserDataStream is not None

    def test_constants(self):
        from src.ws_user_stream import (
            RECONNECT_INITIAL_DELAY,
            RECONNECT_MAX_DELAY,
            SPOT_WS_BASE,
        )

        assert "wss://" in SPOT_WS_BASE
        assert RECONNECT_INITIAL_DELAY > 0
        assert RECONNECT_MAX_DELAY > RECONNECT_INITIAL_DELAY


# ── CCXT Client: helpers and structure ─────────────────────────────────


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
        assert hasattr(BinanceClient, "get_account")
        assert hasattr(BinanceClient, "get_free_balance")


# ── Binance SDK Client: helpers and structure ──────────────────────────


class TestBinanceSDKClient:
    def test_import(self):
        from src._binance_sdk_client import BinanceClient

        assert BinanceClient is not None

    def test_class_has_methods(self):
        from src._binance_sdk_client import BinanceClient

        assert hasattr(BinanceClient, "get_ticker_price")
        assert hasattr(BinanceClient, "get_klines")
        assert hasattr(BinanceClient, "get_24hr_stats")
        assert hasattr(BinanceClient, "place_market_buy")
        assert hasattr(BinanceClient, "place_market_sell")
        assert hasattr(BinanceClient, "get_account")
        assert hasattr(BinanceClient, "get_free_balance")
