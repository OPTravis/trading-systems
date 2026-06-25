"""
Shared fixtures for crypto-ai-trader test suite.
Provides mock objects and temporary data directories.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _reset_daily_loss_breaker(monkeypatch, tmp_path):
    """Reset DailyLossBreaker singleton state between tests to prevent cross-test contamination."""
    try:
        import src.daily_loss_breaker as dlb_mod

        dlb_mod._dlb_instance = None
        yield
        dlb_mod._dlb_instance = None
    except (ImportError, AttributeError):
        yield


@pytest.fixture(autouse=True)
def _set_env(monkeypatch, tmp_path):
    """Set minimal env vars so modules don't crash during import."""
    monkeypatch.setenv("BINANCE_API_KEY", "test-api-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test-api-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("AUTO_EXECUTE", "true")
    monkeypatch.setenv("DCA_CHECK_DISABLED", "1")

    # Redirect data dir to tmp_path for risk_manager persistence
    import src.risk_manager as rm

    monkeypatch.setattr(rm, "_DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)

    # Redirect signals dir so tests don't pollute production notification files
    import src.notifier as notifier_mod
    test_signals_dir = tmp_path / "signals"
    test_signals_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(notifier_mod, "SIGNALS_DIR", test_signals_dir)
    monkeypatch.setattr(notifier_mod, "SIGNALS_FILE", test_signals_dir / "pending.json")
    monkeypatch.setattr(notifier_mod, "NOTIFICATIONS_FILE", test_signals_dir / "pending_notifications.json")

    # Use a unique scan lock file per test to avoid flock contention
    monkeypatch.setenv("SCAN_LOCK_FILE", str(tmp_path / "test_scan.lock"))


@pytest.fixture(autouse=True)
def _isolate_statedb(monkeypatch, tmp_path):
    """Redirect StateDB to temporary database for test isolation."""
    test_db_path = str(tmp_path / "test_state.db")
    monkeypatch.setenv("STATE_DB_PATH", test_db_path)
    # Reset singleton so it picks up the new path
    import src.state_db as sd_mod

    sd_mod._state_db_instance = None
    yield
    sd_mod._state_db_instance = None


@pytest.fixture
def mock_binance_spot():
    """Return a MagicMock that replaces binance.spot.Spot."""
    spot = MagicMock()
    spot.account.return_value = {
        "balances": [
            {"asset": "USDT", "free": "1000.00", "locked": "0.00"},
            {"asset": "BTC", "free": "0.0", "locked": "0.0"},
            {"asset": "ETH", "free": "0.0", "locked": "0.0"},
        ]
    }
    spot.exchange_info.return_value = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "filters": [
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.001",
                        "maxQty": "1000",
                        "stepSize": "0.001",
                    },
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "0.01",
                        "tickSize": "0.01",
                    },
                    {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                ],
            },
            {
                "symbol": "ETHUSDT",
                "status": "TRADING",
                "baseAsset": "ETH",
                "quoteAsset": "USDT",
                "filters": [
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.001",
                        "maxQty": "10000",
                        "stepSize": "0.001",
                    },
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "0.01",
                        "tickSize": "0.01",
                    },
                    {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                ],
            },
            {
                "symbol": "SOLUSDT",
                "status": "TRADING",
                "baseAsset": "SOL",
                "quoteAsset": "USDT",
                "filters": [
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.01",
                        "maxQty": "50000",
                        "stepSize": "0.01",
                    },
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "0.001",
                        "tickSize": "0.001",
                    },
                    {"filterType": "NOTIONAL", "minNotional": "5"},
                ],
            },
        ]
    }
    spot.ticker_24hr.return_value = {
        "symbol": "BTCUSDT",
        "priceChange": "500.00",
        "priceChangePercent": "1.5",
        "volume": "10000.0",
        "quoteVolume": "500000000.0",
        "highPrice": "35000.00",
        "lowPrice": "33000.00",
        "lastPrice": "34500.00",
    }
    spot.get_open_orders.return_value = []
    spot.new_order.return_value = {
        "symbol": "BTCUSDT",
        "orderId": 12345,
        "status": "NEW",
        "fills": [{"price": "34500.00", "qty": "0.01", "commission": "0.01"}],
    }
    spot.cancel_order.return_value = {
        "symbol": "BTCUSDT",
        "orderId": 12345,
        "status": "CANCELED",
    }
    spot.cancel_open_orders.return_value = []
    spot.time.return_value = {"serverTime": 1700000000000}
    return spot


@pytest.fixture
def make_binance_client(mock_binance_spot):
    """Factory fixture: returns a BinanceClient with SDK-style .client mock."""
    from src.binance_client import BinanceClient

    def _make(**overrides):
        with patch.object(BinanceClient, "__init__", lambda self, *a, **kw: None):
            bc = BinanceClient.__new__(BinanceClient)
            bc.testnet = False
            bc.base_url = "https://api.binance.com"
            bc.api_key = "test-key"
            bc.api_secret = "test-secret"
            bc.recv_window = 10000
            bc._balance_cache = {}
            bc._balance_cache_ttl = 30
            # SDK client uses self.client (SpotAccount); set up MagicMock
            bc.client = MagicMock()
            bc.client.account.return_value = {
                "balances": [
                    {"asset": "USDT", "free": "1000.00", "locked": "0.00"},
                    {"asset": "BTC", "free": "0.01", "locked": "0.00"},
                ]
            }
            bc.client.new_order.return_value = {
                "symbol": "BTCUSDT",
                "orderId": 12345,
                "status": "NEW",
                "fills": [{"price": "34500.00", "qty": "0.01", "commission": "0.01"}],
            }
            bc.client.cancel_order.return_value = {
                "symbol": "BTCUSDT",
                "orderId": 12345,
                "status": "CANCELED",
            }
            bc.client.klines.return_value = [
                [1000, "200.0", "210.0", "190.0", "200.0", "1000.0",
                 2000, "200000.0", 100, "500.0", "250.0", "0"]
            ] * 250
            # exchange_info cache attributes
            bc._exchange_info_cache = None
            bc._exchange_info_timestamp = 0.0
            bc._exchange_info_ttl = 3600
            # Apply overrides
            for k, v in overrides.items():
                setattr(bc, k, v)
        return bc

    return _make


@pytest.fixture
def mock_notifier():
    """Mock FeishuNotifier that doesn't send real messages."""
    with patch("main.FeishuNotifier") as MockCls, patch(
        "src.notifier.FeishuNotifier"
    ) as MockCls2:
        instance = MagicMock()
        instance.send_text.return_value = True
        instance.get_strategy_config.return_value = {
            "stop_loss_pct": 2.0,
            "take_profit_levels": [
                {"pct": 2.0, "size_pct": 33},
                {"pct": 3.0, "size_pct": 33},
                {"pct": 5.0, "size_pct": 34},
            ],
            "max_hold_hours": 24,
        }
        MockCls.return_value = instance
        MockCls2.return_value = instance
        yield instance

@pytest.fixture(autouse=True)
def _reset_risk_manager_singleton():
    """Reset RiskManager singleton to prevent cross-test contamination.

    get_risk_manager() caches the instance process-wide; without this reset,
    a mock from an earlier test persists and bypasses patches in later tests.
    """
    import src.risk_manager as rm_mod
    rm_mod._risk_manager_instance = None
    yield
    rm_mod._risk_manager_instance = None

# Prevent pytest from importing standalone scripts that pollute global state
collect_ignore = ["test_integration_recent_changes.py"]
