"""
Comprehensive tests for _binance_sdk_client — covering all methods.
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest


class TestBinanceSDKAllMethods:
    """Test all methods in _binance_sdk_client.BinanceClient."""

    @pytest.fixture
    def client(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}, clear=False
        ):
            with patch("src._binance_sdk_client.BinanceSpotClient") as mock_cls:
                mock_client = MagicMock()
                mock_cls.return_value = mock_client
                c = BinanceClient()
                c._mock = mock_client
                return c

    def test_validate_symbol_no_allowlist(self, client):
        assert client.validate_symbol("BTCUSDT") is True

    def test_validate_symbol_with_allowlist(self, client):
        with patch.dict(os.environ, {"ALLOWED_SYMBOLS": "BTCUSDT,ETHUSDT"}):
            assert client.validate_symbol("BTCUSDT") is True
            assert client.validate_symbol("XYZUSDT") is False

    def test_get_symbols(self, client):
        client._mock.exchange_info.return_value = {
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
        client._mock.exchange_info.side_effect = Exception("API error")
        assert client.get_symbols() == []

    def test_get_exchange_info_cached(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        info1 = client.get_exchange_info()
        info2 = client.get_exchange_info()
        assert info1 == info2
        assert client._mock.exchange_info.call_count == 1

    def test_get_exchange_info_stale(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._exchange_info_cache = {"symbols": [{"old": True}]}
        client._exchange_info_timestamp = time.time() - 7200  # expired
        info = client.get_exchange_info()
        assert "symbols" in info

    def test_get_klines(self, client):
        client._mock.klines.return_value = [
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
                True,
            ]
        ]
        klines = client.get_klines("BTCUSDT", "1h", limit=1)
        assert len(klines) == 1
        assert klines[0]["close"] == 50500.0
        assert klines[0]["open"] == 50000.0

    def test_get_klines_with_times(self, client):
        client._mock.klines.return_value = []
        klines = client.get_klines("BTCUSDT", "1h", start_time=1000, end_time=2000)
        assert klines == []

    def test_get_24hr_stats_single(self, client):
        client._mock.ticker_24hr.return_value = {
            "symbol": "BTCUSDT",
            "priceChange": "1000",
            "priceChangePercent": "2.0",
            "volume": "100",
            "quoteVolume": "5000000",
            "highPrice": "51000",
            "lowPrice": "49000",
            "lastPrice": "50000",
        }
        stats = client.get_24hr_stats("BTCUSDT")
        assert stats["symbol"] == "BTCUSDT"
        assert stats["last_price"] == 50000.0

    def test_get_24hr_stats_all(self, client):
        client._mock.ticker_24hr.return_value = [
            {
                "symbol": "BTCUSDT",
                "quoteAsset": "USDT",
                "priceChangePercent": "2.0",
                "volume": "100",
                "quoteVolume": "5000000",
                "lastPrice": "50000",
            },
            {
                "symbol": "ETHUSDT",
                "quoteAsset": "USDT",
                "priceChangePercent": "3.0",
                "volume": "200",
                "quoteVolume": "3000000",
                "lastPrice": "3000",
            },
        ]
        stats = client.get_24hr_stats()
        assert len(stats) == 2

    def test_get_24hr_stats_error(self, client):
        client._mock.ticker_24hr.side_effect = Exception("API error")
        assert client.get_24hr_stats("BTCUSDT") == {}

    def test_get_order_book(self, client):
        client._mock.depth.return_value = {
            "bids": [["50000", "1.0"], ["49999", "2.0"]],
            "asks": [["50001", "1.5"], ["50002", "2.5"]],
        }
        book = client.get_order_book("BTCUSDT", limit=2)
        assert len(book["bids"]) == 2
        assert book["bids"][0][0] == 50000.0

    def test_get_order_book_error(self, client):
        client._mock.depth.side_effect = Exception("API error")
        book = client.get_order_book("BTCUSDT")
        assert book == {"bids": [], "asks": []}

    def test_get_account(self, client):
        client._mock.account.return_value = {
            "balances": [
                {"asset": "BTC", "free": "0.1", "locked": "0.05"},
                {"asset": "USDT", "free": "10000", "locked": "0"},
            ]
        }
        account = client.get_account()
        assert len(account["balances"]) == 2

    def test_get_balance(self, client):
        client._mock.account.return_value = {
            "balances": [{"asset": "USDT", "free": "10000", "locked": "5000"}]
        }
        bal = client.get_balance("USDT")
        assert bal == 15000.0

    def test_get_balance_cached(self, client):
        client._balance_cache = {"USDT": (10000.0, time.time())}
        bal = client.get_balance("USDT")
        assert bal == 10000.0
        client._mock.account.assert_not_called()

    def test_get_free_balance(self, client):
        client._mock.account.return_value = {
            "balances": [{"asset": "USDT", "free": "10000", "locked": "5000"}]
        }
        bal = client.get_free_balance("USDT")
        assert bal == 10000.0

    def test_get_price_precision(self, client):
        client._mock.exchange_info.return_value = {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"}
                    ],
                }
            ]
        }
        assert client.get_price_precision("BTCUSDT") == 2

    def test_get_price_precision_default(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        assert client.get_price_precision("UNKNOWN") == 4

    def test_get_ticker_price(self, client):
        client._mock.ticker_price.return_value = {"price": "50000.0"}
        assert client.get_ticker_price("BTCUSDT") == 50000.0

    def test_get_server_time(self, client):
        client._mock.time.return_value = {"serverTime": 1234567890}
        assert client.get_server_time() == 1234567890

    def test_close(self, client):
        client.close()

    def test_place_market_buy(self, client):
        client._mock.new_order.return_value = {"orderId": 12345, "status": "FILLED"}
        result = client.place_market_buy("BTCUSDT", 0.1)
        assert result["orderId"] == 12345

    def test_place_market_sell(self, client):
        client._mock.new_order.return_value = {"orderId": 12346, "status": "FILLED"}
        result = client.place_market_sell("BTCUSDT", 0.1)
        assert result["orderId"] == 12346

    def test_place_limit_buy(self, client):
        client._mock.new_order.return_value = {"orderId": 12347, "status": "NEW"}
        result = client.place_limit_buy("BTCUSDT", 0.1, 50000.0)
        assert result["orderId"] == 12347

    def test_place_limit_sell(self, client):
        client._mock.new_order.return_value = {"orderId": 12348, "status": "NEW"}
        result = client.place_limit_sell("BTCUSDT", 0.1, 55000.0)
        assert result["orderId"] == 12348

    def test_cancel_order(self, client):
        client._mock.cancel_order.return_value = {
            "orderId": 12345,
            "status": "CANCELLED",
        }
        result = client.cancel_order("BTCUSDT", 12345)
        assert result["status"] == "CANCELLED"

    def test_get_open_orders(self, client):
        client._mock.get_open_orders.return_value = [
            {"orderId": 12345, "symbol": "BTCUSDT", "status": "NEW"}
        ]
        orders = client.get_open_orders("BTCUSDT")
        assert len(orders) == 1

    def test_get_order(self, client):
        client._mock.get_order.return_value = {"orderId": 12345, "status": "FILLED"}
        order = client.get_order("BTCUSDT", 12345)
        assert order["status"] == "FILLED"

    def test_format_price(self, client):
        client._get_exchange_info = MagicMock(
            return_value={
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"}
                        ],
                    }
                ]
            }
        )
        result = client.format_price("BTCUSDT", 50000.123)
        assert isinstance(result, str)

    def test_format_quantity(self, client):
        client._get_exchange_info = MagicMock(
            return_value={
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "filters": [
                            {"filterType": "LOT_SIZE", "stepSize": "0.00100000"}
                        ],
                    }
                ]
            }
        )
        result = client.format_quantity("BTCUSDT", 0.123456)
        assert isinstance(result, str)

    def test_get_quantity_precision(self, client):
        client._get_exchange_info = MagicMock(
            return_value={
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "filters": [
                            {"filterType": "LOT_SIZE", "stepSize": "0.00100000"}
                        ],
                    }
                ]
            }
        )
        assert client.get_quantity_precision("BTCUSDT") == 3

    def test_cancel_all_orders(self, client):
        client._mock.get_open_orders.return_value = [{"orderId": 1}, {"orderId": 2}]
        client._mock.cancel_order.return_value = {"status": "CANCELLED"}
        result = client.cancel_all_orders("BTCUSDT")
        assert result is True

    def test_get_my_trades(self, client):
        client._mock.my_trades.return_value = [
            {"id": 1, "price": "50000", "qty": "0.1"}
        ]
        trades = client.get_my_trades("BTCUSDT")
        assert len(trades) == 1
