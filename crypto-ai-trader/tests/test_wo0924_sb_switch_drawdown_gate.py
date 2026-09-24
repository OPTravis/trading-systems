"""WO-0924-sb: switch buy-leg drawdown gate (exits-only semantics).

Evidence (2026-09-24): stepwise drawdown escalated to severe at
16:03:39 (block_new_trades=true). PositionOptimizer._execute_switch
carried NO drawdown gate — 17:01 BCH->LTC, 17:02 NEAR->DASH and 18:13
INJ->RAY all ran both legs during severe. (The 16:02 HBAR->BTC switch
predates the escalation by 50s and was legal moderate-band behavior.)

Fix semantics (Leo/Travis spec): the sell leg always runs (loss-cut
priority); while get_drawdown_action().block_new_trades is true the
buy leg is skipped, proceeds return to cash via portfolio bookkeeping,
and the event is audit-logged as SWITCH_BUY_BLOCKED_BY_DRAWDOWN.
Auto-recovers when the level drops below the blocking band. Gate
errors fail closed (buy leg skipped) — same semantics as
risk_manager.pre_trade_check's drawdown rule.
"""
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


def _mk_opt(sell_calls, buy_calls, close_calls):
    opt = object.__new__(PositionOptimizer)
    opt.bc = SimpleNamespace(
        get_symbol_filters=lambda s: {
            "minQty": 0.0, "minNotional": 0.0, "stepSize": 1.0},
        get_ticker_price=lambda symbol=0, **k: 100.0,
        cancel_all_orders=lambda s: [],
        get_account=lambda: {"balances": [
            {"asset": "USDT", "free": "50.0", "locked": "0"},
            {"asset": "BCH", "free": "0.05", "locked": "0"}]},
        get_free_balance=lambda a="USDT", **k: 50.0,
        place_market_sell=lambda symbol, quantity: (
            sell_calls.append((symbol, quantity))
            or {"orderId": 77, "status": "FILLED"}),
        place_market_buy=lambda symbol, quoteOrderQty=None, quantity=None: (
            buy_calls.append((symbol, quoteOrderQty, quantity))
            or {"orderId": 88, "status": "FILLED"}),
    )
    opt.risk_manager = None  # no correlation gate in these tests
    portfolio = SimpleNamespace()
    portfolio.get_all_positions = lambda: [
        {"symbol": "BCHUSDT", "quantity": 0.05, "entry_price": 500.0},
    ]

    def _close(symbol, close_price=None, exit_reason=None,
               client_order_id=None):
        close_calls.append(
            {"symbol": symbol, "close_price": close_price,
             "exit_reason": exit_reason, "order_id": client_order_id})
        return {"symbol": symbol, "closed": True}

    portfolio.close_position = _close
    opt.portfolio = portfolio
    opt._last_switch_time = {}
    opt._save_switch_times = lambda: None
    return opt


DECISION = {"from_symbol": "BCHUSDT", "to_symbol": "LTCUSDT",
            "from_value": 50.0}


def _auth_dd(pct, level="severe", escalated=True):
    """Patch the authoritative drawdown source (WO-0924-gatefix):
    the gate reads state_db.drawdown_get() and evaluates read-only.
    level_entry_time pinned to 'now' so time-escalation (2h in
    moderate) does not fire unless the test asks for it."""
    import time as _t
    from unittest.mock import MagicMock
    sdb = MagicMock()
    sdb.drawdown_get.return_value = {
        "high_watermark": 460.0, "current_drawdown_pct": pct,
        "max_drawdown_pct": pct, "tripped_count": 0,
        "tripped_at": None, "reset_at": None, "history": []}
    sdb.kv_get.return_value = {
        "current_level": level, "level_entry_time": _t.time(),
        "time_in_current_level": 0.0, "escalated": escalated}
    sdb.kv_set = MagicMock()
    return patch("src.state_db.get_state_db", return_value=sdb), sdb


def _audit_rows(action):
    """Read the conftest-isolated real DB (NOT the gate's mocked
    get_state_db) — the audit write happens through whichever
    get_state_db is live INSIDE the gate context, so we instead
    record via the mock and assert the call itself."""
    return []  # replaced by _audit_calls below


def _audit_calls(mock_sdb, action):
    """Collect audit_log(action, ...) invocations from the mock."""
    calls = []
    for c in mock_sdb.audit_log.call_args_list:
        if c.args and c.args[0] == action:
            calls.append(c)
    return calls


class TestSevereBlocksBuyLeg:
    def test_severe_sell_runs_buy_blocked_cash_back_audit_logged(self):
        sell_calls, buy_calls, close_calls = [], [], []
        opt = _mk_opt(sell_calls, buy_calls, close_calls)
        # severe band (8-10%) — authoritative table says 9.0%.
        # The gate must evaluate READ-ONLY (no kv state writes).
        gate, sdb = _auth_dd(9.0)
        with gate:
            decision = dict(DECISION)
            assert opt._execute_switch(decision) is True

        # (1) sell leg ran
        assert sell_calls and sell_calls[0][0] == "BCHUSDT"
        # (2) buy leg blocked
        assert buy_calls == [], "buy leg must be blocked during severe"
        # (3) proceeds returned to cash via portfolio bookkeeping
        assert len(close_calls) == 1
        assert close_calls[0]["symbol"] == "BCHUSDT"
        assert close_calls[0]["exit_reason"] == "switch"
        assert close_calls[0]["order_id"] == "77"
        # (4) audit trail — the SWITCH_BUY_BLOCKED_BY_DRAWDOWN call
        # reached the state-db layer (recorded on the gate's mock)
        rows = _audit_calls(sdb, "SWITCH_BUY_BLOCKED_BY_DRAWDOWN")
        assert rows, "SWITCH_BUY_BLOCKED_BY_DRAWDOWN must be audit-logged"
        assert decision.get("buy_blocked_by_drawdown") is True


class TestAutoRecovery:
    def test_mild_level_lets_buy_leg_run(self):
        """Mild band (3-5%): block_new_trades=False -> full switch."""
        sell_calls, buy_calls, close_calls = [], [], []
        opt = _mk_opt(sell_calls, buy_calls, close_calls)
        gate, sdb = _auth_dd(4.0, level="mild", escalated=False)
        with gate:
            decision = dict(DECISION)
            assert opt._execute_switch(decision) is True
        assert sell_calls, "sell leg must still run in mild"
        assert buy_calls, "mild drawdown must NOT block the buy leg"
        assert not decision.get("buy_blocked_by_drawdown")

    def test_moderate_level_lets_buy_leg_run(self):
        """Moderate band (5-8%) without time escalation: allowed."""
        sell_calls, buy_calls, close_calls = [], [], []
        opt = _mk_opt(sell_calls, buy_calls, close_calls)
        gate, sdb = _auth_dd(6.0, level="moderate", escalated=False)
        with gate:
            assert opt._execute_switch(dict(DECISION)) is True
        assert buy_calls, "moderate (unescalated) must not block"


class TestFailClosed:
    def test_gate_error_skips_buy_leg(self):
        """Gate check itself raises -> buy leg skipped (fail-closed)."""
        sell_calls, buy_calls, close_calls = [], [], []
        opt = _mk_opt(sell_calls, buy_calls, close_calls)
        with patch("src.state_db.get_state_db",
                   side_effect=RuntimeError("state db down")):
            decision = dict(DECISION)
            assert opt._execute_switch(decision) is True
        assert sell_calls, "loss-cut sell still runs"
        assert buy_calls == [], "buy leg must fail closed on gate error"
        assert decision.get("buy_blocked_by_drawdown") is True


class TestDriftImmunity:
    """WO-0924-gatefix core scenario: the 19:25 incident replay.

    Sticky escalated=severe in state, but the caller's drawdown number
    reads LOW (4.06% — the drifted self-computed equity of the old
    gate). The gate must NOT downgrade, NOT allow the buy leg, and
    NOT clear the sticky escalation."""

    def test_drifted_low_pct_with_sticky_severe_still_blocks(self):
        sell_calls, buy_calls, close_calls = [], [], []
        opt = _mk_opt(sell_calls, buy_calls, close_calls)
        # authoritative table still says severe territory would be
        # 5.43%; simulate the drifted-input scenario directly at the
        # action layer: sticky escalated severe state + low pct input
        from src.stepwise_drawdown import get_drawdown_action
        with patch("src.state_db.get_state_db") as sdb:
            sdb.return_value.kv_get.return_value = {
                "current_level": "severe",
                "level_entry_time": 1790237019.79,
                "time_in_current_level": 0.0,
                "escalated": True}
            sdb.return_value.kv_set = MagicMock()
            action = get_drawdown_action(4.06, read_only=True)
        # sticky honored: still severe, still blocking
        assert action["level"] == "severe"
        assert action["block_new_trades"] is True
        # and the state was NOT written (no recovery clear, no transition)
        sdb.return_value.kv_set.assert_not_called()

    def test_read_only_never_writes_on_transition(self):
        """Even when the input level differs from state (transition
        would fire), read_only must not persist anything."""
        from src.stepwise_drawdown import get_drawdown_action
        with patch("src.state_db.get_state_db") as sdb:
            sdb.return_value.kv_get.return_value = {
                "current_level": "mild",
                "level_entry_time": 1790237019.79,
                "time_in_current_level": 0.0,
                "escalated": False}
            sdb.return_value.kv_set = MagicMock()
            action = get_drawdown_action(9.0, read_only=True)
        assert action["level"] == "severe"
        assert action["block_new_trades"] is True
        sdb.return_value.kv_set.assert_not_called()
