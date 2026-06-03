"""
Tests for _binance_sdk_client and ccxt_client — targeting the 534+429 missed lines.
Tests internal logic, helpers, and data transformations without live API calls.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

# ── _binance_sdk_client helpers ───────────────────────────────────────


class TestBinanceSDKHelpers:
    def test_sanitize_error(self):
        from src._binance_sdk_client import _sanitize_error

        msg = "Error api_key=ABCD1234567890 failed"
        result = _sanitize_error(msg)
        assert "ABCD1234567890" not in result
        assert "REDACTED" in result

    def test_sanitize_error_no_secret(self):
        from src._binance_sdk_client import _sanitize_error

        msg = "Connection timeout"
        result = _sanitize_error(msg)
        assert result == "Connection timeout"

    def test_parse_retry_after_normal(self):
        from src._binance_sdk_client import _parse_retry_after

        error = MagicMock()
        error.header = {"Retry-After": "30"}
        result = _parse_retry_after(error, default_wait=10)
        assert result == 30

    def test_parse_retry_after_capped(self):
        from src._binance_sdk_client import _parse_retry_after

        error = MagicMock()
        error.header = {"Retry-After": "120"}
        result = _parse_retry_after(error, default_wait=10)
        assert result == 60

    def test_parse_retry_after_non_numeric(self):
        from src._binance_sdk_client import _parse_retry_after

        error = MagicMock()
        error.header = {"Retry-After": "abc"}
        result = _parse_retry_after(error, default_wait=10)
        assert result == 10

    def test_verify_ssl_env(self):
        from src._binance_sdk_client import VERIFY_SSL

        assert isinstance(VERIFY_SSL, bool)

    def test_sensitive_pattern(self):
        from src._binance_sdk_client import _SENSITIVE_PATTERN

        assert _SENSITIVE_PATTERN.search("api_key=secret12345678") is not None


class TestBinanceSDKClient:
    def test_init_no_keys(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(os.environ, {"BINANCE_API_KEY": "", "BINANCE_API_SECRET": ""}):
            with pytest.raises(ValueError, match="Binance API key"):
                BinanceClient()

    def test_init_with_keys(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ,
            {"BINANCE_API_KEY": "test_key", "BINANCE_API_SECRET": "test_secret"},
        ):
            with patch("src._binance_sdk_client.BinanceSpotClient"):
                client = BinanceClient()
                assert client.api_key == "test_key"
                assert client.testnet is False

    def test_init_testnet(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ,
            {"BINANCE_API_KEY": "test_key", "BINANCE_API_SECRET": "test_secret"},
        ):
            with patch("src._binance_sdk_client.BinanceSpotClient"):
                client = BinanceClient(testnet=True)
                assert client.testnet is True
                assert "testnet" in client.base_url

    def test_validate_symbol_no_allowlist(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ,
            {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s", "ALLOWED_SYMBOLS": ""},
        ):
            with patch("src._binance_sdk_client.BinanceSpotClient"):
                client = BinanceClient()
                assert client.validate_symbol("BTCUSDT") is True

    def test_validate_symbol_with_allowlist(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ,
            {
                "BINANCE_API_KEY": "k",
                "BINANCE_API_SECRET": "s",
                "ALLOWED_SYMBOLS": "BTCUSDT,ETHUSDT",
            },
        ):
            with patch("src._binance_sdk_client.BinanceSpotClient"):
                client = BinanceClient()
                assert client.validate_symbol("BTCUSDT") is True
                assert client.validate_symbol("XYZUSDT") is False

    def test_get_symbols(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
        ):
            with patch("src._binance_sdk_client.BinanceSpotClient") as mock_cls:
                mock_client = MagicMock()
                mock_client.exchange_info.return_value = {
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "quoteAsset": "USDT",
                            "status": "TRADING",
                        },
                        {
                            "symbol": "ETHUSDT",
                            "quoteAsset": "USDT",
                            "status": "TRADING",
                        },
                        {
                            "symbol": "BTCBUSD",
                            "quoteAsset": "BUSD",
                            "status": "TRADING",
                        },
                    ]
                }
                mock_cls.return_value = mock_client
                client = BinanceClient()
                symbols = client.get_symbols("USDT")
                assert "BTCUSDT" in symbols
                assert "ETHUSDT" in symbols
                assert "BTCBUSD" not in symbols

    def test_get_exchange_info_cached(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
        ):
            with patch("src._binance_sdk_client.BinanceSpotClient") as mock_cls:
                mock_client = MagicMock()
                mock_client.exchange_info.return_value = {"symbols": []}
                mock_cls.return_value = mock_client
                client = BinanceClient()
                # First call
                info1 = client.get_exchange_info()
                # Second call should use cache
                info2 = client.get_exchange_info()
                assert info1 == info2
                assert mock_client.exchange_info.call_count == 1

    def test_get_ticker_price(self):
        from src._binance_sdk_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
        ):
            with patch("src._binance_sdk_client.BinanceSpotClient") as mock_cls:
                mock_client = MagicMock()
                mock_client.ticker_price.return_value = {"price": "50000.0"}
                mock_cls.return_value = mock_client
                client = BinanceClient()
                price = client.get_ticker_price("BTCUSDT")
                assert price == 50000.0


# ── ccxt_client ───────────────────────────────────────────────────────


class TestCCXTClient:
    def test_init_no_keys(self):
        from src.ccxt_client import BinanceClient

        with patch.dict(os.environ, {"BINANCE_API_KEY": "", "BINANCE_API_SECRET": ""}):
            with pytest.raises(ValueError, match="Binance API key"):
                BinanceClient()

    def test_init_with_keys(self):
        from src.ccxt_client import BinanceClient

        with patch.dict(
            os.environ,
            {"BINANCE_API_KEY": "test_key", "BINANCE_API_SECRET": "test_secret"},
        ):
            with patch("ccxt.binance") as mock_binance:
                mock_exchange = MagicMock()
                mock_binance.return_value = mock_exchange
                client = BinanceClient()
                assert client.api_key == "test_key"

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

    def test_get_ticker_price(self):
        from src.ccxt_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
        ):
            with patch("ccxt.binance") as mock_binance:
                mock_exchange = MagicMock()
                mock_exchange.fetch_ticker.return_value = {"last": 50000.0}
                mock_binance.return_value = mock_exchange
                client = BinanceClient()
                price = client.get_ticker_price("BTCUSDT")
                assert price == 50000.0

    def test_validate_symbol(self):
        from src.ccxt_client import BinanceClient

        with patch.dict(
            os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
        ):
            with patch("ccxt.binance") as mock_binance:
                mock_binance.return_value = MagicMock()
                client = BinanceClient()
                assert client.validate_symbol("BTCUSDT") is True
