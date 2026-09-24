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
from unittest.mock import patch

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


def _audit_rows(action):
    from src.state_db import get_state_db
    conn = get_state_db()._get_conn()
    return conn.execute(
        "SELECT action, details FROM audit_log WHERE action = ? "
        "ORDER BY timestamp DESC LIMIT 5", (action,)).fetchall()


class TestSevereBlocksBuyLeg:
    def test_severe_sell_runs_buy_blocked_cash_back_audit_logged(self):
        sell_calls, buy_calls, close_calls = [], [], []
        opt = _mk_opt(sell_calls, buy_calls, close_calls)
        # severe band (8-10%) — real get_drawdown_action levels:
        # block_new_trades=True. Only the balance fetching is mocked.
        with patch("src.drawdown_breaker.DrawdownBreaker") as ddb:
            ddb.return_value.check_drawdown.return_value = {
                "drawdown_pct": 9.0, "tripped": False,
                "high_watermark": 460.0, "action": "HOLD", "reason": ""}
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
        # (4) audit trail
        rows = _audit_rows("SWITCH_BUY_BLOCKED_BY_DRAWDOWN")
        assert rows, "SWITCH_BUY_BLOCKED_BY_DRAWDOWN must be audit-logged"
        assert decision.get("buy_blocked_by_drawdown") is True


class TestAutoRecovery:
    def test_mild_level_lets_buy_leg_run(self):
        """Mild band (3-5%): block_new_trades=False -> full switch."""
        sell_calls, buy_calls, close_calls = [], [], []
        opt = _mk_opt(sell_calls, buy_calls, close_calls)
        with patch("src.drawdown_breaker.DrawdownBreaker") as ddb:
            ddb.return_value.check_drawdown.return_value = {
                "drawdown_pct": 4.0, "tripped": False,
                "high_watermark": 460.0, "action": "HOLD", "reason": ""}
            decision = dict(DECISION)
            assert opt._execute_switch(decision) is True
        assert sell_calls, "sell leg must still run in mild"
        assert buy_calls, "mild drawdown must NOT block the buy leg"
        assert not decision.get("buy_blocked_by_drawdown")

    def test_moderate_level_lets_buy_leg_run(self):
        """Moderate band (5-8%) without time escalation: allowed."""
        sell_calls, buy_calls, close_calls = [], [], []
        opt = _mk_opt(sell_calls, buy_calls, close_calls)
        with patch("src.drawdown_breaker.DrawdownBreaker") as ddb:
            ddb.return_value.check_drawdown.return_value = {
                "drawdown_pct": 6.0, "tripped": False,
                "high_watermark": 460.0, "action": "HOLD", "reason": ""}
            assert opt._execute_switch(dict(DECISION)) is True
        assert buy_calls, "moderate (unescalated) must not block"


class TestFailClosed:
    def test_gate_error_skips_buy_leg(self):
        """Gate check itself raises -> buy leg skipped (fail-closed)."""
        sell_calls, buy_calls, close_calls = [], [], []
        opt = _mk_opt(sell_calls, buy_calls, close_calls)
        with patch("src.drawdown_breaker.DrawdownBreaker") as ddb:
            ddb.side_effect = RuntimeError("breaker api down")
            decision = dict(DECISION)
            assert opt._execute_switch(decision) is True
        assert sell_calls, "loss-cut sell still runs"
        assert buy_calls == [], "buy leg must fail closed on gate error"
        assert decision.get("buy_blocked_by_drawdown") is True
