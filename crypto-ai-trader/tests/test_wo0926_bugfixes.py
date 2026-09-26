"""WO-0926 third-dispatch production bug fixes.

bug1  notification loss — durable DB outbox + cross-round redelivery
      (9/25 22:43 LTC->ENA switch never generated a notification; 9/26 03:41
      INJ notification printed once into a stdout tail and was truncated away)
bug2  OCO fill detection blind spot — same-symbol re-buy hid a genuine gap
      (9/26 00:13 ENA TP fill vs 00:23 re-buy: gap 24.1 vs net-anchored tol 46.6)
bug3  portfolio.add_position silent merge -> explicit conflict policy
side  SOL-style qty<1 displayed as 0 (executed_qty:.0f)
"""
import importlib
import json
import os
import re
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.state_db import StateDB, get_state_db


# ==================== bug1: DB outbox (StateDB) ====================

class TestStateDBOutbox:
    def test_add_pending_mark_roundtrip(self, tmp_path):
        db = StateDB(str(tmp_path / "o.db"))
        assert db.notification_outbox_add("n1", "signal", "t1", "b1")
        assert db.notification_outbox_add("n2", "message", "t2", "b2")
        pend = db.notification_outbox_pending()
        assert [p["notif_id"] for p in pend] == ["n1", "n2"]  # oldest first
        assert pend[0]["type"] == "signal" and pend[0]["body"] == "b1"
        # mark delivered once; second mark is a no-op
        assert db.notification_outbox_mark_delivered("n1") == 1
        assert db.notification_outbox_mark_delivered("n1") == 0
        pend = db.notification_outbox_pending()
        assert [p["notif_id"] for p in pend] == ["n2"]
        # single-string input accepted
        assert db.notification_outbox_mark_delivered("n2") == 1
        assert db.notification_outbox_pending() == []
        db.close()

    def test_add_idempotent_on_same_notif_id(self, tmp_path):
        db = StateDB(str(tmp_path / "o.db"))
        db.notification_outbox_add("dup", "signal", "t", "b")
        db.notification_outbox_add("dup", "signal", "t", "b")
        assert len(db.notification_outbox_pending()) == 1
        db.close()

    def test_pending_max_age_filters_stale(self, tmp_path):
        db = StateDB(str(tmp_path / "o.db"))
        old_ts = time.time() - 8 * 86400  # older than the 7d window
        db.notification_outbox_add("old", "signal", "t", "b",
                                   created_ts=old_ts)
        db.notification_outbox_add("new", "signal", "t", "b")
        pend = db.notification_outbox_pending(max_age_s=7 * 86400)
        assert [p["notif_id"] for p in pend] == ["new"]
        db.close()


# ==================== bug1: notifier mirrors into outbox ====================

class TestNotifierOutboxMirror:
    def _redirect(self, tmp_path, monkeypatch):
        import pathlib as _pl

        sig_dir = _pl.Path(str(tmp_path / "signals"))
        monkeypatch.setattr("src.notifier.SIGNALS_DIR", sig_dir)
        monkeypatch.setattr("src.notifier.SIGNALS_FILE", sig_dir / "pending.json")
        monkeypatch.setattr(
            "src.notifier.NOTIFICATIONS_FILE",
            sig_dir / "pending_notifications.json")

    def test_append_notification_dual_write(self, tmp_path, monkeypatch):
        self._redirect(tmp_path, monkeypatch)
        db = get_state_db(str(tmp_path / "nd.db"))
        monkeypatch.setattr("src.state_db.get_state_db", lambda: db)
        from src import notifier

        notifier._append_notification("signal", "T", "B")

        notif_file = json.load(
            open(str(tmp_path / "signals" / "pending_notifications.json")))
        assert len(notif_file) == 1
        nid = notif_file[0]["id"]
        pend = db.notification_outbox_pending()
        assert len(pend) == 1 and pend[0]["notif_id"] == nid
        assert pend[0]["type"] == "signal" and pend[0]["body"] == "B"
        db.close()

    def test_outbox_db_failure_never_breaks_json(self, tmp_path, monkeypatch):
        self._redirect(tmp_path, monkeypatch)

        def _boom():
            raise RuntimeError("db down")

        monkeypatch.setattr("src.state_db.get_state_db", _boom)
        from src import notifier

        notifier._append_notification("message", "T2", "B2")

        notif_file = json.load(
            open(str(tmp_path / "signals" / "pending_notifications.json")))
        assert len(notif_file) == 1 and notif_file[0]["title"] == "T2"

    def test_send_signal_lands_in_outbox(self, tmp_path, monkeypatch):
        self._redirect(tmp_path, monkeypatch)
        db = get_state_db(str(tmp_path / "ns.db"))
        monkeypatch.setattr("src.state_db.get_state_db", lambda: db)
        from src import notifier

        notifier.send_signal("BUY", "SOLUSDT", "OPEN", 150.0,
                             quantity=0.3, strategy="bollinger")
        pend = db.notification_outbox_pending()
        assert len(pend) == 1
        assert "SOLUSDT" in pend[0]["body"] and "BUY" in pend[0]["body"]
        db.close()


# ==================== bug1: reside_scan redelivery attach ====================

class TestResideScanAttachOutbox:
    def test_attach_pending_to_verdict(self, tmp_path, monkeypatch):
        db = get_state_db(str(tmp_path / "rs.db"))
        db.notification_outbox_add("a1", "signal", "t", "body-1")
        monkeypatch.setattr("src.state_db.get_state_db", lambda: db)
        from scripts import reside_scan

        verdict = {"stdout_tail": "x"}
        reside_scan._attach_outbox(verdict)
        assert "pending_notifications" in verdict
        assert verdict["pending_notifications"][0]["notif_id"] == "a1"
        # after delivery the field disappears (redelivery stops)
        db.notification_outbox_mark_delivered("a1")
        verdict2 = {"stdout_tail": "y"}
        reside_scan._attach_outbox(verdict2)
        assert "pending_notifications" not in verdict2
        db.close()

    def test_attach_silent_on_db_error(self, monkeypatch):
        def _boom():
            raise RuntimeError("db down")

        monkeypatch.setattr("src.state_db.get_state_db", _boom)
        from scripts import reside_scan

        verdict = {}
        reside_scan._attach_outbox(verdict)  # must not raise
        assert "pending_notifications" not in verdict


# ==================== bug1: consumer CLI + switch-path contract ====================

class TestNotifyOutboxCLI:
    def test_pending_then_mark_delivered(self, tmp_path):
        db = get_state_db(str(tmp_path / "cli.db"))
        db.notification_outbox_add("cli-1", "signal", "T", "body line")
        db.close()
        env = dict(os.environ)
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run(
            [sys.executable, "scripts/notify_outbox.py", "--pending",
             "--json"],
            cwd=repo, env=env, capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 0, r.stderr
        rows = json.loads(r.stdout)
        assert len(rows) == 1 and rows[0]["notif_id"] == "cli-1"

    def test_switch_optimizer_emits_notification(self):
        """22:43 case regression guard: the switch execution path must call
        notifier.send_signal (code-contract check without driving the full
        optimizer chain)."""
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(repo, "src/position_optimizer.py")).read()
        m = re.search(
            r"emit_alert\(\s*\"SWITCH_EXECUTED\".*?from src\.notifier import "
            r"send_signal as _notify_switch(.*?)except Exception as db_err",
            src, re.S,
        )
        assert m, "switch block with notifier call not found"
        assert "_notify_switch(" in m.group(1), \
            "switch path does not call notifier.send_signal"


# ==================== bug2: same-symbol re-buy gap booking ====================

def _bal(asset, total):
    return {"asset": asset, "free": str(total), "locked": "0.0"}


def _fill(symbol, oid, qty, price, ts, is_buyer=False,
          commission="0", commission_asset="USDT"):
    return {
        "id": oid * 10, "price": str(price), "qty": str(qty),
        "quoteQty": str(round(qty * price, 8)), "commission": commission,
        "commissionAsset": commission_asset, "time": int(ts * 1000),
        "isBuyer": is_buyer, "isMaker": False, "orderId": oid,
        "symbol": symbol,
    }


def _seed_buy(db, symbol, qty, price, ts):
    db.trade_add(symbol, "BUY", qty, price)
    db._get_conn().execute(
        "UPDATE trades SET timestamp = ? WHERE symbol = ? AND side = 'BUY' "
        "AND timestamp > ?", (ts, symbol, ts))
    db._get_conn().commit()


class FakeClient:
    def __init__(self, balances, trades_by_symbol):
        self._balances = balances
        self._trades = trades_by_symbol

    def get_account(self):
        return {"balances": self._balances}

    def get_my_trades(self, symbol, limit=100, from_id=None):
        return self._trades.get(symbol, [])


class TestSameSymbolRebuyGapBooking:
    def test_0013_ena_case_books_tp_fill(self, tmp_path):
        """Exact 9/26 timeline: BUY 24.1 @22:43, TP SELL 24.1 @00:13 (between
        rounds), re-BUY 23.46 @00:23. Old gates: main-axis tol anchored to
        net (46.6) skipped the 24.1 gap; lifecycle anchor excluded the
        pre-buy fill. New code must book the SELL."""
        from src.portfolio_reconciler import reconcile_portfolio_drift

        db = StateDB(str(tmp_path / "ena.db"))
        now = time.time()
        _seed_buy(db, "ENAUSDT", 24.1, 0.2543, now - 7000)   # 22:43 leg
        _seed_buy(db, "ENAUSDT", 23.46, 0.2559, now - 6400)  # 00:23 re-buy
        db.portfolio_set("ENAUSDT", {"quantity": 23.46, "entry_price": 0.2559})
        fills = [_fill("ENAUSDT", 3441429677, 24.1, 0.2580, now - 6500,
                       commission="0.0241", commission_asset="ENA")]
        client = FakeClient([_bal("ENA", 23.46), _bal("USDT", 400)],
                            {"ENAUSDT": fills})

        booked = reconcile_portfolio_drift(client, db)
        assert len(booked) == 1
        b = booked[0]
        assert b["symbol"] == "ENAUSDT"
        assert b["order_id"] == "3441429677"
        assert b["qty"] == pytest.approx(24.1 - 0.0241, abs=1e-9)
        db.close()

    def test_clean_round_unchanged_zero_calls(self, tmp_path):
        """Regression: a balanced same-symbol double-buy round must not
        produce bookings (gate stays quiet without a real gap)."""
        from src.portfolio_reconciler import reconcile_portfolio_drift

        db = StateDB(str(tmp_path / "clean.db"))
        now = time.time()
        _seed_buy(db, "SOLUSDT", 0.3, 148.0, now - 7000)
        _seed_buy(db, "SOLUSDT", 0.3, 151.0, now - 6400)
        db.portfolio_set("SOLUSDT", {"quantity": 0.6, "entry_price": 149.5})
        client = FakeClient([_bal("SOL", 0.6), _bal("USDT", 400)],
                            {"SOLUSDT": []})
        booked = reconcile_portfolio_drift(client, db)
        assert booked == []
        db.close()

    def test_pre_buy_fill_booked_only_with_flag(self, tmp_path):
        """include_pre_buy unit: a SELL predating the latest BUY books with
        the flag, stays excluded without it (Path B conservative default)."""
        from src.portfolio_reconciler import _book_missing_sells

        db = StateDB(str(tmp_path / "pb.db"))
        now = time.time()
        _seed_buy(db, "XUSDT", 24.1, 0.25, now - 7000)   # older lifecycle buy
        _seed_buy(db, "XUSDT", 10.0, 0.26, now - 6400)   # latest BUY (re-buy)
        sells = [_fill("XUSDT", 777, 24.1, 0.258, now - 6500)]

        booked_off = _book_missing_sells(db, "XUSDT", sells, 24.1)
        assert booked_off == []  # strict anchor excludes the pre-buy fill

        db2 = StateDB(str(tmp_path / "pb2.db"))
        _seed_buy(db2, "XUSDT", 24.1, 0.25, now - 7000)
        _seed_buy(db2, "XUSDT", 10.0, 0.26, now - 6400)
        booked_on = _book_missing_sells(db2, "XUSDT", sells, 24.1,
                                        include_pre_buy=True)
        assert len(booked_on) == 1
        assert booked_on[0]["order_id"] == "777"
        db.close(); db2.close()

    def test_gap_guard_still_blocks_foreign_oversized_leg(self, tmp_path):
        """The pre-buy relaxation must not weaken the ZAMA guard: a single
        leg larger than the gap (beyond tolerance) is skipped."""
        from src.portfolio_reconciler import _book_missing_sells

        db = StateDB(str(tmp_path / "fg.db"))
        now = time.time()
        _seed_buy(db, "YUSDT", 64.0, 0.10, now - 7000)
        _seed_buy(db, "YUSDT", 10.0, 0.11, now - 6400)
        foreign = [_fill("YUSDT", 888, 443.0, 0.10, now - 6500)]
        booked = _book_missing_sells(db, "YUSDT", foreign, 64.0,
                                     include_pre_buy=True)
        assert booked == []
        db.close()


# ==================== bug3: add_position explicit conflict ====================

class TestAddPositionConflict:
    @pytest.fixture()
    def pf(self, tmp_path):
        from src.portfolio import PortfolioManager

        db = get_state_db(str(tmp_path / "pf.db"))
        pm = PortfolioManager()
        pm._db = db
        pm.cash_balance = 1_000_000.0
        pm.positions = {}
        yield pm
        db.close()

    def test_merge_default_keeps_weighted_avg(self, pf):
        pf.add_position("ENAUSDT", quantity=24.1, entry_price=0.2543,
                        deduct_cash=False)
        pf.add_position("ENAUSDT", quantity=23.46, entry_price=0.2559,
                        deduct_cash=False)
        pos = pf.get_position("ENAUSDT")
        assert pos["quantity"] == pytest.approx(47.56)
        assert pos["entry_price"] == pytest.approx(
            (24.1 * 0.2543 + 23.46 * 0.2559) / 47.56, rel=1e-6)

    def test_reject_raises_on_existing(self, pf):
        pf.add_position("ENAUSDT", quantity=24.1, entry_price=0.2543,
                        deduct_cash=False)
        with pytest.raises(ValueError, match="on_conflict='reject'"):
            pf.add_position("ENAUSDT", quantity=23.46, entry_price=0.2559,
                            deduct_cash=False, on_conflict="reject")

    def test_replace_drops_stale_row_opens_fresh(self, pf, caplog):
        pf.add_position("ENAUSDT", quantity=24.1, entry_price=0.2543,
                        strategy="switch", deduct_cash=False)
        with caplog.at_level("WARNING", logger="src.portfolio"):
            pf.add_position("ENAUSDT", quantity=23.46, entry_price=0.2559,
                            strategy="bollinger", deduct_cash=False,
                            on_conflict="replace")
        pos = pf.get_position("ENAUSDT")
        assert pos["quantity"] == 23.46          # ghost 24.1 gone
        assert pos["entry_price"] == 0.2559
        assert pos["strategy"] == "bollinger"
        assert any("Position replaced" in r.message for r in caplog.records)

    def test_invalid_policy_rejected(self, pf):
        with pytest.raises(ValueError, match="invalid on_conflict"):
            pf.add_position("AUSDT", quantity=1, entry_price=1.0,
                            deduct_cash=False, on_conflict="overwrite")


# ==================== side: SOL qty<1 display ====================

class TestSOLQtyDisplay:
    def test_execution_notification_keeps_sub_one_qty(self):
        from src.trade_executor import _send_execution_notification

        class _N:
            def __init__(self):
                self.bodies = []

            def send_message(self, title, body):
                self.bodies.append((title, body))

            def send_text(self, text):
                self.bodies.append(("", text))

        n = _N()
        results = []
        _send_execution_notification(
            n, "SOLUSDT", "bollinger", "MEDIUM", 74.0, 1.4, 403.23, 6.00,
            {"position_pct": 1.4}, 0.3, 150.25, "test", 1, 3, results,
        )
        joined = "\n".join(b for _, b in n.bodies) + "\n".join(results)
        assert "買入: 0.3 @" in joined, f"qty<1 mangled: {joined!r}"
        assert "買入: 0 @" not in joined


# ==================== WO-0926 follow-up: lifecycle-aware buy average ====================

class TestTradesBuyAvgLifecycle:
    def test_closed_lifecycle_does_not_contaminate_current(self, tmp_path):
        """9/26 WLD case: closed lifecycle (13.5@0.4471) must not leak into
        the current open lifecycle's entry average (12.7@0.4711)."""
        from src.state_db import StateDB

        db = StateDB(str(tmp_path / "avg.db"))
        # closed lifecycle (9/21-9/22): BUY fully matched by SELL
        _seed_buy(db, "WLDUSDT", 13.5, 0.4471, time.time() - 400000)
        db.trade_add("WLDUSDT", "SELL", 13.5, 0.4405)
        db._get_conn().execute(
            "UPDATE trades SET timestamp = ? WHERE symbol = 'WLDUSDT' "
            "AND side = 'SELL'", (time.time() - 390000,))
        db._get_conn().commit()
        # current open lifecycle (9/26)
        _seed_buy(db, "WLDUSDT", 12.7, 0.4711, time.time() - 3600)
        assert db.trades_buy_avg("WLDUSDT") == pytest.approx(0.4711)

    def test_flat_ledger_returns_none(self, tmp_path):
        from src.state_db import StateDB

        db = StateDB(str(tmp_path / "flat.db"))
        db.trade_add("AUSDT", "BUY", 10.0, 2.0)
        db.trade_add("AUSDT", "SELL", 10.0, 2.2)
        assert db.trades_buy_avg("AUSDT") is None

    def test_partial_sell_keeps_proportional_avg(self, tmp_path):
        from src.state_db import StateDB

        db = StateDB(str(tmp_path / "part.db"))
        db.trade_add("BUSDT", "BUY", 100.0, 1.0)
        db.trade_add("BUSDT", "BUY", 100.0, 2.0)
        db.trade_add("BUSDT", "SELL", 100.0, 1.5)
        # 100 @1.0 + 100 @2.0 -> proportional relief halves the cost
        assert db.trades_buy_avg("BUSDT") == pytest.approx(1.5)


# ===== WO-0926 tail: oco_fill booked path persists an outbox notification =====
# Travis 06:59 merged order, part 2: the booked path (reconciler) only
# emit_alert'ed — the real 06:43 WLD TP (+0.237) left zero trace in the
# outbox. A booked fill must now persist a durable oco_fill notification
# (order-id-keyed) that the consumer CLI can redeliver.

class TestOcoFillOutboxNotification:
    def test_booked_fill_persists_outbox_and_cli_consumes(self, tmp_path):
        """Acceptance: oco_fill 通知进 outbox 且可被 notify_outbox.py 消费；
        retry 轮不重复入账也不重复通知。"""
        from src.portfolio_reconciler import reconcile_portfolio_drift

        db = StateDB(str(tmp_path / "oco.db"))
        now = time.time()
        _seed_buy(db, "WLDUSDT", 12.7, 0.4711, now - 7000)
        db.portfolio_set("WLDUSDT", {"quantity": 12.7, "entry_price": 0.4711})
        fills = [_fill("WLDUSDT", 4189066134, 12.6, 0.4899, now - 2000,
                       commission="0.0126", commission_asset="WLD")]
        client = FakeClient([_bal("WLD", 0.1), _bal("USDT", 400)],
                            {"WLDUSDT": fills})

        booked = reconcile_portfolio_drift(client, db)
        assert len(booked) == 1 and booked[0]["order_id"] == "4189066134"

        # retry round: order-id idempotent — no re-book, no duplicate notify
        assert reconcile_portfolio_drift(client, db) == []
        db.close()

        odb = get_state_db()  # conftest per-test STATE_DB_PATH
        rows = [r for r in odb.notification_outbox_pending()
                if r["notif_id"] == "notif_oco_fill_WLDUSDT_4189066134"]
        assert len(rows) == 1, rows
        r = rows[0]
        assert r["type"] == "oco_fill"
        assert "OCO" in r["title"] and "WLDUSDT" in r["title"]
        assert "0.4899" in (r.get("body") or "")
        assert "4189066134" in (r.get("body") or "")
        odb.close()

        # consumer side: the redelivery CLI must surface it (subprocess
        # inherits STATE_DB_PATH -> same per-test DB)
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cp = subprocess.run(
            [sys.executable, "scripts/notify_outbox.py", "--pending", "--json"],
            cwd=repo, env=dict(os.environ), capture_output=True, text=True,
            timeout=60,
        )
        assert cp.returncode == 0, cp.stderr
        cli_rows = json.loads(cp.stdout)
        match = [x for x in cli_rows
                 if x["notif_id"] == "notif_oco_fill_WLDUSDT_4189066134"]
        assert match and match[0]["type"] == "oco_fill"


# ===== WO-0926 orders ①②: shadow dust value alignment / log rotation =====

class TestLedgerDustValueAlignment:
    """Order ①: legacy portfolio drops dust rows entirely (live 0.0) while
    the ledger keeps them by fill arithmetic (WLD 0.1 ~ $0.05). The qty
    tier (DRIFT_QTY_ABS) misses cheap-coin dust — a VALUE tier must exempt
    it in the info layer; real-dollar gaps and unknown-price gaps still
    report as true diffs."""

    def _seed_shadow(self, db, sym, net, avg_entry):
        db._get_conn().execute(
            "INSERT INTO ledger_shadow_positions "
            "(symbol, net_qty, avg_entry_price, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (sym, net, avg_entry, time.time()),
        )

    def test_wld_dust_gap_value_exempt(self, tmp_path):
        from src.ledger import _positions_diff

        db = StateDB(str(tmp_path / "dv1.db"))
        self._seed_shadow(db, "WLDUSDT", 0.1, 0.4711)
        db.trade_add("WLDUSDT", "SELL", 12.6, 0.4899, 0.23)  # ref price
        # no portfolio WLD row -> live 0.0 (the 9/26 production state)

        true_diffs, pending, dust = _positions_diff(db, db._get_conn(), {})
        assert true_diffs == [] and pending == []
        assert len(dust) == 1
        d = dust[0]
        assert d["kind"] == "position_qty_dust_value"
        assert d["symbol"] == "WLDUSDT"
        assert d["value_usd"] == pytest.approx(0.1 * 0.4899, abs=1e-6)
        db.close()

    def test_real_value_gap_still_true_diff(self, tmp_path):
        from src.ledger import _positions_diff

        db = StateDB(str(tmp_path / "dv2.db"))
        self._seed_shadow(db, "SOLUSDT", 0.05, 121.93)
        db.trade_add("SOLUSDT", "BUY", 0.05, 121.93, 0.0)
        # no portfolio SOL row -> gap 0.05 x $121.93 ~ $6.1 >= $1 floor

        true_diffs, pending, dust = _positions_diff(db, db._get_conn(), {})
        assert dust == [] and pending == []
        assert len(true_diffs) == 1
        assert true_diffs[0]["kind"] == "position_qty"
        assert true_diffs[0]["symbol"] == "SOLUSDT"
        db.close()

    def test_unknown_price_conservative_no_exempt(self, tmp_path):
        from src.ledger import _positions_diff

        db = StateDB(str(tmp_path / "dv3.db"))
        self._seed_shadow(db, "XYZUSDT", 0.5, 0.0)  # no price anywhere

        true_diffs, pending, dust = _positions_diff(db, db._get_conn(), {})
        assert dust == []
        assert len(true_diffs) == 1 and true_diffs[0]["symbol"] == "XYZUSDT"
        db.close()
