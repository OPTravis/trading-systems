"""WO-0921-013: protection_guardian tests.

Fixtures copy the PRODUCTION return shapes (Travis rule 2026-09-21):
- portfolio.get_all_positions() → List[Dict] with symbol / quantity /
  entry_price / current_price / price_is_stale / stop_loss / take_profit
- client.get_open_orders() → list of Binance order dicts (type/side/
  price/stopPrice/origQty/orderId/listId)
- client.get_symbol_filters() → dict of string values
"""

import logging
from types import SimpleNamespace

import pytest

from src import protection_guardian as pg


def _pos(symbol, quantity, entry, take_profit=None, stop_loss=None,
         price=None):
    """Production shape of one get_all_positions() element."""
    return {
        "symbol": symbol,
        "quantity": float(quantity),
        "entry_price": float(entry),
        "current_price": float(price if price is not None else entry),
        "price_is_stale": False,
        "strategy": "switch",
        "opened_at": 1789984344.0,
        "updated_at": 1789984344.0,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "invest_pct": 0.1,
    }


def _oco_leg(sym, qty, order_id, list_id=777):
    return {
        "symbol": sym, "orderId": order_id, "orderListId": list_id,
        "listId": list_id, "contingencyType": "OCO",
        "status": "NEW", "type": "STOP_LOSS_LIMIT", "side": "SELL",
        "price": "0", "stopPrice": "0.70", "origQty": str(qty),
    }


def _tp_leg(sym, qty, order_id, px):
    return {
        "symbol": sym, "orderId": order_id, "status": "NEW",
        "type": "LIMIT", "side": "SELL",
        "price": str(px), "stopPrice": "0", "origQty": str(qty),
    }


def _sl_leg(sym, qty, order_id, stop_px):
    return {
        "symbol": sym, "orderId": order_id, "status": "NEW",
        "type": "STOP_LOSS_LIMIT", "side": "SELL",
        "price": str(stop_px), "stopPrice": str(stop_px),
        "origQty": str(qty),
    }


class FakeClient:
    def __init__(self, orders, filters=None, fail=()):
        self.orders = orders
        self.filters = filters or {
            "stepSize": "0.1", "tickSize": "0.0001",
            "minQty": "0.1", "minNotional": "10"}
        self.fail = set(fail)
        self.tp_placed = []
        self.ocos = []
        self.cancelled = []
        self.sl_placed = []

    def get_open_orders(self, sym):
        if "orders" in self.fail:
            raise RuntimeError("net down")
        return self.orders

    def get_symbol_filters(self, sym):
        return self.filters

    def place_limit_sell(self, sym, qty, px):
        self.tp_placed.append((sym, qty, px))
        return {"orderId": 900}

    def place_oco(self, sym, qty, tp_px, sl_px):
        if "oco" in self.fail:
            return None
        self.ocos.append((sym, qty, tp_px, sl_px))
        return {"orderListId": 901}

    def cancel_order(self, sym, oid):
        if "cancel" in self.fail:
            raise RuntimeError("cancel rejected")
        self.cancelled.append(oid)
        return {"orderId": oid}

    def place_stop_loss_limit(self, sym, qty, lim, stop):
        self.sl_placed.append((sym, qty, lim, stop))
        return {"orderId": 902}


class FakePortfolio:
    def __init__(self, positions):
        self._positions = positions

    def get_all_positions(self):
        return self._positions


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(pg, "emit_alert", lambda *a, **k: None)


class TestGuardian:
    def test_free_slice_tp_heals(self):
        """No orders at all → plain TP on the free full slice."""
        c = FakeClient(orders=[])
        pos = _pos("FETUSDT", 84.6, 0.1954, take_profit=0.2031)
        res = pg.run(c, FakePortfolio([pos]))
        assert res["healed"] == 1
        assert len(c.tp_placed) == 1
        sym, qty, px = c.tp_placed[0]
        assert sym == "FETUSDT"
        assert qty == pytest.approx(84.6)
        assert px == pytest.approx(0.2031)  # DB take_profit, tick round

    def test_partial_free_slice(self):
        """SL already locks part → TP only on the unlocked remainder."""
        c = FakeClient(orders=[_sl_leg("FETUSDT", 40.0, 11, 0.1819)])
        pos = _pos("FETUSDT", 100.0, 0.1954, take_profit=0.2031)
        res = pg.run(c, FakePortfolio([pos]))
        assert res["healed"] == 1
        assert c.tp_placed[0][1] == pytest.approx(60.0)  # 100-40 step floor
        assert c.cancelled == [] and c.ocos == []

    def test_sl_locked_full_swaps_to_oco(self):
        """Full-qty SL lock (the -2010 residue) → cancel SL + one OCO."""
        c = FakeClient(orders=[_sl_leg("WLDUSDT", 30.0, 21, 0.4324)])
        pos = _pos("WLDUSDT", 30.0, 0.4649, take_profit=0.4835)
        res = pg.run(c, FakePortfolio([pos]))
        assert res["healed"] == 1
        assert c.cancelled == [21]
        sym, qty, tp_px, sl_px = c.ocos[0]
        assert qty == pytest.approx(30.0)
        assert sl_px == pytest.approx(0.4324)  # old stop preserved
        assert tp_px == pytest.approx(0.4835)
        assert c.sl_placed == []  # no restore needed

    def test_oco_swap_failure_restores_sl(self):
        """OCO swap fails → old SL legs re-placed (safety net)."""
        c = FakeClient(orders=[_sl_leg("WLDUSDT", 30.0, 21, 0.4324)],
                       fail=("oco",))
        pos = _pos("WLDUSDT", 30.0, 0.4649, take_profit=0.4835)
        res = pg.run(c, FakePortfolio([pos]))
        assert res["healed"] == 0 and res["failed"] == 1
        assert len(c.sl_placed) == 1  # restored
        assert c.sl_placed[0][3] == pytest.approx(0.4324)

    def test_cancel_failure_aborts_swap(self):
        """Cancel rejected → abort, no naked position, no OCO attempt."""
        c = FakeClient(orders=[_sl_leg("WLDUSDT", 30.0, 21, 0.4324)],
                       fail=("cancel",))
        pos = _pos("WLDUSDT", 30.0, 0.4649, take_profit=0.4835)
        res = pg.run(c, FakePortfolio([pos]))
        assert res["failed"] == 1
        assert c.ocos == [] and c.sl_placed == []

    def test_oco_already_covered_skips(self):
        """Existing OCO leg ≥50% → idempotent skip."""
        c = FakeClient(orders=[_oco_leg("BNBUSDT", 0.05, 31)])
        pos = _pos("BNBUSDT", 0.05, 812.47, take_profit=844.97)  # $40+
        res = pg.run(c, FakePortfolio([pos]))
        assert res["healed"] == 0 and res["failed"] == 0
        assert res["skipped"] == 0  # covered→clean continue, not skipped
        assert c.tp_placed == [] and c.ocos == []

    def test_take_profit_stop_order_counts_as_cover(self):
        """Independent TAKE_PROFIT_LIMIT order (no listId) is TP coverage,
        not an OCO leg — must not trigger a heal."""
        tp_stop = {
            "symbol": "BNBUSDT", "orderId": 55, "status": "NEW",
            "type": "TAKE_PROFIT_LIMIT", "side": "SELL",
            "price": "844.97", "stopPrice": "844.97",
            "origQty": "0.05", "listId": None,
        }
        c = FakeClient(orders=[tp_stop])
        pos = _pos("BNBUSDT", 0.05, 812.47, take_profit=844.97)
        res = pg.run(c, FakePortfolio([pos]))
        assert res["healed"] == 0 and res["failed"] == 0
        assert c.tp_placed == [] and c.ocos == []

    def test_tp_split_70pct_covered_skips(self):
        """Strategy-C split (TP 70% + SL 30%) already covers → skip."""
        c = FakeClient(orders=[
            _tp_leg("ETHFIUSDT", 37.2, 41, 0.790),
            _sl_leg("ETHFIUSDT", 16.0, 42, 0.7062),
        ])
        pos = _pos("ETHFIUSDT", 53.2, 0.7594, take_profit=0.79)
        res = pg.run(c, FakePortfolio([pos]))
        assert res["healed"] == 0 and res["failed"] == 0
        assert c.tp_placed == [] and c.ocos == []

    def test_default_tp_when_db_missing(self):
        """No take_profit in DB → entry×1.04 default."""
        c = FakeClient(orders=[])
        pos = _pos("FETUSDT", 84.6, 0.1954, take_profit=None)
        res = pg.run(c, FakePortfolio([pos]))
        assert res["healed"] == 1
        _, qty, px = c.tp_placed[0]
        assert px == pytest.approx(0.2032, abs=1e-8)  # 0.1954×1.04 floor tick

    def test_below_minnotional_skipped(self):
        c = FakeClient(orders=[])
        pos = _pos("DUSTUSDT", 5.0, 1.0)  # 5.0 notional < 10
        res = pg.run(c, FakePortfolio([pos]))
        assert res["skipped"] == 1 and res["healed"] == 0
        assert c.tp_placed == []

    def test_never_raises(self):
        """Client exploding mid-sweep must not propagate."""
        c = FakeClient(orders=[], fail=("orders",))
        pos = _pos("FETUSDT", 84.6, 0.1954)
        res = pg.run(c, FakePortfolio([pos]))  # must not raise
        assert res["failed"] == 1

        class Boom:
            def get_all_positions(self):
                raise RuntimeError("db gone")

        res2 = pg.run(c, Boom())
        assert res2 == {"checked": 0, "healed": 0, "failed": 0,
                        "skipped": 0}

    def test_multi_position_mixed(self):
        """One healed, one covered, one dust — independent processing."""
        c = FakeClient(orders=[_sl_leg("WLDUSDT", 30.0, 21, 0.4324)])
        positions = [
            _pos("WLDUSDT", 30.0, 0.4649, take_profit=0.4835),  # → OCO swap
            _pos("BNBUSDT", 0.007, 812.47, take_profit=844.97),  # $5.7 dust
        ]
        res = pg.run(c, FakePortfolio(positions))
        assert res["checked"] == 2
        assert res["healed"] == 1 and res["skipped"] == 1


class TestStepFloor:
    def test_step_floor_and_tick(self):
        assert pg._step_floor(0.00715042, 0.001) == pytest.approx(0.007)
        assert pg._tick_round(844.972345, 0.01) == pytest.approx(844.97)
