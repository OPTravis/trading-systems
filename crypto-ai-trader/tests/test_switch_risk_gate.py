"""Order-3 regression tests (2026-09-20): switch pre-buy risk gate.

The 9/20 01:31 BCH->SUI switch ran with NO risk check — PositionOptimizer
was never wired to RiskManager. Evidence Travis saw (BLOCK logged after
the fills, citing the already-sold BCH at 0.81) came from the entry-path
pre_trade_check running later in the same scan round.

Fix: _execute_switch now gates the switch BEFORE the sell with a
correlation check on the POST-switch portfolio (current - from + to).
Fail-closed; blocked switches audit-log SWITCH_RISK_BLOCK.
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


def _mk_opt(risk_manager, sell_called):
    opt = object.__new__(PositionOptimizer)
    opt.bc = SimpleNamespace(
        get_symbol_filters=lambda s: {
            "minQty": 0.0, "minNotional": 0.0, "stepSize": 1.0},
        get_ticker_price=lambda symbol=0, **k: 100.0,
        cancel_all_orders=lambda s: [],
        get_account=lambda: {"balances": []},
        place_market_sell=lambda symbol, quantity: (
            sell_called.append(symbol) or {"orderId": 9, "status": "FILLED"}),
    )
    opt.risk_manager = risk_manager
    return opt


DECISION = {
    "from_symbol": "BCHUSDT", "to_symbol": "SUIUSDT", "from_value": 50.0,
}


def _portfolio():
    p = SimpleNamespace()
    p.get_all_positions = lambda: [
        {"symbol": "BCHUSDT", "quantity": 0.05, "entry_price": 500.0},
        {"symbol": "NEARUSDT", "quantity": 2.0, "entry_price": 3.0},
    ]
    return p


class TestSwitchRiskGate:
    def test_blocked_correlation_aborts_before_sell(self):
        sell_calls = []
        opt = _mk_opt(SimpleNamespace(
            correlation_risk=SimpleNamespace(
                check_new_position=lambda new, held: {
                    "allowed": False, "reason": "corr 0.81 with NEAR",
                    "max_correlation": 0.81})), sell_calls)
        opt.portfolio = _portfolio()
        assert opt._execute_switch(dict(DECISION)) is False
        assert sell_calls == [], "blocked switch must not sell"

    def test_allowed_correlation_proceeds_to_sell(self):
        sell_calls = []
        opt = _mk_opt(SimpleNamespace(
            correlation_risk=SimpleNamespace(
                check_new_position=lambda new, held: {
                    "allowed": True, "reason": "corr ok",
                    "max_correlation": 0.2})), sell_calls)
        opt.portfolio = _portfolio()
        opt._last_switch_time = {}
        opt._save_switch_times = lambda: None
        # sell proceeds; buy will fail on the stub (no place_market_buy)
        # — gate already passed, that is what we assert here
        opt._execute_switch(dict(DECISION))
        assert sell_calls == ["BCHUSDT"]

    def test_gate_uses_post_switch_holding_list(self):
        """The correlation input must EXCLUDE the symbol being sold."""
        seen = {}

        def spy(new, held):
            seen["new"], seen["held"] = new, held
            return {"allowed": True, "reason": "", "max_correlation": 0.1}

        opt = _mk_opt(SimpleNamespace(correlation_risk=SimpleNamespace(
            check_new_position=spy)), [])
        opt.portfolio = _portfolio()
        opt._last_switch_time = {}
        opt._save_switch_times = lambda: None
        opt._execute_switch(dict(DECISION))
        assert seen["new"] == "SUI"
        assert seen["held"] == ["NEAR"], "sold symbol must not be in the list"

    def test_gate_error_fails_closed(self):
        sell_calls = []
        def _boom(new, held):
            raise RuntimeError("corr api down")
        opt = _mk_opt(SimpleNamespace(correlation_risk=SimpleNamespace(
            check_new_position=_boom)), sell_calls)
        opt.portfolio = _portfolio()
        assert opt._execute_switch(dict(DECISION)) is False
        assert sell_calls == []

    def test_no_risk_manager_keeps_legacy_behavior(self):
        """risk_manager=None (older call sites) still switches."""
        sell_calls = []
        opt = _mk_opt(None, sell_calls)
        opt.portfolio = _portfolio()
        opt._last_switch_time = {}
        opt._save_switch_times = lambda: None
        opt._execute_switch(dict(DECISION))
        assert sell_calls == ["BCHUSDT"]

    def test_blocked_switch_audits(self, caplog, monkeypatch):
        audited = []
        fake_db = SimpleNamespace(
            audit_log=lambda action, details, source="": audited.append(
                (action, details)))
        monkeypatch.setitem(sys.modules, "src.state_db",
                            SimpleNamespace(get_state_db=lambda: fake_db))
        opt = _mk_opt(SimpleNamespace(
            correlation_risk=SimpleNamespace(
                check_new_position=lambda new, held: {
                    "allowed": False, "reason": "corr 0.9",
                    "max_correlation": 0.9})), [])
        opt.portfolio = _portfolio()
        with caplog.at_level(logging.ERROR):
            opt._execute_switch(dict(DECISION))
        assert audited and audited[0][0] == "SWITCH_RISK_BLOCK"
        assert "SWITCH_RISK_BLOCK" in caplog.text
