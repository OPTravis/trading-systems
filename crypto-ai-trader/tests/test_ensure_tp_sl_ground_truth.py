"""bug(-2010 / SL coverage): order quantities must come from the exchange's
ground truth, never from guessed filter defaults or the un-reconciled book.

Evidence A (-2010): multi-leg TP fills + fee residue leave portfolio.quantity
above the real balance; reordering the full book amount gets rejected with
-2010 insufficient balance.

Evidence B (coverage): get_symbol_filters failures fell back to
stepSize=0.001, flooring BNB's 5-decimal LOT_SIZE and leaving SL covering
89.4% of a 0.00894469 holding.
"""
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import ensure_tp_sl as ensure
from src.state_db import StateDB


# ─────────────────────────── fakes ───────────────────────────

def _filters_block(step, tick, min_notional, symbol="ETHFIUSDT"):
    return {
        "symbol": symbol,
        "filters": [
            {"filterType": "LOT_SIZE", "stepSize": step, "minQty": step},
            {"filterType": "PRICE_FILTER", "tickSize": tick, "minPrice": tick},
            {"filterType": "MIN_NOTIONAL", "minNotional": str(min_notional)},
        ],
    }


class FakeClient:
    """Stateful fake: cancel releases locked->free like the real exchange."""

    def __init__(self, balances, orders, symbols, price=600.0, account_exc=None):
        # balances: {"BNB": (free, locked)}; orders carry symbol+asset
        self._balances = {a: {"free": float(f), "locked": float(l)}
                          for a, (f, l) in balances.items()}
        self._orders = [dict(o) for o in orders]
        self._symbols = symbols
        self._price = price
        self._account_exc = account_exc
        self.calls = []

    # -- account / info --
    def get_account(self):
        if self._account_exc:
            raise self._account_exc
        return {"balances": [
            {"asset": a, "free": str(v["free"]), "locked": str(v["locked"])}
            for a, v in self._balances.items()
        ]}

    def _get_exchange_info(self):
        return {"symbols": self._symbols}

    def get_ticker_price(self, symbol=None):
        return self._price

    def get_open_orders(self, symbol=None):
        return [dict(o) for o in self._orders if o.get("symbol") == symbol]

    def get_free_balance(self, asset="USDT"):
        return self._balances.get(asset, {}).get("free", 0.0)

    # -- mutations --
    def cancel_order(self, symbol, order_id):
        o = next((o for o in self._orders
                  if o.get("orderId") == order_id and o.get("symbol") == symbol), None)
        if o:
            self._orders.remove(o)
            q = float(o["origQty"])
            b = self._balances.setdefault(o["asset"], {"free": 0.0, "locked": 0.0})
            b["locked"] = max(b["locked"] - q, 0.0)
            b["free"] += q
        self.calls.append(("cancel", order_id))
        return True

    def _lock(self, symbol, quantity, tag):
        asset = symbol.replace("USDT", "")
        b = self._balances.setdefault(asset, {"free": 0.0, "locked": 0.0})
        b["free"] = max(b["free"] - quantity, 0.0)
        b["locked"] += quantity
        self.calls.append((tag, quantity))

    def place_oco(self, symbol=None, quantity=None, tp_price=None,
                  sl_price=None, sl_limit_price=None, **kw):
        self._lock(symbol, quantity, "place_oco")
        return {"status": "FILLED"}

    def place_limit_sell(self, symbol, quantity, price, **kw):
        self._lock(symbol, quantity, "place_limit_sell")
        return {"status": "FILLED"}

    def place_stop_loss_limit(self, symbol, quantity, limit_price, stop_price, **kw):
        self._lock(symbol, quantity, "place_stop_loss_limit")
        return {"status": "FILLED"}

    def place_market_sell(self, symbol, quantity, **kw):
        self._lock(symbol, quantity, "place_market_sell")
        return {"status": "FILLED"}


# ─────────────────── unit: _required_filters ───────────────────

class TestRequiredFilters:
    def test_complete_filters_pass_through(self):
        c = FakeClient({}, [], [_filters_block("0.00001", "0.01", 5.0, symbol="BNBUSDT")])
        f = ensure._required_filters(c, "BNBUSDT")
        assert f["stepSize"] == 0.00001
        assert f["minNotional"] == 5.0

    def test_missing_lot_size_returns_none(self):
        bad = {"symbol": "XUSDT", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
        ]}
        c = FakeClient({}, [], [bad])
        assert ensure._required_filters(c, "XUSDT") is None

    def test_symbol_absent_returns_none(self):
        c = FakeClient({}, [], [_filters_block("0.001", "0.01", 5.0)])
        assert ensure._required_filters(c, "NOTLISTEDUSDT") is None  # symbol absent


# ─────────────────── unit: _gt_holding / _placeable_qty ───────────────────

class TestGtHolding:
    def test_book_above_balance_is_calibrated_down(self):
        c = FakeClient({"ETHFI": (8.5, 0.0)}, [], [])
        qty, free, err = ensure._gt_holding(c, "ETHFIUSDT", 10.0)
        assert qty == pytest.approx(8.5)
        assert free == pytest.approx(8.5)
        assert err is None

    def test_locked_counts_toward_holding(self):
        # SL order locks 0.008; holding = free + locked = full position
        c = FakeClient({"BNB": (0.00094469, 0.008)}, [], [])
        qty, free, err = ensure._gt_holding(c, "BNBUSDT", 0.00894469)
        assert qty == pytest.approx(0.00894469)
        assert free == pytest.approx(0.00094469)
        assert err is None

    def test_asset_missing_falls_back_loud(self):
        c = FakeClient({"USDT": (500.0, 0.0)}, [], [])
        qty, free, err = ensure._gt_holding(c, "ZZZUSDT", 3.0)
        assert qty == 3.0 and free is None and err

    def test_api_failure_falls_back_loud(self):
        c = FakeClient({}, [], [], account_exc=RuntimeError("net down"))
        qty, free, err = ensure._gt_holding(c, "AAAUSDT", 2.0)
        assert qty == 2.0 and free is None and "net down" in err


class TestPlaceableQty:
    def test_caps_to_free_after_cancel_released(self):
        # after cancels, everything is free
        c = FakeClient({"ETHFI": (8.5, 0.0)}, [], [])
        assert ensure._placeable_qty(c, "ETHFIUSDT", 10.0) == pytest.approx(8.5)

    def test_keeps_qty_when_balance_unreadable(self):
        c = FakeClient({}, [], [], account_exc=RuntimeError("x"))
        assert ensure._placeable_qty(c, "AAAUSDT", 4.0) == pytest.approx(4.0)


# ─────────────────── end-to-end via main() ───────────────────

def _run_main(monkeypatch, tmp_path, client, positions):
    db = StateDB(db_path=str(tmp_path / "state.db"))
    monkeypatch.setattr(ensure, "BinanceClient", lambda testnet=False: client)
    monkeypatch.setattr(ensure, "get_positions_with_targets", lambda: positions)
    monkeypatch.setattr(ensure, "get_state_db", lambda: db)
    import io as _io
    import contextlib
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        ensure.main()
    try:
        return json.loads(buf.getvalue())
    except json.JSONDecodeError:
        return {"raw": buf.getvalue()}


def _pos(qty, entry, sl, tp):
    return {"quantity": qty, "entry_price": entry, "stop_loss": sl, "take_profit": tp}


class TestMainGroundTruth:
    def test_filters_missing_skips_symbol_no_default_math(self, monkeypatch, tmp_path):
        """Evidence B guard: no exchange info → skip with error, no orders."""
        c = FakeClient(
            {"ETHFI": (10.0, 0.0)},
            [],
            [],          # exchange info has no symbols at all
            price=3.0,
        )
        out = _run_main(monkeypatch, tmp_path, c,
                        {"ETHFIUSDT": _pos(10.0, 2.9, 2.7, 3.3)})
        placed = [k for k, *_ in c.calls if k.startswith("place")]
        assert placed == []
        assert any("filters" in e for e in out["errors"])

    def test_balance_calibration_failure_skips_symbol(self, monkeypatch, tmp_path):
        c = FakeClient(
            {"ETHFI": (10.0, 0.0)},
            [],
            [_filters_block("0.001", "0.01", 5.0)],
            price=3.0,
            account_exc=RuntimeError("acct boom"),
        )
        out = _run_main(monkeypatch, tmp_path, c,
                        {"ETHFIUSDT": _pos(10.0, 2.9, 2.7, 3.3)})
        placed = [k for k, *_ in c.calls if k.startswith("place")]
        assert placed == []
        assert any("校準失敗" in e for e in out["errors"])

    def test_case3_oco_uses_balance_not_book_avoids_2010(self, monkeypatch, tmp_path):
        """Evidence A: book 10 but exchange only holds 8.5 → OCO 8.5, not 10."""
        c = FakeClient(
            {"ETHFI": (8.5, 0.0)},
            [],
            [_filters_block("0.001", "0.01", 5.0)],
            price=3.0,
        )
        out = _run_main(monkeypatch, tmp_path, c,
                        {"ETHFIUSDT": _pos(10.0, 2.9, 2.7, 3.3)})
        ocos = [q for k, q in c.calls if k == "place_oco"]
        assert ocos and ocos[0] == pytest.approx(8.5)
        assert any("OCO" in f for f in out["fixes"])

    def test_case0_restructure_caps_to_fresh_free(self, monkeypatch, tmp_path):
        """Book 10, SL+TP lock 8, free 0.5 → after cancels OCO 8.5 (not 10)."""
        orders = [
            {"symbol": "ETHFIUSDT", "asset": "ETHFI", "type": "STOP_LOSS_LIMIT",
             "side": "SELL", "origQty": "4", "orderId": 101, "stopPrice": 2.7},
            {"symbol": "ETHFIUSDT", "asset": "ETHFI", "type": "LIMIT",
             "side": "SELL", "origQty": "4", "orderId": 102, "price": 3.3},
        ]
        c = FakeClient(
            {"ETHFI": (0.5, 8.0)},
            orders,
            [_filters_block("0.001", "0.01", 5.0)],
            price=3.0,
        )
        out = _run_main(monkeypatch, tmp_path, c,
                        {"ETHFIUSDT": _pos(10.0, 2.9, 2.7, 3.3)})
        ocos = [q for k, q in c.calls if k == "place_oco"]
        assert ocos and ocos[0] == pytest.approx(8.5)   # min(book, free+locked)


class TestCase15TopUp:
    def _bnb_setup(self):
        """The production BNB case: holding 0.00894469, SL covers only 0.008,
        real stepSize 0.00001 (5 decimals), tail below minNotional."""
        orders = [
            {"symbol": "BNBUSDT", "asset": "BNB", "type": "STOP_LOSS_LIMIT",
             "side": "SELL", "origQty": "0.008", "orderId": 201, "stopPrice": 540.0},
            {"symbol": "BNBUSDT", "asset": "BNB", "type": "LIMIT",
             "side": "SELL", "origQty": "0.0005", "orderId": 202, "price": 660.0},
        ]
        client = FakeClient(
            {"BNB": (0.00094469, 0.0085)},   # free + locked = 0.00944469 ≈ book-ish
            orders,
            [_filters_block("0.00001", "0.01", 5.0, symbol="BNBUSDT")],
            price=600.0,
        )
        positions = {"BNBUSDT": _pos(0.00894469, 580.0, 540.0, 660.0)}
        return client, positions

    def test_bnb_tail_restructures_full_oco_99pct_coverage(self, monkeypatch, tmp_path):
        """Old code floored the tail to 0 with stepSize=0.001 default and did
        nothing (89.4% coverage, 11% naked). New code must restructure into a
        full OCO covering >=99% of the calibrated holding."""
        c, positions = self._bnb_setup()
        out = _run_main(monkeypatch, tmp_path, c, positions)
        ocos = [q for k, q in c.calls if k == "place_oco"]
        assert ocos, f"no OCO placed; calls={c.calls}; out={out}"
        covered = ocos[0] / 0.00894469
        assert covered >= 0.99, f"coverage {covered:.3%}"
        # SL must be cancelled too — a live SL next to a full OCO double-counts
        cancelled = [oid for k, oid in c.calls if k == "cancel"]
        assert 201 in cancelled and 202 in cancelled

    def test_top_up_gap_capped_at_free_avoids_2010(self, monkeypatch, tmp_path):
        """Fee-residue variant: book 100.5, SL covers 60, exchange only holds
        99.9 (free 39.9) → top-up 39.9, never the 40.5 the book implies."""
        # two TP legs = tiered exit design → Case 0 restructure is skipped
        # (bug#17), routing straight into the Case 1.5 top-up branch
        orders = [
            {"symbol": "ARBUSDT", "asset": "ARB", "type": "STOP_LOSS_LIMIT",
             "side": "SELL", "origQty": "60", "orderId": 301, "stopPrice": 0.9},
            {"symbol": "ARBUSDT", "asset": "ARB", "type": "LIMIT",
             "side": "SELL", "origQty": "1", "orderId": 302, "price": 1.4},
            {"symbol": "ARBUSDT", "asset": "ARB", "type": "LIMIT",
             "side": "SELL", "origQty": "1", "orderId": 303, "price": 1.5},
        ]
        c = FakeClient(
            {"ARB": (39.9, 60.0)},          # free+locked = 99.9 < book 100.5
            orders,
            [_filters_block("0.001", "0.01", 5.0, symbol="ARBUSDT")],
            price=1.0,
        )
        out = _run_main(monkeypatch, tmp_path, c,
                        {"ARBUSDT": _pos(100.5, 1.0, 0.9, 1.4)})
        tops = [q for k, q in c.calls if k == "place_stop_loss_limit"]
        assert tops and tops[0] == pytest.approx(39.9), (
            f"top-up {tops} should cap at free 39.9 (book gap is 40.5)")
