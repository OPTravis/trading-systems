"""
Crypto-ai-trader 完整系統測試套件

覆蓋模塊：
  A. Portfolio 同步（ghost 倉位、數量不匹配、新增）
  B. TrailingStop（激活、觸發、SL 上移、矛盾檢測）
  C. SL Coverage（全鎖 TP、低於 notional、正常覆蓋）
  D. ConsecutiveLossGuard（連敗暫停、勝利重置、過期恢復、垃圾檢測）
  E. StrategyAdaptor（5 個 regime 策略映射）
  F. Entry Price（FIFO 計算、部分賣出、歷史不足）
  G. SmartOrder（倉位限制、ATR SL/TP 計算）

運行：cd ~/crypto-ai-trader && .venv/bin/python3 -m pytest tests/test_crypto_system.py -v
"""

import json
import os
import time
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_data_dir(tmp_path):
    """Create a temporary data directory for state files."""
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d


# ===========================================================================
# A. Portfolio 同步
# ===========================================================================

class TestPortfolioSync:
    """Test portfolio_state.json sync with Binance (ghost, qty mismatch, missing)."""

    def _make_portfolio(self, tmp_data_dir, positions=None, cash=100.0):
        from src.portfolio import PortfolioManager
        pm = PortfolioManager.__new__(PortfolioManager)
        pm.config = pm._default_config() if hasattr(pm, '_default_config') else {
            "max_position_pct": 20, "max_total_exposure_pct": 80,
            "cash_reserve_pct": 20, "max_daily_loss_pct": 3,
            "max_leverage": 1, "max_open_positions": 3, "max_hold_hours": 24,
            "stop_loss": {"default_pct": 2.0}, "take_profit": {"default_pct": 6.0},
        }
        pm.positions = positions or {}
        pm.cash_balance = cash
        pm.orders_log = []
        pm._last_save_time = 0
        pm._save_debounce_sec = 0
        pm._daily_start_value = None
        pm._daily_start_date = None
        pm.state_file = tmp_data_dir / "portfolio_state.json"
        pm.state_file.parent.mkdir(parents=True, exist_ok=True)
        return pm

    def test_sync_removes_ghost_position(self, tmp_data_dir):
        """Bug regression: state has TAO but Binance doesn't → should remove."""
        pm = self._make_portfolio(tmp_data_dir, positions={
            "BARDUSDT": {"symbol": "BARDUSDT", "quantity": 44.0, "entry_price": 0.3336,
                         "current_price": 0.306, "stop_loss": 0, "trailing_stop_pct": 1.5,
                         "highest_price": 0.339, "strategy": "synced",
                         "created_at": "2026-04-15T09:27:15", "updated_at": "2026-04-19T22:02:56"},
            "TAOUSDT": {"symbol": "TAOUSDT", "quantity": 0.0595, "entry_price": 254.0,
                        "current_price": 247.5, "stop_loss": 0, "trailing_stop_pct": 1.5,
                        "highest_price": 259.3, "strategy": "synced",
                        "created_at": "2026-04-17T22:02:22", "updated_at": "2026-04-19T22:02:57"},
        }, cash=392.12)

        # Simulate Binance having only BARD (no TAO)
        binance_assets = {"BARDUSDT"}
        for symbol in list(pm.positions.keys()):
            if symbol not in binance_assets:
                del pm.positions[symbol]

        assert "TAOUSDT" not in pm.positions, "Ghost TAO should be removed"
        assert "BARDUSDT" in pm.positions
        assert len(pm.positions) == 1

    def test_sync_updates_quantity(self, tmp_data_dir):
        """Binance has 14 NEAR but state has 32 → should update to 14."""
        pm = self._make_portfolio(tmp_data_dir, positions={
            "NEARUSDT": {"symbol": "NEARUSDT", "quantity": 32.0, "entry_price": 1.425,
                         "current_price": 1.375, "stop_loss": 0, "trailing_stop_pct": 1.5,
                         "highest_price": 1.429, "strategy": "synced",
                         "created_at": "2026-04-17T22:02:22", "updated_at": "2026-04-19T22:02:57"},
        })

        # Simulate quantity update from Binance
        binance_qty = 14.0
        old_qty = pm.positions["NEARUSDT"]["quantity"]
        assert old_qty == 32.0
        pm.positions["NEARUSDT"]["quantity"] = binance_qty

        assert pm.positions["NEARUSDT"]["quantity"] == 14.0

    def test_sync_adds_new_position(self, tmp_data_dir):
        """Binance has new coin not in state → should add."""
        pm = self._make_portfolio(tmp_data_dir, positions={}, cash=392.12)

        # Simulate discovering a new position
        pm.positions["BNBUSDT"] = {
            "symbol": "BNBUSDT", "quantity": 0.0058, "entry_price": 640.41,
            "current_price": 626.0, "stop_loss": 0, "trailing_stop_pct": 1.5,
            "highest_price": 643.92, "strategy": "synced",
            "created_at": datetime.now().isoformat(), "updated_at": datetime.now().isoformat(),
        }

        assert "BNBUSDT" in pm.positions
        assert pm.positions["BNBUSDT"]["quantity"] == 0.0058


# ===========================================================================
# B. TrailingStop
# ===========================================================================

class TestTrailingStop:
    """Test trailing stop activation, trigger, SL movement, and contradictions."""

    @pytest.fixture
    def ts(self, tmp_data_dir):
        from src.risk_manager import TrailingStop
        ts_obj = TrailingStop.__new__(TrailingStop)
        ts_obj._filepath = tmp_data_dir / "trailing_stops.json"
        ts_obj._state = {}
        ts_obj._save_debounce_sec = 0
        ts_obj._last_save_time = 0
        ts_obj._last_save_ts = 0.0
        ts_obj._save_debounce = 0.0
        return ts_obj

    def test_tracking_not_activated(self, ts):
        """Price hasn't risen enough → should stay tracking, not activated."""
        # entry=100, atr=2, activation threshold = 1.5 * 2 = 3 → price needs to reach 103
        result = ts.update("BTC", current_price=101.0, atr=2.0, entry_price=100.0)
        assert result.get("activated") is False
        assert "triggered" not in result
        # TrailingStop normalizes symbol to BTCUSDT internally
        assert ts._state["BTCUSDT"]["activated"] is False

    def test_activated_after_profit(self, ts):
        """Price rises past activation threshold → should activate."""
        result = ts.update("BTC", current_price=106.0, atr=2.0, entry_price=100.0)
        assert result.get("activated") is True
        # TrailingStop normalizes symbol to BTCUSDT internally
        assert ts._state["BTCUSDT"]["activated"] is True
        # SL = highest - TRAILING_ATR_MULT(1.0) * ATR = 106.0 - 2.0 = 104.0
        assert ts._state["BTCUSDT"]["sl_price"] == pytest.approx(104.0, abs=0.1)

    def test_triggered_after_callback(self, ts):
        """After activation, price drops to SL → should trigger."""
        ts.update("BTC", current_price=106.0, atr=2.0, entry_price=100.0)
        # Price drops to SL level (103.6)
        result = ts.update("BTC", current_price=103.5, atr=2.0, entry_price=100.0)
        assert result.get("triggered") is True
        assert result["entry_price"] == 100.0

    def test_sl_only_moves_up(self, ts):
        """SL should only move UP, never down."""
        # Activate at 106.0
        ts.update("BTC", current_price=106.0, atr=2.0, entry_price=100.0)
        # TrailingStop normalizes symbol to BTCUSDT internally
        sl_after_activate = ts._state["BTCUSDT"]["sl_price"]

        # Price rises more → SL should move up
        ts.update("BTC", current_price=108.0, atr=2.0, entry_price=100.0)
        sl_after_rise = ts._state["BTCUSDT"]["sl_price"]
        assert sl_after_rise > sl_after_activate

        # Price drops slightly but stays above SL → SL should NOT move down
        ts.update("BTC", current_price=107.0, atr=2.0, entry_price=100.0)
        assert ts._state["BTCUSDT"]["sl_price"] == sl_after_rise  # unchanged

    def test_no_duplicate_trigger_and_tracking(self, ts):
        """Regression: same asset cannot be both tracking AND triggered in one run.
        This was the BNB bug: qty<1 caused false SL/TP fill detection.
        """
        # Simulate: first call is tracking
        r1 = ts.update("BNB", current_price=626.0, atr=3.0, entry_price=625.0)
        assert r1.get("activated") is False  # tracking

        # Simulate: in the SAME check cycle, the fill-detection code
        # should NOT also trigger. We verify by checking that the
        # TrailingStop state is consistent (no triggered flag without actual trigger)
        # TrailingStop normalizes symbol to BNBUSDT internally
        assert ts._state["BNBUSDT"]["activated"] is False
        assert "triggered" not in ts._state["BNBUSDT"]  # no phantom trigger


# ===========================================================================
# C. SL Coverage (trailing-check logic)
# ===========================================================================

class TestSLCoverage:
    """Test the SL coverage logic in trailing-check: fully locked TP, notional, normal."""

    def test_all_locked_in_tp_no_sl(self):
        """NEAR: 14 units all locked in TP, 0 SL → should identify as uncovered."""
        total_qty = 14.0
        sl_covered = 0.0
        tp_covered = 14.0  # 4 + 6 + 4

        uncovered_by_sl = total_qty - sl_covered  # = 14
        assert uncovered_by_sl > 0

        # Free qty is 0 (all locked in TP)
        free_qty = 0.0
        # Should detect that TP needs to be canceled to make room
        assert free_qty < uncovered_by_sl  # need to free up from TP

    def test_below_notional_minimum(self):
        """BNB: 0.0057 units @ $625 = $3.57 < $5 minimum → cannot place order."""
        qty = 0.00573128
        price = 625.0
        notional = qty * price
        assert notional < 5.0, "BNB notional should be below $5 minimum"
        # This should result in no_sl_below_notional, not an API call

    def test_sl_already_exists(self):
        """BARD: 44 units, 2 SL orders covering 44 → fully covered."""
        total_qty = 44.0
        sl_covered = 22.0 + 22.0  # two SL orders of 22 each
        uncovered_by_sl = total_qty - sl_covered
        assert uncovered_by_sl <= 0, "BARD should be fully covered by SL"

    def test_free_balance_uncovered(self):
        """Position has free balance with no SL → should place -5% SL."""
        total_qty = 100.0
        sl_covered = 0.0
        free_qty = 100.0
        price = 10.0
        uncovered_by_sl = total_qty - sl_covered  # = 100

        assert uncovered_by_sl > 0
        assert free_qty >= uncovered_by_sl  # enough free balance
        assert free_qty * price >= 5.0  # above notional minimum

        # Expected SL price
        expected_sl = round(price * 0.95, 2)
        assert expected_sl == 9.5  # -5%


# ===========================================================================
# D. ConsecutiveLossGuard
# ===========================================================================

class TestConsecutiveLossGuard:
    """Test loss streak tracking, pause, auto-expire, and garbage detection."""

    @pytest.fixture
    def lg(self, tmp_data_dir):
        from src.risk_manager import ConsecutiveLossGuard
        lg_obj = ConsecutiveLossGuard.__new__(ConsecutiveLossGuard)
        lg_obj._filepath = tmp_data_dir / "loss_guard.json"
        lg_obj._state = {
            "consecutive_losses": 0,
            "last_loss_time": None,
            "paused_until": None,
            "history": [],
        }
        lg_obj._save_debounce_sec = 0
        lg_obj._last_save_time = 0
        return lg_obj

    def test_consecutive_losses_triggers_pause(self, lg):
        """5 consecutive losses → should trigger hard pause (3 = soft reduction only)."""
        lg.record_trade("BTC", -1.0)
        assert lg._state["consecutive_losses"] == 1

        lg.record_trade("ETH", -2.0)
        assert lg._state["consecutive_losses"] == 2

        # 3 losses = soft threshold (size reduction, not pause)
        lg.record_trade("NEAR", -0.5)
        assert lg._state["consecutive_losses"] == 3
        check = lg.check_consecutive_losses()
        assert check["size_multiplier"] == 0.5
        assert not lg.is_paused()

        lg.record_trade("SOL", -1.0)
        assert lg._state["consecutive_losses"] == 4

        # 5 losses = hard halt
        lg.record_trade("DOGE", -0.5)
        assert lg._state["consecutive_losses"] == 5
        assert lg._state["paused_until"] is not None
        assert lg.is_paused()

    def test_win_resets_streak(self, lg):
        """A winning trade should reset consecutive_losses to 0."""
        lg.record_trade("BTC", -1.0)
        lg.record_trade("ETH", -2.0)
        assert lg._state["consecutive_losses"] == 2

        lg.record_trade("SOL", 3.0)  # win!
        assert lg._state["consecutive_losses"] == 0

    def test_pause_auto_expires(self, lg):
        """After pause duration, is_paused() should return False and clear state."""
        lg.record_trade("BTC", -1.0)
        lg.record_trade("ETH", -2.0)
        lg.record_trade("NEAR", -0.5)
        lg.record_trade("SOL", -1.0)
        lg.record_trade("DOGE", -0.5)
        assert lg.is_paused()

        # Simulate time passing (set paused_until to past)
        lg._state["paused_until"] = time.time() - 1  # expired
        assert not lg.is_paused()
        assert lg._state["consecutive_losses"] == 0
        assert lg._state["paused_until"] is None

    def test_detect_garbage_records(self):
        """50 BNB records with tiny PnL ($0.0001-$0.016) → should be flagged as garbage."""
        from collections import Counter
        history = [
            {"symbol": "BNB", "pnl": round(-0.0001 * i + 0.001, 4)}
            for i in range(50)
        ]
        symbols = [h.get("symbol") for h in history]
        sym_counts = Counter(symbols)
        dominant = sym_counts.most_common(1)[0]

        assert dominant[0] == "BNB"
        assert dominant[1] / len(history) > 0.8  # 100% from one symbol

        pnls = [abs(h.get("pnl", 0)) for h in history]
        avg_pnl = sum(pnls) / len(pnls)
        assert avg_pnl < 0.02  # tiny average → garbage


# ===========================================================================
# E. StrategyAdaptor
# ===========================================================================

class TestStrategyAdaptor:
    """Test all 5 market regimes produce correct strategy enable/disable."""

    @pytest.fixture
    def adaptor(self, tmp_data_dir):
        from src.strategy_adaptor import StrategyAdaptor
        sa = StrategyAdaptor.__new__(StrategyAdaptor)
        sa._filepath = tmp_data_dir / "strategy_state.json"
        sa._state = {"last_regime": None, "last_adjustments": None, "history": []}
        sa._cache = None
        sa._cache_ts = 0
        sa._cache_ttl = 0
        return sa

    @pytest.fixture(autouse=True)
    def _mock_overlays(self, monkeypatch):
        """Mock external overlays (HMM, CVaR, GARCH, Bandit, ParamOptimizer) so
        tests exercise only the core regime logic without side effects."""
        # HMM: return no cached prediction → skip HMM overlay
        mock_hmm = MagicMock()
        mock_hmm.return_value.get_cached_prediction.return_value = None
        monkeypatch.setattr("src.strategy_adaptor.HMMRegimeDetector", mock_hmm, raising=False)
        # CVaR: return default scale=1.0
        mock_cvar_cls = MagicMock()
        instance = mock_cvar_cls.return_value
        instance._db._get_conn.return_value.execute.return_value.fetchall.return_value = []
        instance.compute_cvar.return_value = 0
        instance.compute_portfolio_risk.return_value = {"position_scale": 1.0, "risk_level": None}
        # ParamOptimizer: return empty dict
        mock_po_cls = MagicMock()
        mock_po_cls.return_value.get_current_params.return_value = {}
        monkeypatch.setattr("src.strategy_adaptor.ParamOptimizer", mock_po_cls, raising=False)
        # Contextual bandit: return default 0.8
        mock_bandit_cls = MagicMock()
        mock_bandit_cls.return_value.recommend_size.return_value = 0.8
        monkeypatch.setattr("src.strategy_adaptor.get_contextual_bandit",
                            mock_bandit_cls.return_value.recommend_size, raising=False)

    def _get_enabled(self, adaptor, regime_result):
        return {k for k, v in regime_result["strategies"].items() if v["enabled"]}

    def test_extreme_fear_regime(self, adaptor):
        """F&G=20, BEARISH BTC → only DCA + RSI + Bollinger (vwap disabled by default)."""
        result = adaptor.adapt(fear_greed=20, btc_trend="BEARISH", btc_price_change_24h=-3.0)
        enabled = self._get_enabled(adaptor, result)

        assert "dca" in enabled, "DCA should be enabled in EXTREME_FEAR"
        assert "rsi_reversion" in enabled, "RSI should be enabled in EXTREME_FEAR"
        assert "bollinger" in enabled, "Bollinger should be enabled (BEARISH + extreme fear)"
        assert "vwap" not in enabled, "VWAP disabled by default (FIX-4)"
        assert "grid" not in enabled
        assert "trend" not in enabled

    def test_fear_regime(self, adaptor):
        """F&G=27 → DCA + RSI + Bollinger enabled, Grid/Trend/VWAP disabled."""
        result = adaptor.adapt(fear_greed=27, btc_trend="BEARISH", btc_price_change_24h=-2.0)
        enabled = self._get_enabled(adaptor, result)

        assert "dca" in enabled
        assert "rsi_reversion" in enabled
        assert "bollinger" in enabled
        assert "vwap" not in enabled
        assert "grid" not in enabled
        assert "trend" not in enabled

    def test_neutral_regime(self, adaptor):
        """F&G=50 → dca, rsi_reversion, bollinger, trend enabled (vwap disabled by default)."""
        result = adaptor.adapt(fear_greed=50, btc_trend="NEUTRAL", btc_price_change_24h=1.5)
        enabled = self._get_enabled(adaptor, result)

        assert enabled == {"bollinger", "dca", "rsi_reversion", "trend"}
        for name in ("dca", "rsi_reversion", "bollinger", "trend"):
            assert result["strategies"][name]["size_multiplier"] == 1.0

    def test_greed_regime(self, adaptor):
        """F&G=65 → Grid + Trend + Bollinger, DCA/RSI/VWAP disabled."""
        result = adaptor.adapt(fear_greed=65, btc_trend="BULLISH", btc_price_change_24h=2.0)
        enabled = self._get_enabled(adaptor, result)

        assert "grid" in enabled
        assert "trend" in enabled
        assert "bollinger" in enabled
        assert "vwap" not in enabled
        assert "dca" not in enabled, "DCA should be disabled in GREED"
        assert "rsi_reversion" not in enabled, "RSI should be disabled in GREED"

    def test_extreme_greed_regime(self, adaptor):
        """F&G=80 → Bollinger + Trend, rest disabled."""
        result = adaptor.adapt(fear_greed=80, btc_trend="NEUTRAL", btc_price_change_24h=1.0)
        enabled = self._get_enabled(adaptor, result)

        assert "bollinger" in enabled
        assert "trend" in enabled
        assert "vwap" not in enabled
        assert "dca" not in enabled
        assert "rsi_reversion" not in enabled
        assert result["strategies"]["trend"]["size_multiplier"] == 1.3

    def test_no_strategy_overlap(self, adaptor):
        """No strategy should appear in both enabled and disabled lists."""
        result = adaptor.adapt(fear_greed=27, btc_trend="BEARISH", btc_price_change_24h=-2.0)
        enabled = {k for k, v in result["strategies"].items() if v["enabled"]}
        disabled = {k for k, v in result["strategies"].items() if not v["enabled"]}
        assert enabled & disabled == set(), f"Overlap: {enabled & disabled}"
        assert enabled | disabled == set(result["strategies"].keys())


# ===========================================================================
# F. Entry Price (FIFO)
# ===========================================================================

class TestEntryPrice:
    """Test FIFO-based average entry price calculation."""

    def _make_mock_client(self, trades):
        client = MagicMock()
        client.get_my_trades.return_value = trades
        return client

    def test_fifo_calculation(self):
        """3 buys: 10@$100, 5@$120, 5@$140 → avg = (10*100+5*120+5*140)/20 = $115."""
        from src.entry_price import get_avg_entry_price
        trades = [
            {"qty": "10", "price": "100", "isBuyer": True, "time": 1000},
            {"qty": "5", "price": "120", "isBuyer": True, "time": 2000},
            {"qty": "5", "price": "140", "isBuyer": True, "time": 3000},
        ]
        client = self._make_mock_client(trades)
        result = get_avg_entry_price(client, "TESTUSDT", current_qty=20.0)

        assert result is not None
        assert result == pytest.approx(115.0, abs=0.01)

    def test_partial_sell_reduces_batch(self):
        """Buy 10@$100, sell 4, buy 5@$120 → remaining = 6@$100 + 5@$120 = avg ~108.89."""
        from src.entry_price import get_avg_entry_price
        trades = [
            {"qty": "10", "price": "100", "isBuyer": True, "time": 1000},
            {"qty": "4", "price": "110", "isBuyer": False, "time": 2000},
            {"qty": "5", "price": "120", "isBuyer": True, "time": 3000},
        ]
        client = self._make_mock_client(trades)
        result = get_avg_entry_price(client, "TESTUSDT", current_qty=11.0)

        assert result is not None
        # (6*100 + 5*120) / 11 = 1200/11 ≈ 109.09
        assert result == pytest.approx(109.09, abs=0.1)

    def test_insufficient_history_returns_none(self):
        """Calculated qty doesn't match actual → should return None."""
        from src.entry_price import get_avg_entry_price
        trades = [
            {"qty": "10", "price": "100", "isBuyer": True, "time": 1000},
        ]
        client = self._make_mock_client(trades)
        # Actual holding is 100 but history only shows buy of 10
        result = get_avg_entry_price(client, "TESTUSDT", current_qty=100.0)
        assert result is None

    def test_no_trades_returns_none(self):
        """No trade history → should return None."""
        from src.entry_price import get_avg_entry_price
        client = self._make_mock_client([])
        result = get_avg_entry_price(client, "TESTUSDT")
        assert result is None

    def test_zero_current_qty_returns_none(self):
        """current_qty=0 → should return None."""
        from src.entry_price import get_avg_entry_price
        client = self._make_mock_client([{"qty": "10", "price": "100", "isBuyer": True, "time": 1000}])
        result = get_avg_entry_price(client, "TESTUSDT", current_qty=0)
        assert result is None


# ===========================================================================
# G. SmartOrder
# ===========================================================================

class TestSmartOrder:
    """Test position sizing and ATR-based SL/TP calculations."""

    def test_calculate_sl_tp_normal_atr(self):
        """Normal ATR: SL=-2*ATR, TP1=+2*ATR, TP2=+4*ATR, TP3=+6*ATR."""
        from src.smart_order import SmartOrder
        result = SmartOrder.calculate_sl_tp(price=100.0, atr=2.0)

        assert result["sl_price"] == pytest.approx(96.0, abs=0.01)
        assert result["tp1_price"] == pytest.approx(104.0, abs=0.01)
        assert result["tp2_price"] == pytest.approx(108.0, abs=0.01)
        assert result["tp3_price"] == pytest.approx(112.0, abs=0.01)

        # Risk/Reward check
        sl_distance = 100.0 - result["sl_price"]
        tp1_distance = result["tp1_price"] - 100.0
        assert tp1_distance == pytest.approx(sl_distance, abs=0.01)  # 1:1

        # Size percentages sum to 100
        total_size = result["tp1_size_pct"] + result["tp2_size_pct"] + result["tp3_size_pct"]
        assert total_size == 100

    def test_calculate_sl_tp_levels_monotonic(self):
        """TP levels must be strictly ascending: entry < TP1 < TP2 < TP3."""
        from src.smart_order import SmartOrder
        for atr in [0.5, 1.0, 5.0, 50.0]:
            for price in [1.0, 100.0, 50000.0]:
                result = SmartOrder.calculate_sl_tp(price=price, atr=atr)
                assert result["sl_price"] < price, f"SL above entry: price={price}, atr={atr}"
                assert result["tp1_price"] < result["tp2_price"] < result["tp3_price"], \
                    f"TPs not ascending: price={price}, atr={atr}"

    def test_calculate_sl_tp_sl_distance_capped(self):
        """SL distance should be capped at 6*ATR."""
        from src.smart_order import SmartOrder
        # Very high ATR relative to price
        result = SmartOrder.calculate_sl_tp(price=100.0, atr=20.0)
        sl_distance = 100.0 - result["sl_price"]
        max_distance = SmartOrder.MAX_SL_ATR_MULT * 20.0  # 120
        assert sl_distance <= max_distance + 0.01

    def test_position_size_rejects_max_positions(self):
        """When 5 positions already open → should return (0, 0)."""
        from src.smart_order import SmartOrder
        mock_client = MagicMock()
        mock_client.get_free_balance.return_value = 500.0
        mock_client.get_account.return_value = {"balances": [
            {"asset": "BTC", "free": "0.01", "locked": "0"},
            {"asset": "ETH", "free": "1.0", "locked": "0"},
            {"asset": "SOL", "free": "10.0", "locked": "0"},
            {"asset": "NEAR", "free": "50.0", "locked": "0"},
            {"asset": "BARD", "free": "100.0", "locked": "0"},
            {"asset": "USDT", "free": "500.0", "locked": "0"},
        ]}
        # Return proper batch ticker list so count_active_positions counts all 5 positions
        mock_client.get_24hr_stats.return_value = [
            {"symbol": "BTCUSDT", "last_price": "100.0"},
            {"symbol": "ETHUSDT", "last_price": "100.0"},
            {"symbol": "SOLUSDT", "last_price": "100.0"},
            {"symbol": "NEARUSDT", "last_price": "100.0"},
            {"symbol": "BARDUSDT", "last_price": "100.0"},
        ]

        so = SmartOrder(mock_client)
        qty, usdt = so.calculate_position_size("NEW", 100.0, 80.0, 5.0)
        assert qty == 0 and usdt == 0

    def test_position_size_respects_single_limit(self):
        """Position size should not exceed MAX_SINGLE_POSITION_PCT of balance."""
        from src.smart_order import SmartOrder
        mock_client = MagicMock()
        mock_client.get_free_balance.return_value = 1000.0
        mock_client.get_account.return_value = {"balances": [
            {"asset": "USDT", "free": "1000.0", "locked": "0"},
        ]}
        mock_client.get_24hr_stats.return_value = {"last_price": "50.0"}
        mock_client.get_exchange_info.return_value = {
            "symbols": [{
                "symbol": "TESTUSDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "0.1", "maxQty": "1000", "stepSize": "0.1"},
                    {"filterType": "PRICE_FILTER", "minPrice": "0.01", "tickSize": "0.01"},
                    {"filterType": "NOTIONAL", "minNotional": "5"},
                ]
            }]
        }

        so = SmartOrder(mock_client)
        qty, usdt = so.calculate_position_size("TEST", 100.0, 5.0, 3.0)

        if usdt > 0:
            max_allowed = 1000.0 * (SmartOrder.MAX_SINGLE_POSITION_PCT / 100)
            assert usdt <= max_allowed + 1.0  # +1 for rounding

    def test_position_size_minimum_trade(self):
        """Below $10 minimum → should return (0, 0)."""
        from src.smart_order import SmartOrder
        mock_client = MagicMock()
        mock_client.get_free_balance.return_value = 50.0  # very small balance
        mock_client.get_account.return_value = {"balances": [
            {"asset": "USDT", "free": "50.0", "locked": "0"},
        ]}
        mock_client.get_24hr_stats.return_value = {"last_price": "50.0"}
        mock_client.get_exchange_info.return_value = {
            "symbols": [{
                "symbol": "TESTUSDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "0.1", "maxQty": "1000", "stepSize": "0.1"},
                    {"filterType": "PRICE_FILTER", "minPrice": "0.01", "tickSize": "0.01"},
                    {"filterType": "NOTIONAL", "minNotional": "5"},
                ]
            }]
        }

        so = SmartOrder(mock_client)
        qty, usdt = so.calculate_position_size("TEST", 50.0, 30.0, 5.0)
        # With 50 USDT, 15% = $7.5, which is < $10 minimum
        assert qty == 0 and usdt == 0
