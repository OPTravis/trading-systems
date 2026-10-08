"""WO-1022 (10/8): C1 partial-deleverage fix + S6/S4 switch hardening.

C1 (audit WO-1021, 会丟钱·latent): circuit_tiers._sell_notional partial
sell used to call portfolio.close_position — full-row pop + pnl/trade
qty booked for the WHOLE position while binance sold only a slice:
  ① sync_from_binance rebuilt the row next cycle (max_hold clock reset,
     stop_loss floor lost — protection degraded to the fixed band)
  ② leftover SL/TP legs outlived the trimmed qty (order qty > position,
     partial rejections when triggered)
  ③ trade_add SELL qty ≠ actual fill qty (order-id idempotency passed
     while the booked qty was wrong)

Fix under test (reconciler-anchored, WO-0928 single-writer pattern):
  - FULL sells keep the legacy close_position path (liquidation tier —
    the whole position really is gone, full-pop semantics are correct)
  - PARTIAL sells keep the portfolio row; portfolio_reconciler books
    the fill from binance myTrades next cycle (order-id idempotent) and
    trims qty while preserving stop_loss floor and the BUY-anchored
    max_hold clock
  - resting SELL legs are cancelled BEFORE the market sell

S6: a sell-side cancel failure in _execute_switch aborts the switch
instead of continuing into a min(qty, free) partial sell whose residue
ran naked until the next guardian sweep.

S4: the switch OCO stop reads the per-symbol sl_pct ladder
(exit:{sym}:sl_pct → exit:sl_pct → legacy -7%), same source as
exit_check / the invariant guard band.
"""
import os
import sys
import tempfile
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    import src.binance_client  # noqa: F401
except Exception:
    sys.modules["src.binance_client"] = types.SimpleNamespace(
        BinanceClient=object)

from src.state_db import get_state_db
from src.circuit_tiers import _sell_notional
from src.portfolio_reconciler import reconcile_portfolio_drift
from src.position_optimizer import PositionOptimizer  # noqa: E402

NEAR = "NEARUSDT"
ENTRY = 5.0
FLOOR = 4.5          # trailing floor that must survive the trim
SELL_PX = 4.9


# ── C1: circuit_tiers partial deleverage ──────────────────────────────

class CircuitClient:
    """Fake exchange carrying the post-partial-sell state for both
    _sell_notional and the reconciler pass."""

    def __init__(self, *, remaining_free="5.0", fill_qty="5.0",
                 fill_px="4.9"):
        self.cancels = []
        self.sold = None
        self.remaining_free = remaining_free
        self.fill_qty = fill_qty
        self.fill_px = fill_px

    # circuit_tiers surface
    def cancel_all_orders(self, symbol):
        self.cancels.append(symbol)
        return True

    def place_order(self, symbol, side, order_type, quantity):
        self.sold = (symbol, side, order_type, quantity)
        return {"orderId": 777, "clientOrderId": "ct-777",
                "status": "FILLED",
                "fills": [{"qty": self.fill_qty, "price": self.fill_px}]}

    # reconciler surface
    def get_open_orders(self, symbol=None):
        return []

    def cancel_order(self, symbol, order_id):
        return {"orderId": order_id, "status": "CANCELED"}

    def get_account(self):
        return {"balances": [
            {"asset": "NEAR", "free": self.remaining_free, "locked": "0"},
            {"asset": "USDT", "free": "100", "locked": "0"},
        ]}

    def get_my_trades(self, symbol, limit=100, from_id=None):
        t0 = time.time() - 36000
        return [
            {"id": 1, "orderId": 555, "price": "5.0", "qty": "10",
             "quoteQty": "50", "commission": "0",
             "commissionAsset": "NEAR", "time": int(t0 * 1000),
             "isBuyer": True, "isMaker": False},
            {"id": 2, "orderId": 777, "price": self.fill_px,
             "qty": self.fill_qty, "quoteQty": "24.5", "commission": "0",
             "commissionAsset": "NEAR", "time": int((t0 + 1000) * 1000),
             "isBuyer": False, "isMaker": False},
        ]


class FakePortfolio:
    """Records close_position — a partial sell must NEVER hit it."""

    def __init__(self):
        self.closed = []

    def close_position(self, symbol, close_price=None, exit_reason=None,
                       client_order_id=None):
        self.closed.append((symbol, exit_reason, client_order_id))
        return {"success": True}


@pytest.fixture
def db():
    d = tempfile.mkdtemp()
    os.environ["STATE_DB_PATH"] = os.path.join(d, "wo1022.db")
    os.environ["TESTING"] = "1"
    yield get_state_db()
    os.environ["STATE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "n.db")


def _seed(db, *, qty=10.0, stop_loss=FLOOR):
    now = time.time()
    t_buy = now - 10 * 3600
    db.trade_add(NEAR, "BUY", qty, ENTRY)
    conn = db._get_conn()
    conn.execute(
        "UPDATE trades SET timestamp = ? WHERE symbol = ? AND side = 'BUY'",
        (t_buy, NEAR))
    conn.commit()
    row = {"quantity": qty, "entry_price": ENTRY, "strategy": "technical_v2",
           "opened_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                      time.localtime(t_buy))}
    if stop_loss is not None:
        row["stop_loss"] = stop_loss
    db.portfolio_set(NEAR, row)
    return t_buy


def _buy_ts(db):
    rows = db._get_conn().execute(
        "SELECT timestamp FROM trades WHERE symbol = ? AND side = 'BUY' "
        "ORDER BY timestamp", (NEAR,)).fetchall()
    return [r[0] for r in rows]


def _sell_rows(db):
    return db._get_conn().execute(
        "SELECT qty, price, client_order_id FROM trades "
        "WHERE symbol = ? AND side = 'SELL'", (NEAR,)).fetchall()


class TestPartialSell:

    def test_partial_sell_keeps_row_reconciler_trims(self, db):
        """The five WO-1022 acceptance assertions in one chain:
        floor preserved / max_hold anchor untouched / legs cancelled
        before the sell / booked SELL qty == fill qty / repeat
        reconcile idempotent."""
        _seed(db)
        cli = CircuitClient()
        pf = FakePortfolio()
        mark = {"symbol": NEAR, "qty": 10.0, "price": SELL_PX,
                "notional": 49.0}
        res = _sell_notional(cli, pf, mark, sell_notional=24.5)
        # qty = min(10, 24.5/4.9) = 5 → PARTIAL
        assert res is not None and res["partial"] is True
        assert res["qty"] == pytest.approx(5.0)
        assert res["price"] == pytest.approx(SELL_PX)
        # resting legs cancelled before the market sell, no full close
        assert cli.cancels == [NEAR]
        assert pf.closed == []
        # pre-reconcile: row intact (qty unchanged, floor preserved,
        # no SELL booked yet)
        pos = db.portfolio_get(NEAR)
        assert pos is not None
        assert pos["quantity"] == pytest.approx(10.0)
        assert pos.get("stop_loss") == pytest.approx(FLOOR)
        assert _sell_rows(db) == []
        buy_before = _buy_ts(db)

        # next cycle: reconciler books the fill + trims qty
        reconcile_portfolio_drift(CircuitClient(), db)
        pos = db.portfolio_get(NEAR)
        assert pos is not None
        assert pos["quantity"] == pytest.approx(5.0)          # trimmed
        assert pos.get("stop_loss") == pytest.approx(FLOOR)   # floor kept
        sells = _sell_rows(db)
        assert len(sells) == 1
        assert sells[0][0] == pytest.approx(5.0)   # fill qty, NOT the 10
        assert sells[0][1] == pytest.approx(SELL_PX)
        # max_hold anchor untouched: no new BUY row, timestamps identical
        assert _buy_ts(db) == buy_before

        # idempotent: repeat reconcile neither re-books nor re-trims
        reconcile_portfolio_drift(CircuitClient(), db)
        assert len(_sell_rows(db)) == 1
        assert db.portfolio_get(NEAR)["quantity"] == pytest.approx(5.0)
        assert db.portfolio_get(NEAR).get("stop_loss") == pytest.approx(FLOOR)

    def test_tiny_residual_not_treated_as_full_close(self, db):
        """deleverage excess within 0.1% of notional is still a full
        sell (min() floors qty to the whole position) — boundary lock."""
        _seed(db)
        cli = CircuitClient(fill_qty="10.0")
        pf = FakePortfolio()
        mark = {"symbol": NEAR, "qty": 10.0, "price": SELL_PX,
                "notional": 49.0}
        res = _sell_notional(cli, pf, mark, sell_notional=48.96)
        assert res["partial"] is False        # ≥ notional × 0.999
        assert pf.closed and pf.closed[0][0] == NEAR


class TestFullSell:

    def test_liquidation_still_full_closes(self, db):
        """T3 LIQUIDATE_ALL sells the entire position — the legacy
        close_position path is CORRECT there and must survive."""
        _seed(db)
        cli = CircuitClient(fill_qty="10.0")
        pf = FakePortfolio()
        mark = {"symbol": NEAR, "qty": 10.0, "price": SELL_PX,
                "notional": 49.0}
        res = _sell_notional(cli, pf, mark, sell_notional=49.5)
        assert res["partial"] is False
        assert res["qty"] == pytest.approx(10.0)
        assert pf.closed and pf.closed[0][0] == NEAR
        assert pf.closed[0][1] == "circuit_tiers_sell"
        assert cli.cancels == [NEAR]    # legs cancelled here too

    def test_sell_failure_returns_none_no_book_touch(self, db):
        _seed(db)

        class FailClient(CircuitClient):
            def place_order(self, symbol, side, order_type, quantity):
                raise RuntimeError("binance down")

        cli = FailClient()
        pf = FakePortfolio()
        mark = {"symbol": NEAR, "qty": 10.0, "price": SELL_PX,
                "notional": 49.0}
        assert _sell_notional(cli, pf, mark, sell_notional=24.5) is None
        assert pf.closed == []
        assert db.portfolio_get(NEAR)["quantity"] == pytest.approx(10.0)


# ── S6/S4: switch hardening (harness after test_wo1009) ───────────────

class FakeBC:
    def __init__(self, *, cancel_error_symbols=()):
        self.cancels = []
        self.ocos = []
        self.buys = []
        self.sells = []
        self._cancel_error_symbols = set(cancel_error_symbols)
        self.prices = {"ICPUSDT": 3.4, "NEARUSDT": 5.5}

    def get_symbol_filters(self, symbol):
        return {"minQty": 0.0, "minNotional": 5.0, "stepSize": 0.1,
                "tickSize": 0.001}

    def get_ticker_price(self, symbol=0, **kw):
        return self.prices.get(symbol, 1.0)

    def get_24hr_stats(self, symbol):
        return {"last_price": self.prices.get(symbol, 1.0)}

    def get_account(self):
        return {"balances": [{"asset": "ICP", "free": "10.0"}]}

    def get_free_balance(self, asset="USDT"):
        return 12.0

    def cancel_all_orders(self, symbol):
        if symbol in self._cancel_error_symbols:
            raise RuntimeError("proxy hiccup")
        self.cancels.append(symbol)
        return True

    def place_market_sell(self, symbol, quantity):
        self.sells.append((symbol, quantity))
        return {"orderId": 7, "status": "FILLED",
                "executedQty": f"{quantity}",
                "cummulativeQuoteQty": f"{quantity * self.prices[symbol]}"}

    def place_market_buy(self, symbol, quantity):
        self.buys.append((symbol, quantity))
        return {"orderId": 9, "status": "FILLED",
                "executedQty": f"{quantity}",
                "cummulativeQuoteQty": f"{quantity * self.prices[symbol]}"}

    def place_oco(self, symbol, quantity, tp_price, sl_price,
                  sl_limit_price=None):
        self.ocos.append({"symbol": symbol, "qty": quantity,
                          "tp": tp_price, "sl": sl_price})
        return {"orderId": 11, "status": "FILLED", "origQty": str(quantity)}


class FakeSwitchPortfolio:
    def __init__(self, positions):
        self._positions = positions
        self.closed = []
        self.added = []

    def get_all_positions(self):
        return self._positions

    def close_position(self, symbol, close_price=None, exit_reason=None,
                       client_order_id=None):
        self.closed.append(symbol)
        self._positions = [p for p in self._positions
                           if p["symbol"] != symbol]

    def add_position(self, symbol, quantity, entry_price, strategy=None,
                     deduct_cash=True, _skip_validation=False):
        self.added.append((symbol, quantity, entry_price))


def _run_switch(monkeypatch, tmp_path, *, held_near=True, cancel_error=(),
                db=None):
    bc = FakeBC(cancel_error_symbols=cancel_error)
    positions = [
        {"symbol": "ICPUSDT", "quantity": 1.72, "entry_price": 3.47},
    ]
    if held_near:
        positions.append(
            {"symbol": "NEARUSDT", "quantity": 1.1988, "entry_price": 5.478})
    portfolio = FakeSwitchPortfolio(positions)

    opt = object.__new__(PositionOptimizer)
    opt.bc = bc
    opt.portfolio = portfolio
    opt.risk_manager = None
    opt._last_switch_time = {}
    monkeypatch.setattr(opt, "_save_switch_times", lambda: None)

    fake_db = db or SimpleNamespace(
        drawdown_get=lambda: {"current_drawdown_pct": 0.0})
    monkeypatch.setattr("src.state_db.get_state_db", lambda: fake_db)
    monkeypatch.setattr(
        "src.stepwise_drawdown.get_drawdown_action",
        lambda pct, read_only=False, db=None, now=None:
        {"level": "normal", "block_new_trades": False, "close_all": False,
         "size_multiplier": 1.0, "sl_tightening": 1.0, "reason": "",
         "time_in_level": 0.0, "escalated": False})

    decision = {"from_symbol": "ICPUSDT", "to_symbol": "NEARUSDT",
                "from_value": 6.0, "from_price": 3.4}
    ok = opt._execute_switch(decision)
    return ok, bc, portfolio


class TestSwitchAbortOnCancelFail:
    """S6: sell-side cancel failure aborts the switch."""

    def test_sell_side_cancel_failure_aborts(self, monkeypatch, tmp_path):
        ok, bc, pf = _run_switch(monkeypatch, tmp_path,
                                 cancel_error=("ICPUSDT",))
        assert ok is False
        assert bc.sells == []      # no partial naked sell went out
        assert bc.buys == []       # no buy leg either — full abort
        assert bc.ocos == []
        assert pf.closed == []     # old position book untouched

    def test_buy_side_cancel_failure_still_protects(self, monkeypatch,
                                                    tmp_path):
        """Regression lock vs WO-1009: a failed STALE-LEG cancel on the
        TARGET symbol (step 8b) must NOT abort — the OCO attempt and its
        guardian backstop still run."""
        ok, bc, pf = _run_switch(monkeypatch, tmp_path,
                                 cancel_error=("NEARUSDT",))
        assert ok is True
        assert "ICPUSDT" in bc.cancels
        assert len(bc.ocos) == 1
        assert bc.ocos[0]["qty"] == pytest.approx(2.1)


class TestSwitchSlPctConfig:
    """S4: switch OCO stop reads the per-symbol sl_pct ladder."""

    def test_per_symbol_sl_pct_shapes_oco(self, monkeypatch, tmp_path):
        fake_db = SimpleNamespace(
            drawdown_get=lambda: {"current_drawdown_pct": 0.0},
            kv_get=lambda k: ("5.0" if k == "exit:NEARUSDT:sl_pct"
                              else None))
        ok, bc, pf = _run_switch(monkeypatch, tmp_path, db=fake_db)
        assert ok is True
        assert len(bc.ocos) == 1
        # fill 5.5 → stop at -5% (band tightened per-symbol)
        assert bc.ocos[0]["sl"] == pytest.approx(5.5 * 0.95, abs=2e-3)

    def test_global_sl_pct_applies_when_no_symbol_key(self, monkeypatch,
                                                      tmp_path):
        fake_db = SimpleNamespace(
            drawdown_get=lambda: {"current_drawdown_pct": 0.0},
            kv_get=lambda k: ("6.0" if k == "exit:sl_pct" else None))
        ok, bc, pf = _run_switch(monkeypatch, tmp_path, db=fake_db)
        assert ok is True
        assert bc.ocos[0]["sl"] == pytest.approx(5.5 * 0.94, abs=2e-3)

    def test_no_config_falls_back_to_legacy_7pct(self, monkeypatch,
                                                 tmp_path):
        fake_db = SimpleNamespace(
            drawdown_get=lambda: {"current_drawdown_pct": 0.0},
            kv_get=lambda k: None)
        ok, bc, pf = _run_switch(monkeypatch, tmp_path, db=fake_db)
        assert ok is True
        # legacy -7% on fill 5.5, tick-floored (WO-1009 expectation)
        assert bc.ocos[0]["sl"] == pytest.approx(5.114, abs=1e-6)

    def test_kv_db_without_kv_get_fails_open(self, monkeypatch, tmp_path):
        """Harness shapes like WO-1009's (no kv_get on the fake db):
        S4 must fail open to the legacy -7%, never raise."""
        fake_db = SimpleNamespace(
            drawdown_get=lambda: {"current_drawdown_pct": 0.0})
        ok, bc, pf = _run_switch(monkeypatch, tmp_path, db=fake_db)
        assert ok is True
        assert bc.ocos[0]["sl"] == pytest.approx(5.114, abs=1e-6)
