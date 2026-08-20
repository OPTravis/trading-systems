"""Unit tests for the CONFIRMED_BULL regime-matched Kelly window (2026-08-20).

Leo directive: "by considering it is confirmed bull, don't miss any chances".

Scenario: all-history window is HIGH-confidence negative — dominated by
bear-market losses and currently freezing the system. The most recent
BULL_REGIME_WINDOW trades (regime-matched) are profitable. Verify that:
  - CONFIRMED_BULL -> stats come from the recent window -> Kelly > 0, sized
    normally, reason tagged with the window marker
  - non-bull -> unchanged full-window behaviour (block as before)
  - history no longer than the window -> falls back to full history
"""

from unittest.mock import patch

from src.kelly_sizer import BULL_REGIME_WINDOW, KellyPositionSizer

# 31 closed trades: 12 recent WINS (bull window), then 19 old LOSSES
# full window: 12W/19L = 38.7% -> HIGH-confidence negative (blocks today)
# bull window (20): 12W/8L = 60% -> positive Kelly
FAKE_TRADES = (
    [{"symbol": "TESTUSDT", "pnl": 3.0, "is_win": 1, "strategy": "dca"}] * 12
    + [{"symbol": "TESTUSDT", "pnl": -4.0, "is_win": 0, "strategy": "dca"}] * 19
)


def _sizer():
    k = KellyPositionSizer()
    k.db = object()  # truthy so _get_trade_history uses the DB path
    return k


def _run(bull: bool):
    k = _sizer()
    with patch.object(
        KellyPositionSizer, "_get_trade_history", return_value=list(FAKE_TRADES)
    ), patch.object(
        KellyPositionSizer, "_btc_confirmed_bull", return_value=bull
    ):
        return k.get_position_size(
            symbol="TESTUSDT",
            balance=390.0,
            stop_loss_pct=5.0,
            take_profit_pct=6.0,
            signal_score=76,
            use_historical=True,
            regime_improving=False,  # even with no escape: bull window must size
        )


def test_bull_window_sizes_position_off_recent_edge():
    r = _run(bull=True)
    assert r["position_pct"] > 0, r
    assert not r["is_exploration"]
    assert r["win_rate"] == 12 / BULL_REGIME_WINDOW, r  # 60% from window, not 38.7%
    assert "CONFIRMED_BULL window" in r["reason"], r
    assert r["confidence"] == "HIGH", r  # 31 total >= 30


def test_bear_full_window_still_blocks_without_escape():
    r = _run(bull=False)
    assert r["position_pct"] == 0.0, r
    assert r["win_rate"] == round(12 / 31, 4), r


def test_bull_but_short_history_falls_back_to_full():
    k = _sizer()
    # 7 trades total <= BULL_REGIME_WINDOW -> no slicing, stats from all 7 (4W/3L)
    short = (
        [{"symbol": "TESTUSDT", "pnl": 3.0, "is_win": 1, "strategy": "dca"}] * 4
        + [{"symbol": "TESTUSDT", "pnl": -4.0, "is_win": 0, "strategy": "dca"}] * 3
    )
    with patch.object(
        KellyPositionSizer, "_get_trade_history", return_value=list(short)
    ), patch.object(KellyPositionSizer, "_btc_confirmed_bull", return_value=True):
        r = k.get_position_size(
            symbol="TESTUSDT",
            balance=390.0,
            stop_loss_pct=5.0,
            take_profit_pct=6.0,
            signal_score=76,
            use_historical=True,
        )
    assert r["win_rate"] == round(4 / 7, 4), r
    assert "CONFIRMED_BULL window" not in r["reason"], r  # no marker when unsliced
