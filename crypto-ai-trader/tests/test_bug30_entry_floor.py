#!/usr/bin/env python3
"""bug#30 regression tests: $6 global entry floor.

Covers:
  1. Exploration probe below $6 → bumped to exactly $6 (was $5 pre-fix).
  2. Non-exploration Kelly below $10 → still bumped to $6 (existing bug#24-era
     behavior preserved, not regressed by the floor change).
  3. execute_auto_trade notional target includes the $6 floor (source pin).
"""
import pathlib
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, "/app/data/所有对话/主对话/trading-systems/crypto-ai-trader")

from src import trade_executor as te  # noqa: E402

REPO = pathlib.Path("/app/data/所有对话/主对话/trading-systems/crypto-ai-trader")


def _kelly_result(exploration: bool, pct: float) -> dict:
    return {
        "position_pct": pct,
        "confidence": "high",
        "is_exploration": exploration,
        "win_rate": 0.55,
        "reward_risk": 1.5,
        "reason": "test",
    }


def _patch_sizing_deps(monkeypatch, exploration: bool, pct: float):
    kelly_mock = MagicMock()
    kelly_mock.get_position_size.return_value = _kelly_result(exploration, pct)
    kelly_mock.adjust_for_portfolio.side_effect = lambda r, **kw: r
    fee_mock = MagicMock()
    fee_mock.get_effective_fees.return_value = {"taker_fee": 0.001}
    monkeypatch.setattr(
        "src.kelly_sizer.KellyPositionSizer", MagicMock(return_value=kelly_mock)
    )
    monkeypatch.setattr(
        "src.fee_optimizer.FeeOptimizer", MagicMock(return_value=fee_mock)
    )
    monkeypatch.setattr("src.state_db.get_state_db", lambda: MagicMock())
    return kelly_mock


def test_exploration_probe_bumped_to_6(monkeypatch):
    """Exploration sizing under $6 → invest_amount == 6.0 (bug#30 core fix)."""
    bal = 382.0
    # 1.2% of $382 ≈ $4.58 after fee reserve — old code would leave this at $5.
    _patch_sizing_deps(monkeypatch, exploration=True, pct=0.012)

    sizing = te._compute_kelly_sizing(
        client=None, symbol="TESTUSDT", usdt_bal=bal, score=73,
        stop_loss_pct=5.0, tp_levels=[{"pct": 8.0}],
        active_positions=1, max_positions=5, surge_alert_level="SILENCE",
    )

    assert "error" not in sizing
    assert sizing["invest_amount"] == pytest.approx(6.0)
    assert sizing["invest_pct"] == pytest.approx(6.0 / bal)
    assert sizing["is_exploration"] is True


def test_nonexploration_below_10_still_bumps_to_6(monkeypatch):
    """Kelly-active non-exploration below $10 → bumped to $6 (existing path)."""
    bal = 382.0
    # 2.4% → ~$9.15 after fee reserve, under the $10 non-exploration floor.
    _patch_sizing_deps(monkeypatch, exploration=False, pct=0.024)

    sizing = te._compute_kelly_sizing(
        client=None, symbol="TESTUSDT", usdt_bal=bal, score=88,
        stop_loss_pct=5.0, tp_levels=[{"pct": 8.0}],
        active_positions=1, max_positions=5, surge_alert_level="SILENCE",
    )

    assert "error" not in sizing
    assert sizing["invest_amount"] == pytest.approx(6.0)
    assert sizing["is_exploration"] is False


def test_entry_notional_floor_in_source():
    """Source pin: _notional_target max() must include the $6 entry floor."""
    text = (REPO / "src" / "trade_executor.py").read_text(encoding="utf-8")
    assert "_ENTRY_VALUE_FLOOR = 6.0" in text
    assert "_ENTRY_VALUE_FLOOR,\n    )" in text
    assert "_min_invest = 6 if is_exploration else 10" in text
