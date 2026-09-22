"""WO-0922-017: ledger/reconcile fixes.

1. _order_booked suffix-aware idempotency (prefix rows like
   wo0921015_<oid> / oco_tp_<oid> must count as booked).
2. Path C: 24h myTrades lookback independent of portfolio rows and the
   prev-position snapshot — fully-closed symbols (PROVE 9/22) still get
   their unbooked OCO SELL fills booked.
3. Stale-fill exemption from the live-price sanity check.
"""
import sqlite3
import time
import pytest

from src import portfolio_reconciler as pr


class FakeDB:
    """In-memory StateDB stand-in: real sqlite for trades, dict for kv."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " symbol TEXT, side TEXT, qty REAL, price REAL, pnl REAL,"
            " client_order_id TEXT, timestamp REAL)")
        self.kv = {}

    def _get_conn(self):
        return self.conn

    def trade_add(self, symbol, side, qty, price, pnl=0.0,
                  client_order_id=None):
        self.conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl,"
            " client_order_id, timestamp) VALUES (?,?,?,?,?,?,?)",
            (symbol, side, qty, price, pnl, client_order_id, time.time()))
        self.conn.commit()
        return True

    def kv_get(self, key):
        return self.kv.get(key)

    def kv_set(self, key, value):
        self.kv[key] = value

    def portfolio_get_all(self):
        return {}


class FakeClient:
    def __init__(self, balances=None, fills=None, live=None):
        self.balances = {"USDT": 100.0} if balances is None else balances
        self.fills = fills or {}
        self.live = live or {}
        self.my_trades_calls = []

    def get_account(self):
        return {"balances": [
            {"asset": a, "free": str(q), "locked": "0"}
            for a, q in self.balances.items()]}

    def get_my_trades(self, symbol, limit=100):
        self.my_trades_calls.append(symbol)
        return self.fills.get(symbol, [])

    def get_ticker_price(self, symbol):
        return self.live.get(symbol)


def _sell_fill(order_id, qty, price, age_s=60, commission=0.0,
               client_order_id=""):
    return {"orderId": order_id, "isBuyer": False, "qty": str(qty),
            "price": str(price), "time": int((time.time() - age_s) * 1000),
            "commission": str(commission), "commissionAsset": "USDT",
            "clientOrderId": client_order_id}


# ---------------- 1. suffix-aware idempotency ----------------

class TestOrderBookedSuffix:
    def test_bare_order_id_matches(self):
        db = FakeDB()
        db.trade_add("PROVEUSDT", "SELL", 24.1, 0.2566,
                     client_order_id="313400527")
        assert pr._order_booked(db, 313400527) is True

    def test_wo_prefix_matches(self):
        db = FakeDB()
        db.trade_add("FETUSDT", "SELL", 42.3, 0.2014,
                     client_order_id="wo0921015_3676622046")
        assert pr._order_booked(db, 3676622046) is True

    def test_oco_tp_prefix_matches(self):
        db = FakeDB()
        db.trade_add("HBARUSDT", "SELL", 70.0, 0.09167,
                     client_order_id="oco_tp_4556070710")
        assert pr._order_booked(db, 4556070710) is True

    def test_different_order_not_matched(self):
        db = FakeDB()
        db.trade_add("XUSDT", "SELL", 1, 1, client_order_id="wo_4123")
        assert pr._order_booked(db, 123) is False

    def test_empty_table(self):
        assert pr._order_booked(FakeDB(), 999) is False


# ---------------- 2. Path C lookback ----------------

class TestPathCLookback:
    def _closed_symbol_db(self):
        """PROVE-shaped ledger: BUY booked 24h ago, SELL fill never booked,
        portfolio row gone (sync rebuild), prev snapshot empty."""
        db = FakeDB()
        old = time.time() - 3600
        db.conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl,"
            " client_order_id, timestamp) VALUES (?,?,?,?,?,?,?)",
            ("PROVEUSDT", "BUY", 24.1, 0.2468, 0.0, None, old))
        db.conn.commit()
        db.kv["reconcile_prev_positions"] = {}  # snapshot already rotated
        return db

    def test_closed_symbol_gets_booked(self):
        db = self._closed_symbol_db()
        client = FakeClient(  # exchange flat on PROVE, USDT dust present
            balances={"USDT": 100.0},
            fills={"PROVEUSDT": [_sell_fill(313400527, 24.1, 0.2566,
                                            age_s=1800)]})
        booked = pr.reconcile_portfolio_drift(client, db)
        assert len(booked) == 1
        assert booked[0]["symbol"] == "PROVEUSDT"
        assert booked[0]["order_id"] == "313400527"

    def test_prefixed_row_not_double_booked(self):
        db = self._closed_symbol_db()
        db.trade_add("PROVEUSDT", "SELL", 24.1, 0.2566,
                     client_order_id="wo0922017_313400527")
        client = FakeClient(
            fills={"PROVEUSDT": [_sell_fill(313400527, 24.1, 0.2566)]})
        booked = pr.reconcile_portfolio_drift(client, db)
        assert booked == []
        sells = db.conn.execute(
            "SELECT COUNT(*) c FROM trades WHERE symbol='PROVEUSDT'"
            " AND side='SELL'").fetchone()["c"]
        assert sells == 1  # the pre-existing prefixed row only

    def test_balanced_symbol_skipped(self):
        db = FakeDB()
        old = time.time() - 3600
        db.conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl,"
            " client_order_id, timestamp) VALUES (?,?,?,?,?,?,?)",
            ("FETUSDT", "BUY", 84.6, 0.1879, 0.0, None, old))
        db.conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl,"
            " client_order_id, timestamp) VALUES (?,?,?,?,?,?,?)",
            ("FETUSDT", "SELL", 84.6, 0.1991, 0.9, "3677765608", old + 60))
        db.conn.commit()
        client = FakeClient(
            fills={"FETUSDT": [_sell_fill(3677765608, 84.6, 0.1991)]})
        booked = pr.reconcile_portfolio_drift(client, db)
        assert booked == []
        assert "FETUSDT" not in client.my_trades_calls

    def test_old_buy_outside_window_skipped(self):
        db = FakeDB()
        db.conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl,"
            " client_order_id, timestamp) VALUES (?,?,?,?,?,?,?)",
            ("ZAMAUSDT", "BUY", 443, 0.02, 0.0, None,
             time.time() - 30 * 86400))
        db.conn.commit()
        client = FakeClient(
            fills={"ZAMAUSDT": [_sell_fill(111, 443, 0.02)]})
        booked = pr.reconcile_portfolio_drift(client, db)
        assert booked == []
        assert "ZAMAUSDT" not in client.my_trades_calls


# ---------------- 3. stale sanity exemption ----------------

class TestStaleSanityExemption:
    def test_stale_fill_books_despite_live_drift(self):
        """PROVE case: fill 0.2566 vs live 0.2261 (13.6% off) but the fill
        is hours old — must book."""
        db = FakeDB()
        buy_at = time.time() - 12 * 3600   # opened 12h ago
        db.conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl,"
            " client_order_id, timestamp) VALUES (?,?,?,?,?,?,?)",
            ("PROVEUSDT", "BUY", 24.1, 0.2468, 0.0, None, buy_at))
        db.conn.commit()
        db.kv["reconcile_prev_positions"] = {"PROVEUSDT": 24.1,
                                            "_ts": time.time() - 60}
        client = FakeClient(
            live={"PROVEUSDT": 0.2261},
            fills={"PROVEUSDT": [_sell_fill(313400527, 24.1, 0.2566,
                                            age_s=10 * 3600)]})
        booked = pr.reconcile_portfolio_drift(client, db)
        assert len(booked) == 1

    def test_fresh_fill_still_rejected_on_drift(self):
        """Same drift but fill is 1 min old — sanity still applies."""
        db = FakeDB()
        old = time.time() - 3600
        db.conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl,"
            " client_order_id, timestamp) VALUES (?,?,?,?,?,?,?)",
            ("PROVEUSDT", "BUY", 24.1, 0.2468, 0.0, None, old))
        db.conn.commit()
        db.kv["reconcile_prev_positions"] = {"PROVEUSDT": 24.1,
                                            "_ts": time.time() - 60}
        client = FakeClient(
            live={"PROVEUSDT": 0.2261},
            fills={"PROVEUSDT": [_sell_fill(313400527, 24.1, 0.2566,
                                            age_s=60)]})
        booked = pr.reconcile_portfolio_drift(client, db)
        assert booked == []

    def test_fresh_fill_matching_live_books(self):
        db = FakeDB()
        old = time.time() - 3600
        db.conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl,"
            " client_order_id, timestamp) VALUES (?,?,?,?,?,?,?)",
            ("PROVEUSDT", "BUY", 24.1, 0.2468, 0.0, None, old))
        db.conn.commit()
        db.kv["reconcile_prev_positions"] = {"PROVEUSDT": 24.1,
                                            "_ts": time.time() - 60}
        client = FakeClient(
            live={"PROVEUSDT": 0.2566},
            fills={"PROVEUSDT": [_sell_fill(313400527, 24.1, 0.2566,
                                            age_s=60)]})
        booked = pr.reconcile_portfolio_drift(client, db)
        assert len(booked) == 1


# ---------------- 4. WO-017-vi: own-fill attribution beats deviation ----------------

class TestOwnFillAttribution:
    """BCH 9/22 20:41 regression: own OCO TP filled 283.1 while live had
    run to 299.2 — sanity rejected a real fill. Attribution (tracker row
    or cat_ clientOrderId prefix) must waive the deviation check;
    unattributable orders still face it."""

    def _setup(self, tmp_path, monkeypatch, fills, tracked=None):
        db = FakeDB()
        db.trade_add("BCHUSDT", "BUY", 0.022, 272.3, 0.0)
        # lifecycle anchor: the BUY must predate the 60s-old fill or the
        # P0-2 guard correctly excludes the leg
        db.conn.execute(
            "UPDATE trades SET timestamp = ? WHERE side = 'BUY'",
            (time.time() - 7200,))
        db.conn.commit()
        client = FakeClient(fills={"BCHUSDT": fills},
                            live={"BCHUSDT": 299.2})  # +5.7% vs fill 283.1
        monkeypatch.setattr(
            "src.tp_sl_tracker.get_all_tracked",
            lambda: tracked or {})
        return db, client

    def test_tracked_tp_fill_far_from_live_books(self, tmp_path, monkeypatch):
        """Fresh fill on a tracker-recorded TP order, live price 5.7% away:
        attributed -> booked despite deviation."""
        from src.portfolio_reconciler import _book_missing_sells
        db, client = self._setup(
            tmp_path, monkeypatch,
            fills=[_sell_fill(5510690989, 0.022, 283.1)],
            tracked={"BCHUSDT": {"tp_orders": [
                {"order_id": 5510690989, "price": 283.1, "qty": 0.022}],
                "sl_order": None}})
        booked = _book_missing_sells(db, "BCHUSDT",
                                     client.get_my_trades("BCHUSDT"),
                                     0.022, client)
        assert len(booked) == 1
        assert booked[0]["qty"] == pytest.approx(0.022)

    def test_cat_prefixed_fill_far_from_live_books(self, tmp_path, monkeypatch):
        """BCH live-fire shape: tracker already removed at reconcile time;
        attribution survives via the wrappers' cat_ prefix."""
        from src.portfolio_reconciler import _book_missing_sells
        db, client = self._setup(
            tmp_path, monkeypatch,
            fills=[_sell_fill(5510690989, 0.022, 283.1,
                              client_order_id="cat_BCHUSDT_SELL_x1")],
            tracked={})
        booked = _book_missing_sells(db, "BCHUSDT",
                                     client.get_my_trades("BCHUSDT"),
                                     0.022, client)
        assert len(booked) == 1

    def test_foreign_manual_fill_far_from_live_skipped(self, tmp_path, monkeypatch):
        """No tracker row, no cat_ marker (exchange-UI/manual order):
        deviation check still rejects the leg."""
        from src.portfolio_reconciler import _book_missing_sells
        db, client = self._setup(
            tmp_path, monkeypatch,
            fills=[_sell_fill(5510690989, 0.022, 283.1)],
            tracked={})
        booked = _book_missing_sells(db, "BCHUSDT",
                                     client.get_my_trades("BCHUSDT"),
                                     0.022, client)
        assert booked == []

    def test_tracker_sl_order_id_attributed(self, tmp_path, monkeypatch):
        """SL leg recorded in tracker (not tp_orders) also waives sanity."""
        from src.portfolio_reconciler import _book_missing_sells
        db, client = self._setup(
            tmp_path, monkeypatch,
            fills=[_sell_fill(6251429617, 0.03, 240.0)],
            tracked={"ZECUSDT": {
                "tp_orders": [],
                "sl_order": {"order_id": 6251429617,
                             "stop_price": 241.0, "qty": 0.03}}})
        booked = _book_missing_sells(db, "BCHUSDT",
                                     client.get_my_trades("BCHUSDT"),
                                     0.03, client)
        assert len(booked) == 1
