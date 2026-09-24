"""WO-0924 tracker lifecycle defect (insert ticket, risk-mgmt priority).

Root causes fixed:
  ① no caller ever removed the tracker when a position went flat
    (cmd_trailing_check has cleanup logic but nothing schedules it)
  ② reconciler booked OCO fills but never collapsed the tracker
    (filled TP groups stayed listed, total_qty never decremented)

Contract under test:
  - _collapse_tracker: SL fill -> tracker removed; TP fill -> tier dropped,
    qty decremented, sl_moved history carried; foreign fill -> qty
    decrement; qty<=EPS -> removed
  - all removals/rewrites funnel through the ledger repairs
    (tracker_cleanup / tracker_state kinds) with legacy fallback
  - zombie sweep: trackers with no position and no exchange balance die
  - close_position retires the tracker on a full close
  - shadow_diff exposes orphan trackers in the info layer only
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.state_db import StateDB
from src import ledger
from src import portfolio_reconciler as reconciler
from src.tp_sl_tracker import save_state, get_state, get_all_tracked
from src.portfolio import PortfolioManager


def _db():
    d = StateDB()
    d._get_conn().executescript(
        "DELETE FROM kv; DELETE FROM audit_log; DELETE FROM ledger_events;"
        "DELETE FROM ledger_shadow_positions; DELETE FROM ledger_shadow_rounds;"
        "DELETE FROM portfolio; DELETE FROM trades; DELETE FROM trade_outcomes;"
    )
    d._get_conn().commit()
    return d


def _tp(oid, tier, price, qty):
    return {"order_id": oid, "price": price, "qty": qty, "tier": tier,
            "pct": 0.5, "side": "LIMIT"}


def _tracker(sym, qty=3.0, entry=66.0, sl=True):
    save_state(sym, entry, qty,
               [_tp("tp-1", 1, 70.0, 1.0), _tp("tp-2", 2, 75.0, 2.0)],
               {"order_id": "sl-1", "price": 60.0, "qty": qty,
                "stop_price": 61.0} if sl else None)
    return get_state(sym)


class TestCollapseTracker(unittest.TestCase):
    def test_tp_fill_collapses_tier_and_qty(self):
        db = _db()
        _tracker("LTCUSDT")
        out = reconciler._collapse_tracker(db, "LTCUSDT", "tp-1", 1.0)
        self.assertEqual(out["action"], "tp_collapsed")
        st = get_state("LTCUSDT")
        self.assertAlmostEqual(st["total_qty"], 2.0)
        self.assertEqual([t["order_id"] for t in st["tp_orders"]], ["tp-2"])
        self.assertEqual(st["tp_filled"], [False])
        self.assertEqual(st["sl_moved_after_tp"], 1)  # history carried
        # funnel visible: LEDGER_REPAIR audit + REPAIR event
        audits = db._get_conn().execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='LEDGER_REPAIR'"
        ).fetchone()[0]
        self.assertEqual(audits, 1)
        evs = db._get_conn().execute(
            "SELECT type FROM ledger_events").fetchall()
        self.assertEqual([e["type"] for e in evs], ["REPAIR"])

    def test_sl_fill_removes_tracker(self):
        db = _db()
        _tracker("RAYUSDT")
        out = reconciler._collapse_tracker(db, "RAYUSDT", "sl-1", 3.0)
        self.assertEqual(out["action"], "removed_sl_fill")
        self.assertIsNone(get_state("RAYUSDT"))
        evs = db._get_conn().execute(
            "SELECT payload_json FROM ledger_events").fetchall()
        self.assertIn("tracker_cleanup", evs[0]["payload_json"])

    def test_foreign_fill_decrements_qty(self):
        db = _db()
        _tracker("BTCUSDT")
        out = reconciler._collapse_tracker(db, "BTCUSDT", "switch-777", 1.0)
        self.assertEqual(out["action"], "qty_decremented")
        st = get_state("BTCUSDT")
        self.assertAlmostEqual(st["total_qty"], 2.0)
        self.assertEqual(len(st["tp_orders"]), 2)  # legs untouched

    def test_qty_to_zero_removes_tracker(self):
        db = _db()
        _tracker("NEARUSDT", qty=0.5)
        out = reconciler._collapse_tracker(db, "NEARUSDT", "switch-777", 0.5)
        self.assertEqual(out["action"], "removed_qty_zero")
        self.assertIsNone(get_state("NEARUSDT"))

    def test_no_tracker_is_noop(self):
        db = _db()
        out = reconciler._collapse_tracker(db, "GHOSTUSDT", "x", 1.0)
        self.assertEqual(out["action"], "skipped")

    def test_collapse_funnel_crash_falls_back(self):
        db = _db()
        _tracker("LTCUSDT")
        with mock.patch("src.ledger.record_repair",
                        side_effect=RuntimeError("boom")):
            out = reconciler._collapse_tracker(db, "LTCUSDT", "tp-1", 1.0)
        self.assertEqual(out["action"], "tp_collapsed")
        st = get_state("LTCUSDT")
        self.assertAlmostEqual(st["total_qty"], 2.0)   # legacy path landed

    def test_collapse_flag_off_legacy(self):
        db = _db()
        db.kv_set("ledger:repairs", 0)
        _tracker("LTCUSDT")
        reconciler._collapse_tracker(db, "LTCUSDT", "tp-1", 1.0)
        st = get_state("LTCUSDT")
        self.assertAlmostEqual(st["total_qty"], 2.0)
        self.assertEqual(st["sl_moved_after_tp"], 1)
        self.assertEqual(
            len(db._get_conn().execute(
                "SELECT * FROM ledger_events").fetchall()), 0)


class TestTrackerCleanupKind(unittest.TestCase):
    def test_remove_tracker_via_funnel(self):
        db = _db()
        _tracker("DASHUSDT")
        self.assertTrue(ledger.remove_tracker("DASHUSDT", "zombie_sweep", db=db))
        self.assertIsNone(get_state("DASHUSDT"))
        audits = db._get_conn().execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='LEDGER_REPAIR'"
        ).fetchone()[0]
        self.assertEqual(audits, 1)

    def test_remove_tracker_nothing_there(self):
        db = _db()
        self.assertFalse(ledger.remove_tracker("XXUSDT", "zombie_sweep", db=db))

    def test_remove_tracker_flag_off_legacy(self):
        db = _db()
        db.kv_set("ledger:repairs", 0)
        _tracker("INJUSDT")
        self.assertTrue(ledger.remove_tracker("INJUSDT", "position_closed", db=db))
        self.assertIsNone(get_state("INJUSDT"))
        self.assertEqual(
            len(db._get_conn().execute(
                "SELECT * FROM ledger_events").fetchall()), 0)


class TestZombieSweep(unittest.TestCase):
    def test_sweep_removes_dead_trackers_keeps_live(self):
        db = _db()
        db.portfolio_set("LTCUSDT", {"quantity": 0.5, "entry_price": 66,
                                     "strategy": "t"})
        _tracker("LTCUSDT")    # live position -> keep
        _tracker("BTCUSDT")     # no position, exchange flat -> zombie
        _tracker("RAYUSDT")     # exchange holds -> keep (not zombie yet)
        removed = reconciler._sweep_tracker_zombies(
            db, db.portfolio_get_all(), {"RAY": 3.0})
        self.assertEqual(removed, ["BTCUSDT"])
        self.assertIsNotNone(get_state("LTCUSDT"))
        self.assertIsNotNone(get_state("RAYUSDT"))
        self.assertIsNone(get_state("BTCUSDT"))

    def test_sweep_in_drift_round_and_reported(self):
        db = _db()
        _tracker("HBARUSDT")
        client = mock.Mock()
        client.get_account.return_value = {
            "balances": [{"asset": "USDT", "free": "100", "locked": "0"}]}
        out = reconciler.reconcile_portfolio_drift(client, db)
        self.assertEqual(out, [])
        self.assertIsNone(get_state("HBARUSDT"))
        rep = db.kv_get("reconciler:report")
        self.assertEqual(rep["tracker_zombies_swept"], 1)
        self.assertTrue(rep["actionable"])   # zombie counts as actionable


class TestClosePositionRetiresTracker(unittest.TestCase):
    def test_full_close_removes_tracker(self):
        db = _db()
        pm = PortfolioManager()
        pm.update_balance(10000.0)
        pm.add_position("ZECUSDT", quantity=2.0, entry_price=50.0,
                        strategy="t")
        _tracker("ZECUSDT")
        pos = pm.close_position("ZECUSDT", close_price=55.0)
        self.assertIsNotNone(pos)
        self.assertIsNone(get_state("ZECUSDT"))
        evs = db._get_conn().execute(
            "SELECT payload_json FROM ledger_events ORDER BY id"
        ).fetchall()
        kinds = [e["payload_json"] for e in evs]
        self.assertTrue(any("tracker_cleanup" in k for k in kinds))

    def test_flag_off_legacy_close(self):
        db = _db()
        db.kv_set("ledger:repairs", 0)
        pm = PortfolioManager()
        pm.update_balance(10000.0)
        pm.add_position("ZECUSDT", quantity=2.0, entry_price=50.0,
                        strategy="t")
        _tracker("ZECUSDT")
        pm.close_position("ZECUSDT", close_price=55.0)
        self.assertIsNone(get_state("ZECUSDT"))


class TestShadowDiffOrphansInfoLayer(unittest.TestCase):
    def test_orphans_reported_info_only(self):
        db = _db()
        ts = time.time()
        db.kv_set("ledger:shadow:bootstrap_ts", ts)
        _tracker("ORPHAN1USDT")   # no position anywhere -> orphan
        # live-position tracker: NOT an orphan (portfolio AND shadow rows
        # kept aligned so the positions diff itself stays clean)
        db.portfolio_set("LIVEUSDT", {"quantity": 1, "entry_price": 2,
                                      "strategy": "t"})
        db._get_conn().execute(
            "INSERT INTO ledger_shadow_positions (symbol, net_qty) "
            "VALUES ('LIVEUSDT', 1.0)")
        db._get_conn().commit()
        _tracker("LIVEUSDT")
        # shadow-book tracker: NOT an orphan (portfolio + shadow rows
        # kept aligned so the positions diff itself stays clean)
        db.portfolio_set("SHDWUSDT", {"quantity": 1, "entry_price": 2,
                                      "strategy": "t"})
        db._get_conn().execute(
            "INSERT INTO ledger_shadow_positions (symbol, net_qty) "
            "VALUES ('SHDWUSDT', 1.0)")
        db._get_conn().commit()
        _tracker("SHDWUSDT")
        res = ledger.shadow_diff(db, emit=False)
        self.assertIn("ORPHAN1USDT", res["info"]["tracker_orphans"])
        self.assertNotIn("LIVEUSDT", res["info"]["tracker_orphans"])
        self.assertNotIn("SHDWUSDT", res["info"]["tracker_orphans"])
        # orphans alone do NOT break the clean round (info layer by
        # design — the sweep owns the cleanup)
        self.assertTrue(res["clean"])

    def test_collapse_event_not_traced(self):
        db = _db()
        ts = time.time()
        db.kv_set("ledger:shadow:bootstrap_ts", ts)
        _tracker("LTCUSDT")
        reconciler._collapse_tracker(db, "LTCUSDT", "sl-1", 3.0)
        res = ledger.shadow_diff(db, emit=False)
        self.assertTrue(res["clean"])


if __name__ == "__main__":
    unittest.main()
