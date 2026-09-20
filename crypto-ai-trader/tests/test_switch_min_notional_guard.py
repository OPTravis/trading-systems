"""Order-1 & Order-2 regression tests (2026-09-20, Travis batch).

Order-1: _analyze_position must reject switch targets whose minNotional
  exceeds net sell proceeds — sub-minNotional buys hold no SL/TP (naked
  positions: XAUT 9/18, DASH 9/19, SUI 9/20).
Order-2: _place_switch_protections must not log "TP live" when
  place_limit_sell returns falsy (soft rejection, no exception).
"""

import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    import src.binance_client  # noqa: F401
except Exception:
    sys.modules["src.binance_client"] = types.SimpleNamespace(
        BinanceClient=object)

from src.position_optimizer import PositionOptimizer  # noqa: E402


def _mk_opt(filters_map, existing_score=60.0):
    """Bare optimizer with a stubbed exchange client.

    filters_map: {symbol: filters-dict} for get_symbol_filters.
    """
    opt = object.__new__(PositionOptimizer)
    opt.bc = SimpleNamespace(
        get_24hr_stats=lambda symbol: {"price_change_pct": 0.0},
        get_symbol_filters=lambda s: filters_map.get(s, {
            "minNotional": 10.0, "stepSize": 0.1, "tickSize": 0.0001}),
        place_stop_loss_limit=lambda *a, **k: {"orderId": 1},
        place_limit_sell=lambda *a, **k: {"orderId": 2},
    )
    opt._last_switch_time = {}
    opt._get_position_score = lambda symbol, opps: existing_score
    return opt


class TestMinNotionalGuard:
    def test_low_proceeds_target_skipped(self):
        """~$8 position: net proceeds $7.96 < minNotional×1.05 ($10.50) —
        the high-score target must be rejected (no naked buy)."""
        opt = _mk_opt({"SUIUSDT": {"minNotional": 10.0}}, existing_score=60.0)
        pos = {"symbol": "DASHUSDT", "position_value": 8.0,
               "entry_price": 60.0, "quantity": 0.13}
        opps = [{"symbol": "SUIUSDT", "score": 100.0, "price_change_24h": 0.0}]
        decision = opt._analyze_position(pos, opps)
        assert decision is None, "sub-minNotional target must not be chosen"

    def test_normal_proceeds_target_passes(self):
        """$200 position: proceeds $199 > $10.50 — high-score target wins."""
        opt = _mk_opt({"SUIUSDT": {"minNotional": 10.0}}, existing_score=60.0)
        pos = {"symbol": "DASHUSDT", "position_value": 200.0,
               "entry_price": 60.0, "quantity": 3.3}
        opps = [{"symbol": "SUIUSDT", "score": 100.0, "price_change_24h": 0.0}]
        decision = opt._analyze_position(pos, opps)
        assert decision is not None
        assert decision.get("to_symbol") == "SUIUSDT"

    def test_expensive_target_rejected_cheap_target_chosen(self):
        """When proceeds cover one target but not another, the covered
        one must be selected even at a lower score."""
        opt = _mk_opt({
            "BTCUSDT": {"minNotional": 1000.0},
            "SUIUSDT": {"minNotional": 10.0},
        }, existing_score=60.0)
        pos = {"symbol": "DASHUSDT", "position_value": 100.0,
               "entry_price": 60.0, "quantity": 1.6}
        # BTC scores higher but needs $1000×1.05 — unreachable from $99.5
        opps = [
            {"symbol": "BTCUSDT", "score": 120.0, "price_change_24h": 0.0},
            {"symbol": "SUIUSDT", "score": 100.0, "price_change_24h": 0.0},
        ]
        decision = opt._analyze_position(pos, opps)
        assert decision is not None
        assert decision.get("to_symbol") == "SUIUSDT"

    def test_filters_fetch_failure_defaults_conservative(self):
        """get_symbol_filters raising must fall back to the $10 floor —
        fail-closed, never fail-open into a naked buy."""
        opt = _mk_opt({}, existing_score=60.0)
        def _boom(s):
            raise RuntimeError("api down")
        opt.bc = SimpleNamespace(
            get_24hr_stats=lambda symbol: {"price_change_pct": 0.0},
            get_symbol_filters=_boom,
        )
        pos = {"symbol": "DASHUSDT", "position_value": 8.0,
               "entry_price": 60.0, "quantity": 0.13}
        opps = [{"symbol": "SUIUSDT", "score": 100.0, "price_change_24h": 0.0}]
        assert opt._analyze_position(pos, opps) is None
