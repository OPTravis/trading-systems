"""
Complete coverage tests for _binance_sdk_client — covering ALL remaining uncovered lines.
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest


class TestBinanceComplete:
    """Cover ALL remaining uncovered lines in _binance_sdk_client."""

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

    # ── _load_keys paths ────────────────────────────────────────────

    def test_load_keys_from_env(self, client):
        assert client.api_key == "k"
        assert client.api_secret == "s"

    def test_load_keys_from_testnet_env(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ,
            {
                "BINANCE_API_KEY": "",
                "BINANCE_API_SECRET": "",
                "BINANCE_TESTNET_API_KEY": "tk",
                "BINANCE_TESTNET_API_SECRET": "ts",
            },
            clear=False,
        ):
            with patch("src._binance_sdk_client.BinanceSpotClient"):
                c = BinanceClient()
                assert c.api_key == "tk"

    def test_load_keys_from_env_file(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "", "BINANCE_API_SECRET": ""}, clear=False
        ):
            with patch("src._binance_sdk_client.BinanceSpotClient"):
                with patch("src._binance_sdk_client.load_dotenv"):
                    with patch.dict(
                        os.environ,
                        {
                            "BINANCE_API_KEY": "env_key",
                            "BINANCE_API_SECRET": "env_secret",
                        },
                    ):
                        c = BinanceClient()
                        # Should find keys from env after load_dotenv

    # ── get_klines paths ────────────────────────────────────────────

    def test_get_klines_success(self, client):
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

    def test_get_klines_with_times(self, client):
        client._mock.klines.return_value = []
        klines = client.get_klines("BTCUSDT", "1h", start_time=1000, end_time=2000)
        assert klines == []

    def test_get_klines_ssl_error(self, client):
        import ssl

        client._mock.klines.side_effect = ssl.SSLError("SSL error")
        with patch("src._binance_sdk_client.time.sleep"):
            result = client.get_klines("BTCUSDT", "1h", max_retries=2)
            assert result == []

    def test_get_klines_unexpected_error(self, client):
        client._mock.klines.side_effect = Exception("unexpected")
        with patch("src._binance_sdk_client.time.sleep"):
            result = client.get_klines("BTCUSDT", "1h", max_retries=2)
            assert result == []

    # ── get_24hr_stats paths ────────────────────────────────────────

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
        ]
        stats = client.get_24hr_stats()
        assert len(stats) == 1

    def test_get_24hr_stats_error(self, client):
        client._mock.ticker_24hr.side_effect = Exception("API error")
        assert client.get_24hr_stats("BTCUSDT") == {}
        assert client.get_24hr_stats() == []

    def test_get_24hr_stats_ssl(self, client):
        import ssl

        client._mock.ticker_24hr.side_effect = ssl.SSLError("SSL")
        with patch("src._binance_sdk_client.time.sleep"):
            assert client.get_24hr_stats("BTCUSDT") == {}

    # ── get_order_book paths ────────────────────────────────────────

    def test_get_order_book(self, client):
        client._mock.depth.return_value = {
            "bids": [["50000", "1.0"]],
            "asks": [["50001", "1.5"]],
        }
        book = client.get_order_book("BTCUSDT")
        assert book["bids"][0][0] == 50000.0

    def test_get_order_book_error(self, client):
        client._mock.depth.side_effect = Exception("error")
        assert client.get_order_book("BTCUSDT") == {"bids": [], "asks": []}

    # ── get_account paths ───────────────────────────────────────────

    def test_get_account_success(self, client):
        client._mock.account.return_value = {"balances": []}
        assert "balances" in client.get_account()

    def test_get_account_network_error(self, client):
        import requests

        client._mock.account.side_effect = requests.exceptions.RequestException(
            "timeout"
        )
        with patch("src._binance_sdk_client.time.sleep"):
            result = client.get_account()
            assert isinstance(result, dict)

    # ── get_balance paths ───────────────────────────────────────────

    def test_get_balance_cached(self, client):
        client._balance_cache = {"USDT": (10000.0, time.time())}
        assert client.get_balance("USDT") == 10000.0

    def test_get_balance_cache_expired(self, client):
        client._balance_cache = {"USDT": (999.0, time.time() - 60)}
        client._mock.account.return_value = {
            "balances": [{"asset": "USDT", "free": "5000", "locked": "3000"}]
        }
        assert client.get_balance("USDT") == 8000.0

    def test_get_balance_no_cache(self, client):
        client._balance_cache = {}
        client._mock.account.return_value = {
            "balances": [{"asset": "USDT", "free": "5000", "locked": "3000"}]
        }
        assert client.get_balance("USDT") == 8000.0

    def test_get_balance_missing(self, client):
        client._balance_cache = {}
        client._mock.account.return_value = {"balances": []}
        assert client.get_balance("XYZ") == 0.0

    # ── get_free_balance paths ──────────────────────────────────────

    def test_get_free_balance(self, client):
        client._mock.account.return_value = {
            "balances": [{"asset": "USDT", "free": "10000", "locked": "5000"}]
        }
        assert client.get_free_balance("USDT") == 10000.0

    def test_get_free_balance_missing(self, client):
        client._mock.account.return_value = {"balances": []}
        assert client.get_free_balance("XYZ") == 0.0

    # ── place_order paths ───────────────────────────────────────────

    def test_place_order_market(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 123}
        result = client.place_order("BTCUSDT", "BUY", "MARKET", quantity=0.1)
        assert result is not None

    def test_place_order_limit(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 124}
        result = client.place_order(
            "BTCUSDT", "BUY", "LIMIT", quantity=0.1, price=50000.0
        )
        assert result is not None

    def test_place_order_stop_loss_limit(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 125}
        result = client.place_order(
            "BTCUSDT",
            "SELL",
            "STOP_LOSS_LIMIT",
            quantity=0.1,
            price=49000.0,
            stop_price=49500.0,
        )
        assert result is not None

    def test_place_order_invalid_symbol(self, client):
        with patch.dict(os.environ, {"ALLOWED_SYMBOLS": "BTCUSDT"}):
            assert client.place_order("XYZ", "BUY", "MARKET", quantity=0.1) is None

    def test_place_order_no_quantity(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 126}
        result = client.place_order("BTCUSDT", "BUY", "MARKET")
        assert result is not None

    def test_place_order_with_filters(self, client):
        client._mock.exchange_info.return_value = {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {
                            "filterType": "PRICE_FILTER",
                            "tickSize": "0.01",
                            "minPrice": "0.01",
                            "maxPrice": "100000",
                        },
                        {
                            "filterType": "LOT_SIZE",
                            "stepSize": "0.001",
                            "minQty": "0.001",
                            "maxQty": "1000",
                        },
                        {"filterType": "MIN_NOTIONAL", "minNotional": "10"},
                    ],
                }
            ]
        }
        client._mock.new_order.return_value = {"orderId": 127}
        result = client.place_order(
            "BTCUSDT", "BUY", "LIMIT", quantity=0.1, price=50000.0
        )
        assert result is not None

    # ── place_market_buy/sell ───────────────────────────────────────

    def test_place_market_buy(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 1}
        assert client.place_market_buy("BTCUSDT", 0.1) is not None

    def test_place_market_sell(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 2}
        assert client.place_market_sell("BTCUSDT", 0.1) is not None

    def test_place_limit_buy(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 3}
        assert client.place_limit_buy("BTCUSDT", 0.1, 50000.0) is not None

    def test_place_limit_sell(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 4}
        assert client.place_limit_sell("BTCUSDT", 0.1, 55000.0) is not None

    def test_place_stop_loss_market(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 5}
        assert client.place_stop_loss_market("BTCUSDT", 0.1, 49000.0) is not None

    def test_place_stop_loss_limit(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 6}
        assert (
            client.place_stop_loss_limit("BTCUSDT", 0.1, 49000.0, 49500.0) is not None
        )

    # ── place_oco paths ─────────────────────────────────────────────

    def test_place_oco_success(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.order_oco_sell.return_value = {"orderListId": 1}
        result = client.place_oco("BTCUSDT", 0.1, 55000.0, 49000.0)
        assert result is not None

    def test_place_oco_invalid_symbol(self, client):
        with patch.dict(os.environ, {"ALLOWED_SYMBOLS": "BTCUSDT"}):
            assert client.place_oco("XYZ", 0.1, 55000.0, 49000.0) is None

    def test_place_oco_custom_sl_limit(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.order_oco_sell.return_value = {"orderListId": 2}
        result = client.place_oco(
            "BTCUSDT", 0.1, 55000.0, 49000.0, sl_limit_price=48900.0
        )
        assert result is not None

    # ── cancel_order paths ──────────────────────────────────────────

    def test_cancel_order_success(self, client):
        client._mock.cancel_order.return_value = {"orderId": 123, "status": "CANCELLED"}
        result = client.cancel_order("BTCUSDT", 123)
        assert result["status"] == "CANCELLED"

    # ── get_open_orders paths ───────────────────────────────────────

    def test_get_open_orders_success(self, client):
        client._mock.get_open_orders.return_value = [{"orderId": 1}]
        assert len(client.get_open_orders("BTCUSDT")) == 1

    def test_get_open_orders_no_symbol(self, client):
        client._mock.get_open_orders.return_value = [{"orderId": 1}]
        assert len(client.get_open_orders()) == 1

    def test_get_open_orders_network_error(self, client):
        import requests

        client._mock.get_open_orders.side_effect = requests.exceptions.RequestException(
            "timeout"
        )
        with patch("src._binance_sdk_client.time.sleep"):
            result = client.get_open_orders()
            assert isinstance(result, list)

    # ── cancel_all_orders paths ─────────────────────────────────────

    def test_cancel_all_orders_success(self, client):
        client._mock.cancel_open_orders.return_value = None
        assert client.cancel_all_orders("BTCUSDT") is True

    def test_cancel_all_orders_network_error(self, client):
        import requests

        client._mock.cancel_open_orders.side_effect = (
            requests.exceptions.RequestException("timeout")
        )
        with patch("src._binance_sdk_client.time.sleep"):
            result = client.cancel_all_orders("BTCUSDT")
            assert isinstance(result, bool)

    # ── get_order paths ─────────────────────────────────────────────

    def test_get_order_success(self, client):
        client._mock.get_order.return_value = {"orderId": 123, "status": "FILLED"}
        assert client.get_order("BTCUSDT", 123)["status"] == "FILLED"

    # ── get_trades / get_my_trades ──────────────────────────────────

    def test_get_my_trades(self, client):
        client._mock.my_trades.return_value = [{"id": 1}]
        assert len(client.get_my_trades("BTCUSDT")) == 1

    def test_get_my_trades_error(self, client):
        client._mock.my_trades.side_effect = Exception("error")
        assert client.get_my_trades("BTCUSDT") == []

    # ── format_price / format_quantity ───────────────────────────────

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
        assert isinstance(client.format_price("BTCUSDT", 50000.123), str)

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
        assert isinstance(client.format_quantity("BTCUSDT", 0.123456), str)

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

    # ── get_server_time / close ─────────────────────────────────────

    def test_get_server_time(self, client):
        client._mock.time.return_value = {"serverTime": 1234567890}
        assert client.get_server_time() == 1234567890

    def test_close(self, client):
        client.close()


# ── ccxt_client complete ──────────────────────────────────────────────


class TestCCXTComplete:
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

    def test_validate_symbol(self, client):
        assert client.validate_symbol("BTCUSDT") is True

    def test_close(self, client):
        client.close()
