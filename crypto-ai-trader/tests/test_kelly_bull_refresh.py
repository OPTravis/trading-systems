"""Unit tests for the #27 bull-refresh bypass (CONFIRMED_BULL deadlock).

Scenario: warming-escape budget exhausted (8/8 in 30d) while the BTC gate
tier is CONFIRMED_BULL and the rolling-window verdict is stale (bear-tail
losers; window frozen since 08-19 -> no new outcomes). Verify:
  - bull + 7d budget available  -> exploration-sized entry (1%-2%), tagged
  - bull + 7d budget exhausted  -> hard block
  - no bull + cap exhausted     -> hard block (pre-#27 behaviour preserved)
  - low score (<70) + bull      -> hard block (score gate respected)
"""
from unittest.mock import patch

from src.kelly_sizer import (
    BULL_REFRESH_CAP_7D,
    EXPLORATION_MAX_PCT,
    EXPLORATION_MIN_PCT,
    KellyPositionSizer,
)

# 31 closed trades, ordered most-recent-first: 19 recent losses (bear tail
# + fresh bull window) followed by 12 older wins. Full-history win rate
# 12/31 = 38.7% -> HIGH confidence (>=30); CONFIRMED_BULL 20-window ->
# 1/20 = 5% -> Kelly strongly negative. Same fixture style as
# test_kelly_regime_escape.py.
FAKE_TRADES = (
    [{"symbol": "TESTUSDT", "pnl": -4.0, "is_win": 0, "strategy": "dca"}] * 19
    + [{"symbol": "TESTUSDT", "pnl": 3.0, "is_win": 1, "strategy": "dca"}] * 12
)


def _run(bull=True, refresh_used=0, score=76, balance=398.64):
    k = KellyPositionSizer()
    k.db = object()  # truthy -> DB paths taken, all DB calls mocked below
    with patch.object(
        KellyPositionSizer, "_get_trade_history", return_value=list(FAKE_TRADES)
    ), patch.object(
        KellyPositionSizer, "_exploration_entries_last_30d", return_value=8
    ), patch.object(
        KellyPositionSizer, "_bull_refresh_entries_last_7d", return_value=refresh_used
    ), patch.object(
        KellyPositionSizer, "_btc_confirmed_bull", return_value=bull
    ), patch.object(
        KellyPositionSizer, "_detect_regime_improving", return_value=False
    ):
        return k.get_position_size(
            symbol="TESTUSDT",
            balance=balance,
            stop_loss_pct=5.0,
            take_profit_pct=6.0,
            signal_score=score,
            use_historical=True,
        )


def test_bull_refresh_opens_probe():
    res = _run(bull=True, refresh_used=3)
    assert res["is_exploration"] is True
    assert EXPLORATION_MIN_PCT <= res["position_pct"] <= EXPLORATION_MAX_PCT
    assert "bull regime refresh" in res["confidence"]
    # 4-decimal rounding may land just under $5; trade_executor bumps
    # exploration invest_amount to the $5 Binance min explicitly.
    assert res["position_pct"] * 398.64 >= 4.95


def test_bull_refresh_7d_cap_blocks():
    res = _run(bull=True, refresh_used=BULL_REFRESH_CAP_7D)
    assert res["position_pct"] == 0.0
    assert res["is_exploration"] is False
    assert "7d cap" in res["reason"]


def test_no_bull_still_blocks():
    res = _run(bull=False)
    assert res["position_pct"] == 0.0
    assert res["is_exploration"] is False


def test_low_score_still_blocks():
    res = _run(bull=True, score=65)
    assert res["position_pct"] == 0.0
    assert res["is_exploration"] is False
