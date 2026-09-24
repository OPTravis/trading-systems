"""WO-0924 P2-③: compensator repairs funnel (guardian / reconciler).

Contract under test:
  - guardian / reconciler state writes land via ledger.record_repair
    (one LEDGER_REPAIR audit row + one REPAIR event per state mutation)
  - byte-parity of applied writes vs the legacy direct writes
  - kv 'ledger:repairs' = 0 -> callers fall back to legacy direct writes
  - funnel crash -> caller falls back too (never blocks a compensation)
  - REPAIR events never pollute the shadow traces diff
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.state_db import StateDB
from src import ledger
from src import protection_guardian as guardian
from src import portfolio_reconciler as reconciler
from src.tp_sl_tracker import save_state


def _db():
    d = StateDB()
    d._get_conn().executescript(
        "DELETE FROM kv; DELETE FROM audit_log; DELETE FROM ledger_events;"
        "DELETE FROM ledger_shadow_positions; DELETE FROM portfolio;"
        "DELETE FROM trades; DELETE FROM trade_outcomes;"
    )
    d._get_conn().commit()
    return d


def _events(db):
    return db._get_conn().execute(
        "SELECT * FROM ledger_events ORDER BY id").fetchall()


def _audits(db, action):
    return db._get_conn().execute(
        "SELECT * FROM audit_log WHERE action = ?", (action,)).fetchall()


class TestRepairsFlag(unittest.TestCase):
    def test_flag_defaults_on(self):
        db = _db()
        self.assertTrue(ledger.repairs_enabled(db))

    def test_flag_off_disables(self):
        db = _db()
        db.kv_set("ledger:repairs", 0)
        self.assertFalse(ledger.repairs_enabled(db))
        db.kv_set("ledger:repairs", 1)
        self.assertTrue(ledger.repairs_enabled(db))

    def test_flag_garbage_fails_open(self):
        db = _db()
        db.kv_set("ledger:repairs", "banana")
        self.assertTrue(ledger.repairs_enabled(db))


class TestTrackerStateParity(unittest.TestCase):
    def test_tracker_state_matches_save_state(self):
        db = _db()
        tps = [{"orderId": "tp1", "price": "110", "origQty": "1"}]
        sl = {"orderId": "sl1", "price": "95"}
        res = ledger.record_repair({
            "kind": "tracker_state", "symbol": "AAVEUSDT",
            "source": "protection_guardian",
            "payload": {"entry": 100.0, "qty": 2.0,
                        "tp_orders": tps, "sl_order": sl}}, db=db)
        self.assertEqual(res["status"], "ok")
        save_state("AAVEUSDT2", 100.0, 2.0, tps, sl)
        got = db.kv_get("tp_sl_tracker:AAVEUSDT")
        ref = db.kv_get("tp_sl_tracker:AAVEUSDT2")
        for k in ("entry_price", "total_qty", "tp_orders", "sl_order",
                  "tp_filled", "sl_moved_after_tp"):
            self.assertEqual(got[k], ref[k], f"parity break on {k}")
        # audit + event present
        self.assertEqual(len(_audits(db, "LEDGER_REPAIR")), 1)
        self.assertEqual(len(_events(db)), 1)
        self.assertEqual(_events(db)[0]["type"], "REPAIR")

    def test_swap_ts_repair(self):
        db = _db()
        ledger.record_repair({
            "kind": "swap_ts", "symbol": "ETHUSDT",
            "source": "protection_guardian",
            "payload": {"value": {"ts": 123.0}}}, db=db)
        self.assertEqual(db.kv_get("gov:swap_ts:ETHUSDT"), {"ts": 123.0})
        self.assertEqual(len(_audits(db, "LEDGER_REPAIR")), 1)
        self.assertEqual(_events(db)[0]["type"], "REPAIR")


class TestGuardianFunnels(unittest.TestCase):
    def test_track_via_ledger(self):
        db = _db()
        guardian._track("TSTUSDT", 100.0, 2.0,
                        [{"orderId": "tp1"}], {"orderId": "sl1"})
        st = db.kv_get("tp_sl_tracker:TSTUSDT")
        self.assertEqual(st["entry_price"], 100.0)
        self.assertEqual(st["total_qty"], 2.0)
        self.assertEqual(st["tp_filled"], [False])
        ev = _events(db)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["type"], "REPAIR")
        self.assertEqual(ev[0]["source"], "protection_guardian")
        self.assertEqual(len(_audits(db, "LEDGER_REPAIR")), 1)

    def test_track_flag_off_legacy(self):
        db = _db()
        db.kv_set("ledger:repairs", 0)
        guardian._track("TSTUSDT", 100.0, 2.0, [], None)
        st = db.kv_get("tp_sl_tracker:TSTUSDT")
        self.assertEqual(st["entry_price"], 100.0)
        self.assertEqual(len(_events(db)), 0)      # no REPAIR event
        self.assertEqual(len(_audits(db, "LEDGER_REPAIR")), 0)

    def test_track_funnel_crash_falls_back(self):
        db = _db()
        with mock.patch("src.ledger.record_repair", side_effect=RuntimeError("boom")):
            guardian._track("TSTUSDT", 100.0, 2.0, [], None)
        st = db.kv_get("tp_sl_tracker:TSTUSDT")
        self.assertEqual(st["entry_price"], 100.0)   # legacy write landed
        self.assertEqual(len(_events(db)), 0)

    def test_audit_funnel_keeps_original_action(self):
        db = _db()
        guardian._audit("PROTECTION_TEST", {"k": 1})
        rows = db._get_conn().execute(
            "SELECT * FROM audit_log ORDER BY timestamp").fetchall()
        self.assertEqual(len(rows), 1)               # exactly one row
        self.assertEqual(rows[0]["action"], "PROTECTION_TEST")  # original name
        self.assertEqual(len(_events(db)), 1)         # + REPAIR event
        self.assertEqual(_events(db)[0]["type"], "REPAIR")

    def test_audit_flag_off_legacy(self):
        db = _db()
        db.kv_set("ledger:repairs", 0)
        guardian._audit("PROTECTION_TEST", {"k": 1})
        rows = db._get_conn().execute(
            "SELECT * FROM audit_log ORDER BY timestamp").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "PROTECTION_TEST")
        self.assertEqual(len(_events(db)), 0)

    def test_swap_ts_and_breach_state_funnel(self):
        db = _db()
        guardian._set_swap_ts("XRPUSDT", {"ts": 42.0})
        self.assertEqual(db.kv_get("gov:swap_ts:XRPUSDT"), {"ts": 42.0})
        self.assertEqual(len(_audits(db, "LEDGER_REPAIR")), 1)
        guardian._set_breach_state("XRPUSDT", {"first_ts": 1.0, "last_alert_ts": 2.0})
        self.assertEqual(db.kv_get("tp_breach_state:XRPUSDT"),
                         {"first_ts": 1.0, "last_alert_ts": 2.0})
        # breach state: event but NO audit (throttle state, pre-P2 parity)
        self.assertEqual(len(_audits(db, "LEDGER_REPAIR")), 1)
        evs = _events(db)
        self.assertEqual(len(evs), 2)
        guardian._clear_breach_state("XRPUSDT")
        self.assertEqual(db.kv_get("tp_breach_state:XRPUSDT"), {})

    def test_breach_state_flag_off_legacy(self):
        db = _db()
        db.kv_set("ledger:repairs", 0)
        guardian._set_breach_state("XRPUSDT", {"first_ts": 1.0})
        self.assertEqual(db.kv_get("tp_breach_state:XRPUSDT"), {"first_ts": 1.0})
        self.assertEqual(len(_events(db)), 0)


class TestPortfolioFixFunnel(unittest.TestCase):
    def test_remove_fix(self):
        db = _db()
        db.portfolio_set("NEARUSDT", {"quantity": 5, "entry_price": 3,
                                      "strategy": "t"})
        ledger.record_repair({
            "kind": "portfolio_fix", "symbol": "NEARUSDT",
            "source": "portfolio_reconciler",
            "payload": {"action": "remove"}}, db=db)
        self.assertIsNone(db.portfolio_get("NEARUSDT"))
        self.assertEqual(len(_audits(db, "LEDGER_REPAIR")), 1)
        self.assertEqual(_events(db)[0]["type"], "REPAIR")

    def test_set_fix_preserves_cols(self):
        db = _db()
        db.portfolio_set("NEARUSDT", {"quantity": 5, "entry_price": 3,
                                      "strategy": "t", "stop_loss": 2.5,
                                      "take_profit": 4.0, "invest_pct": 6})
        ledger.record_repair({
            "kind": "portfolio_fix", "symbol": "NEARUSDT",
            "source": "portfolio_reconciler",
            "payload": {"action": "set",
                        "data": {"quantity": 2, "entry_price": 3,
                                 "strategy": "t"}}}, db=db)
        row = db.portfolio_get("NEARUSDT")
        self.assertEqual(row["quantity"], 2)
        self.assertEqual(row["stop_loss"], 2.5)     # COALESCE parity
        self.assertEqual(row["take_profit"], 4.0)
        # invest_pct parity: legacy portfolio_set uses
        # COALESCE(excluded.invest_pct, ...) — a missing key writes 0.
        # The reconciler always passes dict(pos) (full row), so carry it:
        reconciler._repair_portfolio_set(db, "NEARUSDT", {
            "quantity": 2, "entry_price": 3, "strategy": "t",
            "invest_pct": 6, "stop_loss": None})
        row = db.portfolio_get("NEARUSDT")
        self.assertEqual(row["invest_pct"], 6)
        self.assertEqual(row["stop_loss"], 2.5)      # None -> preserved

    def test_reconciler_helpers_flag_off(self):
        db = _db()
        db.kv_set("ledger:repairs", 0)
        db.portfolio_set("NEARUSDT", {"quantity": 5, "entry_price": 3,
                                      "strategy": "t"})
        reconciler._repair_portfolio_remove(db, "NEARUSDT")
        self.assertIsNone(db.portfolio_get("NEARUSDT"))
        self.assertEqual(len(_events(db)), 0)
        reconciler._repair_portfolio_set(db, "NEARUSDT",
                                         {"quantity": 1, "entry_price": 3,
                                          "strategy": "t"})
        self.assertEqual(db.portfolio_get("NEARUSDT")["quantity"], 1)
        reconciler._repair_snapshot(db, {"NEARUSDT": 1, "_ts": 9})
        self.assertEqual(db.kv_get("reconcile_prev_positions"),
                         {"NEARUSDT": 1, "_ts": 9})

    def test_reconciler_helpers_flag_on(self):
        db = _db()
        db.portfolio_set("NEARUSDT", {"quantity": 5, "entry_price": 3,
                                      "strategy": "t"})
        reconciler._repair_portfolio_remove(db, "NEARUSDT")
        self.assertIsNone(db.portfolio_get("NEARUSDT"))
        self.assertEqual(len(_events(db)), 1)        # via ledger


class TestReconcilerReport(unittest.TestCase):
    def test_report_routing_audit_only_when_actionable(self):
        db = _db()
        # actionable=False: kv only — no audit, no event
        ledger.record_repair({
            "kind": "reconcile_report", "source": "portfolio_reconciler",
            "payload": {"report": {"ts": 1, "suspects": {},
                                   "booked": 0, "actionable": False},
                        "audit": False}}, db=db)
        self.assertEqual(db.kv_get("reconciler:report")["booked"], 0)
        self.assertEqual(len(_events(db)), 0)
        self.assertEqual(len(_audits(db, "LEDGER_REPAIR")), 0)
        # actionable=True: kv + audit + event
        ledger.record_repair({
            "kind": "reconcile_report", "source": "portfolio_reconciler",
            "payload": {"report": {"ts": 2, "suspects": {"A": 1.0},
                                   "booked": 1, "actionable": True},
                        "audit": True}}, db=db)
        rep = db.kv_get("reconciler:report")
        self.assertEqual(rep["booked"], 1)
        self.assertEqual(len(_audits(db, "LEDGER_REPAIR")), 1)
        self.assertEqual(len(_events(db)), 1)

    def test_clean_round_report_via_drift(self):
        db = _db()
        client = mock.Mock()
        client.get_account.return_value = {
            "balances": [{"asset": "USDT", "free": "100", "locked": "0"}]}
        out = reconciler.reconcile_portfolio_drift(client, db)
        self.assertEqual(out, [])
        rep = db.kv_get("reconciler:report")
        self.assertIsNotNone(rep)
        self.assertEqual(rep["booked"], 0)
        self.assertFalse(rep["actionable"])
        self.assertEqual(rep["suspects"], {})
        self.assertEqual(len(_events(db)), 0)        # clean round: no event
        self.assertEqual(len(_audits(db, "LEDGER_REPAIR")), 0)


class TestTracesDiffIgnoresRepairs(unittest.TestCase):
    def test_repair_event_not_traced(self):
        db = _db()
        db.kv_set("ledger:shadow:bootstrap_ts", __import__("time").time())
        # a repair carrying an order_id would poison the traces diff if
        # the type filter were missing
        ledger.record_repair({
            "kind": "tracker_state", "symbol": "LTCUSDT",
            "order_id": "r-99",
            "source": "protection_guardian",
            "payload": {"entry": 66, "qty": 0.5, "tp_orders": [],
                        "sl_order": None}}, db=db)
        res = ledger.shadow_diff(db, emit=False)
        self.assertTrue(res["clean"])
        self.assertEqual(res["consecutive_clean"], 1)

    def test_fills_still_traced_after_repairs(self):
        db = _db()
        ts = __import__("time").time()
        db.kv_set("ledger:shadow:bootstrap_ts", ts)
        ledger.record_fill({"type": "BUY", "symbol": "LTCUSDT", "qty": 0.5,
                            "price": 66.0, "order_id": "b-1",
                            "source": "test"}, db=db)
        # delete the matching trades row -> traces diff must catch it
        db._get_conn().execute("DELETE FROM trades WHERE client_order_id='b-1'")
        db._get_conn().commit()
        res = ledger.shadow_diff(db, emit=False)
        self.assertFalse(res["clean"])
        kinds = {d["kind"] for d in res["true_diffs"]}
        self.assertIn("trades_missing", kinds)


if __name__ == "__main__":
    unittest.main()
