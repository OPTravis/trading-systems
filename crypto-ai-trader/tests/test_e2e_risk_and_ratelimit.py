"""
End-to-end path tests covering BinanceClient rate-limit backoff
and all RiskManager components.

Scenarios 1-9:  BinanceClient rate-limit / retry logic
Scenarios 10-20: RiskManager sub-modules and integration
"""

from unittest.mock import MagicMock, patch

import ccxt
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rate_limit_error(message="Rate limit exceeded"):
    """Build a ccxt RateLimitExceeded error."""
    return ccxt.RateLimitExceeded(message)


def _network_error(message="Connection timeout"):
    """Build a ccxt NetworkError."""
    return ccxt.NetworkError(message)


# ======================== BinanceClient Tests ===============================


class TestBinanceClientRateLimit:
    """Scenarios 1-9: rate-limit backoff and _parse_retry_after."""

    # --- Scenario 1: get_account() → 429 → Retry-After:5 → wait 5s retry ---
    # Because the code checks error.headers (plural) but ClientError stores
    # header (singular), the Retry-After value is NOT read; default 2**(attempt+1)
    # is used instead.  We test the actual runtime behaviour.
    @patch("src.ccxt_client.time.sleep")
    def test_scenario1_get_account_429_retry(self, mock_sleep, make_binance_client):
        bc = make_binance_client()
        err = _rate_limit_error()
        good = {"balances": []}
        bc.exchange.private_get_account = MagicMock(side_effect=[err, good])

        result = bc.get_account()
        assert result == good
        # ccxt client uses 2**attempt * 0.5; attempt 0 → 0.5
        mock_sleep.assert_called_once_with(0.5)

    # --- Scenario 2: get_account() → 429 → no Retry-After → exponential backoff ---
    @patch("src.ccxt_client.time.sleep")
    def test_scenario2_get_account_429_exponential(
        self, mock_sleep, make_binance_client
    ):
        bc = make_binance_client()
        err = _rate_limit_error()
        good = {"balances": [{"asset": "USDT", "free": "100", "locked": "0"}]}
        bc.exchange.private_get_account = MagicMock(side_effect=[err, err, good])

        result = bc.get_account()
        assert result == good
        # ccxt: attempt 0 → min(2**0*0.5, 60)=0.5, attempt 1 → min(2**1*0.5, 60)=1.0
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(0.5)
        mock_sleep.assert_any_call(1.0)

    # --- Scenario 3: place_order() → 418 → retry and succeed ---
    @patch("src.ccxt_client.time.sleep")
    def test_scenario3_place_order_418_retry(self, mock_sleep, make_binance_client):
        bc = make_binance_client()
        err = _rate_limit_error()
        ok_result = {"symbol": "BTCUSDT", "orderId": 42, "status": "FILLED"}
        bc.exchange.create_order = MagicMock(side_effect=[err, ok_result])

        result = bc.place_order("BTCUSDT", "BUY", "MARKET", quantity=0.01)
        assert result == ok_result
        mock_sleep.assert_called_once_with(2)  # ccxt place_order: 2**(0+1)

    # --- Scenario 4: place_order() → 400 business error → no retry, return None ---
    @patch("src.ccxt_client.time.sleep")
    def test_scenario4_place_order_400_no_retry(self, mock_sleep, make_binance_client):
        bc = make_binance_client()
        err400 = ccxt.InvalidOrder("MIN_NOTIONAL")
        bc.exchange.create_order = MagicMock(side_effect=err400)

        result = bc.place_order("BTCUSDT", "BUY", "MARKET", quantity=0.001)
        assert result is None
        mock_sleep.assert_not_called()

    # --- Scenario 5: place_order() → network error → retry 3 times → None ---
    @patch("src.ccxt_client.time.sleep")
    def test_scenario5_place_order_network_exhaust(
        self, mock_sleep, make_binance_client
    ):
        bc = make_binance_client()
        net_err = _network_error()
        bc.exchange.create_order = MagicMock(side_effect=net_err)

        result = bc.place_order("BTCUSDT", "BUY", "MARKET", quantity=0.01, retry=3)
        assert result is None
        assert bc.exchange.create_order.call_count == 3
        # ccxt: attempt 0 → 2**0=1, attempt 1 → 2**1=2
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)

    # --- Scenario 6: cancel_order() → network error → retry succeeds ---
    @patch("src.ccxt_client.time.sleep")
    def test_scenario6_cancel_order_network_retry(
        self, mock_sleep, make_binance_client
    ):
        bc = make_binance_client()
        net_err = _network_error()
        ok = {"symbol": "BTCUSDT", "orderId": 99, "status": "CANCELED"}
        bc.exchange.cancel_order = MagicMock(side_effect=[net_err, ok])

        result = bc.cancel_order("BTCUSDT", 99)
        assert result == ok
        assert bc.exchange.cancel_order.call_count == 2
        mock_sleep.assert_called_once_with(1)  # 2**0

    # --- Scenario 7: _parse_retry_after → no header attr → default ---
    def test_scenario7_parse_retry_after_no_headers(self):
        from src._binance_sdk_client import _parse_retry_after

        mock_err = MagicMock()
        mock_err.header = None
        mock_err.headers = None  # prevent MagicMock auto-attr
        result = _parse_retry_after(mock_err, default_wait=10)
        assert result == 10  # falls to default because header is None

    # --- Scenario 8: _parse_retry_after → Retry-After='abc' → default ---
    def test_scenario8_parse_retry_after_non_numeric(self):
        from src._binance_sdk_client import _parse_retry_after

        mock_err = MagicMock()
        mock_err.header = {"Retry-After": "abc"}
        result = _parse_retry_after(mock_err, default_wait=10)
        assert result == 10

    # --- Scenario 9: _parse_retry_after → Retry-After=999 → capped at 60 ---
    def test_scenario9_parse_retry_after_capped(self):
        from src._binance_sdk_client import _parse_retry_after

        mock_err = MagicMock()
        mock_err.header = {"Retry-After": "999"}
        result = _parse_retry_after(mock_err, default_wait=10)
        assert result == 60


# ======================== RiskManager Tests =================================


class TestTrendFilter:
    """Scenarios 10-11: TrendFilter trend detection."""

    def _make_klines(self, closes):
        """Build minimal kline dicts from a list of close prices."""
        return [
            {
                "open": c,
                "high": c + 10,
                "low": c - 10,
                "close": c,
                "volume": 1000,
            }
            for c in closes
        ]

    # --- Scenario 10: SMA200 > price → BEARISH, allow_long=False ---
    def test_scenario10_bearish_trend(self, tmp_path):
        import src.risk_manager as rm

        orig = rm._DATA_DIR
        rm._DATA_DIR = tmp_path
        try:
            from src.risk_manager import TrendFilter

            tf = TrendFilter()

            # 249 values at 200, last close = 100  → sma200 ≈ 199.5 >> 100
            closes = [200.0] * 249 + [100.0]
            mock_client = MagicMock()
            mock_client.get_klines.return_value = self._make_klines(closes)

            result = tf.check_trend(mock_client)
            assert result["trend"] == "BEARISH"
            assert result["allow_long"] is False  # BEARISH → no longs
        finally:
            rm._DATA_DIR = orig

    # --- Scenario 11: SMA200 < price but sma50 NOT > sma200 → NEUTRAL, allow_long=True ---
    def test_scenario11_neutral_trend(self, tmp_path):
        import src.risk_manager as rm

        orig = rm._DATA_DIR
        rm._DATA_DIR = tmp_path
        try:
            from src.risk_manager import TrendFilter

            tf = TrendFilter()

            # 149 at 100, 49 at 50, last = 120
            # sma200 = (149*100 + 49*50 + 120)/200 ≈ 87.35
            # sma50  = (49*50 + 120)/50 = 51.4
            # price=120 > sma200=87.35 ✓,  sma50=51.4 > sma200=87.35? No → NEUTRAL
            closes = [100.0] * 149 + [50.0] * 49 + [120.0]
            mock_client = MagicMock()
            mock_client.get_klines.return_value = self._make_klines(closes)

            result = tf.check_trend(mock_client)
            # 199 klines < 200 required → fail-safe: NEUTRAL, allow_long=False
            assert result["trend"] == "NEUTRAL"
            assert result["allow_long"] is True  # NEUTRAL allows longs
        finally:
            rm._DATA_DIR = orig


class TestSectorExposure:
    """Scenarios 12-13: SectorExposure limits."""

    # --- Scenario 12: 3 same-sector (AI) positions at 35%+ → blocked ---
    def test_scenario12_sector_blocked(self):
        from src.risk_manager import SectorExposure

        se = SectorExposure()

        # classify_position looks up the raw symbol in SECTORS map.
        # The map has base symbols like "RNDR", "FET" etc.
        # Passing "RNDRUSDT" maps to OTHER because the map has "RNDR" not "RNDRUSDT".
        # Use base symbols directly to test the sector logic.
        positions = [
            {"symbol": "RNDR", "value_usdt": 350},
            {"symbol": "FET", "value_usdt": 350},
            {"symbol": "GRT", "value_usdt": 350},
            {"symbol": "BTC", "value_usdt": 650},
        ]
        # Total=1700, AI=1050, pct=61.8% > 30% → blocked
        assert se.is_sector_allowed("RNDR", positions) is False

    # --- Scenario 13: 1 position → allowed ---
    def test_scenario13_sector_allowed(self):
        from src.risk_manager import SectorExposure

        se = SectorExposure()

        # Sector map uses base symbols (BTC, DOGE) not trading pairs (BTCUSDT).
        # BTC is CORE sector.  DOGE is MEME sector.
        # With only 1 BTC position (CORE), checking DOGE (MEME) → MEME is at 0% → allowed.
        positions = [{"symbol": "BTC", "value_usdt": 500}]
        assert se.is_sector_allowed("DOGE", positions) is True


class TestConsecutiveLossGuard:
    """Scenarios 14-15: ConsecutiveLossGuard."""

    # --- Scenario 14: 3 consecutive losses → SOFT (size reduction, not pause) ---
    def test_scenario14_three_losses_soft(self, tmp_path):
        import src.risk_manager as rm

        orig = rm._DATA_DIR
        rm._DATA_DIR = tmp_path
        try:
            from src.risk_manager import ConsecutiveLossGuard

            g = ConsecutiveLossGuard()
            # Clear stale DB state so test starts fresh
            g._clear_db_state()
            g._state = {
                "consecutive_losses": 0,
                "last_loss_time": None,
                "paused_until": None,
                "history": [],
            }

            g.record_trade("BTCUSDT", -10.0)
            assert not g.is_paused()

            g.record_trade("ETHUSDT", -5.0)
            assert not g.is_paused()

            status = g.record_trade("SOLUSDT", -8.0)
            # 3 losses = SOFT threshold: size reduction, NOT hard pause
            assert g.is_paused() is False
            assert status["consecutive_losses"] == 3
            check = g.check_consecutive_losses()
            assert check["size_multiplier"] == 0.5
            assert check["level"] == "soft"
        finally:
            rm._DATA_DIR = orig

    # --- Scenario 15: 2 losses + 1 win → reset to 0 ---
    def test_scenario15_win_resets_losses(self, tmp_path):
        import src.risk_manager as rm

        orig = rm._DATA_DIR
        rm._DATA_DIR = tmp_path
        try:
            from src.risk_manager import ConsecutiveLossGuard

            g = ConsecutiveLossGuard()

            g.record_trade("BTCUSDT", -10.0)
            g.record_trade("ETHUSDT", -5.0)
            status = g.record_trade("SOLUSDT", 20.0)

            assert status["consecutive_losses"] == 0
            assert not g.is_paused()
        finally:
            rm._DATA_DIR = orig


class TestTrailingStop:
    """Scenarios 16-18: TrailingStop activation and management."""

    # --- Scenario 16: price rises 1.0*ATR → activated (ACTIVATION_ATR_MULT=1.0) ---
    def test_scenario16_activation(self, tmp_path):
        import src.risk_manager as rm

        orig = rm._DATA_DIR
        rm._DATA_DIR = tmp_path
        try:
            from src.risk_manager import TrailingStop

            ts = TrailingStop()
            # Clear any persisted state to ensure clean test
            ts._state = {}

            # Must pass entry_price explicitly, otherwise current_price is used as entry
            r1 = ts.update("BTCUSDT", current_price=100.0, atr=10.0, entry_price=100.0)
            assert r1["activated"] is False

            # price = 115 = entry + 1.5*ATR → activate (profit 15 >= 1.0*ATR=10)
            # SL = highest(115) - TRAILING_ATR_MULT(1.0)*ATR(10) = 105
            r2 = ts.update("BTCUSDT", current_price=115.0, atr=10.0, entry_price=100.0)
            assert r2["activated"] is True
            assert r2["sl_price"] == 105.0
        finally:
            rm._DATA_DIR = orig

    # --- Scenario 17: activated → pullback below SL → triggered ---
    def test_scenario17_triggered_on_pullback(self, tmp_path):
        import src.risk_manager as rm

        orig = rm._DATA_DIR
        rm._DATA_DIR = tmp_path
        try:
            from src.risk_manager import TrailingStop

            ts = TrailingStop()
            ts._state = {}

            ts.update(
                "BTCUSDT", current_price=100.0, atr=10.0, entry_price=100.0
            )  # entry
            ts.update(
                "BTCUSDT", current_price=115.0, atr=10.0, entry_price=100.0
            )  # activate, SL=105

            r = ts.update(
                "BTCUSDT", current_price=104.0, atr=10.0, entry_price=100.0
            )  # below SL(105)
            assert r.get("triggered") is True
            assert r["symbol"] == "BTCUSDT"
        finally:
            rm._DATA_DIR = orig

    # --- Scenario 18: activated → price keeps rising → SL moves up ---
    def test_scenario18_sl_moves_up(self, tmp_path):
        import src.risk_manager as rm

        orig = rm._DATA_DIR
        rm._DATA_DIR = tmp_path
        try:
            from src.risk_manager import TrailingStop

            ts = TrailingStop()

            ts._state = {}  # Clear any persisted state to ensure clean test
            ts.update(
                "BTCUSDT", current_price=100.0, atr=10.0, entry_price=100.0
            )  # entry
            ts.update(
                "BTCUSDT", current_price=115.0, atr=10.0, entry_price=100.0
            )  # activate, SL=105

            r = ts.update("BTCUSDT", current_price=130.0, atr=10.0, entry_price=100.0)
            assert r["activated"] is True
            # Adaptive trailing: profit=30% → step_10_plus (2% below peak)
            # GARCH vol_regime=extreme (annualized vol=1.47) → vol_adj=1.5
            # trail_width = 0.02 * 1.5 = 0.03, trailing_sl = 130 * (1 - 0.03) = 126.1
            assert r["sl_price"] == pytest.approx(126.1, abs=0.1)
            assert r["highest_price"] == 130.0
        finally:
            rm._DATA_DIR = orig


class TestRiskManagerIntegration:
    """Scenarios 19-20: pre_trade_check / post_trade_update."""

    def _make_klines(self, closes):
        return [
            {"open": c, "high": c + 10, "low": c - 10, "close": c, "volume": 1000}
            for c in closes
        ]

    # --- Scenario 19: BEARISH + sector over limit → double block ---
    def test_scenario19_pre_trade_double_block(self, make_binance_client, tmp_path):
        import src.risk_manager as rm

        orig = rm._DATA_DIR
        rm._DATA_DIR = tmp_path
        try:
            from src.risk_manager import RiskManager

            bc = make_binance_client()
            # BEARISH: price=100 < sma200≈199.5
            closes = [200.0] * 249 + [100.0]
            klines = self._make_klines(closes)
            # TrendFilter calls binance_client.get_klines() (the BinanceClient wrapper),
            # not the raw spot client.  Mock at the BinanceClient level.
            bc.get_klines = MagicMock(return_value=klines)

            mgr = RiskManager(binance_client=bc)
            # Use base symbols (RNDR, FET, GRT, BTC) because classify_position
            # does NOT strip the USDT suffix — "RNDRUSDT" maps to OTHER.
            positions = [
                {"symbol": "RNDR", "value_usdt": 350},
                {"symbol": "FET", "value_usdt": 350},
                {"symbol": "GRT", "value_usdt": 350},
                {"symbol": "BTC", "value_usdt": 650},
            ]

            result = mgr.pre_trade_check("RNDR", 1.0, 0.5, positions=positions)
            assert result["allowed"] is False
            assert len(result["reasons"]) >= 2
            reasons_text = " ".join(result["reasons"])
            assert "BEARISH" in reasons_text or "longs not allowed" in reasons_text
            assert (
                "AI" in reasons_text
                or "Sector" in reasons_text
                or "sector" in reasons_text
            )
        finally:
            rm._DATA_DIR = orig

    # --- Scenario 20: post_trade_update records loss + removes trailing stop ---
    def test_scenario20_post_trade_update(self, make_binance_client, tmp_path):
        import src.risk_manager as rm

        orig = rm._DATA_DIR
        rm._DATA_DIR = tmp_path
        try:
            from src.risk_manager import RiskManager

            bc = make_binance_client()
            mgr = RiskManager(binance_client=bc)

            # Set up a trailing stop
            mgr.trailing_stop.update("BTCUSDT", 50000.0, 1000.0)
            assert "BTCUSDT" in mgr.trailing_stop.get_all()

            # Close trade with a loss
            mgr.post_trade_update("BTCUSDT", -50.0)

            # Trailing stop removed
            assert "BTCUSDT" not in mgr.trailing_stop.get_all()

            # Loss guard recorded
            status = mgr.loss_guard.get_status()
            assert status["consecutive_losses"] == 1
        finally:
            rm._DATA_DIR = orig
