"""WO-1020 (10/8): execution invariant guard — the third protection layer.

Lineage (why this module exists):
  DASH 9/24  tiered SL ladder settled at -9.4%/-10.07% vs a 5-7% design
             band — the resting orders themselves were outside the band
             and nothing re-checked them (WO-1006 point-fixed the price
             loop, not the band).
  PENGU #91  partial exit 10/1, remainder sat 54h (max_hold 48h) and
             settled at -13.96% through the guardian-clamped OCO floor
             while the pre-WO-1011 orchestrator starved the exit step.
  WO-1006    guardian clamp re-hangs SL at px*0.93 (up to px*0.87) —
             legal to PLACE but outside the design band; waiting for it
             to fill means accepting a -7%..-13% loss by construction.

Common shape: a protective action executed (or rested) OUTSIDE the
design band with no independent re-computation. This module is that
re-computation — modelled after the ledger shadow diff's success
pattern (a cheap per-round audit that never trusts the layers it
watches):

  a) sl_band      the position must have in-band downside protection:
                  - price already at/below the breach line (fixed
                    -sl_pct or the trailed floor, whichever is higher)
                    with no recent exit attempt -> should-have-left
                    (PENGU), OR
                  - price inside the band but every resting stop leg
                    sits >3% below the fixed line -> waiting for it
                    means an out-of-band loss (DASH tail, guardian
                    clamps, emergency -13% re-lists).
  b) max_hold     held beyond hold_hours (+30min grace for the normal
                  exit step to consume it first) with no recent exit
                  attempt.
  c) qty_drift    portfolio row vs exchange balance disagree beyond
                  the reconciler tolerance — alert only (the reconciler
                  owns the books; selling on an uncertain qty is the
                  one action that could make it worse).

Dispositions:
  - a/b breach  -> ERROR alert (pending_notifications outbox + a
                   live_alerts event) and, when exit:mode is auto, an
                   immediate protective close via execute_exit(...,
                   bypass_cooldown=True) — the human-command lane, so
                   the guard outranks the exit step's cooldown cadence
                   while still reusing its whole cancel->sell->book
                   chain (dust pre-flight included: never hard-sell a
                   below-tradable remainder).
  - qty_drift   -> ERROR alert only.
  - mode=off/notify is a human decision and is honoured: alert-only
                   (same contract as run_exit_step's kill-switch).

Non-goals (hard constraints from the work order): never touches
protection_guardian (clamp stays a pure backstop), never touches the
strategy/evolver layer, never touches portfolio_reconciler (WO-1019
dff8487 intact), and adds no active-trading logic — the only orders it
can ever place are protective closes of positions that already exist.
"""
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# max_hold grace: the normal exit step gets the first shot every round;
# the guard fires only when the deadline is STILL blown afterwards.
MAX_HOLD_GRACE_H = 0.5

# a resting stop leg is "off band" when it sits more than this below the
# fixed line. 3% tolerates the executor's initial -7% stop against a 6%
# band (0.9300 vs 0.94*0.97=0.9118) while still catching -10% ladders,
# px*0.93 clamps from inside the band and -13% emergency re-lists.
OFF_BAND_TOL = 0.03

# qty-drift tolerance mirrors the reconciler main axis (2% of exchange
# qty, absolute floor for dust-scale rows).
QTY_DRIFT_FRACTION = 0.02
QTY_DRIFT_ABS = 0.001

# stop-type order types that constitute downside protection on the
# SELL side (OCO stop legs come through as STOP_LOSS_LIMIT).
_STOP_TYPES = ("STOP_LOSS_LIMIT", "STOP_LOSS", "STOP_LOSS_MARKET")

ALERT_EVENT = "INVARIANT_BREACH"


def _exit_recent(db, symbol: str, now: float, window_s: float) -> bool:
    """True when the exit chain executed (successfully sold) inside the
    cooldown window — the race guard that keeps the guard from fighting
    a working exit step. The stamp is only written on the success path
    (execute_exit step 5), so its presence means the books already moved."""
    try:
        last = float(db.kv_get(f"exit:{symbol}:last_exit_ts") or 0)
    except Exception:
        return False
    return 0.0 < last and (now - last) < window_s


def check_position(
    client: Any,
    db: Any,
    pos: Dict,
    *,
    now: float,
    open_orders: Optional[List[Dict]] = None,
    ex_qty: Optional[float] = None,
) -> Optional[Dict]:
    """Re-compute the three invariants for ONE open position.

    Returns a violation dict (kind/sym/action/detail fields) or None.
    Pure decision function: no side effects, every failure is
    fail-open (returns None — an unreadable price must not close a
    position on bad data)."""
    symbol = pos.get("symbol") or ""
    if not symbol:
        return None
    try:
        price = float(client.get_ticker_price(symbol) or 0)
    except Exception:
        logger.warning("invariant_guard: price read failed for %s — "
                       "skipped this round", symbol, exc_info=True)
        return None
    if price <= 0:
        return None

    from src.exit_check import EXIT_COOLDOWN_S, _held_hours, _param

    try:
        sl_pct = _param(db, "sl_pct", symbol)
        hold_hours = _param(db, "hold_hours", symbol)
    except Exception:
        logger.warning("invariant_guard: param read failed for %s",
                       symbol, exc_info=True)
        return None

    exit_recent = _exit_recent(db, symbol, now, EXIT_COOLDOWN_S)
    held_hours = _held_hours(db, pos, now)
    entry = float(pos.get("entry_price") or 0)
    db_qty = float(pos.get("quantity") or 0)

    # (b) max_hold — the exit step consumes this every round; firing
    # here with no recent exit attempt means that chain is stuck.
    if (hold_hours > 0 and not exit_recent
            and held_hours > hold_hours + MAX_HOLD_GRACE_H):
        return {
            "kind": "max_hold", "sym": symbol, "action": "exit",
            "price": price,
            "pnl_pct": (price - entry) / entry * 100.0 if entry > 0 else 0.0,
            "held_hours": held_hours,
            "detail": f"held {held_hours:.1f}h > {hold_hours:.0f}h "
                      f"(+{MAX_HOLD_GRACE_H}h grace) with no exit attempt "
                      f"in the last {EXIT_COOLDOWN_S:.0f}s",
        }

    # (a) sl_band
    floor_fixed = entry * (1 - sl_pct / 100.0) if entry > 0 else 0.0
    floor_trail = float(pos.get("stop_loss") or 0)
    breach_line = max(floor_fixed, floor_trail)
    if breach_line > 0:
        if price <= breach_line and not exit_recent:
            # should-have-left: the breach line (fixed band or trailed
            # floor, whichever is tighter) is under our feet and the
            # exit chain has not executed inside the cooldown window.
            return {
                "kind": "sl_band_breach", "sym": symbol,
                "action": "exit", "price": price,
                "pnl_pct": (price - entry) / entry * 100.0
                if entry > 0 else 0.0,
                "held_hours": held_hours,
                "detail": f"price {price:.6g} <= breach line "
                          f"{breach_line:.6g} (fixed "
                          f"{floor_fixed:.6g} @-{sl_pct:.0f}%, trail "
                          f"{floor_trail:.6g}) with no exit attempt in "
                          f"the last {EXIT_COOLDOWN_S:.0f}s",
            }
        if price > floor_fixed > 0 and open_orders:
            # in-band price: every resting SELL leg that would fill on
            # the LOSS side must itself sit in (or near) the band. This
            # covers BOTH DASH 9/24 shapes — the STOP_LOSS_LIMIT ladder
            # AND the loss-side LIMIT_MAKER ladder (#117/#119 settled
            # -9.4/-10.07%) — plus guardian clamps and emergency
            # re-lists. A leg >3% under the fixed line means waiting
            # for it to fill IS the out-of-band loss. Profit-side TP
            # legs (price > entry > floor) can never match.
            legs = []
            for o in open_orders:
                if (o.get("symbol") != symbol
                        or str(o.get("side", "")).upper() != "SELL"):
                    continue
                otype = str(o.get("type", "")).upper()
                is_stop = otype in _STOP_TYPES
                is_limit = otype in ("LIMIT_MAKER", "LIMIT")
                if not (is_stop or is_limit):
                    continue
                try:
                    fill_px = float(
                        o.get("stopPrice") if is_stop
                        else (o.get("price") or 0))
                except (TypeError, ValueError):
                    continue
                if fill_px > 0:
                    legs.append(fill_px)
            if legs:
                worst = min(legs)
                if worst < floor_fixed * (1 - OFF_BAND_TOL):
                    return {
                        "kind": "sl_order_off_band", "sym": symbol,
                        "action": "exit", "price": price,
                        "pnl_pct": (price - entry) / entry * 100.0
                        if entry > 0 else 0.0,
                        "held_hours": held_hours,
                        "detail": f"resting sell leg {worst:.6g} sits >"
                                  f"{OFF_BAND_TOL:.0%} below the fixed "
                                  f"line {floor_fixed:.6g} while price "
                                  f"{price:.6g} is still in band — "
                                  f"waiting for that leg = out-of-band "
                                  f"loss by construction",
                    }

    # (c) qty_drift — alert only: the reconciler owns the books and
    # runs before this guard on full rounds; selling on a qty the
    # exchange does not confirm is the one action that could worsen it.
    if ex_qty is not None and db_qty > 0:
        tol = max(ex_qty * QTY_DRIFT_FRACTION, QTY_DRIFT_ABS)
        if abs(db_qty - ex_qty) > tol:
            return {
                "kind": "qty_drift", "sym": symbol, "action": "alert_only",
                "price": price,
                "pnl_pct": (price - entry) / entry * 100.0
                if entry > 0 else 0.0,
                "held_hours": held_hours,
                "detail": f"portfolio qty {db_qty:.8g} vs exchange "
                          f"{ex_qty:.8g} (drift "
                          f"{abs(db_qty - ex_qty):.8g} > tol {tol:.8g})",
            }

    return None


def _alert(db, v: Dict, now: float) -> None:
    """ERROR-level alert through both channels. Stable notif_id per
    (symbol, kind, hour bucket) so a persisting breach re-alerts at most
    once an hour instead of once a round."""
    bucket = time.strftime("%Y%m%d%H", time.localtime(now))
    notif_id = f"invariant:{v['sym']}:{v['kind']}:{bucket}"
    try:
        db.notification_outbox_add(
            notif_id, "invariant_breach",
            f"🚨 Invariant breach — {v['sym']} ({v['kind']})",
            f"{v['detail']} — price {v['price']:.6g}, pnl "
            f"{v.get('pnl_pct', 0.0):+.2f}%, held "
            f"{v.get('held_hours', 0.0):.1f}h.")
    except Exception:
        logger.warning("invariant outbox write failed (non-fatal)",
                       exc_info=True)
    try:
        from src import live_alerts
        live_alerts.emit(
            ALERT_EVENT, v["sym"],
            {k: v.get(k) for k in
             ("kind", "detail", "price", "pnl_pct", "held_hours")},
        )
    except Exception:
        logger.warning("invariant live_alerts emit failed (non-fatal)",
                       exc_info=True)


def run_guard(client: Any, db: Any, *, now: Optional[float] = None,
              mode_override: Optional[str] = None) -> Dict:
    """Audit every open position against the three invariants; enforce
    a/b breaches with a protective close when exit:mode is auto.

    Returns a summary dict {checked, breaches: [...], closed: [...],
    alerts: [...], mode} — the orchestrator step logs it. Fail-open at
    every level: an exception inside one position never blocks the
    others, and a total failure returns an empty summary (the scan
    round continues; the next round retries)."""
    now = now or time.time()
    summary: Dict[str, Any] = {
        "checked": 0, "breaches": [], "closed": [], "alerts": [], "mode": None,
    }
    try:
        positions = db.portfolio_get_all()
    except Exception:
        logger.warning("invariant_guard: portfolio read failed",
                       exc_info=True)
        return summary
    if not positions:
        return summary

    from src.exit_check import _mode

    mode = mode_override if mode_override is not None else _mode(db)
    summary["mode"] = mode

    # one shared exchange view for the whole round (price + open orders
    # + balances); failure degrades per-position checks to price-only.
    open_orders: List[Dict] = []
    ex_qty_by_base: Dict[str, float] = {}
    try:
        open_orders = client.get_open_orders() or []
    except Exception:
        logger.warning("invariant_guard: get_open_orders failed — "
                       "order-side checks degraded this round",
                       exc_info=True)
    try:
        account = client.get_account()
        for b in account.get("balances", []):
            try:
                ex_qty_by_base[b["asset"]] = (
                    float(b.get("free", 0)) + float(b.get("locked", 0)))
            except (TypeError, ValueError):
                continue
    except Exception:
        logger.warning("invariant_guard: get_account failed — qty-drift "
                       "checks degraded this round", exc_info=True)

    for sym, pos in sorted(positions.items()):
        summary["checked"] += 1
        try:
            base = sym[:-4] if sym.endswith("USDT") else sym
            v = check_position(
                client, db, pos, now=now, open_orders=open_orders,
                ex_qty=ex_qty_by_base.get(base),
            )
        except Exception:
            logger.warning("invariant_guard: check failed for %s",
                           sym, exc_info=True)
            continue
        if not v:
            continue
        summary["breaches"].append(v)
        _alert(db, v, now)
        summary["alerts"].append(v["sym"])
        if v["action"] != "exit" or mode != "auto":
            # human kill-switch / notify mode: alert-only, same contract
            # as run_exit_step's kill-switch branch.
            logger.warning(
                "invariant_guard: %s %s breach HELD BACK (exit:mode=%s) "
                "— %s", v["sym"], v["kind"], mode, v["detail"])
            continue
        try:
            from src.exit_check import execute_exit
            decision = {
                "symbol": v["sym"], "kind": f"invariant_{v['kind']}",
                "auto": True, "sell_pct": 100, "price": v["price"],
                "qty": float(pos.get("quantity") or 0),
                "pnl_pct": v.get("pnl_pct", 0.0),
                "held_hours": v.get("held_hours", 0.0),
                "reason": f"invariant guard: {v['detail']}",
                "ts": now,
            }
            out = execute_exit(client, db, decision, now=now,
                               bypass_cooldown=True)
            summary["closed"].append(out)
            if out.get("status") != "ok":
                logger.error(
                    "invariant_guard: protective close FAILED for %s "
                    "(%s) — position remains, alert stands: %s",
                    v["sym"], v["kind"], out)
        except Exception:
            logger.error(
                "invariant_guard: protective close raised for %s (%s) "
                "— position remains, alert stands",
                v["sym"], v["kind"], exc_info=True)
    return summary
