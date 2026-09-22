"""P0-2 Defense Item 2: stuck (pending/open) order monitor.

Each scan cycle, list open orders across all symbols and act on orders
that have sat unfilled (or partially filled and stalled) for longer than
STUCK_TIMEOUT_S (default 15 min):

  - emit a live_alerts JSON (event STUCK_ORDER) with full order details
  - auto-cancel via client.cancel_order(symbol, order_id)
  - a failed cancel escalates to STUCK_ORDER_CANCEL_FAIL

Protective orders (OCO legs, stop-loss / take-profit types) are EXCLUDED
by design — those are long-lived by intent and cancelling them would
strip existing positions of their SL/TP protection. Only plain entry
LIMIT orders are monitored.

Fail-open: any API/list error skips the round silently (never raises
into the pipeline). SPOT ONLY.
"""

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Orders older than this (seconds) with status NEW/PARTIALLY_FILLED are stuck.
STUCK_TIMEOUT_S = 15 * 60

#: orderType / type substrings marking protective orders — never cancel.
PROTECTIVE_TYPES = (
    "STOP_LOSS", "TAKE_PROFIT", "OCO", "STOP", "TRAILING", "LIMIT_MAKER",
)

#: Statuses considered live-but-unfilled.
ACTIVE_STATUSES = {"NEW", "PARTIALLY_FILLED", "PARTIAL_FILLED", "PENDING"}


def _is_protective(order: Dict[str, Any]) -> bool:
    otype = str(
        order.get("orderType") or order.get("type") or ""
    ).upper()
    if any(p in otype for p in PROTECTIVE_TYPES):
        return True
    # OCO parents/legs expose contingencyType (e.g. OCO/OTO)
    if order.get("contingencyType") or order.get("listStatusType"):
        return True
    # OCO legs carry orderListId > 0 (Binance native field, preserved by wrapper);
    # legacy listId kept for compatibility. Fix WO-0922-017-iv: `listId` never
    # existed on openOrders legs, so LIMIT_MAKER TP legs were misjudged as plain
    # entry orders and cancelled, stripping positions of SL via OCO atomicity.
    try:
        if int(order.get("orderListId") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    if (order.get("listId") or order.get("origClientOrderId")
            or order.get("listClientOrderId")):
        return True
    # guardian-healed protective orders use cat_ clientOrderId prefix
    if str(order.get("clientOrderId") or "").startswith("cat_"):
        return True
    return False


def _order_age_s(order: Dict[str, Any], now: Optional[float] = None) -> float:
    """Seconds since order creation/update. Uses ms `time` then `updateTime`."""
    now = now if now is not None else time.time()
    ts_ms = order.get("time") or order.get("updateTime") or 0
    try:
        ts_s = float(ts_ms) / 1000.0
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, now - ts_s)


def _is_stuck(order: Dict[str, Any], now: Optional[float] = None) -> bool:
    status = str(order.get("status") or "").upper()
    if status not in ACTIVE_STATUSES:
        return False
    return _order_age_s(order, now) > STUCK_TIMEOUT_S


def run(client: Any, log: Optional[logging.Logger] = None) -> Dict[str, Any]:
    """One monitor round. Returns summary dict (never raises)."""
    log = log or logger
    summary = {
        "checked": 0, "protective_skipped": 0, "stuck": 0,
        "cancelled": 0, "cancel_failed": 0,
    }
    try:
        orders = client.get_open_orders(symbol=None) or []
    except Exception:
        log.warning("stuck_order_monitor: get_open_orders failed "
                    "(fail-open, round skipped)", exc_info=True)
        return summary

    from src.live_alerts import emit as emit_alert

    for order in orders:
        if not isinstance(order, dict):
            continue
        summary["checked"] += 1
        symbol = order.get("symbol") or "NA"
        order_id = order.get("orderId")
        if _is_protective(order):
            summary["protective_skipped"] += 1
            continue
        if not _is_stuck(order):
            continue

        summary["stuck"] += 1
        age_s = _order_age_s(order)
        detail = {
            "order_id": order_id, "symbol": symbol,
            "status": order.get("status"),
            "type": order.get("type") or order.get("orderType"),
            "price": order.get("price"),
            "orig_qty": order.get("origQty") or order.get("quantity"),
            "executed_qty": order.get("executedQty"),
            "age_s": round(age_s, 1),
            "action": "cancel",
        }
        emit_alert("STUCK_ORDER", symbol, detail)
        log.warning("stuck_order_monitor: %s order %s stuck %.1f min — "
                    "cancelling", symbol, order_id, age_s / 60.0)
        try:
            res = client.cancel_order(symbol=symbol, order_id=order_id)
            # cancel succeeded iff the call returned without raising —
            # Binance acks with the cancelled order object (or None on
            # some paper-client implementations).
            summary["cancelled"] += 1
            if res:
                detail["cancel_ack_status"] = res.get("status")
        except Exception:
            summary["cancel_failed"] += 1
            fail_detail = dict(detail)
            fail_detail["action"] = "cancel_failed"
            emit_alert("STUCK_ORDER_CANCEL_FAIL", symbol, fail_detail)
            log.error("stuck_order_monitor: cancel failed for %s order %s",
                      symbol, order_id, exc_info=True)

    if summary["stuck"]:
        log.warning("stuck_order_monitor: round summary %s", summary)
    return summary
