"""Portfolio drift reconciler — OCO passive-fill booking (2026-09-17).

Bridge blind-spot fix (Travis dispatch): TP/SL legs triggered exchange-side
by OCO never pass through the active trade path, so they never reach the
trades table, never print a SELL log line, and reside_scan's latest.json
trades extraction stays empty — the bridge never reports them. Real cases:
9/16 18:48 ARB TP 67.8 @ 0.1611 (+$0.62, silent for 10+ scan rounds) and
the 9/15 night SL closes.

Detection — LEDGER-VS-EXCHANGE net-quantity reconciliation (main axis):
  booked_net(symbol) = trades BUY qty − trades SELL qty, and the live
  exchange balance is ground truth. net − balance > tolerance ⇒ unbooked
  SELL fills exist (TP1/TP2 partial ladders, full SL closes, anything the
  active path missed). The portfolio table is deliberately NOT a trigger:
  sync_from_binance runs at scan Step 0 and rebuilds portfolio rows from
  the exchange balance, so by the time this step runs a partial close has
  already been silently absorbed into the portfolio row — the ledger is
  the only signal that survives the sync (9/17 night case: UNI TP1/TP2
  2.01+2.01 and NEAR TP2 6.1 ladders).

  Path B (kept): a kv snapshot of the previous round's portfolio catches
  fully-closed symbols that the clear-and-rebuild sync dropped from the
  table entirely — those are no longer in `positions`, so the main axis
  would never visit them (the 9/16 ARB case).

On suspicion: get_my_trades(symbol) → aggregate SELL fills by orderId →
book the ones missing from DB via trade_add (client_order_id = orderId,
UNIQUE index makes re-runs no-ops). A net-quantity gap guard caps what may
be booked (DB buys − DB sells − exchange balance), so legacy NULL-id rows
can never trigger over-booking. pnl uses the DB BUY weighted-average entry.

Booked fills print a bridge-visible log line — "🔁 RECONCILE OCO FILL:
SELL <SYM>USDT @ <px> ..." — which matches reside_scan's trades filter
("SELL " + "@" + "USDT"), so the bridge picks it up on the next round.

Steady-state cost per scan: one get_account + one kv read/write. No drift
→ zero extra API calls, zero writes.
"""
import logging
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

KV_PREV_POSITIONS = "reconcile_prev_positions"
#: exchange balance below this fraction of the DB qty ⇒ drift
DRIFT_QTY_FRACTION = 0.98
#: absolute qty slack (dust-sized remainders are not drift)
DRIFT_QTY_ABS = 0.001
#: symbols from the previous snapshot stay suspects for this many days
PREV_SNAPSHOT_MAX_AGE_S = 7 * 24 * 3600


def _base_of(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def _exchange_holdings(account: Dict) -> Dict[str, float]:
    """asset → total (free+locked), non-zero only."""
    out: Dict[str, float] = {}
    for b in account.get("balances", []):
        total = float(b.get("free", 0) or 0) + float(b.get("locked", 0) or 0)
        if total > 0:
            out[b.get("asset", "")] = total
    return out


def _db_buy_avg(db, symbol: str) -> Optional[float]:
    """Weighted-average entry of all booked BUYs for *symbol* (or None)."""
    rows = db._get_conn().execute(
        "SELECT qty, price FROM trades WHERE symbol = ? AND UPPER(side) = 'BUY'",
        (symbol,),
    ).fetchall()
    q = sum(float(r["qty"] or 0) for r in rows)
    if q <= 0:
        return None
    return sum(float(r["qty"] or 0) * float(r["price"] or 0) for r in rows) / q


def _db_net_qty(db, symbol: str) -> float:
    """booked BUY qty − booked SELL qty"""
    row = db._get_conn().execute(
        "SELECT COALESCE(SUM(CASE WHEN UPPER(side)='BUY' THEN qty ELSE -qty END), 0) "
        "AS net FROM trades WHERE symbol = ?",
        (symbol,),
    ).fetchone()
    return float(row["net"] or 0) if row else 0.0


def _order_booked(db, order_id) -> bool:
    row = db._get_conn().execute(
        "SELECT 1 FROM trades WHERE client_order_id = ? LIMIT 1", (str(order_id),)
    ).fetchone()
    return row is not None


def _book_missing_sells(db, symbol: str, fills: List[Dict], gap_qty: float) -> List[Dict]:
    """Aggregate SELL fills by orderId and book the unrecorded ones.

    Booking stops once the accumulated qty reaches the drift gap — the gap
    guard makes legacy NULL-id rows unable to cause over-booking.
    Returns the booked entries.
    """
    booked: List[Dict] = []
    sells = [f for f in fills if not f.get("isBuyer") and int(f.get("orderId") or 0) > 0]
    if not sells:
        return booked
    by_order: Dict[int, List[Dict]] = {}
    for f in sells:
        by_order.setdefault(int(f["orderId"]), []).append(f)

    entry_avg = _db_buy_avg(db, symbol)
    base = _base_of(symbol)
    remaining_gap = gap_qty

    for oid in sorted(by_order, key=lambda o: min(f["time"] for f in by_order[o])):
        if remaining_gap <= DRIFT_QTY_ABS:
            break
        if _order_booked(db, oid):
            continue
        legs = by_order[oid]
        leg_qty_raw = sum(float(f["qty"]) for f in legs)
        if leg_qty_raw <= 0:
            continue
        avg_px = sum(float(f["qty"]) * float(f["price"]) for f in legs) / leg_qty_raw
        # base-asset commission is paid out of the received qty
        comm = sum(
            float(f.get("commission") or 0)
            for f in legs
            if f.get("commissionAsset") == base
        )
        qty = max(leg_qty_raw - comm, 0.0)
        if qty <= DRIFT_QTY_ABS:
            continue
        pnl = qty * (avg_px - entry_avg) if entry_avg else 0.0
        inserted = db.trade_add(
            symbol, "SELL", round(qty, 8), round(avg_px, 8),
            round(pnl, 6), client_order_id=str(oid),
        )
        if inserted:
            booked.append(
                {"symbol": symbol, "qty": round(qty, 8), "price": round(avg_px, 8),
                 "pnl": round(pnl, 6), "order_id": str(oid), "source": "reconcile/oco_fill"}
            )
            logger.info(
                "🔁 RECONCILE OCO FILL: SELL %s @ %.6g (pnl %+.4f) "
                "[oco_fill orderId=%s qty=%.8g]",
                symbol, avg_px, pnl, oid, qty,
            )
        remaining_gap -= qty
    return booked


def reconcile_portfolio_drift(client, db, log: Optional[logging.Logger] = None) -> List[Dict]:
    """Detect DB↔exchange drift, book missing OCO SELL fills.

    Fail-open: any exchange/API error returns [] without touching state.
    Returns the list of booked fills (empty on a clean round).
    """
    log = log or logger
    try:
        account = client.get_account()
    except Exception:
        log.warning("reconcile: get_account failed — skipping this round", exc_info=True)
        return []
    if not account or not account.get("balances"):
        log.warning("reconcile: empty account payload — skipping this round")
        return []

    ex = _exchange_holdings(account)
    positions = db.portfolio_get_all()

    # --- main axis: ledger net vs exchange balance, per held symbol ---
    # gap = booked BUY − booked SELL − live balance. Positive gap ⇒ SELL
    # fills the ledger never booked (partial TP ladders / SL closes that
    # sync_from_binance silently absorbed into the portfolio row).
    suspects: Dict[str, float] = {}
    for sym in positions:
        net = _db_net_qty(db, sym)
        ex_qty = ex.get(_base_of(sym), 0.0)
        gap = net - ex_qty
        tol = max(net * (1.0 - DRIFT_QTY_FRACTION), DRIFT_QTY_ABS)
        if gap > tol:
            suspects[sym] = gap
        elif gap < -tol:
            # exchange holds more than the ledger explains: unbooked BUYs
            # — out of scope for the SELL booker, log for diagnosis only
            log.info(
                "reconcile: %s exchange exceeds ledger by %.8g (BUY-side gap, not actionable)",
                sym, -gap,
            )

    # --- Path B: fully-closed symbols dropped by the sync rebuild ---
    prev = db.kv_get(KV_PREV_POSITIONS) or {}
    if isinstance(prev, dict):
        prev_at = float(prev.get("_ts") or 0)
        fresh = (time.time() - prev_at) <= PREV_SNAPSHOT_MAX_AGE_S
        for sym in prev:
            if sym.startswith("_") or sym in positions:
                continue
            if fresh and ex.get(_base_of(sym), 0.0) <= DRIFT_QTY_ABS:
                net = _db_net_qty(db, sym)
                if net > DRIFT_QTY_ABS:
                    suspects.setdefault(sym, net)

    booked_total: List[Dict] = []
    for sym in sorted(suspects):
        try:
            fills = client.get_my_trades(sym, limit=100)
        except Exception:
            log.warning("reconcile: get_my_trades(%s) failed", sym, exc_info=True)
            continue
        if not fills:
            continue
        # the gap doubles as the booking cap — a balanced ledger can never
        # be over-booked, and legacy NULL-id rows cannot inflate it
        booked_total.extend(_book_missing_sells(db, sym, fills, suspects[sym]))

        # keep the portfolio row aligned with the exchange (belt and
        # braces; sync normally handles this at Step 0)
        pos = positions.get(sym)
        if pos:
            db_qty = float(pos.get("quantity") or 0)
            leftover = ex.get(_base_of(sym), 0.0)
            drift = abs(db_qty - leftover)
            if drift > max(leftover * (1.0 - DRIFT_QTY_FRACTION), DRIFT_QTY_ABS):
                if leftover <= DRIFT_QTY_ABS:
                    db.portfolio_remove(sym)
                    log.info("reconcile: removed stale position %s (exchange flat)", sym)
                else:
                    upd = dict(pos)
                    upd["quantity"] = leftover
                    db.portfolio_set(sym, upd)
                    log.info("reconcile: trimmed %s to exchange qty %.8g", sym, leftover)

    # snapshot for the next round's Path B (post-booking state, so cleaned
    # positions don't re-enter the suspect set next round)
    try:
        db.kv_set(
            KV_PREV_POSITIONS,
            dict(
                {s: float(p.get("quantity") or 0)
                 for s, p in db.portfolio_get_all().items()},
                _ts=time.time(),
            ),
        )
    except Exception:
        log.warning("reconcile: snapshot update failed", exc_info=True)

    return booked_total
