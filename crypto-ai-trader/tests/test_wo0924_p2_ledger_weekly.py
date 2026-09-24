"""WO-0924 P2-④: weekly report + promotion gate for the shadow book.

Contract under test:
  - every shadow_diff round leaves one durable row (outcome / diff kinds /
    streak) in ledger_shadow_rounds
  - weekly aggregate math: per-day rounds / clean rate / diff kinds /
    max streak / coverage start
  - promotion gate: threshold definition, one notify per clean cycle
    (audit + alert), latch re-armed when the streak breaks, never flips
    the mode itself
  - reset_shadow clears the rounds history and the promote latch
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.state_db import StateDB
from src import ledger


def _db():
    d = StateDB()
    d._get_conn().executescript(
        "DELETE FROM kv; DELETE FROM audit_log; DELETE FROM ledger_events;"
        "DELETE FROM ledger_shadow_positions; DELETE FROM ledger_shadow_rounds;"
        "DELETE FROM portfolio; DELETE FROM trades; DELETE FROM trade_outcomes;"
    )
    d._get_conn().commit()
    return d


def _round_rows(db):
    return db._get_conn().execute(
        "SELECT * FROM ledger_shadow_rounds ORDER BY id").fetchall()


class TestRoundHistory(unittest.TestCase):
    def test_clean_round_row(self):
        db = _db()
        db.kv_set("ledger:shadow:bootstrap_ts", time.time())
        res = ledger.shadow_diff(db, emit=False)
        self.assertTrue(res["clean"])
        rows = _round_rows(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "clean")
        self.assertEqual(rows[0]["diff_count"], 0)
        self.assertEqual(rows[0]["consecutive_clean"], 1)

    def test_diff_round_row_with_kinds(self):
        db = _db()
        ts = time.time()
        db.kv_set("ledger:shadow:bootstrap_ts", ts)
        db.portfolio_set("LTCUSDT", {"quantity": 1, "entry_price": 66,
                                     "strategy": "t"})
        ledger.shadow_diff(db, emit=False)   # round 1: live-only -> diff
        rows = _round_rows(db)
        self.assertEqual(rows[-1]["outcome"], "diff")
        kinds = json.loads(rows[-1]["diff_kinds"])
        self.assertIn("position_qty", kinds)

    def test_error_round_row(self):
        db = _db()
        db.kv_set("ledger:shadow:bootstrap_ts", time.time())
        db.kv_set("ledger:shadow:stats",
                  {"rounds": 9, "consecutive_clean": 5, "pending_ages": {}})
        with mock.patch("src.ledger._positions_diff",
                        side_effect=RuntimeError("boom")):
            res = ledger.shadow_diff(db, emit=False)
        self.assertEqual(res["status"], "error")
        rows = _round_rows(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "error")
        # streak neither advanced nor reset by the error round
        self.assertEqual(ledger.get_stats(db).get("consecutive_clean"), 5)


class TestPromotionGate(unittest.TestCase):
    def test_threshold_definition(self):
        self.assertEqual(ledger.PROMOTE_ROUNDS, 432)
        st = ledger.promote_status(_db())
        self.assertEqual(st["threshold_rounds"], 432)
        self.assertEqual(st["current_streak"], 0)
        self.assertEqual(st["remaining_rounds"], 432)
        self.assertFalse(st["ready"])
        self.assertIn("manual", st["switch_note"])

    def test_notify_once_per_cycle(self):
        db = _db()
        db.kv_set("ledger:shadow:bootstrap_ts", time.time())
        # fake a mature streak, then run clean rounds
        db.kv_set("ledger:shadow:stats",
                  {"rounds": 500, "consecutive_clean": 431,
                   "pending_ages": {}})
        with mock.patch("src.live_alerts.emit") as em:
            ledger.shadow_diff(db, emit=True)    # streak -> 432: notify
            self.assertEqual(em.call_count, 1)
            self.assertEqual(em.call_args[0][0], "LEDGER_PROMOTE_READY")
            audits = db._get_conn().execute(
                "SELECT COUNT(*) FROM audit_log WHERE action = "
                "'LEDGER_PROMOTE_READY'").fetchone()[0]
            self.assertEqual(audits, 1)
            ledger.shadow_diff(db, emit=True)    # 433: latch held, no re-alert
            self.assertEqual(em.call_count, 1)
            self.assertEqual(audits, 1)
        self.assertEqual(ledger.promote_status(db)["ready"], True)

    def test_streak_break_rearms_latch(self):
        db = _db()
        ts = time.time()
        db.kv_set("ledger:shadow:bootstrap_ts", ts)
        db.kv_set("ledger:shadow:stats",
                  {"rounds": 500, "consecutive_clean": 432,
                   "pending_ages": {}})
        with mock.patch("src.live_alerts.emit"):
            ledger.shadow_diff(db, emit=True)     # notify fires
        self.assertIsNotNone(db.kv_get("ledger:shadow:promote_notified"))
        # introduce a diff: ghost live row -> streak breaks, latch re-arms
        db.portfolio_set("GHOSTUSDT", {"quantity": 9, "entry_price": 1,
                                       "strategy": "t"})
        ledger.shadow_diff(db, emit=False)
        self.assertEqual(ledger.promote_status(db)["current_streak"], 0)
        self.assertIsNone(db.kv_get("ledger:shadow:promote_notified"))

    def test_pending_round_resets_streak(self):
        db = _db()
        ts = time.time()
        db.kv_set("ledger:shadow:bootstrap_ts", ts)
        db.kv_set("ledger:shadow:stats",
                  {"rounds": 100, "consecutive_clean": 100,
                   "pending_ages": {}})
        # position with a fresh sell event -> pending layer (legacy lag)
        ledger.record_fill({"type": "SELL", "symbol": "LTCUSDT",
                            "qty": 1.0, "price": 66.0, "order_id": "s-1",
                            "source": "test"}, db=db)
        db._get_conn().execute("DELETE FROM trades WHERE client_order_id='s-1'")
        db._get_conn().commit()
        res = ledger.shadow_diff(db, emit=False)
        # pending present -> not clean, streak reset
        self.assertFalse(res["clean"])
        self.assertEqual(ledger.promote_status(db)["current_streak"], 0)

    def test_mode_stays_shadow_after_threshold(self):
        db = _db()
        db.kv_set("ledger:shadow:bootstrap_ts", time.time())
        db.kv_set("ledger:shadow:stats",
                  {"rounds": 500, "consecutive_clean": 431,
                   "pending_ages": {}})
        with mock.patch("src.live_alerts.emit"):
            ledger.shadow_diff(db, emit=True)
        # gate NEVER flips the mode by itself
        self.assertEqual(ledger.get_mode(db), "shadow")


class TestWeeklyReport(unittest.TestCase):
    def _seed_round(self, db, ts, outcome, kinds=None, streak=0, round_n=1):
        db._get_conn().execute(
            "INSERT INTO ledger_shadow_rounds "
            "(ts, round, outcome, diff_count, pending_count, diff_kinds, "
            " consecutive_clean) VALUES (?,?,?,?,?,?,?)",
            (ts, round_n, outcome,
             len(kinds or []) if outcome == "diff" else 0,
             1 if outcome == "pending" else 0,
             json.dumps(kinds or []), streak),
        )
        db._get_conn().commit()

    def test_aggregate_math(self):
        db = _db()
        db.kv_set("ledger:shadow:bootstrap_ts", time.time())
        db.kv_set("ledger:shadow:stats",
                  {"rounds": 10, "consecutive_clean": 7, "pending_ages": {}})
        now = time.time()
        day = lambda offset, hour: now - offset * 86400 - hour * 3600
        # day -1: 3 clean + 1 diff(2 kinds) + 1 pending
        for i in range(3):
            self._seed_round(db, day(1, i), "clean", streak=i + 1, round_n=i)
        self._seed_round(db, day(1, 4), "diff",
                         kinds=["position_qty", "trades_missing"])
        self._seed_round(db, day(1, 5), "pending")
        # day 0 (today): 2 clean
        for i in range(2):
            self._seed_round(db, now - (i + 1) * 60, "clean",
                             streak=6 + i, round_n=10 + i)
        rep = ledger.shadow_report(db, days=7)
        self.assertEqual(rep["overall"]["rounds"], 7)
        self.assertEqual(rep["overall"]["clean_rounds"], 5)
        self.assertEqual(rep["overall"]["clean_rate"], round(5 / 7, 4))
        self.assertEqual(rep["overall"]["diff_rounds"], 1)
        self.assertEqual(rep["overall"]["pending_rounds"], 1)
        self.assertEqual(rep["overall"]["diffs_by_kind"],
                         {"position_qty": 1, "trades_missing": 1})
        self.assertEqual(rep["overall"]["max_consecutive_clean"], 7)
        self.assertEqual(len(rep["daily"]), 2)
        d0 = rep["daily"][0]
        self.assertEqual(d0["rounds"], 5)
        self.assertEqual(d0["clean"], 3)
        self.assertEqual(d0["clean_rate"], round(3 / 5, 4))
        self.assertEqual(rep["daily"][1]["rounds"], 2)
        # promotion gate embedded in the report
        self.assertEqual(rep["overall"]["promote"]["threshold_rounds"], 432)
        self.assertEqual(rep["overall"]["promote"]["current_streak"], 7)

    def test_empty_and_off(self):
        db = _db()
        rep = ledger.shadow_report(db, days=7)
        self.assertEqual(rep["overall"]["rounds"], 0)
        self.assertIsNone(rep["overall"]["clean_rate"])
        self.assertIsNone(rep["overall"]["coverage_start"])
        db.kv_set("ledger:mode", "off")
        self.assertEqual(ledger.shadow_report(db)["status"], "off")

    def test_coverage_respects_days_window(self):
        db = _db()
        db.kv_set("ledger:shadow:bootstrap_ts", time.time())
        now = time.time()
        self._seed_round(db, now - 30 * 86400, "clean", round_n=1)   # out
        self._seed_round(db, now - 3600, "clean", streak=1, round_n=2)  # in
        rep = ledger.shadow_report(db, days=7)
        self.assertEqual(rep["overall"]["rounds"], 1)
        self.assertEqual(rep["overall"]["coverage_start"],
                         time.strftime("%Y-%m-%d %H:%M",
                                       time.localtime(now - 3600)))


class TestResetClearsHistory(unittest.TestCase):
    def test_reset(self):
        db = _db()
        db.kv_set("ledger:shadow:bootstrap_ts", time.time())
        db.kv_set("ledger:shadow:stats",
                  {"rounds": 500, "consecutive_clean": 432,
                   "pending_ages": {}})
        with mock.patch("src.live_alerts.emit"):
            ledger.shadow_diff(db, emit=True)
        self.assertIsNotNone(db.kv_get("ledger:shadow:promote_notified"))
        self.assertEqual(len(_round_rows(db)), 1)
        ledger.reset_shadow(db)
        self.assertEqual(len(_round_rows(db)), 0)
        self.assertIsNone(db.kv_get("ledger:shadow:promote_notified"))
        self.assertIsNone(db.kv_get("ledger:shadow:stats"))
        # mode flag untouched by reset
        self.assertEqual(ledger.get_mode(db), "shadow")


if __name__ == "__main__":
    unittest.main()
