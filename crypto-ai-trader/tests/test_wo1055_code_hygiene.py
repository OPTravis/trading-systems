"""WO-1055: code-hygiene batch — evidence-backed tests for ②/③.

② protection_guardian: L1-calibrated-to-0 protective SELLs must SKIP
   (skipped+=1) instead of place(0) → reject → failed+=1 (02:12 INJ
   race: the sweep read a stale snapshot against a just-filled OCO).
③ portfolio_reconciler: OCO fill booking classifies the leg by realized
   pnl — 'tp' for the winning leg, 'sl' for the losing leg, exact-zero
   breakeven keeps 'reconciled'; protections.stoploss_guard no longer
   counts 'reconciled' toward 3-loss streaks (DB 10/14: all 19
   historical 'reconciled' rows were profitable — counting them was
   always wrong).

①/④ (daily_review giveback 口径, cron_bridge uid) live in the codeact
   scripts dir outside this repo — verified there, not here.
"""
import time
from types import SimpleNamespace

import pytest

from src import protection_guardian as pg
from src.portfolio_reconciler import (
    _oco_leg_reason, reconcile_portfolio_drift,
)
from src.protections import DEFAULTS, evaluate_locks
from src.state_db import StateDB


NOW = 1_000_000_000.0


# ── ③a: leg classification helper ───────────────────────────────────

class TestOcoLegReason:
    def test_winning_leg_books_tp(self):
        assert _oco_leg_reason(0.7514) == "tp"

    def test_losing_leg_books_sl(self):
        assert _oco_leg_reason(-0.296) == "sl"

    def test_exact_zero_breakeven_stays_reconciled(self):
        # fee-eaten breakeven: neither win nor loss
        assert _oco_leg_reason(0.0) == "reconciled"


# ── ③b: stoploss_guard streak semantics ────────────────────────────

def _cfg():
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    cfg["enabled"] = True
    return cfg


def _ev(ts, typ, reason=None, pnl=None, sym="LTCUSDT"):
    return {"symbol": sym, "ts": ts, "type": typ,
            "exit_reason": reason, "pnl": pnl}


class TestSlStreakAfterWo1055:
    def test_profitable_reconciled_no_longer_counts(self):
        # the 02:12-documented hazard: 2 SL + 1 profitable reconciled
        # OCO fill used to count as a 3-streak → 48h lock. Gone.
        evs = [
            _ev(NOW - 5000, "SELL", "sl", -1.0),
            _ev(NOW - 4000, "SELL", "sl", -1.0),
            _ev(NOW - 3000, "SELL", "reconciled", 2.0),
        ]
        locks = evaluate_locks(evs, "LTCUSDT", NOW, _cfg())
        assert "stoploss_guard" not in [l.reason for l in locks]

    def test_profitable_tp_leg_breaks_streak(self):
        evs = [
            _ev(NOW - 5000, "SELL", "sl", -1.0),
            _ev(NOW - 4000, "SELL", "sl", -1.0),
            _ev(NOW - 3000, "SELL", "tp", 2.0),
        ]
        locks = evaluate_locks(evs, "LTCUSDT", NOW, _cfg())
        assert "stoploss_guard" not in [l.reason for l in locks]

    def test_three_real_sl_legs_still_lock(self):
        evs = [
            _ev(NOW - 5000, "SELL", "sl", -1.0),
            _ev(NOW - 4000, "SELL", "sl", -1.0),
            _ev(NOW - 3000, "SELL", "sl", -1.0),
        ]
        locks = evaluate_locks(evs, "LTCUSDT", NOW, _cfg())
        assert "stoploss_guard" in [l.reason for l in locks]


# ── ③c: drift booking carries the classified leg ────────────────────

def _bal(asset, total, locked=0.0):
    return {"asset": asset, "free": str(total - locked), "locked": str(locked)}


def _fill(symbol, oid, qty, price, ts, commission="0",
          commission_asset="USDT"):
    return {
        "id": oid * 10, "price": str(price), "qty": str(qty),
        "quoteQty": str(round(qty * price, 8)), "commission": commission,
        "commissionAsset": commission_asset, "time": int(ts * 1000),
        "isBuyer": False, "isMaker": False, "orderId": oid,
        "symbol": symbol,
    }


class FakeClient:
    def __init__(self, balances, trades_by_symbol):
        self._balances = balances
        self._trades = trades_by_symbol

    def get_account(self):
        return {"balances": self._balances}

    def get_my_trades(self, symbol, limit=100, from_id=None):
        return self._trades.get(symbol, [])


@pytest.fixture
def db(tmp_path):
    d = StateDB(db_path=str(tmp_path / "state.db"))
    yield d
    d.close()


def _drift_case(db, fill_price):
    db.trade_add("ARBUSDT", "BUY", 67.8, 0.15)
    db._get_conn().execute(
        "UPDATE trades SET timestamp = ? WHERE symbol = 'ARBUSDT' "
        "AND side = 'BUY'", (time.time() - 7200,))
    db._get_conn().commit()
    db.portfolio_set("ARBUSDT", {"quantity": 67.8, "entry_price": 0.15,
                                 "strategy": "trend",
                                 "opened_at": time.time()})
    return FakeClient(
        [_bal("ARB", 0.0), _bal("USDT", 410)],
        {"ARBUSDT": [_fill("ARBUSDT", 555, 67.8, fill_price,
                           time.time() - 3600, commission="0.1",
                           commission_asset="ARB")]})


class TestDriftBookingLeg:
    @pytest.fixture(autouse=True)
    def _cap_record_fill(self, monkeypatch):
        import src.ledger as ledger_mod
        self.captured = []
        monkeypatch.setattr(
            ledger_mod, "record_fill",
            lambda ev, **kw: self.captured.append(ev) or {"status": "ok"})

    def test_profitable_drift_fill_books_tp(self, db):
        client = _drift_case(db, fill_price=0.1611)
        booked = reconcile_portfolio_drift(client, db)
        assert len(booked) == 1 and booked[0]["pnl"] > 0
        ev = next(e for e in self.captured
                  if e.get("type") == "SELL"
                  and e.get("source") == "reconciler.oco_fill")
        assert ev["exit_reason"] == "tp"

    def test_losing_drift_fill_books_sl(self, db):
        client = _drift_case(db, fill_price=0.14)
        booked = reconcile_portfolio_drift(client, db)
        assert len(booked) == 1 and booked[0]["pnl"] < 0
        ev = next(e for e in self.captured
                  if e.get("type") == "SELL"
                  and e.get("source") == "reconciler.oco_fill")
        assert ev["exit_reason"] == "sl"


# ── ②: guardian skips dust-calibrated protective SELLs ─────────────

def _pos(symbol, quantity, entry, take_profit=None, stop_loss=None,
         price=None):
    return {
        "symbol": symbol, "quantity": float(quantity),
        "entry_price": float(entry),
        "current_price": float(price if price is not None else entry),
        "price_is_stale": False, "strategy": "switch",
        "opened_at": 1789984344.0, "updated_at": 1789984344.0,
        "stop_loss": stop_loss, "take_profit": take_profit,
        "invest_pct": 0.1,
    }


class FakePortfolio:
    def __init__(self, positions):
        self._positions = positions

    def get_all_positions(self):
        return self._positions


class DustClient:
    """Guardian-shape client whose free balance is dust (02:12 INJ race:
    the OCO filled a moment before the sweep read its stale snapshot).

    get_free_balance → 0.00924 < stepSize 0.1 → L1 floors to 0.
    """

    def __init__(self, orders, free=0.00924):
        self.orders = orders
        self._free = free
        self.filters = {"stepSize": "0.1", "tickSize": "0.0001",
                        "minQty": "0.1", "minNotional": "10"}
        self.sl_placed = []
        self.free_calls = []

    def get_open_orders(self, sym):
        return self.orders

    def get_symbol_filters(self, sym):
        return self.filters

    def get_free_balance(self, asset):
        self.free_calls.append(asset)
        return self._free

    def place_stop_loss_limit(self, sym, qty, lim, stop):
        self.sl_placed.append((sym, qty, lim, stop))
        return {"orderId": 902}


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(pg, "emit_alert", lambda *a, **k: None)


class TestGuardianDustSkip:
    def test_naked_position_dust_free_balance_skips_not_fails(self):
        """02:12 INJ race reproduction: DB says 140.41 held (stale
        snapshot), the exchange actually has dust — L1 calibrates the
        emergency SL to 0 → SKIP (next sweep re-reads fresh state),
        never place(0) → reject → failed."""
        c = DustClient(orders=[])
        pos = _pos("INJUSDT", 140.41, 0.189, price=0.2226)
        res = pg.run(c, FakePortfolio([pos]))
        assert res["skipped"] == 1
        assert res["failed"] == 0
        assert res["healed"] == 0
        assert c.sl_placed == []          # never attempted place(0)
        assert c.free_calls == ["INJ"]    # calibration really ran

    def test_breach_naked_dust_free_balance_skips_not_fails(
            self, monkeypatch):
        """Same guard on the TP-breached emergency-SL branch (site ①)."""
        store = {}
        db = SimpleNamespace(
            kv_get=lambda k: store.get(k),
            kv_set=lambda k, v: store.__setitem__(k, v))
        import src.state_db as sdb
        monkeypatch.setattr(sdb, "get_state_db", lambda: db)
        monkeypatch.setattr(pg, "get_state_db", lambda: db, raising=False)
        c = DustClient(orders=[])
        pos = _pos("FETUSDT", 84.6, 0.1879, take_profit=0.199174,
                   price=0.2056)
        res = pg.run(c, FakePortfolio([pos]))
        assert res["skipped"] == 1
        assert res["failed"] == 0
        assert res["healed"] == 0
        assert c.sl_placed == []
