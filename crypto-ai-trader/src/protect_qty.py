"""WO-1042 (10/11): protective SELL quantity calibration — single source.

Incident (10/11 08:02, real production): the ATOM→OGN switch bought 176
OGN market; Binance SPOT settles the 0.1% BUY taker fee in the BASE
asset, so only 175.824 OGN landed. The switch's protective OCO and the
guardian's heal both sized their SELL legs from the DB nominal quantity
(176) and were rejected with -2010 "insufficient balance" — the fresh
position sat naked until Travis manually re-placed the OCO at 175
(listId 25259247975).

Every "place a protective SELL after a fill" path must size its order
from what the account actually holds, not from the nominal book qty.
Two layers, both required by the work order:

  L1  calibrate_sell_qty() — upfront calibration, BEFORE placing:
      cap the requested qty at the exchange free balance (floored to
      stepSize). When a fill response is at hand (fresh-buy paths),
      net_buy_qty() refines the requested amount first (executedQty −
      base-asset commission). Calibrated (shrunk) requests leave a
      PROTECT_QTY_CALIBRATED audit row.

  L2  place_sell_with_downgrade() — post-rejection downgrade: when a
      protective SELL is rejected, re-query the free balance; if
      0 < free < qty (the -2010 signature — any other rejection shape
      retried blind would just fail again), retry ONCE with
      floor(free, stepSize) and leave a PROTECT_QTY_DOWNGRADE audit row.

Callers (see worklogs WO-1042 impact analysis for the full inventory):
  - position_optimizer._place_switch_protections  (switch 8b)   L1 + L2
  - protection_guardian.run: emergency_sl ×2, free_slice_tp,
    tp_oco_rebuild OCO, oco_swap OCO                          L1 + L2
  - trade_executor entry chain                                 (upstream
    min(fills_qty, actual_free) calibration already existed — the
    normal-entry path never had this bug; documented, unchanged)

The normal-entry precedent (trade_executor "Fee-adjusted qty" block)
is the proven pattern this module generalizes: after a market BUY the
only number that can actually be sold is the exchange free balance.

All functions are fail-open: a balance API failure never blocks the
protective placement itself (best evidence at hand wins), and nothing
here ever RAISES into a caller that predates this module.
"""

import json
import logging
import math
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def base_asset(symbol: str) -> str:
    """Base asset of a spot pair (OGNUSDT -> OGN; unknown suffix -> as-is)."""
    for suffix in ("USDT", "USDC", "BUSD", "FDUSD", "TUSD"):
        if symbol.upper().endswith(suffix) and len(symbol) > len(suffix):
            return symbol[: -len(suffix)]
    return symbol


def _audit(action: str, details: Dict[str, Any]) -> None:
    """Best-effort audit row — never raises into the placement path."""
    try:
        from src.state_db import get_state_db

        get_state_db().audit_log(
            action, json.dumps(details, ensure_ascii=False, default=str))
    except Exception:
        logger.debug("protect_qty: audit write failed (%s)", action,
                     exc_info=True)


def floor_step(qty: float, step: float) -> float:
    """Floor qty to the exchange stepSize (identity when step unknown)."""
    try:
        s = float(step or 0.0)
        if s > 0:
            return round(math.floor(qty / s + 1e-9) * s, 12)
    except Exception:
        pass
    return float(qty)


def net_buy_qty(symbol: str, order: Optional[Dict], fallback_qty: float) -> float:
    """Net base qty actually received from a BUY fill response.

    Binance SPOT BUY: the taker fee settles in the BASE asset — fills[]
    carry commission + commissionAsset==BASE, executedQty is GROSS.
    net = gross − Σ commission(base). When the response carries no
    commission detail (paper/mocks/TWAP aggregates) the gross/fallback
    quantity is returned unchanged — the caller's L1 free-balance cap
    remains the backstop.
    """
    gross = float(fallback_qty or 0.0)
    try:
        if not order:
            return gross
        eq = order.get("executedQty") or order.get("executed_qty")
        if eq is not None and float(eq) > 0:
            gross = float(eq)
        base = base_asset(symbol)
        fills = order.get("fills") or []
        comm_base = 0.0
        for f in fills:
            asset = (f.get("commissionAsset") or "").upper()
            if asset == base.upper():
                comm_base += float(f.get("commission") or 0.0)
        if comm_base > 0:
            return max(gross - comm_base, 0.0)
        return gross
    except Exception:
        return gross


def _free_balance(client: Any, symbol: str) -> float:
    """Free balance of the base asset; -1.0 when unreadable."""
    try:
        fn = getattr(client, "get_free_balance", None)
        if fn is None:
            return -1.0
        return float(fn(base_asset(symbol)) or 0.0)
    except Exception:
        return -1.0


def calibrate_sell_qty(
    client: Any,
    symbol: str,
    requested: float,
    step: float,
    *,
    fill_order: Optional[Dict] = None,
    context: str = "",
) -> Tuple[float, Dict[str, Any]]:
    """L1 — calibrate a protective SELL qty against what is placeable.

    Priority: exchange free balance (the only number that can actually
    be sold) → fill-response net qty (fresh-buy paths) → requested as-is.
    Never raises, never returns more than ``requested``.

    Returns (qty, trace) — trace documents which source decided, for the
    audit trail and tests.
    """
    trace: Dict[str, Any] = {
        "symbol": symbol, "requested": requested,
        "source": "requested", "context": context,
    }
    free = _free_balance(client, symbol)
    if free > 0:
        trace["free"] = free
        qty = floor_step(min(requested, free), step)
        if qty < requested:
            trace.update(source="free_balance", calibrated_qty=qty)
            logger.info(
                "protect_qty: %s request %.8g capped at free balance %.8g "
                "-> %.8g (%s)", symbol, requested, free, qty, context or "-")
            _audit("PROTECT_QTY_CALIBRATED", trace)
            return qty, trace
        trace["source"] = "free_balance_ok"
        return floor_step(requested, step), trace
    # balance unknown (mock clients / API failure) → fill-based refinement
    if fill_order is not None:
        net = net_buy_qty(symbol, fill_order, requested)
        if 0 < net < requested:
            qty = floor_step(net, step)
            trace.update(source="fill_net", calibrated_qty=qty, net_fill=net)
            logger.info(
                "protect_qty: %s request %.8g refined to net fill %.8g "
                "-> %.8g (%s)", symbol, requested, net, qty, context or "-")
            _audit("PROTECT_QTY_CALIBRATED", trace)
            return qty, trace
    return floor_step(requested, step), trace


def place_sell_with_downgrade(
    place: Callable[[float], Any],
    client: Any,
    symbol: str,
    qty: float,
    step: float,
    *,
    kind: str,
    price: float = 0.0,
    min_qty: float = 0.0,
    min_notional: float = 0.0,
    context: str = "",
    already_rejected: bool = False,
) -> Tuple[Any, float]:
    """L2 — place a protective SELL; on rejection downgrade to free balance.

    Retry happens ONLY when 0 < free < qty — the -2010 signature. A
    rejection with free >= qty (bad price/params/permissions) or
    free == 0 (nothing to sell) is returned as-is: retrying blind would
    just fail again and mask the real error. Every downgrade attempt
    leaves a PROTECT_QTY_DOWNGRADE audit row (留痕).

    Returns (result, qty_used): result falsy ⇒ both attempts failed and
    qty_used is the originally requested qty (callers fall through to
    their own PROTECTION_FAILED handling unchanged).
    """
    if already_rejected:
        # caller already attempted qty (e.g. tp_oco_rebuild's own retry
        # loop) and got a rejection — never re-place the same quantity
        # blind (WO-0923-viii semantics); go straight to the free-balance
        # downgrade check
        res = None
    else:
        try:
            res = place(qty)
        except Exception as e:
            logger.warning(
                "protect_qty: %s place failed for %s qty %.8g: %s",
                kind, symbol, qty, e)
            res = None
        if res:
            return res, qty

    free = _free_balance(client, symbol)
    if not (0 < free < qty):
        return None, qty
    retry = floor_step(free, step)
    if retry <= 0 or retry >= qty:
        return None, qty
    if min_qty and retry < min_qty:
        return None, qty
    if min_notional and price > 0 and retry * price < min_notional:
        return None, qty

    trace = {"symbol": symbol, "kind": kind, "requested": qty,
             "free": free, "downgraded_qty": retry,
             "price": price, "context": context}
    _audit("PROTECT_QTY_DOWNGRADE", trace)
    logger.warning(
        "protect_qty: %s rejected for %s (requested %.8g) — -2010 "
        "signature, retrying ONCE with free balance %.8g",
        kind, symbol, qty, retry)
    try:
        res = place(retry)
    except Exception as e:
        logger.warning(
            "protect_qty: %s downgraded retry failed for %s qty %.8g: %s",
            kind, symbol, retry, e)
        res = None
    return res, retry
