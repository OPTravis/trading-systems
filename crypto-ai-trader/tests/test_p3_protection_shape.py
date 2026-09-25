"""P3: ProtectionShape state machine — tests.

Three layers:
1. classify() exhaustively equals the pre-P3 inline formulas (an oracle
   copy of the OLD guardian code runs against every combination of the
   (oco, plain_tp, plain_sl) presence space — 8 shapes × qty variants).
2. The transition table's structural invariants hold: terminals have no
   out-edges (the 666de00 swap-loop class is structurally impossible),
   products stay in the known set, no non-self two-step cycle exists.
3. The guardian consumes classify() with byte-equal behaviour on every
   branch (each shape lands on its historical action, and the new
   GUARDIAN_SHAPE_TRANSITION audit rows only observe).

Plus the two Travis 9/25 rulings delivered with this batch:
- ② paper_pending_orders: a committed fill retires the row; a second
  check_pending_orders sweep cannot re-fill it.
- ① CVaR overlay stays pinned inert (compute_portfolio_risk([]) must
  keep returning the 1.0 zero-default until the P7 topic decides).
"""

import logging
from typing import Any, Dict, List

import pytest

from src import protection_guardian as pg
from src.protection_shape import (
    KNOWN_PRODUCT_SHAPES,
    LEGAL_TERMINAL_SHAPES,
    TRANSITIONS,
    Shape,
    ShapeInfo,
    audit_transition,
    check_transition,
    classify,
    structural_invariants,
)
from src.state_db import StateDB

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- helpers
def _oco_leg(sym="FETUSDT", qty=10.0, order_id=1, list_id=777):
    return {
        "symbol": sym, "orderId": order_id, "orderListId": list_id,
        "status": "NEW", "type": "STOP_LOSS_LIMIT", "side": "SELL",
        "price": "0.22", "stopPrice": "0.18", "origQty": str(qty),
    }


def _tp_leg(sym="FETUSDT", qty=10.0, order_id=2, px=0.22):
    return {
        "symbol": sym, "orderId": order_id, "orderListId": -1,
        "status": "NEW", "type": "LIMIT", "side": "SELL",
        "price": str(px), "stopPrice": "0", "origQty": str(qty),
    }


def _sl_leg(sym="FETUSDT", qty=10.0, order_id=3, stop_px=0.18):
    return {
        "symbol": sym, "orderId": order_id, "orderListId": -1,
        "status": "NEW", "type": "STOP_LOSS_LIMIT", "side": "SELL",
        "price": str(stop_px), "stopPrice": str(stop_px),
        "origQty": str(qty),
    }


def _pos(symbol="FETUSDT", quantity=100.0, entry=0.1954,
         take_profit=0.2031, stop_loss=0.1819, price=None):
    return {
        "symbol": symbol, "quantity": float(quantity),
        "entry_price": float(entry),
        "current_price": float(price if price is not None else entry),
        "price_is_stale": False, "strategy": "switch",
        "opened_at": 1789984344.0, "updated_at": 1789984344.0,
        "stop_loss": stop_loss, "take_profit": take_profit,
        "invest_pct": 0.1,
    }


class FakeClient:
    """Production-shaped client (mirrors test_protection_guardian)."""

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


# ================================================ 1. classify equivalence
def _oracle(orders):
    """Verbatim copy of the PRE-P3 guardian inline aggregation — the
    equivalence target. Returns (oco_qty, tp_qty, sl_qty, sl_orders)."""
    def _oq(o):
        for k in ("origQty", "quantity", "qty"):
            try:
                v = float(o.get(k) or 0)
                if v > 0:
                    return v
            except (TypeError, ValueError):
                continue
        return 0.0

    def _oco(o):
        try:
            olid = float(o.get("orderListId") or -1)
            if olid > 0:
                return True
        except (TypeError, ValueError):
            pass
        return bool(o.get("listId") or o.get("contingencyType"))

    def _tp(o):
        if _oco(o):
            return False
        if str(o.get("side") or "").upper() != "SELL":
            return False
        t = str(o.get("type") or o.get("orderType") or "").upper()
        return t in ("LIMIT", "LIMIT_MAKER") or t.startswith("TAKE_PROFIT")

    def _sl(o):
        if _oco(o):
            return False
        return str(o.get("type") or o.get("orderType") or "").upper().startswith(
            "STOP_LOSS")

    oco_qty = sum(_oq(o) for o in orders if _oco(o))
    tp_qty = sum(_oq(o) for o in orders if _tp(o))
    sl_orders = [o for o in orders if _sl(o)]
    sl_qty = sum(_oq(o) for o in sl_orders)
    return oco_qty, tp_qty, sl_qty, sl_orders


def _all_combos():
    """8-combination presence space × qty variety × noise orders."""
    qty_sets = [10.0, 4.2]           # qty variety per present leg
    noise = [                         # orders matching NO class
        {"symbol": "FETUSDT", "orderId": 50, "orderListId": -1,
         "type": "LIMIT", "side": "BUY", "origQty": "7.7",
         "price": "0.1", "stopPrice": "0"},
    ]
    for n_oco in (0, 1, 2):
        for n_tp in (0, 1, 2):
            for n_sl in (0, 1, 2):
                for q in qty_sets:
                    orders: List[Dict[str, Any]] = list(noise)
                    orders += [_oco_leg(qty=q, order_id=100 + i)
                               for i in range(n_oco)]
                    orders += [_tp_leg(qty=q, order_id=200 + i)
                               for i in range(n_tp)]
                    orders += [_sl_leg(qty=q, order_id=300 + i)
                               for i in range(n_sl)]
                    yield n_oco, n_tp, n_sl, q, orders


class TestClassifyExhaustive:
    """Every combination: numeric fields equal the pre-P3 oracle AND
    the discrete shape is exactly the presence mapping."""

    EXPECTED_SHAPE = {
        # (n_oco>0, n_tp>0, n_sl>0) -> Shape
        (0, 0, 0): Shape.NAKED,
        (0, 1, 0): Shape.TP_ONLY,
        (0, 0, 1): Shape.SL_ONLY,
        (0, 1, 1): Shape.PAIR,
        (1, 0, 0): Shape.OCO,
        (1, 1, 0): Shape.OCO_TP,
        (1, 0, 1): Shape.OCO_SL,
        (1, 1, 1): Shape.OCO_TP_SL,
    }

    @pytest.mark.parametrize("n_oco,n_tp,n_sl,q,orders",
                             list(_all_combos()))
    def test_numeric_and_shape_equivalence(self, n_oco, n_tp, n_sl, q,
                                           orders):
        info = classify(orders)
        o_oco, o_tp, o_sl, o_sl_orders = _oracle(orders)
        assert info.oco_qty == pytest.approx(o_oco)
        assert info.tp_qty == pytest.approx(o_tp)
        assert info.sl_qty == pytest.approx(o_sl)
        assert len(info.sl_orders) == len(o_sl_orders)
        assert info.tp_covered == pytest.approx(o_oco + o_tp)
        assert info.sl_covered == pytest.approx(o_oco + o_sl)
        expected = self.EXPECTED_SHAPE[
            (min(n_oco, 1), min(n_tp, 1), min(n_sl, 1))]
        assert info.shape is expected

    def test_guardian_private_names_reexported(self):
        """pg._is_oco_leg / _is_plain_* stay patchable module attrs."""
        assert pg._is_oco_leg(_oco_leg()) is True
        assert pg._is_oco_leg(_sl_leg()) is False
        assert pg._is_plain_tp(_tp_leg()) is True
        assert pg._is_plain_tp(_oco_leg()) is False
        assert pg._is_plain_sl(_sl_leg()) is True
        assert pg._order_qty({"qty": "2.5"}) == pytest.approx(2.5)

    def test_qty_field_priority(self):
        o = {"origQty": "3.0", "quantity": "9.9", "qty": "1.1"}
        from src.protection_shape import order_qty
        assert order_qty(o) == pytest.approx(3.0)
        assert order_qty({}) == 0.0
        assert order_qty({"qty": "junk", "quantity": None}) == 0.0


class TestProtected:
    """The WO-017-4a both-side test, oracle-equivalent."""

    def test_pair_check_oracle(self):
        import random
        rng = random.Random(925)
        for _ in range(200):
            n_oco = rng.choice([0, 1])
            n_tp = rng.choice([0, 1])
            n_sl = rng.choice([0, 1])
            qty = rng.uniform(1, 100)
            orders = []
            if n_oco:
                orders.append(_oco_leg(qty=rng.uniform(1, 60)))
            if n_tp:
                orders.append(_tp_leg(qty=rng.uniform(1, 60)))
            if n_sl:
                orders.append(_sl_leg(qty=rng.uniform(1, 60)))
            info = classify(orders)
            oracle = (info.tp_covered >= qty * pg.TP_COVER_MIN_FRAC
                      and info.sl_covered > 0.0)
            assert info.protected(qty) is oracle

    def test_fraction_boundary(self):
        info = classify([_tp_leg(qty=50.0), _sl_leg(qty=1.0)])
        assert info.protected(100.0) is True    # exactly 0.5 coverage
        info2 = classify([_tp_leg(qty=49.9), _sl_leg(qty=1.0)])
        assert info2.protected(100.0) is False
        # SL side is existence, not fraction
        info3 = classify([_tp_leg(qty=80.0)])
        assert info3.protected(100.0) is False  # no SL at all


# ============================================ 2. transition table proofs
class TestTransitionTable:
    def test_structural_invariants_hold(self):
        rep = structural_invariants()
        assert rep == {"ok": True, "violations": []}

    def test_swap_loop_structurally_impossible(self):
        """The 666de00 class: viii's demote product is PAIR; the swap
        branch may only depart from strict SL_ONLY. PAIR carries no
        out-edge at all, so no action in the table can ever touch it."""
        assert LEGAL_TERMINAL_SHAPES == frozenset({Shape.PAIR})
        for action, edges in TRANSITIONS.items():
            for frm in edges:
                assert frm is not Shape.PAIR, action
        # demote product is exactly the terminal shape
        assert Shape.PAIR in TRANSITIONS["sl_demote"][Shape.TP_ONLY]
        # swap departs only from strict SL_ONLY (the ix fix, now a
        # structural property instead of an inline condition)
        assert set(TRANSITIONS["oco_swap"]) == {Shape.SL_ONLY}

    def test_no_cross_shape_two_step_cycle(self):
        """Any two non-fail-open actions chained cannot return to the
        starting shape — compensator trampling is table-impossible."""
        fail_open = {"sl_rescue_failed", "oco_swap_failed_sl_restored"}
        for a_act, a_edges in TRANSITIONS.items():
            if a_act in fail_open:
                continue
            for frm, a_tos in a_edges.items():
                for b_act, b_edges in TRANSITIONS.items():
                    if b_act in fail_open or b_act == a_act:
                        continue
                    for mid in a_tos:
                        if mid in b_edges:
                            assert frm not in b_edges[mid], (
                                frm, a_act, mid, b_act)

    def test_fail_open_edges_are_pure_self_loops(self):
        assert TRANSITIONS["sl_rescue_failed"] == {
            Shape.TP_ONLY: frozenset({Shape.TP_ONLY})}
        assert TRANSITIONS["oco_swap_failed_sl_restored"] == {
            Shape.SL_ONLY: frozenset({Shape.SL_ONLY})}

    def test_check_transition(self):
        assert check_transition("oco_swap", Shape.SL_ONLY, Shape.OCO)
        assert not check_transition("oco_swap", Shape.PAIR, Shape.OCO)
        assert not check_transition("oco_swap", Shape.SL_ONLY,
                                    Shape.TP_ONLY)
        assert not check_transition("no_such_action", Shape.NAKED,
                                    Shape.SL_ONLY)

    def test_every_nonterminal_shape_has_an_exit(self):
        """No deadlock: every shape outside the unconditional terminal
        set appears as a from on at least one edge (possibly via the
        free-slice heal), so a sweep never faces an unhandled shape."""
        covered = {frm for edges in TRANSITIONS.values()
                   for frm in edges}
        for shape in Shape:
            if shape in LEGAL_TERMINAL_SHAPES:
                assert shape not in covered
            else:
                assert shape in covered, shape


# ================================= 3. guardian branch equivalence
class TestGuardianBranchEquivalence:
    """Each shape lands on its historical action; classify() swap
    changed zero decisions. (Fixtures mirror test_protection_guardian;
    these re-pin the branch map through the shape lens.)"""

    @pytest.fixture(autouse=True)
    def _quiet(self, monkeypatch):
        monkeypatch.setattr(pg, "emit_alert", lambda *a, **k: None)

    def test_naked_goes_emergency_sl(self):
        """NAKED + TP target already breached (tp 0.18 < px 0.20) — a
        TP placement would fill instantly, so the wide emergency SL is
        the only legal heal (edge emergency_sl: NAKED -> SL_ONLY)."""
        c = FakeClient(orders=[])
        pos = _pos(quantity=100.0, entry=0.20, take_profit=0.18)
        res = pg.run(c, FakePortfolio([pos]))
        assert res["healed"] == 1
        assert len(c.sl_placed) == 1
        assert c.tp_placed == [] and c.ocos == []

    def test_pair_is_untouched_terminal(self):
        """plain TP + plain SL: skip — NO api call of any kind (the
        WO-0924-ix ruling; PAIR has no out-edge in the table)."""
        c = FakeClient(orders=[_tp_leg(qty=60.0),
                               _sl_leg(qty=40.0, order_id=4)])
        pos = _pos(quantity=100.0, entry=0.20)
        res = pg.run(c, FakePortfolio([pos]))
        assert res == {"checked": 1, "healed": 0, "failed": 0,
                       "skipped": 0}
        assert (c.tp_placed == [] and c.ocos == []
                and c.cancelled == [] and c.sl_placed == [])

    def test_covered_oco_skips_idempotently(self):
        c = FakeClient(orders=[_oco_leg(qty=100.0)])
        pos = _pos(quantity=100.0, entry=0.20)
        res = pg.run(c, FakePortfolio([pos]))
        assert res["checked"] == 1 and res["healed"] == 0
        assert c.ocos == [] and c.tp_placed == []

    def test_tp_only_rebuilds_oco(self):
        c = FakeClient(orders=[_tp_leg(qty=100.0)])
        pos = _pos(quantity=100.0, entry=0.20)
        res = pg.run(c, FakePortfolio([pos]))
        assert res["healed"] == 1
        assert len(c.ocos) == 1 and len(c.cancelled) == 1

    def test_sl_only_swaps_to_oco(self, monkeypatch):
        monkeypatch.setattr(pg.time, "time", lambda: 9e9)
        c = FakeClient(orders=[_sl_leg(qty=100.0)])
        pos = _pos(quantity=100.0, entry=0.20)
        res = pg.run(c, FakePortfolio([pos]))
        assert res["healed"] == 1
        assert len(c.ocos) == 1 and len(c.cancelled) == 1


class TestGuardianShapeAuditRows:
    """The new GUARDIAN_SHAPE_TRANSITION rows observe only: right
    action, right from_shape, right declared to-set — and never fire
    on skip paths."""

    @pytest.fixture(autouse=True)
    def _quiet(self, monkeypatch):
        monkeypatch.setattr(pg, "emit_alert", lambda *a, **k: None)

    @pytest.fixture()
    def audits(self, monkeypatch):
        rows: List[tuple] = []

        def fake_audit(action, details):
            rows.append((action, details))

        monkeypatch.setattr(pg, "_audit", fake_audit)
        return rows

    def _run_naked(self):
        # tp target BELOW market -> breach branch -> naked -> emergency
        c = FakeClient(orders=[])
        pos = _pos(quantity=100.0, entry=0.20, take_profit=0.18)
        return pg.run(c, FakePortfolio([pos])), c

    def test_emergency_sl_audit_row(self, audits):
        res, c = self._run_naked()
        assert res["healed"] == 1
        rows = [d for a, d in audits if a == "GUARDIAN_SHAPE_TRANSITION"]
        assert len(rows) == 1
        assert rows[0]["action"] == "emergency_sl"
        assert rows[0]["from_shape"] == "NAKED"
        assert rows[0]["expected_to"] == ["SL_ONLY"]

    def test_swap_audit_row(self, audits, monkeypatch):
        monkeypatch.setattr(pg.time, "time", lambda: 9e9)
        c = FakeClient(orders=[_sl_leg(qty=100.0)])
        pos = _pos(quantity=100.0, entry=0.20)
        res = pg.run(c, FakePortfolio([pos]))
        assert res["healed"] == 1
        rows = [d for a, d in audits if a == "GUARDIAN_SHAPE_TRANSITION"]
        assert len(rows) == 1
        assert rows[0]["action"] == "oco_swap"
        assert rows[0]["from_shape"] == "SL_ONLY"
        assert rows[0]["expected_to"] == ["OCO"]

    def test_no_audit_on_skip_paths(self, audits):
        c = FakeClient(orders=[_tp_leg(qty=60.0),
                               _sl_leg(qty=40.0, order_id=4)])
        pos = _pos(quantity=100.0, entry=0.20)
        pg.run(c, FakePortfolio([pos]))
        assert audits == []  # PAIR terminal: zero side effects

    def test_audit_transition_never_raises(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("audit funnel down")

        monkeypatch.setattr(pg, "_audit", boom)
        info = classify([])
        audit_transition("emergency_sl", info)  # must swallow
        # explicit audit_fn path also swallows
        audit_transition("oco_swap", info, audit_fn=boom)

    def test_audit_transition_table_selfcheck_ok(self):
        assert structural_invariants()["ok"] is True


# ============================== Travis 9/25 ruling ② (paper pending)
class TestPaperPendingFillOnce:
    @pytest.fixture()
    def db(self, tmp_path):
        d = StateDB(str(tmp_path / "state.db"))
        yield d
        d._get_conn().close()

    @pytest.fixture()
    def trader(self, db):
        from src.paper_trader import PaperTrader
        pt = object.__new__(PaperTrader)
        pt._db = db
        pt._in_transaction = False
        return pt

    def test_mark_filled_retires_row(self, db):
        db.paper_pending_add("1", "BTCUSDT", "BUY", "LIMIT", 1.0,
                             100.0, None, "{}")
        assert db.paper_pending_get("1") is not None
        assert db.paper_pending_mark_filled("1") is True
        assert db.paper_pending_get("1") is None            # reader 1
        assert db.paper_pending_open() == []                # reader 2
        assert db.paper_pending_open("BTCUSDT") == []       # reader 3
        # idempotent
        assert db.paper_pending_mark_filled("1") is False

    def test_second_sweep_does_not_refill(self, db, trader,
                                          monkeypatch):
        """The bug this ruling fixes: fill commits, row stays 'open',
        every later sweep re-fills the same order. Now the committed
        fill retires the row and the second sweep sees nothing."""
        db.paper_pending_add("7", "BTCUSDT", "BUY", "LIMIT", 1.0,
                             100.0, None, "{}")
        fills = []
        orig = trader._fill_market

        def spy_fill(symbol, side, quantity, price):
            res = orig(symbol, side, quantity, price)
            if res:
                fills.append((side, quantity))
            return res

        monkeypatch.setattr(trader, "_fill_market", spy_fill)
        monkeypatch.setattr(trader, "get_current_price",
                            lambda s: 99.0)   # BUY fills at <= limit

        trader.check_pending_orders()
        assert len(fills) == 1
        assert db.paper_pending_open() == []

        trader.check_pending_orders()           # second sweep
        assert len(fills) == 1                  # NOT re-filled
        # the fill actually committed (balance moved)
        assert trader._get_sim_balance() < 10000.0

    def test_failed_fill_keeps_row_open(self, db, trader, monkeypatch):
        """Fail path must NOT retire the row (retry semantics stay)."""
        db.paper_pending_add("8", "BTCUSDT", "BUY", "LIMIT", 1.0,
                             100.0, None, "{}")
        monkeypatch.setattr(trader, "get_current_price",
                            lambda s: 99.0)
        # drain the balance so _fill_market hits the insufficient
        # branch and rolls back
        trader._set_sim_balance(1.0)
        res = trader._fill_limit_order("8", 99.0)
        assert res is None
        assert db.paper_pending_get("8") is not None  # still open
        # and the balance snapshot was rolled back intact
        assert trader._get_sim_balance() == pytest.approx(1.0)

    def test_pending_status_written_filled(self, db, trader,
                                           monkeypatch):
        db.paper_pending_add("9", "BTCUSDT", "SELL", "LIMIT", 1.0,
                             200.0, None, "{}")
        trader._set_sim_positions({"BTC": {
            "qty": 5.0, "entry_price": 100.0, "symbol": "BTCUSDT",
            "opened_at": 1.0, "updated_at": 1.0}})
        monkeypatch.setattr(trader, "get_current_price",
                            lambda s: 201.0)  # SELL fills at >= limit
        res = trader.check_pending_orders()
        row = db._get_conn().execute(
            "SELECT status FROM paper_pending_orders WHERE id = '9'"
        ).fetchone()
        assert row["status"] == "filled"


# ============================ Travis 9/25 ruling ① (CVaR stays inert)
class TestCVaROverlayStaysPinned:
    def test_empty_positions_zero_default(self):
        """compute_portfolio_risk([]) must keep returning the 1.0
        zero-default scale until the P7 topic decides otherwise —
        guards against an accidental activation sneaking in."""
        from src.cvar_risk import CVaRRiskManager
        mgr = CVaRRiskManager.__new__(CVaRRiskManager)
        risk = mgr.compute_portfolio_risk([])
        assert risk.get("position_scale", 1.0) == 1.0
