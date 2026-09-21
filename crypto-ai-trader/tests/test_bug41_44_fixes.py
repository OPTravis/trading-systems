"""bug#41-44 regression tests (2026-09-15).

Covers the four code patches re-landed after the vefaas sandbox wipe:
  bug#41  ensure_tp_sl Case 1.5 — SL undersized vs holding -> top-up the gap
  bug#41a position_optimizer 8b — immediate SL/TP after a switch buy fills
  bug#42  exchange-orderId exact-fill dedup (ensure_tp_sl + reconcile_fills)
  bug#42b switch books the ACTUAL fill price, not the signal price
  bug#44b report_validator stale-report time-scope guard for balance claims
"""

import sqlite3
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# ---- stub heavy deps before importing the modules under test ----
try:
    import src.binance_client  # noqa: F401
except Exception:
    sys.modules["src.binance_client"] = types.SimpleNamespace(BinanceClient=object)
try:
    import src.state_db  # noqa: F401
except Exception:
    _sd = types.SimpleNamespace()
    _sd.get_state_db = lambda: None
    _sd.db_write_with_verify = lambda *a, **k: None
    _sd.StateDB = object
    sys.modules["src.state_db"] = _sd

from scripts.ensure_tp_sl import insert_sell_dedup  # noqa: E402
from src.position_optimizer import PositionOptimizer  # noqa: E402


def _mem_trades():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, side TEXT, qty REAL, price REAL,
            pnl REAL, timestamp REAL, client_order_id TEXT)"""
    )
    return conn


class TestFillUniquenessDedup:
    """bug#42: orderId exact dedup with tolerance fallback for legacy rows."""

    def test_same_orderid_booked_once(self):
        conn = _mem_trades()
        assert insert_sell_dedup(conn, "ETHFI", 35.7, 0.6868, -2.5704,
                                 1000000000, client_order_id="2021755653")
        assert not insert_sell_dedup(conn, "ETHFI", 35.7, 0.6868, -2.5704,
                                     1000000600, client_order_id="2021755653")
        n = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert n == 1

    def test_distinct_orderids_both_kept(self):
        conn = _mem_trades()
        assert insert_sell_dedup(conn, "ETHFI", 18.5, 0.7117, -0.87,
                                 1000000000, client_order_id="2020211630")
        assert insert_sell_dedup(conn, "ETHFI", 20.0, 0.7284, 0.518,
                                 1000000000, client_order_id="2019697610")
        n = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert n == 2

    def test_null_orderid_falls_back_to_tolerance(self):
        conn = _mem_trades()
        ts = 1000000000.0
        assert insert_sell_dedup(conn, "ENA", 33.8, 0.1628, -0.5, ts)
        assert not insert_sell_dedup(conn, "ENA", 33.9, 0.1630, -0.5, ts + 60)
        assert insert_sell_dedup(conn, "ENA", 33.9, 0.1630, -0.5, ts + 7200)
        n = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert n == 2

    def test_u_case_repro(self):
        """9/15 U case: one real 66-share fill vs a phantom 42-share row —
        qty-tolerance dedup let the 66 through; orderId dedup cannot."""
        conn = _mem_trades()
        ts = 1000000000.0
        assert insert_sell_dedup(conn, "U", 42.0, 0.9998, 0.0, ts,
                                 client_order_id="987654321")
        assert not insert_sell_dedup(conn, "U", 66.0, 0.9998, 0.0066, ts,
                                     client_order_id="987654321")
        n = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert n == 1


class TestFillAvgPrice:
    """bug#42b: actual executed average price extraction (3 sources)."""

    def test_cummulative_quote(self):
        order = {"cummulativeQuoteQty": "40.4", "executedQty": "53.2"}
        assert PositionOptimizer._fill_avg_price(order, 0.7588) == pytest.approx(0.759398, abs=1e-6)

    def test_fills_weighted(self):
        order = {"fills": [
            {"qty": "18.7", "price": "0.7284"},
            {"qty": "1.3", "price": "0.7284"},
        ]}
        assert PositionOptimizer._fill_avg_price(order, 0.7) == pytest.approx(0.7284)

    def test_fallback(self):
        assert PositionOptimizer._fill_avg_price(None, 0.7588) == 0.7588
        assert PositionOptimizer._fill_avg_price({"status": "NEW"}, 0.7588) == 0.7588


class TestSwitchProtectionAndFillPrice:
    """bug#41a + WO-0921-013: OCO-first protection after the switch buy.

    The legacy SL-full→TP-full sequence hit -2010 (SL locks the whole
    balance) — tests now assert the OCO path and the split fallback.
    """

    @staticmethod
    def _bc(calls, oco_ret=None, sl_ret=None, tp_ret=None, min_notional="10"):
        def _oco(sym, q, tp, sl):
            calls.setdefault("oco", []).append((sym, q, tp, sl))
            if oco_ret is None:
                raise RuntimeError("OCO rejected")
            return oco_ret
        return SimpleNamespace(
            get_symbol_filters=lambda s: {
                "stepSize": "0.1", "tickSize": "0.0001",
                "minNotional": min_notional, "minQty": "0.1",
            },
            place_oco=_oco,
            place_stop_loss_limit=lambda *a, **k: (
                calls.setdefault("sl", []).append(a) and False
                if sl_ret is False else (
                    calls.setdefault("sl", []).append(a) or
                    {"orderId": 333} if sl_ret is None else sl_ret)),
            place_limit_sell=lambda *a, **k: (
                calls.setdefault("tp", []).append(a) and False
                if tp_ret is False else (
                    calls.setdefault("tp", []).append(a) or
                    {"orderId": 334} if tp_ret is None else tp_ret)),
        )

    def test_protection_placed_immediately(self):
        """OCO-first: one order covers both legs on the full qty."""
        calls = {}
        opt = object.__new__(PositionOptimizer)
        opt.bc = self._bc(calls, oco_ret={"orderListId": 9})
        buy_order = {"cummulativeQuoteQty": "40.4", "executedQty": "53.2"}
        opt._place_switch_protections("ETHFIUSDT", 53.2, buy_order, 0.7588)
        assert len(calls.get("oco", [])) == 1
        assert calls.get("sl", []) == [] and calls.get("tp", []) == []
        sym, q, tp_px, sl_px = calls["oco"][0]
        assert q == pytest.approx(53.2)
        assert sl_px == pytest.approx(0.7062, abs=1e-6)  # fill×0.93 tick

    def test_entry_fill_price_levels(self):
        """OCO legs derive from the FILL price, not the stale signal price."""
        calls = {}
        opt = object.__new__(PositionOptimizer)
        opt.bc = self._bc(calls, oco_ret={"orderListId": 9})
        opt._place_switch_protections("ETHFIUSDT", 53.2,
                                      {"cummulativeQuoteQty": "40.4", "executedQty": "53.2"},
                                      0.7588)
        fill = 40.4 / 53.2
        _, q, tp_px, sl_px = calls["oco"][0]
        assert sl_px == pytest.approx(int(fill * 0.93 / 0.0001) * 0.0001, abs=1e-9)
        assert tp_px == pytest.approx(int(fill * 1.04 / 0.0001) * 0.0001, abs=1e-9)

    def test_oco_rejected_falls_back_to_split(self):
        """OCO rejected → split legs (Strategy C semantics), no lock clash."""
        calls = {}
        opt = object.__new__(PositionOptimizer)
        opt.bc = self._bc(calls)  # oco raises
        opt._place_switch_protections("ETHFIUSDT", 53.2,
                                      {"cummulativeQuoteQty": "40.4", "executedQty": "53.2"},
                                      0.7588)
        assert len(calls["sl"]) == 1 and len(calls["tp"]) == 1
        sl_qty = calls["sl"][0][1]
        tp_qty = calls["tp"][0][1]
        assert sl_qty == pytest.approx(53.2 - 37.2)  # 70% TP floor→37.2
        assert tp_qty == pytest.approx(37.2)
        assert sl_qty + tp_qty == pytest.approx(53.2)

    def test_split_sl_slice_below_min_full_qty_sl(self):
        """Tiny position: split SL slice below minNotional → full-qty SL +
        PROTECTION_FAILED(TP) alert, guardian to heal later."""
        calls = {}
        opt = object.__new__(PositionOptimizer)
        # qty 20 @ ~0.76: total 15.2 > 10 OK; TP70% floor→14, SL slice 6
        # → 6×0.76=4.56 < 10 minNotional → full-qty SL branch
        opt.bc = self._bc(calls)
        opt._place_switch_protections("ETHFIUSDT", 20.0,
                                      {"cummulativeQuoteQty": "15.2", "executedQty": "20.0"},
                                      0.76)
        assert len(calls["sl"]) == 1
        assert calls["sl"][0][1] == pytest.approx(20.0)  # full qty
        assert calls.get("tp", []) == []

    def test_no_tp_when_sl_rejected(self):
        """OCO + split SL both rejected → no naked TP (capital first)."""
        calls = {}
        opt = object.__new__(PositionOptimizer)
        opt.bc = self._bc(calls, sl_ret=False)
        opt._place_switch_protections("ETHFIUSDT", 53.2,
                                      {"cummulativeQuoteQty": "40.4", "executedQty": "53.2"},
                                      0.7588)
        assert calls.get("tp", []) == []

    def test_below_minnotional_skipped(self):
        calls = {}
        opt = object.__new__(PositionOptimizer)
        opt.bc = self._bc(calls, min_notional="500")
        opt._place_switch_protections("ETHFIUSDT", 5.0,
                                      {"cummulativeQuoteQty": "3.8", "executedQty": "5.0"},
                                      0.76)
        assert calls.get("sl", []) == [] and calls.get("tp", []) == [] \
            and calls.get("oco", []) == []


class TestValidatorStaleReportScope:
    """bug#44b: balance claims in pre-last-trade reports are out of scope."""

    def _validator(self, last_ts):
        from scripts.report_validator import ReportValidator
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE trades (timestamp REAL)")
        if last_ts is not None:
            conn.execute("INSERT INTO trades (timestamp) VALUES (?)", (last_ts,))
        v = object.__new__(ReportValidator)
        v._db = SimpleNamespace(_get_conn=lambda: conn)
        return v

    def test_latest_trade_ts_reads_db(self):
        v = self._validator(1789277574.0)
        assert v._latest_trade_ts() == 1789277574.0
        v2 = self._validator(None)
        assert v2._latest_trade_ts() is None

    def test_stale_report_flagged(self):
        last = datetime(2026, 9, 15, 2, 0, 0)
        v = self._validator(last.timestamp())
        stale_notif = {"timestamp": (last - timedelta(minutes=30)).isoformat()}
        assert v._balance_claim_in_stale_scope(stale_notif) is True

    def test_fresh_report_not_flagged(self):
        last = datetime(2026, 9, 15, 2, 0, 0)
        v = self._validator(last.timestamp())
        fresh_notif = {"timestamp": (last + timedelta(minutes=30)).isoformat()}
        assert v._balance_claim_in_stale_scope(fresh_notif) is False
        assert v._balance_claim_in_stale_scope({}) is False


class TestTpFalsyReport:
    """2026-09-20 order-2: place_limit_sell returning falsy (soft
    rejection, no exception) must log TP FAILED, never "TP live"."""

    def _run(self, tp_ret, caplog):
        import logging as _lg
        opt = object.__new__(PositionOptimizer)
        opt.bc = SimpleNamespace(
            get_symbol_filters=lambda s: {
                "stepSize": "0.1", "tickSize": "0.0001", "minNotional": "10"},
            place_stop_loss_limit=lambda *a, **k: {"orderId": 1},
            place_limit_sell=lambda *a, **k: tp_ret,
        )
        buy_order = {"cummulativeQuoteQty": "40.4", "executedQty": "53.2"}
        with caplog.at_level(_lg.ERROR, logger="src.position_optimizer"):
            opt._place_switch_protections("ETHFIUSDT", 53.2, buy_order, 0.7588)

    def test_falsy_tp_logs_failed_not_live(self, caplog):
        self._run(None, caplog)
        assert "TP FAILED" in caplog.text
        assert "TP live" not in caplog.text

    def test_truthy_tp_logs_live(self, caplog):
        import logging as _lg
        opt = object.__new__(PositionOptimizer)
        opt.bc = SimpleNamespace(
            get_symbol_filters=lambda s: {
                "stepSize": "0.1", "tickSize": "0.0001", "minNotional": "10"},
            place_stop_loss_limit=lambda *a, **k: {"orderId": 1},
            place_limit_sell=lambda *a, **k: {"orderId": 334},
        )
        buy_order = {"cummulativeQuoteQty": "40.4", "executedQty": "53.2"}
        with caplog.at_level(_lg.INFO, logger="src.position_optimizer"):
            opt._place_switch_protections("ETHFIUSDT", 53.2, buy_order, 0.7588)
        assert "TP live" in caplog.text
