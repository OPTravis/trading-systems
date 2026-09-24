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
        # Travis C (2026-09-24): 216 = 3 fully-clean days at the
        # post-ded9128 effective cadence (72 rounds/day)
        self.assertEqual(ledger.PROMOTE_ROUNDS, 216)
        st = ledger.promote_status(_db())
        self.assertEqual(st["threshold_rounds"], 216)
        self.assertEqual(st["current_streak"], 0)
        self.assertEqual(st["remaining_rounds"], 216)
        self.assertFalse(st["ready"])
        self.assertIn("manual", st["switch_note"])
        self.assertTrue(st["threshold_desc"].startswith("216 ="))
        self.assertIn("72 rounds/day", st["threshold_desc"])
        # historical pre-fix value only appears as provenance, never as
        # the current threshold
        self.assertIn("was 432", st["threshold_desc"])

    def test_notify_once_per_cycle(self):
        db = _db()
        db.kv_set("ledger:shadow:bootstrap_ts", time.time())
        # fake a mature streak, then run clean rounds
        db.kv_set("ledger:shadow:stats",
                  {"rounds": 500, "consecutive_clean": 215,
                   "pending_ages": {}})
        with mock.patch("src.live_alerts.emit") as em:
            ledger.shadow_diff(db, emit=True)    # streak -> 216: notify
            self.assertEqual(em.call_count, 1)
            self.assertEqual(em.call_args[0][0], "LEDGER_PROMOTE_READY")
            audits = db._get_conn().execute(
                "SELECT COUNT(*) FROM audit_log WHERE action = "
                "'LEDGER_PROMOTE_READY'").fetchone()[0]
            self.assertEqual(audits, 1)
            ledger.shadow_diff(db, emit=True)    # 217: latch held, no re-alert
            self.assertEqual(em.call_count, 1)
            self.assertEqual(audits, 1)
        self.assertEqual(ledger.promote_status(db)["ready"], True)

    def test_streak_break_rearms_latch(self):
        db = _db()
        ts = time.time()
        db.kv_set("ledger:shadow:bootstrap_ts", ts)
        db.kv_set("ledger:shadow:stats",
                  {"rounds": 500, "consecutive_clean": 216,
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
        # WO-0924 P6-B1 followup: freeze the report clock. Seeding offsets
        # from wall-clock `now` made the daily buckets depend on time of
        # day (run between 00:00-06:00, "now - 86400 - 5h" lands two
        # calendar days back → 3 buckets instead of 2 → flake). Anchoring
        # both the seeds and shadow_report's cutoff to the same fixed
        # noon timestamp makes the test deterministic at any run hour.
        FIXED_NOW = 1789905600.0  # 2026-09-23 12:00:00 HKT (a Wednesday noon)
        day = lambda offset, hour: FIXED_NOW - offset * 86400 - hour * 3600
        with mock.patch.object(ledger.time, "time", return_value=FIXED_NOW):
            # day -1: 3 clean + 1 diff(2 kinds) + 1 pending
            for i in range(3):
                self._seed_round(db, day(1, i), "clean", streak=i + 1, round_n=i)
            self._seed_round(db, day(1, 4), "diff",
                             kinds=["position_qty", "trades_missing"])
            self._seed_round(db, day(1, 5), "pending")
            # day 0 (today): 2 clean
            for i in range(2):
                self._seed_round(db, FIXED_NOW - (i + 1) * 60, "clean",
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
        self.assertEqual(rep["overall"]["promote"]["threshold_rounds"], 216)
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


class TestDustExemption(unittest.TestCase):
    """Travis A (2026-09-24): position_qty gaps < DRIFT_QTY_ABS are
    dust-tier — info layer, streak unbroken, audit + weekly column
    (never silent). Gaps >= DRIFT_QTY_ABS still report as true diffs.
    EPS/equality judgment untouched.
    """

    def _seed_shadow(self, db, sym, qty):
        db._get_conn().execute(
            "INSERT INTO ledger_shadow_positions (symbol, net_qty) "
            "VALUES (?, ?)", (sym, qty))
        db._get_conn().commit()

    def test_dust_gap_exempt_streak_unbroken_and_audited(self):
        db = _db()
        db.kv_set("ledger:shadow:bootstrap_ts", time.time())
        db.kv_set("ledger:shadow:stats",
                  {"rounds": 3, "consecutive_clean": 2, "pending_ages": {}})
        # gap = 0.0005 < DRIFT_QTY_ABS (0.001) -> exempt, clean
        db.portfolio_set("DUSTUSDT", {"quantity": 0.0, "entry_price": 1,
                                      "strategy": "t"})
        self._seed_shadow(db, "DUSTUSDT", 0.0005)
        res = ledger.shadow_diff(db, emit=False)
        # exempted round stays clean and advances the streak
        self.assertTrue(res["clean"])
        self.assertEqual(res["consecutive_clean"], 3)
        self.assertEqual(res["dust_exemptions"][0]["kind"],
                         "position_qty_dust")
        self.assertEqual(res["dust_exemptions"][0]["symbol"], "DUSTUSDT")
        # constraint ①: never silent — one audit row for the round
        audits = db._get_conn().execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = "
            "'LEDGER_DUST_EXEMPT'").fetchone()[0]
        self.assertEqual(audits, 1)
        # durable rounds row carries the exemption count
        rows = _round_rows(db)
        self.assertEqual(rows[-1]["outcome"], "clean")
        self.assertEqual(rows[-1]["dust_exempt_count"], 1)

    def test_boundary_gap_equal_to_threshold_reports_true_diff(self):
        db = _db()
        db.kv_set("ledger:shadow:bootstrap_ts", time.time())
        db.kv_set("ledger:shadow:stats",
                  {"rounds": 3, "consecutive_clean": 2, "pending_ages": {}})
        # gap == DRIFT_QTY_ABS exactly: "达到 DRIFT_QTY_ABS 的差异照报
        # 真 diff" — exemption is strictly <
        db.portfolio_set("EDGUSDT", {"quantity": 0.0, "entry_price": 1,
                                     "strategy": "t"})
        self._seed_shadow(db, "EDGUSDT", 0.001)
        res = ledger.shadow_diff(db, emit=False)
        self.assertFalse(res["clean"])
        self.assertEqual(res["consecutive_clean"], 0)
        self.assertEqual(res["true_diffs"][0]["kind"], "position_qty")
        self.assertEqual(res["dust_exemptions"], [])
        audits = db._get_conn().execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = "
            "'LEDGER_DUST_EXEMPT'").fetchone()[0]
        self.assertEqual(audits, 0)
        diff_audits = db._get_conn().execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = "
            "'LEDGER_SHADOW_DIFF'").fetchone()[0]
        self.assertEqual(diff_audits, 1)

    def test_above_threshold_gap_reports_true_diff(self):
        db = _db()
        db.kv_set("ledger:shadow:bootstrap_ts", time.time())
        db.kv_set("ledger:shadow:stats",
                  {"rounds": 3, "consecutive_clean": 2, "pending_ages": {}})
        # gap = 0.01 >> DRIFT_QTY_ABS -> true diff
        db.portfolio_set("BIGUSDT", {"quantity": 0.0, "entry_price": 1,
                                     "strategy": "t"})
        self._seed_shadow(db, "BIGUSDT", 0.01)
        res = ledger.shadow_diff(db, emit=False)
        self.assertFalse(res["clean"])
        self.assertEqual(res["true_diffs"][0]["symbol"], "BIGUSDT")
        self.assertEqual(res["dust_exemptions"], [])

    def test_eps_equality_judgment_untouched(self):
        # A-③: the pre-existing equality window max(EPS_QTY, 0.5% rel)
        # is unchanged — it still short-circuits BEFORE the dust layer
        # and never produces an exemption or a diff.
        db = _db()
        db.kv_set("ledger:shadow:bootstrap_ts", time.time())
        # lv=10, gap=0.04 (0.4% < 0.5% rel window) -> equal
        db.portfolio_set("RELTOL1", {"quantity": 10.0, "entry_price": 1,
                                     "strategy": "t"})
        self._seed_shadow(db, "RELTOL1", 10.04)
        # lv=2, gap=0.012 (0.6% > 0.5% rel, > DRIFT_QTY_ABS) -> true diff
        db.portfolio_set("RELTOL2", {"quantity": 2.0, "entry_price": 1,
                                     "strategy": "t"})
        self._seed_shadow(db, "RELTOL2", 2.012)
        res = ledger.shadow_diff(db, emit=False)
        syms = {d["symbol"] for d in res["true_diffs"]}
        self.assertEqual(syms, {"RELTOL2"})
        self.assertEqual(res["dust_exemptions"], [])
        self.assertFalse(res["clean"])

    def test_weekly_report_carries_dust_column(self):
        db = _db()
        db.kv_set("ledger:shadow:bootstrap_ts", time.time())
        db.kv_set("ledger:shadow:stats",
                  {"rounds": 0, "consecutive_clean": 0, "pending_ages": {}})
        # round 1: dust-exempted clean round
        db.portfolio_set("DUSTUSDT", {"quantity": 0.0, "entry_price": 1,
                                      "strategy": "t"})
        self._seed_shadow(db, "DUSTUSDT", 0.0005)
        ledger.shadow_diff(db, emit=False)
        # round 2: plain clean round
        ledger.reset_stats_only = None  # noqa — keep linters quiet
        db.portfolio_set("DUSTUSDT", {"quantity": 0.0005, "entry_price": 1,
                                      "strategy": "t"})
        ledger.shadow_diff(db, emit=False)
        rep = ledger.shadow_report(db, days=1)
        self.assertEqual(rep["overall"]["dust_exempt_total"], 1)
        today = rep["daily"][-1]
        self.assertEqual(today["dust_exempt"], 1)
        self.assertEqual(today["rounds"], 2)
        self.assertEqual(today["clean"], 2)


class TestResetClearsHistory(unittest.TestCase):
    def test_reset(self):
        db = _db()
        db.kv_set("ledger:shadow:bootstrap_ts", time.time())
        db.kv_set("ledger:shadow:stats",
                  {"rounds": 500, "consecutive_clean": 216,
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
