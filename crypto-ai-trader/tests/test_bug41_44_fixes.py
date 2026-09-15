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
    """bug#41a: SL/TP placed immediately after the switch buy fills."""

    def test_protection_placed_immediately(self):
        calls = {"sl": [], "tp": []}
        opt = object.__new__(PositionOptimizer)
        opt.bc = SimpleNamespace(
            get_symbol_filters=lambda s: {
                "stepSize": "0.1", "tickSize": "0.0001", "minNotional": "10",
            },
            place_stop_loss_limit=lambda *a, **k: calls.__setitem__(
                "sl", calls["sl"] + [a]) or {"orderId": 333},
            place_limit_sell=lambda *a, **k: calls.__setitem__(
                "tp", calls["tp"] + [a]) or {"orderId": 334},
        )
        buy_order = {"cummulativeQuoteQty": "40.4", "executedQty": "53.2"}
        opt._place_switch_protections("ETHFIUSDT", 53.2, buy_order, 0.7588)
        assert len(calls["sl"]) == 1 and len(calls["tp"]) == 1
        assert calls["sl"][0][1] == pytest.approx(53.2)
        assert calls["sl"][0][3] == pytest.approx(0.7062, abs=1e-6)

    def test_entry_fill_price_levels(self):
        """SL/TP derive from the FILL price, not the stale signal price."""
        seen = {}
        opt = object.__new__(PositionOptimizer)
        opt.bc = SimpleNamespace(
            get_symbol_filters=lambda s: {
                "stepSize": "0.1", "tickSize": "0.0001", "minNotional": "10",
            },
            place_stop_loss_limit=lambda sym, q, lim, px: seen.update(sl=px) or {"orderId": 1},
            place_limit_sell=lambda sym, q, px: seen.update(tp=px) or {"orderId": 2},
        )
        opt._place_switch_protections("ETHFIUSDT", 53.2,
                                      {"cummulativeQuoteQty": "40.4", "executedQty": "53.2"},
                                      0.7588)
        fill = 40.4 / 53.2
        assert seen["sl"] == pytest.approx(int(fill * 0.93 / 0.0001) * 0.0001, abs=1e-9)
        assert seen["tp"] == pytest.approx(int(fill * 1.04 / 0.0001) * 0.0001, abs=1e-9)

    def test_no_tp_when_sl_rejected(self):
        calls = {"tp": 0}
        opt = object.__new__(PositionOptimizer)
        opt.bc = SimpleNamespace(
            get_symbol_filters=lambda s: {
                "stepSize": "0.1", "tickSize": "0.0001", "minNotional": "10",
            },
            place_stop_loss_limit=lambda *a, **k: None,
            place_limit_sell=lambda *a, **k: calls.__setitem__("tp", calls["tp"] + 1),
        )
        opt._place_switch_protections("ETHFIUSDT", 53.2,
                                      {"cummulativeQuoteQty": "40.4", "executedQty": "53.2"},
                                      0.7588)
        assert calls["tp"] == 0

    def test_below_minnotional_skipped(self):
        calls = {"sl": 0, "tp": 0}
        opt = object.__new__(PositionOptimizer)
        opt.bc = SimpleNamespace(
            get_symbol_filters=lambda s: {
                "stepSize": "0.1", "tickSize": "0.0001", "minNotional": "500",
            },
            place_stop_loss_limit=lambda *a, **k: calls.__setitem__("sl", calls["sl"] + 1),
            place_limit_sell=lambda *a, **k: calls.__setitem__("tp", calls["tp"] + 1),
        )
        opt._place_switch_protections("ETHFIUSDT", 5.0,
                                      {"cummulativeQuoteQty": "3.8", "executedQty": "5.0"},
                                      0.76)
        assert calls["sl"] == 0 and calls["tp"] == 0


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
