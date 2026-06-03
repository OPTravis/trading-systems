"""
Comprehensive tests for ccxt_client — covering ALL uncovered code paths.
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest


class TestCCXTClientAllPaths:
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

    # ── Init paths ──────────────────────────────────────────────────

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

    # ── validate_symbol ─────────────────────────────────────────────

    def test_validate_symbol_no_allowlist(self, client):
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

    # ── get_symbols ─────────────────────────────────────────────────

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

    # ── get_exchange_info ────────────────────────────────────────────

    def test_get_exchange_info_cached(self, client):
        client._mock.publicGetExchangeInfo.return_value = {"symbols": []}
        info1 = client.get_exchange_info()
        info2 = client.get_exchange_info()
        assert info1 == info2

    def test_get_exchange_info_stale(self, client):
        client._exchange_info_cache = {"symbols": []}
        client._exchange_info_timestamp = time.time() - 7200
        client._mock.publicGetExchangeInfo.return_value = {"symbols": [{"new": True}]}
        info = client.get_exchange_info()
        assert "symbols" in info

    def test_get_exchange_info_error(self, client):
        client._exchange_info_cache = None
        client._mock.publicGetExchangeInfo.side_effect = Exception("error")
        result = client.get_exchange_info()
        assert result == {}

    # ── get_klines ──────────────────────────────────────────────────

    def test_get_klines_success(self, client):
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

    def test_get_klines_empty(self, client):
        client._mock.publicGetKlines.return_value = []
        klines = client.get_klines("BTCUSDT", "1h")
        assert klines == []

    def test_get_klines_rate_limit(self, client):
        import ccxt

        client._mock.publicGetKlines.side_effect = ccxt.RateLimitExceeded("rate limit")
        with patch("src.ccxt_client.time.sleep"):
            result = client.get_klines("BTCUSDT", "1h", max_retries=2)
            assert result == []

    def test_get_klines_exchange_error(self, client):
        import ccxt

        client._mock.publicGetKlines.side_effect = ccxt.ExchangeError("error")
        result = client.get_klines("BTCUSDT", "1h", max_retries=1)
        assert result == []

    def test_get_klines_unexpected_error(self, client):
        client._mock.publicGetKlines.side_effect = Exception("unexpected")
        with patch("src.ccxt_client.time.sleep"):
            result = client.get_klines("BTCUSDT", "1h", max_retries=2)
            assert result == []

    # ── get_24hr_stats ──────────────────────────────────────────────

    def test_get_24hr_stats_network_error(self, client):
        import ccxt

        client._mock.publicGetTicker24hr.side_effect = ccxt.NetworkError("network")
        with patch("src.ccxt_client.time.sleep"):
            result = client.get_24hr_stats("BTCUSDT")
            assert isinstance(result, dict)

    def test_get_24hr_stats_rate_limit(self, client):
        import ccxt

        client._mock.publicGetTicker24hr.side_effect = ccxt.RateLimitExceeded("rate")
        with patch("src.ccxt_client.time.sleep"):
            result = client.get_24hr_stats("BTCUSDT")
            assert isinstance(result, dict)

    # ── get_order_book ──────────────────────────────────────────────

    def test_get_order_book(self, client):
        client._mock.fetch_order_book.return_value = {
            "bids": [[50000, 1.0]],
            "asks": [[50001, 1.5]],
        }
        book = client.get_order_book("BTCUSDT")
        assert book["bids"][0][0] == 50000.0

    def test_get_order_book_error(self, client):
        client._mock.fetch_order_book.side_effect = Exception("error")
        assert client.get_order_book("BTCUSDT") == {"bids": [], "asks": []}

    # ── get_account ─────────────────────────────────────────────────

    # ── get_balance ─────────────────────────────────────────────────

    def test_get_balance_cached(self, client):
        client._balance_cache = {"USDT": (10000.0, time.time())}
        assert client.get_balance("USDT") == 10000.0

    def test_get_balance_missing(self, client):
        client._balance_cache = {}
        client._mock.privateGetAccount.return_value = {"balances": []}
        assert client.get_balance("XYZ") == 0.0

    # ── get_free_balance ────────────────────────────────────────────

    def test_get_free_balance_missing(self, client):
        client._mock.privateGetAccount.return_value = {"balances": []}
        assert client.get_free_balance("XYZ") == 0.0

    # ── get_position ────────────────────────────────────────────────

    def test_get_position_no_balance(self, client):
        client._mock.privateGetAccount.return_value = {"balances": []}
        pos = client.get_position("BTCUSDT")
        assert pos["total"] == 0

    # ── place_order ─────────────────────────────────────────────────

    def test_place_order_success(self, client):
        client._mock.create_order.return_value = {"orderId": 123, "status": "FILLED"}
        result = client.place_order("BTCUSDT", "BUY", "MARKET", quantity=0.1)
        assert result is not None

    def test_place_order_limit(self, client):
        client._mock.create_order.return_value = {"orderId": 124}
        result = client.place_order(
            "BTCUSDT", "BUY", "LIMIT", quantity=0.1, price=50000.0
        )
        assert result is not None

    def test_place_order_stop_limit(self, client):
        client._mock.create_order.return_value = {"orderId": 125}
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
        client._mock.create_order.return_value = {"orderId": 126}
        result = client.place_order("BTCUSDT", "BUY", "MARKET")
        assert result is not None

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
        client._mock.create_order.return_value = {"orderId": 127}
        result = client.place_order(
            "BTCUSDT", "BUY", "LIMIT", quantity=0.1, price=50000.0
        )
        assert result is not None

    # ── place_market_buy/sell ───────────────────────────────────────

    def test_place_market_buy(self, client):
        client._mock.create_order.return_value = {"orderId": 1}
        assert client.place_market_buy("BTCUSDT", 0.1) is not None

    def test_place_market_sell(self, client):
        client._mock.create_order.return_value = {"orderId": 2}
        assert client.place_market_sell("BTCUSDT", 0.1) is not None

    def test_place_limit_buy(self, client):
        client._mock.create_order.return_value = {"orderId": 3}
        assert client.place_limit_buy("BTCUSDT", 0.1, 50000.0) is not None

    def test_place_limit_sell(self, client):
        client._mock.create_order.return_value = {"orderId": 4}
        assert client.place_limit_sell("BTCUSDT", 0.1, 55000.0) is not None

    def test_place_stop_loss_market(self, client):
        client._mock.create_order.return_value = {"orderId": 5}
        assert client.place_stop_loss_market("BTCUSDT", 0.1, 49000.0) is not None

    def test_place_stop_loss_limit(self, client):
        client._mock.create_order.return_value = {"orderId": 6}
        assert (
            client.place_stop_loss_limit("BTCUSDT", 0.1, 49000.0, 49500.0) is not None
        )

    # ── place_oco ───────────────────────────────────────────────────

    def test_place_oco_success(self, client):
        client._mock.create_oco_order.return_value = {"orderListId": 1}
        assert client.place_oco("BTCUSDT", 0.1, 55000.0, 49000.0) is not None

    def test_place_oco_invalid_symbol(self, client):
        with patch.dict(os.environ, {"ALLOWED_SYMBOLS": "BTCUSDT"}):
            assert client.place_oco("XYZ", 0.1, 55000.0, 49000.0) is None

    def test_place_oco_custom_sl_limit(self, client):
        client._mock.create_oco_order.return_value = {"orderListId": 2}
        assert (
            client.place_oco("BTCUSDT", 0.1, 55000.0, 49000.0, sl_limit_price=48900.0)
            is not None
        )

    # ── cancel_order ────────────────────────────────────────────────

    def test_cancel_order_success(self, client):
        client._mock.cancel_order.return_value = {"orderId": 123, "status": "CANCELLED"}
        result = client.cancel_order("BTCUSDT", 123)
        assert result["status"] == "CANCELLED"

    def test_cancel_order_error(self, client):
        client._mock.cancel_order.side_effect = Exception("error")
        assert client.cancel_order("BTCUSDT", 999) is None

    # ── get_open_orders ─────────────────────────────────────────────

    def test_get_open_orders_success(self, client):
        client._mock.fetch_open_orders.return_value = [{"orderId": 1}]
        assert len(client.get_open_orders("BTCUSDT")) == 1

    def test_get_open_orders_no_symbol(self, client):
        client._mock.fetch_open_orders.return_value = [{"orderId": 1}]
        assert len(client.get_open_orders()) == 1

    def test_get_open_orders_error(self, client):
        client._mock.fetch_open_orders.side_effect = Exception("error")
        assert client.get_open_orders() == []

    # ── cancel_all_orders ───────────────────────────────────────────

    def test_cancel_all_orders_success(self, client):
        client._mock.cancel_all_orders.return_value = None
        assert client.cancel_all_orders("BTCUSDT") is True

    # ── get_order ───────────────────────────────────────────────────

    def test_get_order_success(self, client):
        client._mock.fetch_order.return_value = {"orderId": 123, "status": "FILLED"}
        assert client.get_order("BTCUSDT", 123)["status"] == "FILLED"

    def test_get_order_error(self, client):
        client._mock.fetch_order.side_effect = Exception("error")
        assert client.get_order("BTCUSDT", 999) is None

    # ── get_trades ──────────────────────────────────────────────────

    def test_get_trades_success(self, client):
        client._mock.fetch_trades.return_value = [{"id": 1, "price": "50000"}]
        trades = client.get_trades("BTCUSDT")
        assert len(trades) == 1

    def test_get_trades_error(self, client):
        client._mock.fetch_trades.side_effect = Exception("error")
        assert client.get_trades("BTCUSDT") == []

    # ── get_ticker_price ────────────────────────────────────────────

    def test_get_ticker_price(self, client):
        client._mock.fetch_ticker.return_value = {"last": 50000.0}
        assert client.get_ticker_price("BTCUSDT") == 50000.0

    # ── get_server_time ─────────────────────────────────────────────

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

    # ── close ───────────────────────────────────────────────────────

    def test_close(self, client):
        client.close()


# ── _binance_sdk_client error paths ───────────────────────────────────


class TestBinanceSDKErrorPaths:
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

    def test_get_24hr_stats_success(self, client):
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

    def test_get_order_book(self, client):
        client._mock.depth.return_value = {
            "bids": [["50000", "1.0"]],
            "asks": [["50001", "1.5"]],
        }
        book = client.get_order_book("BTCUSDT")
        assert book["bids"][0][0] == 50000.0

    def test_get_balance(self, client):
        client._balance_cache = {}
        client._mock.account.return_value = {
            "balances": [{"asset": "USDT", "free": "5000", "locked": "3000"}]
        }
        assert client.get_balance("USDT") == 8000.0

    def test_place_order_success(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 123}
        assert client.place_order("BTCUSDT", "BUY", "MARKET", quantity=0.1) is not None

    def test_place_market_buy(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        client._mock.new_order.return_value = {"orderId": 1}
        assert client.place_market_buy("BTCUSDT", 0.1) is not None

    def test_cancel_order_success(self, client):
        client._mock.cancel_order.return_value = {"orderId": 123, "status": "CANCELLED"}
        assert client.cancel_order("BTCUSDT", 123)["status"] == "CANCELLED"

    def test_get_open_orders(self, client):
        client._mock.get_open_orders.return_value = [{"orderId": 1}]
        assert len(client.get_open_orders("BTCUSDT")) == 1

    def test_cancel_all_orders(self, client):
        client._mock.cancel_open_orders.return_value = None
        assert client.cancel_all_orders("BTCUSDT") is True

    def test_get_order(self, client):
        client._mock.get_order.return_value = {"orderId": 123, "status": "FILLED"}
        assert client.get_order("BTCUSDT", 123)["status"] == "FILLED"

    def test_get_my_trades(self, client):
        client._mock.my_trades.return_value = [{"id": 1}]
        assert len(client.get_my_trades("BTCUSDT")) == 1

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

    def test_close(self, client):
        client.close()
