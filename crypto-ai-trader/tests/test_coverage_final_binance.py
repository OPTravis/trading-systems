"""
Final coverage tests for _binance_sdk_client — targeting specific uncovered lines.
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest


class TestBinanceSDKFinal:
    """Target specific uncovered lines in _binance_sdk_client."""

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

    # ── get_klines error paths ──────────────────────────────────────

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

    # ── get_24hr_stats error paths ──────────────────────────────────

    def test_get_24hr_stats_ssl_error(self, client):
        import ssl

        client._mock.ticker_24hr.side_effect = ssl.SSLError("SSL error")
        with patch("src._binance_sdk_client.time.sleep"):
            result = client.get_24hr_stats("BTCUSDT")
            assert result == {}

    def test_get_24hr_stats_unexpected(self, client):
        client._mock.ticker_24hr.side_effect = Exception("unexpected")
        result = client.get_24hr_stats("BTCUSDT")
        assert result == {}

    def test_get_24hr_stats_all_ssl(self, client):
        import ssl

        client._mock.ticker_24hr.side_effect = ssl.SSLError("SSL error")
        with patch("src._binance_sdk_client.time.sleep"):
            result = client.get_24hr_stats()
            assert result == []

    # ── get_account error paths ─────────────────────────────────────

    def test_get_account_network_error(self, client):
        import requests

        client._mock.account.side_effect = requests.exceptions.RequestException(
            "timeout"
        )
        with patch("src._binance_sdk_client.time.sleep"):
            result = client.get_account()
            assert isinstance(result, dict)

    # ── get_balance cache ───────────────────────────────────────────

    def test_get_balance_cache_hit(self, client):
        client._balance_cache = {"USDT": (10000.0, time.time())}
        result = client.get_balance("USDT")
        assert result == 10000.0
        client._mock.account.assert_not_called()

    def test_get_balance_cache_miss(self, client):
        client._balance_cache = {}
        client._mock.account.return_value = {
            "balances": [{"asset": "USDT", "free": "5000", "locked": "3000"}]
        }
        result = client.get_balance("USDT")
        assert result == 8000.0

    def test_get_balance_cache_expired(self, client):
        client._balance_cache = {"USDT": (999.0, time.time() - 60)}
        client._mock.account.return_value = {
            "balances": [{"asset": "USDT", "free": "5000", "locked": "3000"}]
        }
        result = client.get_balance("USDT")
        assert result == 8000.0

    def test_get_balance_missing_asset(self, client):
        client._balance_cache = {}
        client._mock.account.return_value = {"balances": []}
        result = client.get_balance("XYZ")
        assert result == 0.0

    # ── get_free_balance ────────────────────────────────────────────

    def test_get_free_balance(self, client):
        client._mock.account.return_value = {
            "balances": [{"asset": "USDT", "free": "10000", "locked": "5000"}]
        }
        result = client.get_free_balance("USDT")
        assert result == 10000.0

    def test_get_free_balance_missing(self, client):
        client._mock.account.return_value = {"balances": []}
        result = client.get_free_balance("XYZ")
        assert result == 0.0

    # ── get_position ────────────────────────────────────────────────

    # ── place_order ─────────────────────────────────────────────────

    def test_place_order_invalid_symbol(self, client):
        with patch.dict(os.environ, {"ALLOWED_SYMBOLS": "BTCUSDT"}):
            result = client.place_order("XYZUSDT", "BUY", "MARKET", quantity=0.1)
            assert result is None

    def test_place_order_network_error(self, client):
        import requests

        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.side_effect = requests.exceptions.RequestException(
            "timeout"
        )
        with patch("src._binance_sdk_client.time.sleep"):
            result = client.place_order(
                "BTCUSDT", "BUY", "MARKET", quantity=0.1, retry=1
            )
            assert result is None

    # ── place_market_buy/sell ───────────────────────────────────────

    def test_place_market_buy(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 123}
        result = client.place_market_buy("BTCUSDT", 0.1)
        assert result["orderId"] == 123

    def test_place_market_sell(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 124}
        result = client.place_market_sell("BTCUSDT", 0.1)
        assert result["orderId"] == 124

    def test_place_limit_buy(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 125}
        result = client.place_limit_buy("BTCUSDT", 0.1, 50000.0)
        assert result["orderId"] == 125

    def test_place_limit_sell(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 126}
        result = client.place_limit_sell("BTCUSDT", 0.1, 55000.0)
        assert result["orderId"] == 126

    # ── cancel_order ────────────────────────────────────────────────

    def test_cancel_order(self, client):
        client._mock.cancel_order.return_value = {"orderId": 123, "status": "CANCELLED"}
        result = client.cancel_order("BTCUSDT", 123)
        assert result["status"] == "CANCELLED"

    # ── get_open_orders ─────────────────────────────────────────────

    def test_get_open_orders(self, client):
        client._mock.get_open_orders.return_value = [
            {"orderId": 1, "symbol": "BTCUSDT", "status": "NEW"}
        ]
        orders = client.get_open_orders("BTCUSDT")
        assert len(orders) == 1

    # ── cancel_all_orders ───────────────────────────────────────────

    def test_cancel_all_orders(self, client):
        client._mock.get_open_orders.return_value = [{"orderId": 1}, {"orderId": 2}]
        client._mock.cancel_order.return_value = {"status": "CANCELLED"}
        result = client.cancel_all_orders("BTCUSDT")
        assert result is True

    def test_cancel_all_orders_empty(self, client):
        client._mock.get_open_orders.return_value = []
        result = client.cancel_all_orders("BTCUSDT")
        assert result is True

    # ── get_order ───────────────────────────────────────────────────

    def test_get_order(self, client):
        client._mock.get_order.return_value = {"orderId": 123, "status": "FILLED"}
        order = client.get_order("BTCUSDT", 123)
        assert order["status"] == "FILLED"

    # ── get_trades ──────────────────────────────────────────────────

    # ── get_my_trades ───────────────────────────────────────────────

    def test_get_my_trades(self, client):
        client._mock.my_trades.return_value = [{"id": 1}]
        trades = client.get_my_trades("BTCUSDT")
        assert len(trades) == 1

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

    # ── get_server_time ─────────────────────────────────────────────

    def test_get_server_time(self, client):
        client._mock.time.return_value = {"serverTime": 1234567890}
        assert client.get_server_time() == 1234567890

    # ── close ───────────────────────────────────────────────────────

    def test_close(self, client):
        client.close()
