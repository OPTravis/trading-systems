"""P3: ProtectionShape state machine — the single order-shape classifier.

Phase-1 audit finding: the legal-protection-shape set had no single data
structure. The pair check's 50% coverage line, the swap branch's entry
condition and the ladder's demote rules were each hard-coded inline in
protection_guardian (4 of the 7 guardian patches were literally "patch
the shape set"). This module makes the shape judgement ONE implementation:

  classify(open_orders) -> ShapeInfo
      discrete shape over the 8-combination space of
      (oco_qty, plain_tp_qty, plain_sl_qty) in {0, +}
      NAKED / TP_ONLY / SL_ONLY / PAIR / OCO / OCO_TP / OCO_SL / OCO_TP_SL

  LEGAL_TERMINAL_SHAPES — shapes a sweep must not touch unconditionally
  (PAIR: plain TP(s)+plain SL is a legal terminal product — WO-0924-ix,
  the SL-demote rescue product and manual de-escalation both land here).
  Shapes carrying an OCO are terminal *when covered* (protected(qty)).

  TRANSITIONS — action edges as an explicit (action, from) -> {to} table.
  Structural invariants (exhaustively proven by tests, not by review):

    I1  no action's from-set intersects the unconditional terminal set
        -> a legal terminal shape can never be picked apart again
        (the 666de00 swap-loop class: viii's demote product PAIR was
        re-judged illegal by the swap branch and torn apart — the bug
        becomes structurally impossible because PAIR has no out-edges)
    I2  every action's to-set is a subset of the known shape set
    I3  no two-step action sequence returns to its starting shape
        except documented fail-open self-edges (swap failed -> SL legs
        re-placed -> SL_ONLY again; guarded by the 24h kv debounce)

The classifier is a pure function over the order list — same field
semantics as the pre-P3 guardian inline code (orderListId>0 / legacy
listId / contingencyType for OCO legs; independent LIMIT/TAKE_PROFIT
SELLs for plain TP; independent STOP_LOSS* for plain SL). The guardian
consumes ShapeInfo quantities unchanged (byte-equal numeric paths) and
emits GUARDIAN_SHAPE_TRANSITION audit rows at action points for offline
reconciliation; no decision logic moved in P3 — the state machine is
first a single source of truth + a proven-closed transition table.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

#: fraction of holding the TP side must cover to count as protected
#: (mirrors protection_guardian.TP_COVER_MIN_FRAC — single value here;
#: guardian keeps importing its own constant name for grep stability)
TP_COVER_MIN_FRAC = 0.5


def order_qty(o: Dict[str, Any]) -> float:
    """First positive qty field of an order dict (0.0 when absent)."""
    for k in ("origQty", "quantity", "qty"):
        try:
            v = float(o.get(k) or 0)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return 0.0


def is_oco_leg(o: Dict[str, Any]) -> bool:
    """Only a genuine OCO member order counts here. Production Binance
    get_open_orders marks OCO legs via orderListId (> 0; -1 for
    independent orders) — the 'listId' key does not exist there. A bare
    STOP_LOSS_LIMIT with orderListId == -1 is an INDEPENDENT stop order:
    it still locks the balance and must be treated as a plain SL leg."""
    try:
        olid = float(o.get("orderListId") or -1)
        if olid > 0:
            return True
    except (TypeError, ValueError):
        pass
    return bool(o.get("listId") or o.get("contingencyType"))


def is_plain_tp(o: Dict[str, Any]) -> bool:
    if is_oco_leg(o):
        return False
    if str(o.get("side") or "").upper() != "SELL":
        return False
    otype = str(o.get("type") or o.get("orderType") or "").upper()
    # plain TP limit sell, or an independent TAKE_PROFIT stop order
    return otype in ("LIMIT", "LIMIT_MAKER") or otype.startswith(
        "TAKE_PROFIT")


def is_plain_sl(o: Dict[str, Any]) -> bool:
    if is_oco_leg(o):
        return False
    return str(o.get("type") or o.get("orderType") or "").upper().startswith(
        "STOP_LOSS")


class Shape(Enum):
    """Discrete order shape over (oco, plain_tp, plain_sl) presence.

    Eight combinations, no overlap, no leftover: the (0,0,0) case is
    NAKED (no protective orders at all — orders that match none of the
    three classes, e.g. a resting plain BUY, do not count either).
    """

    NAKED = "NAKED"          # nothing protective on the book
    TP_ONLY = "TP_ONLY"      # plain TP sell(s), no SL side, no OCO
    SL_ONLY = "SL_ONLY"      # plain SL leg(s), no TP side, no OCO
    PAIR = "PAIR"            # plain TP(s) + plain SL — LEGAL TERMINAL
    OCO = "OCO"              # one OCO list covering the position
    OCO_TP = "OCO_TP"        # OCO + extra plain TP leg(s)
    OCO_SL = "OCO_SL"        # OCO + extra plain SL leg(s)
    OCO_TP_SL = "OCO_TP_SL"  # OCO + extra plain TP and SL legs


#: Unconditional legal terminal shapes — a sweep never acts on these
#: (shape-carrying OCO forms are terminal only while covered; PAIR is
#: terminal outright — WO-0924-ix)
LEGAL_TERMINAL_SHAPES = frozenset({Shape.PAIR})

#: Shapes any known action may produce (products of heal/swap/demote):
#:   emergency_sl -> SL_ONLY          free_slice_tp -> adds a plain TP
#:   tp_oco_rebuild -> OCO            sl_demote -> PAIR
#:   oco_swap -> OCO                  fail-open edges -> self
KNOWN_PRODUCT_SHAPES = frozenset({
    Shape.SL_ONLY, Shape.TP_ONLY, Shape.PAIR,
    Shape.OCO, Shape.OCO_TP, Shape.OCO_TP_SL,
})

#: The transition table. Action edges as (action, from) -> {allowed to}.
#: Fail-open edges (action failed, old orders restored) map back to the
#: same shape and are annotated inline; they are bounded in runtime by
#: the 24h swap debounce / per-sweep idempotence, and by invariant I3
#: in structure (no non-self two-step cycle exists).
TRANSITIONS: Dict[str, Dict[Shape, frozenset]] = {
    # naked position -> wide emergency stop (TP-breached branch and the
    # plain NAKED branch of the guardian both use this edge)
    "emergency_sl": {
        Shape.NAKED: frozenset({Shape.SL_ONLY}),
    },
    # free (unlocked) slice large enough -> plain TP limit sell on it;
    # the position keeps its other legs, so the product shape is the
    # current shape plus a plain TP leg
    "free_slice_tp": {
        Shape.NAKED: frozenset({Shape.TP_ONLY}),
        Shape.SL_ONLY: frozenset({Shape.PAIR}),
        Shape.TP_ONLY: frozenset({Shape.TP_ONLY}),
        Shape.OCO: frozenset({Shape.OCO_TP}),
        Shape.OCO_SL: frozenset({Shape.OCO_TP_SL}),
        # already carries plain TP legs — another one keeps the class
        Shape.OCO_TP: frozenset({Shape.OCO_TP}),
        Shape.OCO_TP_SL: frozenset({Shape.OCO_TP_SL}),
    },
    # TP-only lock -> cancel TP legs, rebuild one full OCO over the
    # slice (WO-017-4a)
    "tp_oco_rebuild": {
        Shape.TP_ONLY: frozenset({Shape.OCO}),
    },
    # rebuild rejected by the exchange -> rescue ladder: keep TP legs
    # except the last, demote that one to a plain STOP_LOSS_LIMIT
    # (WO-0923-viii; the 666de00 swap-loop tore this product apart)
    "sl_demote": {
        Shape.TP_ONLY: frozenset({Shape.PAIR}),
    },
    # demote also failed -> restore every TP leg and scream
    # (fail-open edge, self-loop)
    "sl_rescue_failed": {
        Shape.TP_ONLY: frozenset({Shape.TP_ONLY}),
    },
    # balance fully locked by plain SL, no TP can stand -> cancel-first
    # swap to one full OCO (WO-0924-ix: only from strict SL_ONLY)
    "oco_swap": {
        Shape.SL_ONLY: frozenset({Shape.OCO}),
    },
    # OCO rejected after cancel -> re-place the old SL legs immediately
    # (fail-open edge, self-loop; runtime-bounded by the 24h debounce)
    "oco_swap_failed_sl_restored": {
        Shape.SL_ONLY: frozenset({Shape.SL_ONLY}),
    },
}


@dataclass
class ShapeInfo:
    """Classified order shape + the raw components the guardian needs.

    Numeric fields mirror the pre-P3 inline computation exactly:
        oco_qty = sum(qty of OCO legs)
        tp_qty  = sum(qty of plain TP sells)
        sl_qty  = sum(qty of plain SL legs)
        tp_covered = oco_qty + tp_qty
        sl_covered = oco_qty + sl_qty
    """

    shape: Shape
    oco_qty: float = 0.0
    tp_qty: float = 0.0
    sl_qty: float = 0.0
    tp_covered: float = 0.0
    sl_covered: float = 0.0
    oco_orders: List[Dict[str, Any]] = field(default_factory=list)
    tp_orders: List[Dict[str, Any]] = field(default_factory=list)
    sl_orders: List[Dict[str, Any]] = field(default_factory=list)

    def protected(self, qty: float,
                  frac: float = TP_COVER_MIN_FRAC) -> bool:
        """Both-side coverage test (the WO-017-4a pair check).

        TP side needs frac of the holding; SL side is an existence
        check (partial SL ladders are a legitimate end-state; ZERO SL
        means the downside is naked)."""
        return (self.tp_covered >= qty * frac
                and self.sl_covered > 0.0)


def classify(orders: List[Dict[str, Any]]) -> ShapeInfo:
    """Single implementation of the order-shape judgement."""
    oco_orders = [o for o in orders if is_oco_leg(o)]
    tp_orders = [o for o in orders if is_plain_tp(o)]
    sl_orders = [o for o in orders if is_plain_sl(o)]
    oco_qty = sum(order_qty(o) for o in oco_orders)
    tp_qty = sum(order_qty(o) for o in tp_orders)
    sl_qty = sum(order_qty(o) for o in sl_orders)

    has_oco, has_tp, has_sl = oco_qty > 0, tp_qty > 0, sl_qty > 0
    if has_oco:
        shape = (Shape.OCO_TP_SL if has_tp and has_sl
                 else Shape.OCO_TP if has_tp
                 else Shape.OCO_SL if has_sl
                 else Shape.OCO)
    elif has_tp:
        shape = Shape.PAIR if has_sl else Shape.TP_ONLY
    elif has_sl:
        shape = Shape.SL_ONLY
    else:
        shape = Shape.NAKED

    return ShapeInfo(
        shape=shape, oco_qty=oco_qty, tp_qty=tp_qty, sl_qty=sl_qty,
        tp_covered=oco_qty + tp_qty, sl_covered=oco_qty + sl_qty,
        oco_orders=oco_orders, tp_orders=tp_orders, sl_orders=sl_orders,
    )


def check_transition(action: str, from_shape: Shape,
                     to_shape: Shape) -> bool:
    """Is (from --action--> to) an edge of the declared table?"""
    allowed = TRANSITIONS.get(action, {}).get(from_shape)
    return allowed is not None and to_shape in allowed


def audit_transition(action: str, info: "ShapeInfo",
                      audit_fn=None) -> None:
    """Best-effort GUARDIAN_SHAPE_TRANSITION audit row at an action
    point (observation only — never blocks, never retries, adds no API
    call; the audit goes through the guardian's existing ledger-funnel
    _audit so shadow visibility comes for free)."""
    row = {"action": action, "from_shape": info.shape.value,
           "expected_to": sorted(s.value for s in
                                 TRANSITIONS.get(action, {}).get(
                                     info.shape, frozenset())),
           "oco_qty": info.oco_qty, "tp_qty": info.tp_qty,
           "sl_qty": info.sl_qty}
    try:
        if audit_fn is not None:
            audit_fn("GUARDIAN_SHAPE_TRANSITION", row)
        else:
            from src.protection_guardian import _audit
            _audit("GUARDIAN_SHAPE_TRANSITION", row)
    except Exception:
        logger.debug("shape transition audit failed (%s)", action,
                     exc_info=True)


def structural_invariants() -> Dict[str, Any]:
    """Exhaustive table self-check (used by tests; cheap enough to run
    anywhere). Returns a report dict; 'ok' False means the table itself
    violates I1/I2/I3 and the state machine must not ship.

    I1 unconditional terminals have no out-edges
    I2 every to-set ⊆ KNOWN_PRODUCT_SHAPES
    I3 no two-step sequence (a≠b) returns to its start shape except
       via documented fail-open self-edges (which map to themselves)"""
    violations: List[str] = []

    # I1 — no action departs from an unconditional terminal shape
    for action, edges in TRANSITIONS.items():
        for frm in edges:
            if frm in LEGAL_TERMINAL_SHAPES:
                violations.append(
                    f"I1: {action} departs from terminal {frm.value}")

    # I2 — products stay inside the known product set
    for action, edges in TRANSITIONS.items():
        for frm, tos in edges.items():
            bad = tos - KNOWN_PRODUCT_SHAPES
            if bad:
                violations.append(
                    f"I2: {action} from {frm.value} produces "
                    f"{sorted(s.value for s in bad)}")

    # I3 — two-step cycles. Fail-open self-edges (action maps a shape
    # only to itself) cannot create a cross-shape cycle; any pair of
    # distinct actions a->b with from(S_a) ∩ to(S_b) returning to the
    # start would be a compensator trampling loop.
    fail_open = {"sl_rescue_failed", "oco_swap_failed_sl_restored"}
    for a_act, a_edges in TRANSITIONS.items():
        for frm, a_tos in a_edges.items():
            if a_act in fail_open:
                # self-edge only by construction; still verify
                if a_tos != frozenset({frm}):
                    violations.append(
                        f"I3: fail-open {a_act} from {frm.value} is not "
                        f"a pure self-edge")
                continue
            for b_act, b_edges in TRANSITIONS.items():
                if b_act in fail_open or b_act == a_act:
                    continue
                for mid in a_tos:
                    if mid in b_edges and frm in b_edges[mid]:
                        violations.append(
                            f"I3: cycle {frm.value} --{a_act}--> "
                            f"{mid.value} --{b_act}--> {frm.value}")

    return {"ok": not violations, "violations": violations}
