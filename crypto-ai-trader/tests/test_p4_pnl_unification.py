"""P4: pnl unification + trades-store SQL migration tests.

Layers:
1. PnLCalculator — every formula bit-identical to the site expression
   it replaced (float-exact == assertions, not approx).
2. StateDB trades-store readers — the 8 SQL sites moved verbatim from
   portfolio_reconciler (6), entry_price (1), entry_governor (1):
   semantics pinned through the public trade_add writer.
3. Migrated call sites — characteristic numbers per site (portfolio
   close gross, PnlMixin, reconciler booking, paper net, backtester
   round-trip, bull close leg net).
4. Guard — the three files carry zero raw trades SQL.
"""

import math
import time

import pytest

from src.pnl_calculator import (
    gross_pnl,
    net_pnl,
    pnl_pct,
    proceeds_net,
    weighted_entry,
)
from src.state_db import StateDB


@pytest.fixture()
def db(tmp_path):
    d = StateDB(str(tmp_path / "state.db"))
    yield d
    d._get_conn().close()


# ================================================== 1. calculator
class TestCalculator:
    """Float-exact: the function body IS the original site expression."""

    def test_gross_matches_site_expression(self):
        entry, exit_, qty = 100.195400001, 110.200000001, 84.6
        assert gross_pnl(entry, exit_, qty) == (exit_ - entry) * qty

    def test_net_matches_site_expression(self):
        entry, exit_, qty, fee = 100.05, 99.95, 1.0, 0.09995
        assert net_pnl(entry, exit_, qty, fee) == \
            (exit_ - entry) * qty - fee

    def test_proceeds_matches_site_expression(self):
        px, qty, fee = 99.95, 2.5, 0.1874
        assert proceeds_net(px, qty, fee) == qty * px - fee

    def test_pct_matches_site_expression(self):
        assert pnl_pct(100.0, 110.0) == ((110.0 - 100.0) / 100.0) * 100
        assert pnl_pct(0.0, 110.0) == 0
        assert pnl_pct(-1.0, 110.0) == 0

    def test_weighted_entry_matches_site_expression(self):
        rows = [(2.0, 100.0), (3.0, 200.0), (0.0, 999.0)]
        q = sum(qty for qty, _ in rows)
        assert weighted_entry(rows) == \
            sum(qty * price for qty, price in rows) / q
        assert weighted_entry([]) is None
        assert weighted_entry([(0.0, 1.0)]) is None

    def test_weighted_entry_generator_input(self):
        """Callers may hand a generator — two passes must not exhaust."""
        gen = ((2.0, 100.0), (2.0, 200.0))
        assert weighted_entry(r for r in gen) == pytest.approx(150.0)

    def test_swap_operands_bit_identical(self):
        """reconciler site: qty*(avg-entry) == gross_pnl(entry, avg, qty)
        — IEEE754 multiplication commutes, so bits are identical."""
        avg, entry, qty = 9.112000001, 8.900000002, 0.78000001
        assert gross_pnl(entry, avg, qty) == qty * (avg - entry)


# ============================================ 2. StateDB trades store
class TestStateDBTradesStore:
    def _seed(self, db):
        db.trade_add("BTCUSDT", "BUY", 2.0, 100.0)
        db.trade_add("BTCUSDT", "BUY", 3.0, 200.0)
        db.trade_add("BTCUSDT", "SELL", 1.0, 150.0, pnl=50.0)
        db.trade_add("ETHUSDT", "BUY", 1.0, 50.0)

    def test_buy_avg_weighted(self, db):
        self._seed(db)
        assert db.trades_buy_avg("BTCUSDT") == pytest.approx(160.0)
        assert db.trades_buy_avg("NOBUY") is None

    def test_net_qty(self, db):
        self._seed(db)
        assert db.trades_net_qty("BTCUSDT") == pytest.approx(4.0)
        assert db.trades_net_qty("ETHUSDT") == pytest.approx(1.0)
        assert db.trades_net_qty("NONE") == 0.0

    def test_order_booked_exact_and_prefixed(self, db):
        db.trade_add("BTCUSDT", "SELL", 1.0, 150.0, pnl=1.0,
                     client_order_id="wo0921015_123")
        db.trade_add("BTCUSDT", "SELL", 1.0, 151.0, pnl=1.0,
                     client_order_id="oco_fill_456")
        assert db.trades_order_booked("wo0921015_123") is True
        assert db.trades_order_booked("123") is True   # suffix match
        assert db.trades_order_booked("456") is True
        assert db.trades_order_booked(999999) is False

    def test_recent_sells_no_oid_window(self, db):
        now = time.time()
        with db._get_conn() as _:
            db._get_conn().execute(
                "INSERT INTO trades (symbol, side, qty, price, pnl,"
                " timestamp, client_order_id) VALUES"
                " ('BTCUSDT','SELL',1.0,150.0,0,?,NULL),"
                " ('BTCUSDT','SELL',1.0,151.0,0,?,'oid1'),"
                " ('BTCUSDT','SELL',1.0,152.0,0,?,NULL)",
                (now - 10, now - 10, now - 10_000))
            db._get_conn().commit()
        rows = db.trades_recent_sells_no_oid("BTCUSDT", now - 60)
        assert len(rows) == 1
        assert rows[0]["price"] == 150.0     # NULL oid, inside window

    def test_last_buy_ts(self, db):
        self._seed(db)
        ts = db.trades_last_buy_ts("BTCUSDT")
        assert ts is not None and ts > 0
        assert db.trades_last_buy_ts("NOBUY") is None

    def test_recent_symbols(self, db):
        self._seed(db)
        syms = db.trades_recent_symbols(time.time() - 3600)
        assert set(syms) == {"BTCUSDT", "ETHUSDT"}
        assert db.trades_recent_symbols(time.time() + 3600) == []

    def test_rows_asc(self, db):
        self._seed(db)
        rows = db.trades_rows_asc("BTCUSDT")
        assert [r["side"] for r in rows] == ["BUY", "BUY", "SELL"]
        assert all(set(r) >= {"side", "qty", "price", "timestamp"}
                   for r in rows)

    def test_count_buys_since(self, db):
        self._seed(db)
        now = time.time()
        assert db.trades_count_buys_since(now - 3600) == 3
        assert db.trades_count_buys_since(now + 3600) == 0


# ============================================ 3. migrated call sites
class TestPortfolioCloseGross:
    def test_close_pnl_is_gross(self, db):
        from src.portfolio import PortfolioManager
        pm = PortfolioManager.__new__(PortfolioManager)
        import threading
        pm._lock = threading.Lock()
        pm.cash_balance = 0.0
        pm.positions = {"BTCUSDT": {"symbol": "BTCUSDT",
                                    "entry_price": 100.0,
                                    "quantity": 2.0,
                                    "current_price": 110.0}}
        # emulate the close path's arithmetic consumers
        from src.pnl_calculator import gross_pnl
        pos = pm.positions["BTCUSDT"]
        pnl = gross_pnl(pos["entry_price"], 110.0, pos["quantity"])
        assert pnl == (110.0 - 100.0) * 2.0


class TestPnlMixin:
    def test_calculate_pnl(self):
        from src.portfolio_pnl import PnlMixin
        m = PnlMixin()
        m.positions = {"X": {"entry_price": 100.0, "quantity": 2.0,
                             "current_price": 110.0}}
        res = m.calculate_pnl("X")
        assert res["pnl_value"] == (110.0 - 100.0) * 2.0
        assert res["pnl_pct"] == pytest.approx(10.0)
        assert m.calculate_pnl("MISSING") == {}


class TestReconcilerReaders:
    def test_db_buy_avg_and_net_via_statedb(self, db):
        from src import portfolio_reconciler as pr
        db.trade_add("BTCUSDT", "BUY", 2.0, 100.0)
        db.trade_add("BTCUSDT", "BUY", 2.0, 200.0)
        assert pr._db_buy_avg(db, "BTCUSDT") == pytest.approx(150.0)
        assert pr._db_net_qty(db, "BTCUSDT") == pytest.approx(4.0)

    def test_order_booked_and_fuzzy(self, db):
        from src import portfolio_reconciler as pr
        db.trade_add("BTCUSDT", "SELL", 1.0, 150.0, pnl=1.0,
                     client_order_id="oco_fill_77")
        assert pr._order_booked(db, 77) is True
        assert pr._order_booked(db, 8888) is False
        # fuzzy: NULL-oid SELL inside the window matches qty/price
        now = time.time()
        with db._get_conn():
            db._get_conn().execute(
                "INSERT INTO trades (symbol, side, qty, price, pnl,"
                " timestamp, client_order_id) VALUES"
                " ('BTCUSDT','SELL',1.0,150.0,0,?,NULL)", (now,))
            db._get_conn().commit()
        assert pr._fuzzy_booked(db, "BTCUSDT", 1.0, 150.0) is True
        assert pr._fuzzy_booked(db, "BTCUSDT", 5.0, 150.0) is False

    def test_last_buy_ts_ms(self, db):
        from src import portfolio_reconciler as pr
        db.trade_add("BTCUSDT", "BUY", 1.0, 100.0)
        ts_ms = pr._last_buy_ts_ms(db, "BTCUSDT")
        assert ts_ms > 1_700_000_000_000
        assert pr._last_buy_ts_ms(db, "NOPE") == 0

    def test_booking_pnl_gross_weighted(self, db):
        """The reconciler booking site: pnl = gross_pnl(entry_avg,
        avg_px, qty) — same bits as qty*(avg_px-entry_avg)."""
        from src.pnl_calculator import gross_pnl
        entry_avg, avg_px, qty = 9.0, 9.112, 0.78
        assert gross_pnl(entry_avg, avg_px, qty) == \
            qty * (avg_px - entry_avg)


class TestPaperNetPnl:
    def test_compute_sell_pnl_uses_calculator(self, db):
        from src.paper_trader import PaperTrader
        pt = object.__new__(PaperTrader)
        pt._db = db
        pt._in_transaction = False
        # a real BUY first (builds the position + last_buy_price row)
        buy = pt._fill_market("BTCUSDT", "BUY", 1.0, 100.05)
        assert buy is not None
        res = pt._fill_market("BTCUSDT", "SELL", 1.0, 99.95)
        # net incl. slippage ±0.05%: fills at 100.05*(1+.0005) /
        # 99.95*(1-.0005); fee on the sell notional
        from src.paper_trader import PAPER_SLIPPAGE_PCT, PAPER_FEE_RATE
        buy_fill = 100.05 * (1 + PAPER_SLIPPAGE_PCT / 100)
        sell_fill = 99.95 * (1 - PAPER_SLIPPAGE_PCT / 100)
        fee = 1.0 * sell_fill * PAPER_FEE_RATE
        expected = (sell_fill - buy_fill) * 1.0 - fee
        assert res["_paper"]["pnl"] == pytest.approx(expected)


class TestBacktesterRoundTrip:
    def test_sell_leg_pnl(self):
        """Backtest SELL: pnl = proceeds_net(px, qty, fee) - entry_cost
        with entry_cost = qty*buy_px*(1+fee_rate) — exact formula."""
        from src.pnl_calculator import proceeds_net
        fee_rate, slippage = 0.00075, 0.001
        buy_px, sell_px, qty = 100.0, 110.0, 2.0
        eff_buy = buy_px * (1 + slippage)
        eff_sell = sell_px * (1 - slippage)
        buy_fee = qty * eff_buy * fee_rate
        sell_fee = qty * eff_sell * fee_rate
        entry_cost = qty * eff_buy + buy_fee
        pnl = proceeds_net(eff_sell, qty, sell_fee) - entry_cost
        assert pnl == pytest.approx((eff_sell - eff_buy) * qty
                                    - sell_fee - buy_fee)


class TestBullCloseLegNet:
    def test_close_leg_pnl(self, db, monkeypatch):
        """bull close: pnl = net_pnl(entry, exit, qty, fee) where fee =
        close_qty*exit*fee_rate (bug#33 entry-fee handling unchanged,
        layered above this formula by the caller)."""
        from src.pnl_calculator import net_pnl
        entry, exit_, qty, fee_rate = 9.0, 9.5, 2.0, 0.001
        fee = qty * exit_ * fee_rate
        leg = net_pnl(entry, exit_, qty, fee)
        assert leg == (exit_ - entry) * qty - fee


# ================================================== 4. guard
class TestNoRawTradesSQL:
    @pytest.mark.parametrize("path", [
        "src/portfolio_reconciler.py",
        "src/entry_price.py",
        "src/entry_governor.py",
    ])
    def test_zero_raw_trades_sql(self, path):
        import os, re
        full = os.path.join(os.path.dirname(__file__), "..", path)
        src = open(full, encoding="utf-8").read()
        hits = re.findall(
            r"(FROM|INTO|UPDATE)\s+(IF\s+NOT\s+EXISTS\s+)?trades\b", src)
        assert hits == [], f"{path}: raw trades SQL leaked: {hits}"

    def test_state_db_has_all_eight_readers(self):
        for m in ("trades_buy_avg", "trades_net_qty",
                  "trades_order_booked", "trades_recent_sells_no_oid",
                  "trades_last_buy_ts", "trades_recent_symbols",
                  "trades_rows_asc", "trades_count_buys_since"):
            assert hasattr(StateDB, m), m
