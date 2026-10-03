"""WO-1011 (10/3): StateDB trailing target SL — proactive enforcement.

Evidence chain (PENGUUSDT, cron-scan.log + audit, 10/3):
  01:00-02:30  every 10-min event tick logged `DynamicGate: BYPASS —
               event trigger: exit:PENGUUSDT hold_expiry 53.5h` — the
               trigger fired correctly and admitted the full scan.
  every round  `Found 27 opportunities` -> `0 opportunities after
               adapted threshold (77)` -> NO_OPPORTUNITIES -> the ctx
               None early-return in cmd_cron_scan skipped
               _step_exit_positions entirely: the exit step never ran.
  meanwhile    evaluate_one / scan_exit_triggers never read
               portfolio.stop_loss — the trailing target (raised to
               0.009725 = -5%) was invisible to the decision core, which
               only compares pnl_pct against the FIXED -sl_pct line.
  02:42:03     the position finally settled through the guardian-clamped
               OCO floor 0.008808 at -14.16% — ~9pp worse than target.

Verdict: BUG (not a scan-interval blind spot). Two defects fixed:
  A) scan_orchestrator: both early-return rounds (starved AND
     no-opportunity) now run _step_exit_positions before returning.
  B) exit_check: evaluate_exits carries portfolio.stop_loss;
     evaluate_one enforces `price <= stop_loss` (kind=stop_loss) BEFORE
     the fixed -sl_pct line; scan_exit_triggers mirrors it so the
     10-min event tick also sees a pure floor breach.

Double-trigger safety (unchanged semantics, under test): execute_exit
keeps its OCO-aware cancel-then-sell chain — resting exchange SL legs
are cancelled BEFORE the market sell, so the engine exit and the
exchange floor can never both fill. WO-1009-③ dust pre-flight stays
first in the chain.
"""

import sys
import sqlite3
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.state_db import get_state_db  # noqa: E402
from src.exit_check import (  # noqa: E402
    evaluate_one, evaluate_exits, execute_exit, scan_exit_triggers)
from tests.test_wo0931_exit_check import ExitFakeClient  # noqa: E402

NOW = 1791000000.0

# the live PENGU shape: entry 0.010237, target trailed to 0.009725 (-5%)
POS = {"symbol": "PENGUUSDT", "quantity": 428.386,
       "entry_price": 0.010237, "opened_at": "2026-10-01T00:42:03"}


@pytest.fixture
def db():
    d = get_state_db()
    d.kv_set("ledger:shadow:bootstrap_ts", time.time() - 86400)
    yield d


# ── (B) decision core: trailing floor beats the fixed -sl_pct line ──────

class TestEvaluateOneTrailing:
    def test_floor_breach_exits_before_fixed_sl(self):
        # -5.2% pnl with sl_pct=8: fixed line NOT hit, floor IS (live case)
        # hold young so ONLY the floor path can fire (live case had
        # hold_expiry also true — priority: hold > tp > floor > fixed sl)
        pos = {**POS, "stop_loss": 0.009725}
        dec = evaluate_one(pos, 0.009666, 30.0,
                           tp_pct=15.0, sl_pct=8.0, hold_hours=96.0,
                           include_momentum=False, now=NOW)
        assert dec and dec["kind"] == "stop_loss" and dec["auto"]
        assert "trailing stop breach" in dec["reason"]
        assert "0.009725" in dec["reason"]

    def test_floor_above_entry_locks_profit(self):
        # trailed floor above entry: breach with POSITIVE pnl still exits
        pos = {**POS, "stop_loss": 0.010500}
        dec = evaluate_one(pos, 0.010400, 10.0,
                           tp_pct=15.0, sl_pct=8.0, hold_hours=48.0,
                           include_momentum=False, now=NOW)
        assert dec and dec["kind"] == "stop_loss"
        assert dec["pnl_pct"] > 0

    def test_no_stop_loss_key_unchanged(self):
        # backtest replays / legacy holdings: absent key -> old semantics
        dec = evaluate_one(POS, 0.009469, 30.0,   # -7.5%: above fixed line
                           tp_pct=15.0, sl_pct=8.0, hold_hours=96.0,
                           include_momentum=False, now=NOW)
        assert dec is None
        dec2 = evaluate_one(POS, 0.009200, 30.0,  # -10.1%: fixed line
                            tp_pct=15.0, sl_pct=8.0, hold_hours=96.0,
                            include_momentum=False, now=NOW)
        assert dec2 and "stop loss" in dec2["reason"] \
            and "trailing" not in dec2["reason"]

    def test_zero_stop_loss_treated_absent(self):
        dec = evaluate_one({**POS, "stop_loss": 0}, 0.009469, 30.0,
                           tp_pct=15.0, sl_pct=8.0, hold_hours=96.0,
                           include_momentum=False, now=NOW)
        assert dec is None


# ── (B) evaluate_exits reads the real column ────────────────────────────

class TestEvaluateExitsReadsColumn:
    def test_db_row_with_floor_triggers_exit(self, db):
        conn = db._get_conn()
        conn.execute("DELETE FROM portfolio WHERE symbol='XUSDT'")
        conn.execute(
            "INSERT INTO portfolio (symbol, quantity, entry_price, "
            "strategy, opened_at, stop_loss) "
            "VALUES ('XUSDT', 10, 100.0, 't', ?, 97.5)",
            (time.strftime("%Y-%m-%dT%H:%M:%S",
                           time.localtime(NOW - 30 * 3600)),))
        conn.commit()
        try:
            cli = ExitFakeClient(price=97.4)  # <= floor 97.5, pnl only -2.6%
            decs = evaluate_exits(cli, db, now=NOW, include_momentum=False)
            hits = [d for d in decs if d["symbol"] == "XUSDT"]
            assert hits and hits[0]["kind"] == "stop_loss"
            assert "trailing stop breach" in hits[0]["reason"]
        finally:
            conn.execute("DELETE FROM portfolio WHERE symbol='XUSDT'")
            conn.commit()


# ── (B) event tick trigger sees a pure floor breach ─────────────────────

class TestScanTriggerSeesFloor:
    def _mk_db(self, tmp_path, stop_loss):
        p = str(tmp_path / "t.db")
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE portfolio (symbol TEXT, quantity REAL, "
                  "entry_price REAL, opened_at TEXT, stop_loss REAL)")
        c.execute("CREATE TABLE trades (symbol TEXT, side TEXT, qty REAL, "
                  "price REAL, pnl REAL, timestamp REAL, client_order_id TEXT)")
        # hold young (2h of 48h), pnl -3% (above the fixed -8% line)
        c.execute("INSERT INTO portfolio (symbol, quantity, entry_price, "
                  "opened_at, stop_loss) VALUES ('XUSDT', 10, 100.0, ?, ?)",
                  ("2026-10-03T00:00:00", stop_loss))
        c.execute("INSERT INTO trades VALUES ('XUSDT','BUY',10,100,0,?,NULL)",
                  (NOW - 2 * 3600,))
        c.commit()
        c.close()
        return sqlite3.connect(f"file:{p}?mode=ro", uri=True)

    def test_pure_floor_breach_triggers(self, tmp_path):
        ro = self._mk_db(tmp_path, 97.5)  # price 97.0 <= floor 97.5
        try:
            hit = scan_exit_triggers(ro, ["XUSDT"], {"XUSDT": 97.0}, now=NOW)
            assert hit and "trailing_stop_breach" in hit and "97.5" in hit
        finally:
            ro.close()

    def test_no_floor_no_trigger(self, tmp_path):
        ro = self._mk_db(tmp_path, None)  # same shape, no trailing state
        try:
            hit = scan_exit_triggers(ro, ["XUSDT"], {"XUSDT": 97.0}, now=NOW)
            assert hit is None
        finally:
            ro.close()


# ── (2) engine exit never double-fills against the exchange OCO ────────

class OrderedClient(ExitFakeClient):
    """Records global call order across cancel/sell surfaces."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.order = []

    def cancel_order(self, symbol, order_id):
        self.order.append(("cancel", order_id))
        return super().cancel_order(symbol, order_id)

    def place_market_sell(self, symbol, quantity):
        self.order.append(("sell", quantity))
        return super().place_market_sell(symbol, quantity)


class TestNoOcoDoubleTrigger:
    # real PENGUUSDT exchange filters (live-verified 10/3): minNotional
    # 1.0 -> $4.14 notional IS tradable; the fake's default 5.0 would
    # trip the WO-1009-③ dust pre-flight (which must stay first-in-chain)
    PENGU_FILTERS = {"stepSize": 1.0, "minNotional": 1.0}

    def test_floor_exit_cancels_legs_then_sells_once(self, db):
        cli = OrderedClient(
            price=0.009666,
            open_orders=[
                {"symbol": "PENGUUSDT", "side": "SELL", "status": "NEW",
                 "orderId": 111, "type": "LIMIT_MAKER"},
                {"symbol": "PENGUUSDT", "side": "SELL", "status": "NEW",
                 "orderId": 112, "type": "STOP_LOSS_LIMIT"},
            ],
            balances={"PENGU": 428.0})
        decision = {**POS, "stop_loss": 0.009725, "kind": "stop_loss",
                    "auto": True, "sell_pct": 100, "price": 0.009666,
                    "qty": 428.0,
                    "pnl_pct": -5.58, "held_hours": 53.5,
                    "reason": "trailing stop breach 0.009666 <= "
                              "stop_loss 0.009725", "ts": NOW}
        cli.get_symbol_filters = lambda sym: dict(self.PENGU_FILTERS)
        out = execute_exit(cli, db, decision, now=NOW)
        assert out["status"] == "ok", out
        # exactly one market sell, after every SELL leg was cancelled
        assert [op for op in cli.order if op[0] == "sell"] == \
            [("sell", 428.0)]
        cancels = [op for op in cli.order if op[0] == "cancel"]
        assert sorted(op[1] for op in cancels) == [111, 112]
        assert cli.order.index(cancels[0]) < cli.order.index(
            [op for op in cli.order if op[0] == "sell"][0])
        # no emergency SL re-list on the success path
        assert cli.emergency_sl_calls == []


# ── (A) early-return rounds still run the exit step ────────────────────

class TestNoOpportunityRoundRunsExitStep:
    def test_early_return_branches_call_exit_step(self):
        src = open(REPO / "src" / "scan_orchestrator.py").read()
        lines = src.splitlines()

        def branch_body(anchor):
            """Lines from `anchor` down to the first bare `return` line."""
            i = next(k for k, l in enumerate(lines) if anchor in l)
            body = []
            for l in lines[i:]:
                if l.strip() == "return":
                    break
                body.append(l)
            return body

        # branch 2: no-opportunity round calls exit before its return
        no_opp = branch_body("if ctx is None:")
        assert any("_step_exit_positions(ctx)" in l for l in no_opp), \
            "no-opportunity round must run the exit step"
        # branch 1: starved/exception round likewise (first shadow-diff)
        starved = branch_body("_step_ledger_shadow_diff(None)")
        assert sum("_step_exit_positions(ctx)" in l for l in starved) == 1
        # normal chain unchanged (single run per round, never both)
        chain = ("_step_reconcile_portfolio(ctx)\n"
                 "        _step_config_guard(ctx)\n"
                 "        _step_exit_positions(ctx)\n"
                 "        _step_defense_sweep(ctx)")
        assert src.count(chain) == 1
