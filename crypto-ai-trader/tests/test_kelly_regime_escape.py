"""Unit tests for the Kelly deadlock-escape (regime warming exploration).

Scenario: global rolling win-rate is HIGH-confidence negative (the exact
state that froze the system on 2026-08-18/19). Verify that:
  - regime_improving=False  -> hard block (unchanged behaviour)
  - regime_improving=True   -> tiny exploratory position (1%-2%), tagged
"""
from unittest.mock import patch

from src.kelly_sizer import (
    BINANCE_MIN_NOTIONAL,
    EXPLORATION_MAX_PCT,
    EXPLORATION_MIN_PCT,
    KellyPositionSizer,
)

# 31 closed trades, 12 wins / 19 losses -> win_rate 38.7%, HIGH confidence
FAKE_TRADES = (
    [{"symbol": "TESTUSDT", "pnl": 3.0, "is_win": 1, "strategy": "dca"}] * 12
    + [{"symbol": "TESTUSDT", "pnl": -4.0, "is_win": 0, "strategy": "dca"}] * 19
)


def _sizer():
    k = KellyPositionSizer()
    k.db = object()  # truthy so _get_trade_history uses the DB path
    return k


def _run(regime_improving):
    k = _sizer()
    with patch.object(KellyPositionSizer, "_get_trade_history", return_value=list(FAKE_TRADES)), \
         patch.object(KellyPositionSizer, "_detect_regime_improving", return_value=regime_improving), \
         patch.object(KellyPositionSizer, "_btc_confirmed_bull", return_value=False):
        return k.get_position_size(
            symbol="TESTUSDT",
            balance=390.0,
            stop_loss_pct=5.0,
            take_profit_pct=6.0,
            signal_score=76,
            use_historical=True,
            regime_improving=regime_improving,
        )


def test_regime_cold_still_blocks():
    res = _run(regime_improving=False)
    assert res["position_pct"] == 0.0
    assert res["is_exploration"] is False
    assert res["confidence"] == "HIGH"


def test_regime_warming_allows_tiny_exploration():
    res = _run(regime_improving=True)
    assert res["is_exploration"] is True
    assert EXPLORATION_MIN_PCT <= res["position_pct"] <= EXPLORATION_MAX_PCT
    # on $390 balance the $5 min-notional floor dominates
    assert abs(res["position_pct"] - round(BINANCE_MIN_NOTIONAL / 390.0, 4)) < 1e-9
    assert "regime warming" in res["confidence"]


def test_auto_detect_called_when_param_none():
    k = _sizer()
    with patch.object(KellyPositionSizer, "_get_trade_history", return_value=list(FAKE_TRADES)), \
         patch.object(KellyPositionSizer, "_btc_confirmed_bull", return_value=False), \
         patch.object(
             KellyPositionSizer, "_detect_regime_improving", return_value=True
         ) as detect:
        res = k.get_position_size(
            symbol="TESTUSDT",
            balance=390.0,
            stop_loss_pct=5.0,
            take_profit_pct=6.0,
            signal_score=76,
            use_historical=True,
        )
        detect.assert_called_once()
        assert res["is_exploration"] is True


def _run_capped(regime_improving, used):
    k = _sizer()
    with patch.object(KellyPositionSizer, "_get_trade_history", return_value=list(FAKE_TRADES)), \
         patch.object(KellyPositionSizer, "_btc_confirmed_bull", return_value=False), \
         patch.object(KellyPositionSizer, "_detect_regime_improving", return_value=regime_improving), \
         patch.object(KellyPositionSizer, "_exploration_entries_last_30d", return_value=used):
        return k.get_position_size(
            symbol="TESTUSDT",
            balance=390.0,
            stop_loss_pct=5.0,
            take_profit_pct=6.0,
            signal_score=76,
            use_historical=True,
            regime_improving=regime_improving,
        )


def test_escape_blocked_at_cap():
    """Slow-bleed guard: 5 exploration entries in 30d -> stay blocked."""
    from src.kelly_sizer import EXPLORATION_CAP_30D

    res = _run_capped(True, used=EXPLORATION_CAP_30D)
    assert res["position_pct"] == 0.0
    assert res["is_exploration"] is False


def test_escape_allowed_below_cap():
    from src.kelly_sizer import EXPLORATION_CAP_30D

    res = _run_capped(True, used=EXPLORATION_CAP_30D - 1)
    assert res["is_exploration"] is True
    assert "regime warming" in res["confidence"]
