"""WO-0921-013: TP-protection backstop that actually exists.

Background: every "ensure_tp_sl will retry" comment in the codebase
referred to a function that was never implemented — when the switch
path's TP limit sell failed (-2010, balance locked by the just-placed
full-qty SL), nothing ever retried. 9/20 TRX and 9/21 17:52
FET/WLD/BNB all ended up SL-only with no TP.

This guardian runs once per scan cycle and heals missing TP coverage:

  per position:
    - classify open orders: OCO legs (listId/contingencyType/STOP_LOSS/
      TAKE_PROFIT types), plain TP limit sells, plain SL stop-limits
    - tp_covered = oco_qty + plain TP qty
    - covered >= TP_COVER_MIN_FRAC * holding → OK, skip (idempotent)
    - free (unlocked) qty big enough for minQty+minNotional → place a
      plain TP limit sell on the free slice
    - balance fully locked by plain SL legs (no OCO) → cancel-first
      swap: cancel SL legs → place one OCO covering the whole step-
      floored holding (TP from DB take_profit or entry*1.04, SL at the
      old stop price) → on OCO failure re-place the old SL legs
      immediately (never leave the position naked)

Design rules:
- SPOT ONLY; never raises into the pipeline (per-position fail-open)
- emits PROTECTION_HEALED / PROTECTION_HEAL_FAILED live_alerts
- qty/price always floored to exchange stepSize/tickSize
- minNotional-ineligible positions are skipped (dust_reaper territory)
"""

import logging
import math
import time
from typing import Any, Dict, List, Optional

from src.live_alerts import emit as emit_alert

logger = logging.getLogger(__name__)

#: a position whose TP coverage (OCO + plain TP) is at least this
#: fraction of holding is considered protected
TP_COVER_MIN_FRAC = 0.5

#: fallback TP level when the DB row carries none
DEFAULT_TP_PCT = 0.04

#: order types marking OCO legs / stop orders
_OCO_MARKERS = ("STOP_LOSS", "TAKE_PROFIT", "OCO")


def _step_floor(qty: float, step: float) -> float:
    if step and step > 0:
        return math.floor(qty / step + 1e-9) * step
    return qty


def _tick_round(px: float, tick: float) -> float:
    if tick and tick > 0:
        return round(math.floor(px / tick + 1e-9) * tick, 8)
    return round(px, 8)


def _order_qty(o: Dict[str, Any]) -> float:
    for k in ("origQty", "quantity", "qty"):
        try:
            v = float(o.get(k) or 0)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return 0.0


def _is_oco_leg(o: Dict[str, Any]) -> bool:
    """Only a genuine OCO member order counts here (listId set). A bare
    STOP_LOSS_LIMIT without a listId is an INDEPENDENT stop order — it
    still locks the balance and must be treated as a plain SL leg."""
    return bool(o.get("listId") or o.get("contingencyType"))


def _is_plain_tp(o: Dict[str, Any]) -> bool:
    if _is_oco_leg(o):
        return False
    if str(o.get("side") or "").upper() != "SELL":
        return False
    otype = str(o.get("type") or o.get("orderType") or "").upper()
    # plain TP limit sell, or an independent TAKE_PROFIT stop order
    return otype in ("LIMIT", "LIMIT_MAKER") or otype.startswith(
        "TAKE_PROFIT")


def _is_plain_sl(o: Dict[str, Any]) -> bool:
    if _is_oco_leg(o):
        return False
    return str(o.get("type") or o.get("orderType") or "").upper().startswith(
        "STOP_LOSS")


def run(client: Any, portfolio: Any,
        log: Optional[logging.Logger] = None) -> Dict[str, Any]:
    """One guardian sweep. Returns summary (never raises)."""
    log = log or logger
    summary = {"checked": 0, "healed": 0, "failed": 0, "skipped": 0}
    try:
        positions = portfolio.get_all_positions() or []
    except Exception:
        log.warning("protection_guardian: positions read failed",
                    exc_info=True)
        return summary
    if isinstance(positions, dict):
        positions = list(positions.values())


    for pos in positions:
        try:
            sym = pos.get("symbol")
            qty = float(pos.get("quantity") or pos.get("qty") or 0.0)
            if not sym or qty <= 0:
                continue
            summary["checked"] += 1
            entry = float(pos.get("entry_price") or 0.0)
            price = pos.get("current_price") or entry
            if not price or float(price) <= 0:
                price = entry
            price = float(price)

            try:
                filters = client.get_symbol_filters(sym) or {}
            except Exception:
                filters = {}
            step = float(filters.get("stepSize", 0.0) or 0.0)
            tick = float(filters.get("tickSize", 0.0) or 0.0)
            min_qty = float(filters.get("minQty", 0.0) or 0.0)
            min_notional = float(filters.get("minNotional", 10.0) or 10.0)

            if qty * price < min_notional:
                summary["skipped"] += 1
                continue

            try:
                orders = client.get_open_orders(sym) or []
            except Exception:
                log.warning("protection_guardian: open orders failed for "
                            "%s (fail-open)", sym, exc_info=True)
                summary["failed"] += 1
                continue

            oco_qty = sum(_order_qty(o) for o in orders if _is_oco_leg(o))
            tp_qty = sum(_order_qty(o) for o in orders if _is_plain_tp(o))
            sl_orders = [o for o in orders if _is_plain_sl(o)]
            sl_qty = sum(_order_qty(o) for o in sl_orders)

            covered = oco_qty + tp_qty
            if covered >= qty * TP_COVER_MIN_FRAC:
                continue  # protected enough — idempotent skip

            tp_px = pos.get("take_profit")
            try:
                tp_px = float(tp_px) if tp_px else 0.0
            except (TypeError, ValueError):
                tp_px = 0.0
            if not tp_px or tp_px <= 0:
                tp_px = entry * (1 + DEFAULT_TP_PCT)
            tp_px = _tick_round(tp_px, tick)

            free_qty = _step_floor(qty - oco_qty - tp_qty - sl_qty, step)
            if free_qty >= min_qty and free_qty * price >= min_notional:
                # free slice big enough → plain TP on it (no lock dance)
                try:
                    res = client.place_limit_sell(sym, free_qty, tp_px)
                except Exception:
                    res = None
                if res:
                    summary["healed"] += 1
                    emit_alert("PROTECTION_HEALED", sym, {
                        "mode": "free_slice_tp", "qty": free_qty,
                        "tp_px": tp_px, "ts": time.time(),
                    })
                    log.info("protection_guardian: TP placed for %s "
                             "%.8g @ %.8g (free slice)", sym, free_qty,
                             tp_px)
                    continue
                summary["failed"] += 1
                emit_alert("PROTECTION_HEAL_FAILED", sym, {
                    "mode": "free_slice_tp", "qty": free_qty,
                    "tp_px": tp_px, "ts": time.time(),
                })
                continue

            if sl_qty > 0 and oco_qty <= 0:
                # fully locked by plain SL → cancel-first OCO swap with
                # safety net (re-place old SL if OCO fails)
                old_legs = [{
                    "qty": _order_qty(o),
                    "stop": float(o.get("stopPrice") or o.get("price") or 0),
                    "id": o.get("orderId"),
                } for o in sl_orders]
                cancelled = []
                for leg in old_legs:
                    try:
                        client.cancel_order(sym, leg["id"])
                        cancelled.append(leg)
                    except Exception:
                        log.warning("protection_guardian: cancel SL %s "
                                    "failed for %s", leg["id"], sym,
                                    exc_info=True)
                if len(cancelled) != len(old_legs):
                    summary["failed"] += 1
                    emit_alert("PROTECTION_HEAL_FAILED", sym, {
                        "mode": "oco_swap_partial_cancel",
                        "cancelled": len(cancelled),
                        "total": len(old_legs), "ts": time.time(),
                    })
                    continue
                oco_qty_step = _step_floor(qty, step)
                old_stop = max((l["stop"] for l in cancelled
                                if l["stop"] > 0), default=0.0) or \
                    _tick_round(price * 0.93, tick)
                try:
                    oco = client.place_oco(sym, oco_qty_step, tp_px,
                                           old_stop)
                except Exception:
                    oco = None
                if oco:
                    summary["healed"] += 1
                    emit_alert("PROTECTION_HEALED", sym, {
                        "mode": "oco_swap", "qty": oco_qty_step,
                        "tp_px": tp_px, "sl_px": old_stop,
                        "ts": time.time(),
                    })
                    log.warning("protection_guardian: %s SL→OCO swapped "
                                "(TP restored) qty %.8g tp %.8g sl %.8g",
                                sym, oco_qty_step, tp_px, old_stop)
                else:
                    # safety net: restore old SL legs immediately
                    restored = 0
                    for leg in cancelled:
                        try:
                            client.place_stop_loss_limit(
                                sym, leg["qty"],
                                _tick_round(leg["stop"] * 0.995, tick),
                                leg["stop"])
                            restored += 1
                        except Exception:
                            logger.error("protection_guardian: SL restore "
                                         "FAILED for %s leg %s", sym,
                                         leg["id"], exc_info=True)
                    summary["failed"] += 1
                    emit_alert("PROTECTION_HEAL_FAILED", sym, {
                        "mode": "oco_swap_failed_sl_restored",
                        "restored": restored, "legs": len(cancelled),
                        "ts": time.time(),
                    })
                    log.error("protection_guardian: OCO swap failed for "
                              "%s — %d/%d SL legs restored", sym, restored,
                              len(cancelled))
                continue

            # nothing locked, nothing free (tiny residue) — nothing to do
            summary["skipped"] += 1
        except Exception:
            summary["failed"] += 1
            log.warning("protection_guardian: per-position error (%s)",
                        pos.get("symbol"), exc_info=True)

    if summary["healed"] or summary["failed"]:
        log.warning("protection_guardian: sweep %s", summary)
    return summary
