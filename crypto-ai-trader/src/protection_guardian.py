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

#: WO-017-4a: default SL distance for SL-side restores (pos.stop_loss wins)
DEFAULT_SL_PCT = 0.08
#: WO-017-4c: tracker entries with SL=no older than this raise an alert (s)
SL_MISSING_WARN_S = 15 * 60

#: fallback TP level when the DB row carries none
DEFAULT_TP_PCT = 0.04

#: order types marking OCO legs / stop orders
_OCO_MARKERS = ("STOP_LOSS", "TAKE_PROFIT", "OCO")


_BREACH_ALERT_INTERVAL_S = 3600  # one PROTECTION_TP_TARGET_BREACHED
# per symbol per hour while the state persists


def _emit_breach_throttled(sym: str, payload: dict) -> None:
    """TP-target-breached persists for hours — without throttling that
    is ~144 alerts/day/position at the 10-min scan cadence. First alert
    immediate, then hourly; state cleared when the breach resolves."""
    try:
        from src.state_db import get_state_db
        db = get_state_db()
        now = time.time()
        key = f"tp_breach_state:{sym}"
        st = db.kv_get(key)
        st = st if isinstance(st, dict) else {}
        last = float(st.get("last_alert_ts") or 0)
        first = float(st.get("first_ts") or now)
        if last <= 0 or now - last >= _BREACH_ALERT_INTERVAL_S:
            emit_alert("PROTECTION_TP_TARGET_BREACHED", sym, payload)
            _set_breach_state(sym, {"first_ts": first,
                                    "last_alert_ts": now})
    except Exception:
        # opaque/unavailable db —宁可重复告警也不静默
        emit_alert("PROTECTION_TP_TARGET_BREACHED", sym, payload)


def _clear_breach_state(sym: str) -> None:
    _set_breach_state(sym, {})


def _err_tag(exc) -> str:
    """WO-0923-viii: classify an exchange error for the rescue ladder.

    Wrapper business errors surface as tuple (http, code, msg, headers,
    body); anything else is treated as transient network noise.
    """
    code, msg = None, str(exc)
    try:
        t = exc.args[0] if exc.args else None
        if isinstance(t, tuple) and len(t) >= 3:
            code, msg = t[1], str(t[2])
    except Exception:
        pass
    low = msg.lower()
    if code in (-1001, -1003, -1006, -1021) or any(
            k in low for k in ("timeout", "disconnect", "timed out")):
        return "transient"
    if code == -2010 and "insufficient" in low:
        return "insufficient"
    return "business"


def _audit(action: str, details: dict) -> None:
    """Best-effort audit_log row (timestamp/action/details schema).

    P2-③: routes through ledger.record_repair (adds a REPAIR event for
    shadow visibility; the audit row keeps its ORIGINAL action name so
    existing greps/dashboards are unaffected). Legacy direct write is
    kept as the rollback fallback (kv 'ledger:repairs' = 0)."""
    try:
        from src.state_db import get_state_db
        from src.ledger import record_repair, repairs_enabled
        _db = get_state_db()
        if repairs_enabled(_db):
            record_repair({"kind": "guard_audit",
                           "source": "protection_guardian",
                           "payload": {"action": action, "details": details}},
                          db=_db)
            return
    except Exception:
        logger.warning("protection_guardian: ledger audit funnel failed "
                       "(%s) — falling back to direct write", action,
                       exc_info=True)
    try:
        import json as _json
        from src.state_db import get_state_db
        # P6-B3: StateDB.audit_log replaces the direct INSERT. details is
        # pre-dumped with ensure_ascii=False so the stored row bytes match
        # the legacy write; source now records the default 'system'
        # instead of NULL (fallback-only path; readers grep by action).
        get_state_db().audit_log(
            action, _json.dumps(details, ensure_ascii=False))
    except Exception:
        logger.warning("protection_guardian: audit write failed (%s)",
                       action, exc_info=True)


def _track(symbol: str, entry: float, qty: float, tp_orders: list,
           sl_order: Optional[dict]) -> None:
    """Persist tp_sl_tracker state after a heal (best-effort).

    P2-③: routes through ledger.record_repair — the ledger owns the kv
    write (same key/value shape as tp_sl_tracker.save_state) plus one
    LEDGER_REPAIR audit row and one REPAIR event. Legacy direct write is
    kept as the rollback fallback (kv 'ledger:repairs' = 0)."""
    try:
        from src.state_db import get_state_db
        from src.ledger import record_repair, repairs_enabled
        _db = get_state_db()
        if repairs_enabled(_db):
            record_repair({"kind": "tracker_state", "symbol": symbol,
                           "source": "protection_guardian",
                           "payload": {"entry": entry, "qty": qty,
                                       "tp_orders": tp_orders,
                                       "sl_order": sl_order}}, db=_db)
            return
    except Exception:
        logger.warning("protection_guardian: ledger tracker funnel failed "
                       "for %s — falling back to direct write", symbol,
                       exc_info=True)
    try:
        from src.tp_sl_tracker import save_state
        save_state(symbol, entry, qty, tp_orders, sl_order)
    except Exception:
        logger.warning("protection_guardian: tracker save failed for %s",
                       symbol, exc_info=True)


def _set_swap_ts(sym: str, value: dict) -> None:
    """gov:swap_ts debounce write (24h SL→OCO swap guard).

    P2-③: funneled through ledger.record_repair; legacy direct kv write
    kept as the rollback fallback (kv 'ledger:repairs' = 0)."""
    try:
        from src.state_db import get_state_db
        from src.ledger import record_repair, repairs_enabled
        _db = get_state_db()
        if repairs_enabled(_db):
            record_repair({"kind": "swap_ts", "symbol": sym,
                           "source": "protection_guardian",
                           "payload": {"value": value}}, db=_db)
            return
    except Exception:
        logger.warning("protection_guardian: swap_ts ledger funnel failed "
                       "for %s — falling back to direct write", sym,
                       exc_info=True)
    try:
        from src.state_db import get_state_db
        get_state_db().kv_set("gov:swap_ts:" + sym, value)
    except Exception:
        pass


def _set_breach_state(sym: str, state: dict) -> None:
    """tp_breach_state:{sym} throttle-state write (alert dedup window).

    P2-③: funneled through ledger.record_repair (REPAIR event only —
    no audit row, matching the pre-P2 behavior of writing no audit for
    throttle state); legacy direct kv write kept as rollback fallback."""
    try:
        from src.state_db import get_state_db
        from src.ledger import record_repair, repairs_enabled
        _db = get_state_db()
        if repairs_enabled(_db):
            record_repair({"kind": "breach_state", "symbol": sym,
                           "source": "protection_guardian",
                           "payload": {"state": state}}, db=_db)
            return
    except Exception:
        logger.warning("protection_guardian: breach_state ledger funnel "
                       "failed for %s — falling back to direct write", sym,
                       exc_info=True)
    try:
        from src.state_db import get_state_db
        get_state_db().kv_set(f"tp_breach_state:{sym}", state)
    except Exception:
        pass


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

            # WO-017-4a: pair check — BOTH sides must be covered. The
            # old single-axis check treated a TP-only state as protected
            # and never looked at the SL side (TRUMP 9/22 14:31: stuck
            # cancel killed the OCO, heal re-placed only the TP leg).
            tp_covered = oco_qty + tp_qty
            sl_covered = oco_qty + sl_qty
            # SL side uses an existence check (not a fraction): partial
            # SL ladders are a legitimate strategy end-state (Strategy-C
            # 70/30 split), but ZERO SL means the downside is naked.
            if (tp_covered >= qty * TP_COVER_MIN_FRAC
                    and sl_covered > 0.0):
                continue  # both sides protected — idempotent skip

            tp_px = pos.get("take_profit")
            try:
                tp_px = float(tp_px) if tp_px else 0.0
            except (TypeError, ValueError):
                tp_px = 0.0
            if not tp_px or tp_px <= 0:
                tp_px = entry * (1 + DEFAULT_TP_PCT)
            tp_px = _tick_round(tp_px, tick)

            # A LIMIT SELL at/below market fills IMMEDIATELY — never
            # place a TP the market has already run past (that is the
            # take-profit-target-breached state; deciding to sell is a
            # strategy decision, not the guardian's).
            if tp_px <= price * 1.001:
                _emit_breach_throttled(sym, {
                    "tp_px": tp_px, "price": price,
                    "note": "price at/above TP target — "
                            "take-profit is a strategy decision",
                    "ts": time.time(),
                })
                log.info("protection_guardian: %s TP target %.8g "
                         "breached (px %.8g) — no TP placement", sym,
                         tp_px, price)
                # WO-017-4a: nakedness is judged on the SL side — a
                # hanging TP does not protect the downside
                if oco_qty <= 0 and sl_qty <= 0:
                    em_stop = _tick_round(price * 0.87, tick)
                    try:
                        ret = client.place_stop_loss_limit(
                            sym, _step_floor(qty, step),
                            _tick_round(em_stop * 0.995, tick), em_stop)
                    except Exception:
                        ret = None
                    if ret:
                        summary["healed"] += 1
                        emit_alert("PROTECTION_HEALED", sym, {
                            "mode": "emergency_sl", "qty": qty,
                            "sl_px": em_stop, "urgent": True,
                            "note": "naked position + TP target "
                                    "breached — wide -13% stop, manual "
                                    "review advised",
                            "ts": time.time()})
                        _track(sym, entry, qty, [], {
                            "order_id": (ret.get("orderId")
                                         if isinstance(ret, dict) else
                                         None),
                            "price": em_stop, "qty": qty,
                            "stop_price": em_stop})
                    else:
                        summary["failed"] += 1
                        emit_alert("PROTECTION_HEAL_FAILED", sym, {
                            "mode": "emergency_sl", "urgent": True,
                            "note": "naked position, SL place FAILED — "
                                    "manual protection required",
                            "ts": time.time()})
                else:
                    summary["skipped"] += 1
                continue

            # breach resolved — clear throttle state
            _clear_breach_state(sym)

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
                    _track(sym, entry, qty, [{
                        "order_id": res.get("orderId"),
                        "price": tp_px, "qty": free_qty, "tier": 1,
                        "pct": None, "side": "LIMIT",
                    }], None)
                    continue
                summary["failed"] += 1
                emit_alert("PROTECTION_HEAL_FAILED", sym, {
                    "mode": "free_slice_tp", "qty": free_qty,
                    "tp_px": tp_px, "ts": time.time(),
                })
                continue

            if sl_qty <= 0 and oco_qty <= 0 and tp_qty <= 0:
                # NAKED position (e.g. ENA 22:03: OCO rejected, old SL
                # cancel already done, restore hit PERCENT_PRICE -1013).
                # A wide emergency SL beats staying unprotected.
                em_stop = _tick_round(price * 0.87, tick)
                try:
                    ret = client.place_stop_loss_limit(
                        sym, _step_floor(qty, step),
                        _tick_round(em_stop * 0.995, tick), em_stop)
                except Exception:
                    ret = None
                if ret:
                    summary["healed"] += 1
                    emit_alert("PROTECTION_HEALED", sym, {
                        "mode": "emergency_sl", "qty": qty,
                        "sl_px": em_stop, "urgent": True,
                        "note": "naked position — wide -13% stop, manual "
                                "review advised", "ts": time.time(),
                    })
                    _track(sym, entry, qty, [], {
                        "order_id": (ret.get("orderId")
                                     if isinstance(ret, dict) else None),
                        "price": em_stop, "qty": qty,
                        "stop_price": em_stop,
                    })
                else:
                    summary["failed"] += 1
                    emit_alert("PROTECTION_HEAL_FAILED", sym, {
                        "mode": "emergency_sl", "urgent": True,
                        "note": "naked position, SL place FAILED — "
                                "manual protection required",
                        "ts": time.time(),
                    })
                continue

            if tp_qty > 0 and oco_qty <= 0 and sl_qty <= 0:
                # WO-017-4a: TP-only lock — the mirror case of the
                # SL→OCO swap below. Plain TP sells lock the base asset,
                # so an independent SL would double-lock and reject;
                # the only correct restore is cancelling the orphan TP
                # legs and rebuilding a full OCO over the same slice
                # (TRUMP 9/22: the manual 15:02 restore path).
                tp_legs = [{
                    "qty": _order_qty(o),
                    "px": float(o.get("price") or 0),
                    "id": o.get("orderId"),
                } for o in orders if _is_plain_tp(o)]
                tp_px_live = (sum(l["qty"] * l["px"] for l in tp_legs)
                              / sum(l["qty"] for l in tp_legs
                                    if l["qty"] > 0)
                              ) if tp_legs else 0.0
                if not tp_px_live or tp_px_live <= price * 1.001:
                    tp_px_live = tp_px  # computed default above
                sl_px_new = pos.get("stop_loss")
                try:
                    sl_px_new = float(sl_px_new) if sl_px_new else 0.0
                except (TypeError, ValueError):
                    sl_px_new = 0.0
                if not sl_px_new or sl_px_new <= 0:
                    sl_px_new = _tick_round(
                        (entry or price) * (1 - DEFAULT_SL_PCT), tick)
                else:
                    sl_px_new = _tick_round(sl_px_new, tick)
                # never cancel a leg whose replacement would be rejected:
                # Binance PERCENT_PRICE_BY_SIDE refuses stops >~13% away
                if sl_px_new < price * 0.87:
                    summary["skipped"] += 1
                    emit_alert("PROTECTION_SL_OUT_OF_BAND", sym, {
                        "planned_stop": sl_px_new, "price": price,
                        "note": "TP-only position, planned SL too far "
                                "below market — rebuild aborted, manual "
                                "review advised",
                        "ts": time.time(),
                    })
                    log.info("protection_guardian: %s planned stop %.8g "
                             "outside ±13%% band of px %.8g — skip "
                             "TP→OCO rebuild", sym, sl_px_new, price)
                    continue
                cancelled_tp = []
                for leg in tp_legs:
                    try:
                        client.cancel_order(sym, leg["id"])
                        cancelled_tp.append(leg)
                    except Exception:
                        log.warning("protection_guardian: cancel TP %s "
                                    "failed for %s", leg["id"], sym,
                                    exc_info=True)
                if len(cancelled_tp) != len(tp_legs):
                    summary["failed"] += 1
                    emit_alert("PROTECTION_HEAL_FAILED", sym, {
                        "mode": "tp_oco_rebuild_partial_cancel",
                        "cancelled": len(cancelled_tp),
                        "total": len(tp_legs), "ts": time.time(),
                    })
                    continue
                rebuild_qty = _step_floor(
                    min(qty, sum(l["qty"] for l in cancelled_tp)
                        + max(qty - tp_qty - sl_qty, 0.0)), step)
                oco = None
                oco_err = None
                # WO-0923-viii: retry transient/rate-limit errors with
                # backoff; business errors (-2010 price-relationship,
                # insufficient balance, filter rejects) fall through to
                # the rescue ladder immediately — blind retries cannot
                # fix a rejected price relationship.
                for attempt in range(3):
                    try:
                        oco = client.place_oco(
                            sym, rebuild_qty, tp_px_live, sl_px_new)
                        oco_err = None
                        break
                    except Exception as e:
                        oco_err = e
                        if (_err_tag(e) == "transient"
                                and attempt < 2):
                            log.warning(
                                "protection_guardian: OCO rebuild for %s "
                                "transient failure (attempt %d) — "
                                "retrying", sym, attempt + 1)
                            time.sleep(0.5 * (2 ** attempt))
                            continue
                        break
                if oco:
                    summary["healed"] += 1
                    emit_alert("PROTECTION_HEALED", sym, {
                        "mode": "tp_oco_rebuild", "qty": rebuild_qty,
                        "tp_px": tp_px_live, "sl_px": sl_px_new,
                        "note": "TP-only lock rebuilt as full OCO — "
                                "SL side restored",
                        "ts": time.time(),
                    })
                    log.warning("protection_guardian: %s TP→OCO rebuilt "
                                "(SL restored) qty %.8g tp %.8g sl %.8g",
                                sym, rebuild_qty, tp_px_live, sl_px_new)
                    _list_id = (oco.get("orderListId")
                                if isinstance(oco, dict) else None)
                    _track(sym, entry, rebuild_qty, [{
                        "order_id": _list_id,
                        "price": tp_px_live, "qty": rebuild_qty,
                        "tier": 1, "pct": None, "side": "OCO_TP",
                    }], {
                        "order_id": _list_id,
                        "price": sl_px_new, "qty": rebuild_qty,
                        "stop_price": sl_px_new,
                    })
                else:
                    # WO-0923-viii rescue ladder — invariant: the position
                    # must never end this branch without SL or equivalent
                    # downside protection, and never silently.
                    #   1. transient OCO failures already retried above
                    #   2. DEMOTE: restore all TP legs except the last,
                    #      then place a plain STOP_LOSS_LIMIT over that
                    #      leg's qty (its base stays free, so no -2010
                    #      double-lock; Travis 9/23 UNI manual play:
                    #      cancel TP2 -> SL 9.251/9.112 qty 0.78)
                    #   3. plain SL fails too -> restore the demoted TP
                    #      leg as well and emit an ERROR-grade invariant
                    #      breach alert (cron bridge must see it)
                    demote_leg = cancelled_tp[-1]
                    keep_legs = cancelled_tp[:-1]
                    kept_orders = []
                    for leg in keep_legs:
                        try:
                            ret = client.place_limit_sell(
                                sym, leg["qty"], leg["px"])
                            kept_orders.append({
                                "order_id": (ret.get("orderId")
                                             if isinstance(ret, dict)
                                             else None),
                                "price": leg["px"], "qty": leg["qty"],
                                "tier": None, "pct": None,
                                "side": "LIMIT",
                            })
                        except Exception:
                            logger.error("protection_guardian: TP restore "
                                         "FAILED for %s leg %s", sym,
                                         leg["id"], exc_info=True)
                    sl_px_limit = _tick_round(sl_px_new * 0.995, tick)
                    demoted_qty = _step_floor(demote_leg["qty"], step)
                    sl_ret = None
                    try:
                        sl_ret = client.place_stop_loss_limit(
                            sym, demoted_qty, sl_px_limit, sl_px_new)
                    except Exception:
                        sl_ret = None
                    if sl_ret:
                        summary["healed"] += 1
                        sl_id = (sl_ret.get("orderId")
                                 if isinstance(sl_ret, dict) else None)
                        _track(sym, entry, qty, kept_orders, {
                            "order_id": sl_id, "price": sl_px_limit,
                            "qty": demoted_qty, "stop_price": sl_px_new,
                        })
                        _audit("GUARDIAN_SL_DEMOTE", {
                            "symbol": sym,
                            "reason": "tp_oco_rebuild_failed",
                            "oco_error": str(oco_err),
                            "demoted_tp": {"id": demote_leg["id"],
                                           "px": demote_leg["px"],
                                           "qty": demoted_qty},
                            "kept_tps": len(kept_orders),
                            "sl": {"stop": sl_px_new,
                                   "limit": sl_px_limit,
                                   "qty": demoted_qty,
                                   "order_id": sl_id},
                            "ts": time.time(),
                        })
                        emit_alert("PROTECTION_HEALED", sym, {
                            "mode": "sl_demote", "qty": demoted_qty,
                            "sl_stop": sl_px_new, "sl_limit": sl_px_limit,
                            "note": "OCO rebuild rejected — TP leg demoted "
                                    "to plain SL, downside protected",
                            "ts": time.time(),
                        })
                        log.warning("protection_guardian: %s OCO rebuild "
                                    "failed (%s) — demoted 1 TP leg to "
                                    "plain SL qty %.8g stop %.8g",
                                    sym, oco_err, demoted_qty, sl_px_new)
                    else:
                        # restore the demoted TP too, then scream
                        try:
                            client.place_limit_sell(
                                sym, demote_leg["qty"], demote_leg["px"])
                        except Exception:
                            logger.error("protection_guardian: demoted TP "
                                         "restore also FAILED for %s", sym,
                                         exc_info=True)
                        summary["failed"] += 1
                        emit_alert("PROTECTION_HEAL_FAILED", sym, {
                            "mode": "sl_rescue_failed",
                            "urgent": True,
                            "oco_error": str(oco_err),
                            "note": "OCO rebuild AND plain SL demote both "
                                    "FAILED — position may lack downside "
                                    "protection, manual action REQUIRED",
                            "ts": time.time(),
                        })
                        log.error("protection_guardian: %s SL rescue "
                                  "failed (OCO err: %s) — TP legs "
                                  "restored, NO SL, manual action "
                                  "required", sym, oco_err)
                continue

            if sl_qty > 0 and oco_qty <= 0:
                # WO-0924-ix: plain TP(s) + plain SL is a LEGAL terminal
                # shape (SL-demote rescue product, or manual de-escalation
                # like Travis' 9/23 UNI TP1+SL). Swap is only needed when
                # the plain SL locks the FULL base and NO TP can stand —
                # if any plain TP leg is live, don't touch the pair.
                if tp_qty > 0:
                    summary["skipped"] += 1
                    log.info("protection_guardian: %s plain TP(%.8g)+SL "
                             "pair legal — skip SL→OCO swap", sym, tp_qty)
                    continue
                # 24h debounce: at most one swap attempt per symbol per
                # day. Stored under own kv key — tp_sl_tracker state may
                # be missing (stale/absent) exactly in the situations
                # that need debouncing.
                try:
                    from src.state_db import get_state_db
                    _db = get_state_db()
                    _mig = _db.kv_get("gov:swap_ts:" + sym)
                    if _mig is None:
                        _mig = _db.kv_get("guardian_swap_ts:" + sym)
                        if _mig is not None:
                            _set_swap_ts(sym, _mig)
                            _db.kv_remove("guardian_swap_ts:" + sym)
                    _last_swap = float(((_mig or {}).get("ts")) or 0)
                except Exception:
                    _last_swap = 0.0
                if _last_swap and time.time() - _last_swap < 24 * 3600:
                    summary["skipped"] += 1
                    log.info("protection_guardian: %s SL→OCO swap "
                             "debounced (%.1fh since last attempt) — skip",
                             sym, (time.time() - _last_swap) / 3600)
                    continue
                # fully locked by plain SL → cancel-first OCO swap with
                # safety net (re-place old SL if OCO fails).
                # Precondition learned from the 22:03 first sweep: the
                # old SL stop must still be re-placeable — Binance
                # PERCENT_PRICE_BY_SIDE rejects stops >~15% from market
                # (ENA restore hit -1013). Never cancel a leg we cannot
                # put back.
                old_legs = [{
                    "qty": _order_qty(o),
                    "stop": float(o.get("stopPrice") or o.get("price") or 0),
                    "id": o.get("orderId"),
                } for o in sl_orders]
                max_old_stop = max((float(l["stop"] or 0)
                                    for l in old_legs), default=0.0)
                if max_old_stop and max_old_stop < price * 0.87:
                    # restoring the old stop after a failed OCO would hit
                    # PERCENT_PRICE (-1013) — never cancel a leg we
                    # cannot put back
                    summary["skipped"] += 1
                    emit_alert("PROTECTION_SL_OUT_OF_BAND", sym, {
                        "old_stop": max_old_stop, "price": price,
                        "note": "old SL too far below market to "
                                "re-place — swap aborted, SL kept",
                        "ts": time.time(),
                    })
                    log.info("protection_guardian: %s old stop %.8g "
                             "outside ±13%% band of px %.8g — skip swap",
                             sym, max_old_stop, price)
                    continue
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
                    try:
                        _set_swap_ts(sym, {"ts": time.time()})
                    except Exception:
                        pass
                    emit_alert("PROTECTION_HEALED", sym, {
                        "mode": "oco_swap", "qty": oco_qty_step,
                        "tp_px": tp_px, "sl_px": old_stop,
                        "ts": time.time(),
                    })
                    log.warning("protection_guardian: %s SL→OCO swapped "
                                "(TP restored) qty %.8g tp %.8g sl %.8g",
                                sym, oco_qty_step, tp_px, old_stop)
                    _list_id = (oco.get("orderListId")
                                if isinstance(oco, dict) else None)
                    _track(sym, entry, qty, [{
                        "order_id": _list_id,
                        "price": tp_px, "qty": oco_qty_step, "tier": 1,
                        "pct": None, "side": "OCO_TP",
                    }], {
                        "order_id": _list_id,
                        "price": old_stop, "qty": oco_qty_step,
                        "stop_price": old_stop,
                    })
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
                    # WO-0924-ix fix ②: keep tp_sl_tracker in sync with
                    # the restored plain-SL reality (was left stale →
                    # every sweep re-visited the symbol).
                    try:
                        if restored:
                            _track(sym, entry, qty, [], {
                                "order_id": "plain_sl_restored",
                                "price": old_stop, "qty": qty,
                            })
                        _set_swap_ts(sym, {"ts": time.time()})
                    except Exception:
                        log.warning("guardian: swap safety-net tracker "
                                    "sync failed for %s", sym, exc_info=True)
                    emit_alert("PROTECTION_HEAL_FAILED", sym, {
                        "mode": "oco_swap_failed_sl_restored",
                        "restored": restored, "legs": len(cancelled),
                        "urgent": restored < len(cancelled),
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

    # WO-017-4c: a held position whose tracker says SL=no for longer
    # than SL_MISSING_WARN_S must be visible in the logs/alerts (TRUMP
    # 9/22 sat SL=no for ~30min with zero output anywhere).
    try:
        from src.tp_sl_tracker import get_all_tracked
        held = {p.get("symbol") for p in positions
                if isinstance(p, dict) and p.get("symbol")}
        now = time.time()
        for _sym, _st in (get_all_tracked() or {}).items():
            if _sym not in held or _st.get("sl_order"):
                continue
            _upd = float(_st.get("updated_at")
                         or _st.get("created_at") or 0)
            _age = now - _upd
            if _age >= SL_MISSING_WARN_S:
                log.warning("protection_guardian: %s tracker SL=no for "
                            "%.0f min — stop protection missing", _sym,
                            _age / 60)
                emit_alert("SL_MISSING_STALE", _sym, {
                    "sl_missing_age_s": round(_age, 1), "urgent": True,
                    "note": "tracker shows no SL for a held position",
                    "ts": now,
                })
    except Exception:
        logger.debug("guardian SL-stale check failed", exc_info=True)

    if summary["healed"] or summary["failed"]:
        log.warning("protection_guardian: sweep %s", summary)
    return summary
