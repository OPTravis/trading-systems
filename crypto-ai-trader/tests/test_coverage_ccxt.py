"""
Tests for ccxt_client — covering all uncovered code paths.
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest


class TestCCXTClientAll:
    """Cover all methods in ccxt_client.BinanceClient."""

    @pytest.fixture
    def client(self):
        from src.ccxt_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}, clear=False
        ):
            with patch("ccxt.binance") as mock_binance:
                mock_exchange = MagicMock()
                mock_binance.return_value = mock_exchange
                c = BinanceClient()
                c._mock = mock_exchange
                return c

    def test_init_success(self):
        from src.ccxt_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
        ):
            with patch("ccxt.binance") as mock_binance:
                mock_binance.return_value = MagicMock()
                client = BinanceClient()
                assert client.api_key == "k"

    def test_init_no_keys(self):
        from src.ccxt_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "", "BINANCE_API_SECRET": ""}, clear=False
        ):
            with pytest.raises(ValueError):
                BinanceClient()

    def test_init_testnet(self):
        from src.ccxt_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
        ):
            with patch("ccxt.binance") as mock_binance:
                mock_exchange = MagicMock()
                mock_binance.return_value = mock_exchange
                client = BinanceClient(testnet=True)
                mock_exchange.set_sandbox_mode.assert_called_with(True)

    def test_validate_symbol(self, client):
        assert client.validate_symbol("BTCUSDT") is True

    def test_validate_symbol_with_allowlist(self):
        from src.ccxt_client import BinanceClient

        with patch.dict(
            os.environ,
            {
                "BINANCE_API_KEY": "k",
                "BINANCE_API_SECRET": "s",
                "ALLOWED_SYMBOLS": "BTCUSDT,ETHUSDT",
            },
        ):
            with patch("ccxt.binance") as mock_binance:
                mock_binance.return_value = MagicMock()
                client = BinanceClient()
                assert client.validate_symbol("BTCUSDT") is True
                assert client.validate_symbol("XYZ") is False

    def test_get_symbols(self, client):
        client._mock.publicGetExchangeInfo.return_value = {
            "symbols": [
                {"symbol": "BTCUSDT", "quoteAsset": "USDT", "status": "TRADING"},
                {"symbol": "ETHUSDT", "quoteAsset": "USDT", "status": "TRADING"},
                {"symbol": "BTCBUSD", "quoteAsset": "BUSD", "status": "TRADING"},
            ]
        }
        symbols = client.get_symbols("USDT")
        assert "BTCUSDT" in symbols
        assert "BTCBUSD" not in symbols

    def test_get_symbols_error(self, client):
        client._mock.publicGetExchangeInfo.side_effect = Exception("error")
        assert client.get_symbols() == []

    def test_get_exchange_info_cached(self, client):
        client._mock.publicGetExchangeInfo.return_value = {"symbols": []}
        info1 = client.get_exchange_info()
        info2 = client.get_exchange_info()
        assert info1 == info2

    def test_get_exchange_info_error(self, client):
        client._mock.publicGetExchangeInfo.side_effect = Exception("error")
        client._exchange_info_cache = None
        result = client.get_exchange_info()
        assert result == {}

    def test_get_klines(self, client):
        client._mock.publicGetKlines.return_value = [
            [
                1000000,
                "50000",
                "51000",
                "49000",
                "50500",
                "100",
                1000100,
                "5000000",
                50,
                "50000",
                "0",
                "0",
            ]
        ]
        klines = client.get_klines("BTCUSDT", "1h", limit=1)
        assert len(klines) == 1
        assert klines[0]["close"] == 50500.0

    def test_get_klines_with_times(self, client):
        client._mock.publicGetKlines.return_value = []
        klines = client.get_klines("BTCUSDT", "1h", start_time=1000, end_time=2000)
        assert klines == []

    def test_get_klines_error(self, client):
        client._mock.publicGetKlines.side_effect = Exception("error")
        klines = client.get_klines("BTCUSDT", "1h", max_retries=1)
        assert klines == []

    def test_get_order_book_error(self, client):
        client._mock.publicGetDepth.side_effect = Exception("error")
        assert client.get_order_book("BTCUSDT") == {"bids": [], "asks": []}

    def test_get_balance_cached(self, client):
        client._balance_cache = {"USDT": (10000.0, time.time())}
        assert client.get_balance("USDT") == 10000.0

    def test_get_balance_missing(self, client):
        client._balance_cache = {}
        client._mock.privateGetAccount.return_value = {"balances": []}
        assert client.get_balance("XYZ") == 0.0

    def test_get_free_balance_missing(self, client):
        client._mock.privateGetAccount.return_value = {"balances": []}
        assert client.get_free_balance("XYZ") == 0.0

    def test_place_market_buy(self, client):
        client._mock.create_order.return_value = {"orderId": 123, "status": "FILLED"}
        result = client.place_market_buy("BTCUSDT", 0.1)
        assert result["orderId"] == 123

    def test_place_market_sell(self, client):
        client._mock.create_order.return_value = {"orderId": 124, "status": "FILLED"}
        result = client.place_market_sell("BTCUSDT", 0.1)
        assert result["orderId"] == 124

    def test_place_limit_buy(self, client):
        client._mock.create_order.return_value = {"orderId": 125, "status": "NEW"}
        result = client.place_limit_buy("BTCUSDT", 0.1, 50000.0)
        assert result["orderId"] == 125

    def test_place_limit_sell(self, client):
        client._mock.create_order.return_value = {"orderId": 126, "status": "NEW"}
        result = client.place_limit_sell("BTCUSDT", 0.1, 55000.0)
        assert result["orderId"] == 126

    def test_cancel_order(self, client):
        client._mock.cancel_order.return_value = {"orderId": 123, "status": "CANCELLED"}
        result = client.cancel_order("BTCUSDT", 123)
        assert result["status"] == "CANCELLED"

    def test_cancel_order_error(self, client):
        client._mock.cancel_order.side_effect = Exception("error")
        result = client.cancel_order("BTCUSDT", 999)
        assert result is None

    def test_get_open_orders(self, client):
        client._mock.fetch_open_orders.return_value = [{"orderId": 1}]
        orders = client.get_open_orders("BTCUSDT")
        assert len(orders) == 1

    def test_get_open_orders_error(self, client):
        client._mock.fetch_open_orders.side_effect = Exception("error")
        assert client.get_open_orders() == []

    def test_cancel_all_orders(self, client):
        client._mock.cancel_all_orders.return_value = None
        assert client.cancel_all_orders("BTCUSDT") is True

    def test_get_order(self, client):
        client._mock.fetch_order.return_value = {"orderId": 123, "status": "FILLED"}
        assert client.get_order("BTCUSDT", 123)["status"] == "FILLED"

    def test_get_order_error(self, client):
        client._mock.fetch_order.side_effect = Exception("error")
        assert client.get_order("BTCUSDT", 999) is None

    def test_get_trades(self, client):
        client._mock.fetch_trades.return_value = [{"id": 1}]
        assert len(client.get_trades("BTCUSDT")) == 1

    def test_get_trades_error(self, client):
        client._mock.fetch_trades.side_effect = Exception("error")
        assert client.get_trades("BTCUSDT") == []

    def test_close(self, client):
        client.close()
