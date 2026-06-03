"""
Comprehensive tests for _binance_sdk_client and ccxt_client.
Tests all static/pure methods and mocks API-dependent methods.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

# ── _binance_sdk_client static/pure methods ───────────────────────────


class TestBinanceSDKStatic:
    def test_sanitize_error_with_key(self):
        from src._binance_sdk_client import _sanitize_error

        result = _sanitize_error("Error api_key=ABCD1234567890 failed")
        assert "ABCD1234567890" not in result

    def test_sanitize_error_no_key(self):
        from src._binance_sdk_client import _sanitize_error

        result = _sanitize_error("Connection timeout")
        assert result == "Connection timeout"


class TestBinanceSDKInit:
    def test_no_keys_raises(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "", "BINANCE_API_SECRET": ""}, clear=False
        ):
            with pytest.raises(ValueError, match="Binance API key"):
                BinanceClient()

    def test_no_secret_raises(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ,
            {"BINANCE_API_KEY": "test", "BINANCE_API_SECRET": ""},
            clear=False,
        ):
            with pytest.raises(ValueError, match="Binance API secret"):
                BinanceClient()

    def test_init_success(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}, clear=False
        ):
            with patch("src._binance_sdk_client.BinanceSpotClient"):
                client = BinanceClient()
                assert client.api_key == "k"
                assert client.testnet is False

    def test_init_testnet(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}, clear=False
        ):
            with patch("src._binance_sdk_client.BinanceSpotClient"):
                client = BinanceClient(testnet=True)
                assert "testnet" in client.base_url


class TestBinanceSDKMethods:
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
        symbols = client.get_symbols()
        assert symbols == []

    def test_get_exchange_info_cached(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        info1 = client.get_exchange_info()
        info2 = client.get_exchange_info()
        assert info1 == info2
        assert client._mock.exchange_info.call_count == 1

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

    def test_get_ticker_price(self, client):
        client._mock.ticker_price.return_value = {"price": "50000.0"}
        price = client.get_ticker_price("BTCUSDT")
        assert price == 50000.0

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
        prec = client.get_price_precision("BTCUSDT")
        assert prec == 2

    def test_get_price_precision_default(self, client):
        client._mock.exchange_info.return_value = {"symbols": []}
        prec = client.get_price_precision("UNKNOWN")
        assert prec == 4

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
        prec = client.get_quantity_precision("BTCUSDT")
        assert prec == 3

    def test_get_server_time(self, client):
        client._mock.time.return_value = {"serverTime": 1234567890}
        t = client.get_server_time()
        assert t == 1234567890

    def test_close(self, client):
        client.close()


# ── ccxt_client static/pure methods ───────────────────────────────────


class TestCCXTStatic:
    def test_sanitize_error(self):
        from src.ccxt_client import _sanitize_error

        result = _sanitize_error("Error api_key=ABCD1234567890 failed")
        assert "ABCD1234567890" not in result


class TestCCXTInit:
    def test_no_keys_raises(self):
        from src.ccxt_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "", "BINANCE_API_SECRET": ""}, clear=False
        ):
            with pytest.raises(ValueError, match="Binance API key"):
                BinanceClient()

    def test_init_success(self):
        from src.ccxt_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}, clear=False
        ):
            with patch("ccxt.binance") as mock_binance:
                mock_binance.return_value = MagicMock()
                client = BinanceClient()
                assert client.api_key == "k"

    def test_init_testnet(self):
        from src.ccxt_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}, clear=False
        ):
            with patch("ccxt.binance") as mock_binance:
                mock_exchange = MagicMock()
                mock_binance.return_value = mock_exchange
                client = BinanceClient(testnet=True)
                mock_exchange.set_sandbox_mode.assert_called_with(True)


class TestCCXTMethods:
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
