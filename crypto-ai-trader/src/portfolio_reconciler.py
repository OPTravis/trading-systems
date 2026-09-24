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
#: fuzzy-dedup tolerances — a reconcile SELL fill whose qty/price sit
#: this close to an already-booked NULL-id row (legacy active-path
#: bookings that carry no orderId) is treated as booked, not double-
#: counted. 0.5% covers qty-precision truncation and avg-vs-fill skew.
FUZZY_QTY_REL_TOL = 0.005
FUZZY_PRICE_REL_TOL = 0.005
#: only recent rows are matched — the active path books within
#: seconds of the fill, so a 15-min window is ample
FUZZY_WINDOW_S = 15 * 60
# P0-1.5: fills older than this are never candidates for gap booking —
# they belong to positions closed long before the current ledger window
# (ZAMA 9/21 incident: an April orderId 98217977 leg of 443 got booked
# against a 64-unit gap because my_trades returns full history).
FILL_LOOKBACK_S = 24 * 3600
# a single aggregated order leg may exceed the gap by at most this factor
# before it is rejected as a stale/foreign leg
LEG_GAP_TOLERANCE = 1.05
# P0-2: before booking, the leg's avg fill price must sit within this
# relative distance of the live ticker price — an April leg @0.03178 vs a
# live 0.088 market fails by 177% and is rejected. Fail-open when the
# ticker cannot be fetched (validation must never block booking).
FILL_PRICE_SANITY_REL = 0.05
# WO-017-2: fills older than this skip the live-price sanity check (ms)
STALE_SANITY_EXEMPT_MS = 30 * 60 * 1000
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
    """WO-017-2: match the orderId as an exact key OR as a ``<prefix>_<oid>``
    suffix. Manual/active-path rows carry prefixes (``wo0921015_<oid>``,
    ``oco_tp_<oid>``, ``oco_fill_<oid>``); a plain equality check misses
    them and reconcile re-books the same physical fill (FET 9/22: id79
    wo-prefixed + id86 re-booked under the bare orderId)."""
    oid = str(order_id)
    row = db._get_conn().execute(
        "SELECT 1 FROM trades WHERE client_order_id = ? "
        "OR client_order_id LIKE '%\\_' || ? ESCAPE '\\' LIMIT 1",
        (oid, oid),
    ).fetchone()
    return row is not None


from src.live_alerts import emit as emit_alert


def _fuzzy_booked(db, symbol: str, qty: float, price: float) -> bool:
    """True if a recent NULL-id SELL row already covers this fill.

    Bridges the gap left by active-path bookings (switch close, manual
    close) that predate the client_order_id plumbing: those rows carry
    no orderId, so _order_booked cannot see them. Without this check a
    reconcile round re-books the same physical sell under its exchange
    orderId — the id=36/id=38 double-count bug.
    """
    cutoff = time.time() - FUZZY_WINDOW_S
    rows = db._get_conn().execute(
        "SELECT qty, price FROM trades "
        "WHERE symbol=? AND side='SELL' AND client_order_id IS NULL "
        "AND timestamp >= ? ORDER BY timestamp DESC LIMIT 50",
        (symbol, cutoff),
    ).fetchall()
    for r in rows:
        q, p = float(r["qty"]), float(r["price"])
        if q <= 0 or p <= 0:
            continue
        if (abs(q - qty) <= FUZZY_QTY_REL_TOL * max(q, qty)
                and abs(p - price) <= FUZZY_PRICE_REL_TOL * max(p, price)):
            return True
    return False


def _last_buy_ts_ms(db, symbol: str) -> int:
    """P0-2: latest BUY row for the symbol anchors the current position
    lifecycle. Any SELL fill older than it belongs to a previous, already
    closed position and must never book against the current gap (INJ 9/20:
    fills from 5/13, 9/15, 9/16, 9/19 all predate the 07:31 BUY and were
    still booked). Returns 0 when no BUY exists."""
    row = db._get_conn().execute(
        "SELECT MAX(timestamp) FROM trades WHERE symbol = ? AND side = 'BUY'",
        (symbol,),
    ).fetchone()
    ts = row[0] if row else None
    return int(float(ts) * 1000) if ts else 0


def _is_own_fill(order_id, legs) -> bool:
    """WO-017-vi: attribution first, deviation second.

    A fill on an order THIS system placed (TP/SL legs, OCO legs, guardian
    heals, switch-flow exits) is bookable regardless of how far the live
    price has traveled since the exit — a resting limit that filled and
    then the market moved on is drift, not a foreign leg (BCH 9/22 20:41:
    own OCO TP filled 283.1, live 299.2, sanity rejected a real fill and
    the ledger stayed short until a manual id95 backfill).

    Two independent attribution signals:
      1. order_id recorded in the live TP/SL tracker (placed by us);
      2. fill clientOrderId carries the wrappers' universal ``cat_``
         prefix — survives tracker cleanup after the position closed
         (the BCH tracker was already removed when reconcile ran).
    Manual/exchange-UI orders carry no cat_ marker and no tracker row,
    so they still face the deviation check.
    """
    oid = str(order_id)
    try:
        from src.tp_sl_tracker import get_all_tracked
        for state in (get_all_tracked() or {}).values():
            if not isinstance(state, dict):
                continue
            for tp in state.get("tp_orders") or []:
                if str((tp or {}).get("order_id")) == oid:
                    return True
            sl = state.get("sl_order") or {}
            if str(sl.get("order_id") or "") == oid:
                return True
    except Exception:
        pass  # tracker unavailable -> fall through to prefix check
    for f in legs:
        if str(f.get("clientOrderId") or "").startswith("cat_"):
            return True
    return False


def _book_missing_sells(db, symbol: str, fills: List[Dict], gap_qty: float,
                        client=None) -> List[Dict]:
    """Aggregate SELL fills by orderId and book the unrecorded ones.

    Booking stops once the accumulated qty reaches the drift gap — the gap
    guard makes legacy NULL-id rows unable to cause over-booking.
    Returns the booked entries.
    """
    booked: List[Dict] = []
    cutoff = int((time.time() - FILL_LOOKBACK_S) * 1000)  # fills are ms
    # P0-2: candidates must postdate BOTH the 24h window and the latest
    # BUY row — the stricter of the two anchors the current position
    # lifecycle and excludes fills from any earlier closed position.
    lifecycle = max(cutoff, _last_buy_ts_ms(db, symbol))
    sells = [
        f for f in fills
        if not f.get("isBuyer")
        and int(f.get("orderId") or 0) > 0
        and int(f.get("time") or 0) >= lifecycle  # P0-1.5/P0-2: stale legs excluded
    ]
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
        if qty > remaining_gap * LEG_GAP_TOLERANCE + DRIFT_QTY_ABS:
            # P0-1.5: a single leg larger than the gap (beyond tolerance) is a
            # stale/foreign leg — booking it would over-sell the ledger
            # (ZAMA incident: 443 booked against a 64-unit gap). Skip it and
            # keep scanning younger orders; a real 64-unit leg follows.
            logger.warning(
                "reconcile: SELL %s orderId=%s qty %.8g exceeds gap %.8g "
                "x%.2f — stale/foreign leg, skipped",
                symbol, oid, qty, remaining_gap, LEG_GAP_TOLERANCE,
            )
            continue
        # WO-017-2: the live-price sanity check only guards FRESH fills.
        # A fill older than the exemption window has legitimately drifted
        # from the live price (the asset kept trading after the exit) —
        # rejecting it forever is how PROVE 9/22 stayed unbooked for 10h
        # (fill 0.2566 vs live 0.2261). Lifecycle anchor + gap cap already
        # guard against foreign legs for stale fills.
        fill_age_ms = int(time.time() * 1000) - min(int(f["time"]) for f in legs)
        sanity_applies = fill_age_ms <= STALE_SANITY_EXEMPT_MS
        # WO-017-vi: own fills bypass the deviation check entirely
        own_fill = _is_own_fill(oid, legs)
        if own_fill:
            logger.info(
                "reconcile: SELL %s orderId=%s attributed to our own order "
                "(tracker/cat_ prefix) — sanity check waived",
                symbol, oid,
            )
        if not own_fill and client is not None and sanity_applies:
            try:
                live = client.get_ticker_price(symbol)
            except Exception:
                live = None   # fail-open: never block booking on validation
            if live and live > 0 and abs(avg_px - live) / live > FILL_PRICE_SANITY_REL:
                logger.warning(
                    "reconcile: SELL %s orderId=%s avg %.8g deviates >%.0f%% "
                    "from live %.8g — stale/foreign leg, skipped",
                    symbol, oid, avg_px, FILL_PRICE_SANITY_REL * 100, live,
                )
                continue
        if _fuzzy_booked(db, symbol, qty, avg_px):
            logger.info(
                "reconcile: SELL %s orderId=%s matches a recent NULL-id row "
                "(fuzzy dedup, qty/price within %.1f%%) — skipping to avoid "
                "double-count",
                symbol, oid, FUZZY_QTY_REL_TOL * 100,
            )
            continue
        pnl = qty * (avg_px - entry_avg) if entry_avg else 0.0
        inserted = db.trade_add(
            symbol, "SELL", round(qty, 8), round(avg_px, 8),
            round(pnl, 6), client_order_id=str(oid),
        )
        if inserted:
            if pnl < 0:  # WO-0924-x: loss exit → 24h re-entry cooldown
                try:
                    from src.entry_governor import note_loss_exit
                    note_loss_exit(symbol, pnl)
                except Exception:
                    pass
            # WO-0924-z P1-3: close the outcome lifecycle — reconciled
            # exits are the LAST exit path with no record_outcome hook
            # (the 27-row stale pile-up root cause).
            try:
                from src.trade_outcome_recorder import TradeOutcomeRecorder
                TradeOutcomeRecorder().record_outcome(
                    symbol, exit_price=avg_px,
                    exit_reason="reconciled" if pnl >= 0 else "sl")
            except Exception:
                pass
            booked.append(
                {"symbol": symbol, "qty": round(qty, 8), "price": round(avg_px, 8),
                 "pnl": round(pnl, 6), "order_id": str(oid), "source": "reconcile/oco_fill"}
            )
            logger.info(
                "🔁 RECONCILE OCO FILL: SELL %s @ %.6g (pnl %+.4f) "
                "[oco_fill orderId=%s qty=%.8g]",
                symbol, avg_px, pnl, oid, qty,
            )
            emit_alert(
                "OCO_FILL", symbol,
                {"side": "SELL", "qty": round(qty, 8),
                 "price": round(avg_px, 8), "pnl": round(pnl, 6),
                 "order_id": str(oid), "source": "reconcile"})
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
        booked_total.extend(_book_missing_sells(db, sym, fills, suspects[sym], client=client))

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

    # --- Path C: 24h myTrades lookback (WO-017-2) ---
    # A fully-closed symbol whose portfolio row was dropped by the sync
    # rebuild falls out of both the main axis (positions) and Path B
    # (prev snapshot, refreshed every round) after a single missed round —
    # permanently (PROVE 9/22: OCO filled 03:09, ledger still short at
    # 14:59). This axis is snapshot-independent: any symbol with a ledger
    # row inside 24h gets a myTrades lookback; unbooked SELL fills go
    # through the full _book_missing_sells guard chain, capped at the
    # ledger net gap (orderId idempotency + gap cap prevent double-booking).
    cutoff_s = time.time() - FILL_LOOKBACK_S
    try:
        rows = db._get_conn().execute(
            "SELECT DISTINCT symbol FROM trades WHERE timestamp >= ?",
            (cutoff_s,),
        ).fetchall()
        recent_syms = {r["symbol"] for r in rows if r["symbol"]}
    except Exception:
        recent_syms = set()
        log.warning("reconcile: Path C symbol query failed", exc_info=True)
    for sym in sorted(recent_syms - set(suspects)):
        net = _db_net_qty(db, sym)
        if net <= DRIFT_QTY_ABS:
            continue  # ledger already balanced for this symbol
        gap = net - ex.get(_base_of(sym), 0.0)
        if gap <= max(net * (1.0 - DRIFT_QTY_FRACTION), DRIFT_QTY_ABS):
            continue  # no meaningful SELL-side gap
        try:
            fills = client.get_my_trades(sym, limit=100)
        except Exception:
            log.warning("reconcile: Path C get_my_trades(%s) failed", sym,
                        exc_info=True)
            continue
        if fills:
            log.info(
                "reconcile: Path C lookback %s (ledger net %.8g, gap %.8g)",
                sym, net, gap,
            )
            booked_total.extend(
                _book_missing_sells(db, sym, fills, gap, client=client))

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
