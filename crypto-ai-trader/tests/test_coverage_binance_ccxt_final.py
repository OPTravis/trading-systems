"""
Final targeted tests for _binance_sdk_client and ccxt_client uncovered lines.
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest


class TestCCXTClientPaths:
    """Cover specific uncovered paths in ccxt_client."""

    @pytest.fixture
    def client(self):
        from src.ccxt_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
        ):
            with patch("ccxt.binance") as mock_binance:
                mock_exchange = MagicMock()
                mock_binance.return_value = mock_exchange
                c = BinanceClient()
                c._mock = mock_exchange
                return c

    # ── get_price_precision ──────────────────────────────────────────

    def test_get_price_precision_from_markets(self, client):
        client._mock.markets = {"BTCUSDT": {"price": {"precision": 2}}}
        assert client.get_price_precision("BTCUSDT") == 2

    def test_get_price_precision_from_exchange_info(self, client):
        client._mock.markets = {}
        client._mock.publicGetExchangeInfo.return_value = {
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

    def test_get_price_precision_error(self, client):
        client._mock.markets = {}
        client._mock.publicGetExchangeInfo.side_effect = Exception("error")
        assert client.get_price_precision("UNKNOWN") == 4

    # ── get_quantity_precision ────────────────────────────────────────

    def test_get_quantity_precision_from_markets(self, client):
        client._mock.markets = {"BTCUSDT": {"amount": {"precision": 3}}}
        assert client.get_quantity_precision("BTCUSDT") == 3

    def test_get_quantity_precision_from_exchange_info(self, client):
        client._mock.markets = {}
        client._mock.publicGetExchangeInfo.return_value = {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.00100000"}],
                }
            ]
        }
        assert client.get_quantity_precision("BTCUSDT") == 3

    def test_get_quantity_precision_error(self, client):
        client._mock.markets = {}
        client._mock.publicGetExchangeInfo.side_effect = Exception("error")
        assert client.get_quantity_precision("UNKNOWN") == 4

    # ── _get_precision_from_step ─────────────────────────────────────

    def test_get_precision_from_step(self):
        from src.ccxt_client import BinanceClient

        assert BinanceClient._get_precision_from_step("0.00100000") == 3
        assert BinanceClient._get_precision_from_step("0.01") == 2
        assert BinanceClient._get_precision_from_step("1.0") == 0

    # ── _floor_to_step ───────────────────────────────────────────────

    def test_floor_to_step(self):
        from src.ccxt_client import BinanceClient

        assert BinanceClient._floor_to_step(1.23456, "0.01") == pytest.approx(1.23)
        assert BinanceClient._floor_to_step(10.5, "1.0") == pytest.approx(10.0)

    def test_floor_to_step_zero(self):
        from src.ccxt_client import BinanceClient

        assert BinanceClient._floor_to_step(1.0, "0") == 1.0

    # ── place_order with filters ─────────────────────────────────────

    def test_place_order_with_filters(self, client):
        client._mock.publicGetExchangeInfo.return_value = {
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
        client._mock.create_order.return_value = {"orderId": 123}
        result = client.place_order(
            "BTCUSDT", "BUY", "LIMIT", quantity=0.1, price=50000.0
        )
        assert result is not None

    def test_place_order_no_filters(self, client):
        client._mock.publicGetExchangeInfo.return_value = {"symbols": []}
        client._mock.create_order.return_value = {"orderId": 124}
        result = client.place_order("BTCUSDT", "BUY", "MARKET", quantity=0.1)
        assert result is not None

    def test_place_order_invalid_symbol(self, client):
        with patch.dict(os.environ, {"ALLOWED_SYMBOLS": "BTCUSDT"}):
            assert client.place_order("XYZ", "BUY", "MARKET", quantity=0.1) is None

    # ── place_oco ────────────────────────────────────────────────────

    def test_place_oco_success(self, client):
        client._mock.create_oco_order.return_value = {"orderListId": 1}
        assert client.place_oco("BTCUSDT", 0.1, 55000.0, 49000.0) is not None

    def test_place_oco_invalid_symbol(self, client):
        with patch.dict(os.environ, {"ALLOWED_SYMBOLS": "BTCUSDT"}):
            assert client.place_oco("XYZ", 0.1, 55000.0, 49000.0) is None


class TestBinanceSDKClientPaths:
    """Cover specific uncovered paths in _binance_sdk_client."""

    @pytest.fixture
    def client(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
        ):
            with patch("src._binance_sdk_client.BinanceSpotClient") as mock_cls:
                mock_client = MagicMock()
                mock_cls.return_value = mock_client
                c = BinanceClient()
                c._mock = mock_client
                return c

    # ── _load_keys paths ─────────────────────────────────────────────

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

    # ── get_klines with filters ──────────────────────────────────────

    def test_get_klines_with_start_end(self, client):
        client._mock.klines.return_value = []
        assert client.get_klines("BTCUSDT", "1h", start_time=1000, end_time=2000) == []

    def test_get_klines_rate_limit(self, client):
        from binance.error import ClientError

        client._mock.klines.side_effect = ClientError(429, -1003, "rate limit", {})
        with patch("src._binance_sdk_client.time.sleep"):
            assert client.get_klines("BTCUSDT", "1h", max_retries=2) == []

    # ── get_24hr_stats paths ─────────────────────────────────────────

    def test_get_24hr_single(self, client):
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

    def test_get_24hr_all(self, client):
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
        assert len(client.get_24hr_stats()) == 1

    # ── get_account paths ────────────────────────────────────────────

    def test_get_account_success(self, client):
        client._mock.account.return_value = {"balances": []}
        assert "balances" in client.get_account()

    def test_get_account_error(self, client):
        from binance.error import ClientError

        client._mock.account.side_effect = ClientError(400, -1003, "error", {})
        assert client.get_account() == {}

    # ── get_balance paths ────────────────────────────────────────────

    def test_get_balance_cached(self, client):
        client._balance_cache = {"USDT": (10000.0, time.time())}
        assert client.get_balance("USDT") == 10000.0

    def test_get_balance_no_cache(self, client):
        client._balance_cache = {}
        client._mock.account.return_value = {
            "balances": [{"asset": "USDT", "free": "5000", "locked": "3000"}]
        }
        assert client.get_balance("USDT") == 8000.0

    # ── place_order paths ────────────────────────────────────────────

    def test_place_order_success(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 123}
        assert client.place_order("BTCUSDT", "BUY", "MARKET", quantity=0.1) is not None

    def test_place_order_limit(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 124}
        assert (
            client.place_order("BTCUSDT", "BUY", "LIMIT", quantity=0.1, price=50000.0)
            is not None
        )

    def test_place_order_stop(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 125}
        assert (
            client.place_order(
                "BTCUSDT",
                "SELL",
                "STOP_LOSS_LIMIT",
                quantity=0.1,
                price=49000.0,
                stop_price=49500.0,
            )
            is not None
        )

    def test_place_order_invalid_symbol(self, client):
        with patch.dict(os.environ, {"ALLOWED_SYMBOLS": "BTCUSDT"}):
            assert client.place_order("XYZ", "BUY", "MARKET", quantity=0.1) is None

    # ── place_market_buy/sell ────────────────────────────────────────

    def test_place_market_buy(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 1}
        assert client.place_market_buy("BTCUSDT", 0.1) is not None

    def test_place_market_sell(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 2}
        assert client.place_market_sell("BTCUSDT", 0.1) is not None

    # ── place_oco ────────────────────────────────────────────────────

    def test_place_oco(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.order_oco_sell.return_value = {"orderListId": 1}
        assert client.place_oco("BTCUSDT", 0.1, 55000.0, 49000.0) is not None

    def test_place_oco_invalid_symbol(self, client):
        with patch.dict(os.environ, {"ALLOWED_SYMBOLS": "BTCUSDT"}):
            assert client.place_oco("XYZ", 0.1, 55000.0, 49000.0) is None

    # ── cancel_order ─────────────────────────────────────────────────

    def test_cancel_order(self, client):
        client._mock.cancel_order.return_value = {"orderId": 123, "status": "CANCELLED"}
        assert client.cancel_order("BTCUSDT", 123)["status"] == "CANCELLED"

    # ── get_open_orders ──────────────────────────────────────────────

    def test_get_open_orders(self, client):
        client._mock.get_open_orders.return_value = [{"orderId": 1}]
        assert len(client.get_open_orders("BTCUSDT")) == 1

    # ── cancel_all_orders ────────────────────────────────────────────

    def test_cancel_all_orders(self, client):
        client._mock.cancel_open_orders.return_value = None
        assert client.cancel_all_orders("BTCUSDT") is True

    # ── get_order ────────────────────────────────────────────────────

    def test_get_order(self, client):
        client._mock.get_order.return_value = {"orderId": 123, "status": "FILLED"}
        assert client.get_order("BTCUSDT", 123)["status"] == "FILLED"

    # ── get_my_trades ────────────────────────────────────────────────

    def test_get_my_trades(self, client):
        client._mock.my_trades.return_value = [{"id": 1}]
        assert len(client.get_my_trades("BTCUSDT")) == 1

    # ── format_price / format_quantity ────────────────────────────────

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

    # ── get_server_time / close ──────────────────────────────────────

    def test_get_server_time(self, client):
        client._mock.time.return_value = {"serverTime": 1234567890}
        assert client.get_server_time() == 1234567890

    def test_close(self, client):
        client.close()
