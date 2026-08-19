"""Reconcile exchange SELL fills against the local ledger.

Structural gap (2026-08-20): when an SL order fires on the exchange, nothing
in our stack notices. No SELL row lands in `trades`, the trade_outcome stays
'open', and the portfolio only drops the symbol at the next qty sync. EDEN
22:42 SL fill (92.6 @ 0.05518, -$0.09) and the 20:30 TP-breach close both had
to be booked by hand.

This module closes that gap: for every open outcome whose asset is no longer
held on the exchange, it finds the exit fills via get_my_trades() and books
whatever is missing (SELL trade row + outcome close). Idempotent — safe to
run every trailing-check cycle (5 min).

Exit-reason heuristic: exit price below entry ⇒ 'sl'; otherwise 'tp_fill'.
The exchange does not label intent, so partial-truth is acceptable here —
the ledger cares about price/qty/time, the reason is for stats only.
"""

import logging
import time

logger = logging.getLogger(__name__)

# An asset counts as "still held" above this notional ($)
_HELD_NOTIONAL_FLOOR = 1.0


def _asset_of(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def _db_retry(label: str, fn, errors, attempts=2):
    """Run a DB write with one retry; escalate to errors[] on double failure
    (same pattern as scripts/ensure_tp_sl.py)."""
    last_err = None
    for i in range(attempts):
        try:
            fn()
            return True
        except Exception as e:
            last_err = e
            logger.warning(f"{label} failed (attempt {i + 1}/{attempts}): {e}")
            time.sleep(1.0)
    errors.append(
        f"{label} failed {attempts}x ({last_err}) — exchange fill exists but "
        f"DB bookkeeping gap needs manual patch"
    )
    return False


def reconcile_fills(client=None, db=None, dry_run: bool = False) -> dict:
    """Detect exchange exits missing from the ledger and book them.

    Returns {"patched": [...], "closed_only": [...], "skipped": [...],
             "anomalies": [...], "errors": [...]} for the cron output JSON.
    """
    from src.state_db import get_state_db

    db = db or get_state_db()
    if client is None:
        from src.binance_client import BinanceClient

        client = BinanceClient(testnet=False)

    out = {"patched": [], "closed_only": [], "skipped": [], "anomalies": [], "errors": []}

    # 1) open outcomes
    try:
        conn = db._get_conn()
        rows = conn.execute(
            "SELECT id, symbol, entry_time, entry_price, qty, status "
            "FROM trade_outcomes WHERE status = 'open' ORDER BY entry_time ASC"
        ).fetchall()
    except Exception as e:
        out["errors"].append(f"cannot read open outcomes: {e}")
        return out

    if not rows:
        return out

    # 2) exchange holdings snapshot
    try:
        acct = client.get_account()
        held = {}
        for b in acct.get("balances", []):
            total = float(b.get("free", 0)) + float(b.get("locked", 0))
            if total > 0:
                held[b["asset"]] = total
    except Exception as e:
        out["errors"].append(f"cannot read exchange account: {e}")
        return out

    for row in rows:
        symbol = row["symbol"]
        asset = _asset_of(symbol)
        oid = row["id"]
        entry_time = float(row["entry_time"] or 0)
        entry_price = float(row["entry_price"] or 0)
        qty = float(row["qty"] or 0)

        held_qty = held.get(asset, 0.0)
        if held_qty > 0:
            # cheap dust check: any real balance above floor keeps outcome open
            try:
                stats = client.get_24hr_stats(symbol)
                px = float(stats.get("last_price", 0)) if stats else 0
            except Exception:
                px = 0
            if held_qty * px > _HELD_NOTIONAL_FLOOR or px <= 0:
                out["skipped"].append(f"{symbol}: still holding {held_qty}")
                continue

        # 3) position is gone (or dust): find exit fills after entry
        try:
            trades = client.get_my_trades(symbol=symbol, limit=50)
        except Exception as e:
            out["anomalies"].append(f"{symbol}: cannot fetch my_trades ({e})")
            continue

        sells = [
            t
            for t in (trades or [])
            if not t.get("isBuyer", True)
            and float(t.get("time", 0)) / 1000 >= entry_time - 60
        ]
        if not sells:
            out["anomalies"].append(
                f"{symbol}#{oid}: position gone but no SELL fill found after entry — "
                f"check for manual transfer/withdrawal"
            )
            continue

        sells.sort(key=lambda t: float(t.get("time", 0)))
        exit_qty = sum(float(t.get("qty", 0)) for t in sells)
        exit_price = (
            sum(float(t["qty"]) * float(t["price"]) for t in sells) / exit_qty
            if exit_qty > 0
            else 0.0
        )
        last_fill_ts = float(sells[-1].get("time", 0)) / 1000
        if exit_price <= 0 or exit_qty <= 0:
            out["anomalies"].append(f"{symbol}#{oid}: unparseable sell fills")
            continue

        # 4) idempotency: has the ledger already booked this exit?
        try:
            booked = conn.execute(
                "SELECT timestamp FROM trades WHERE symbol = ? AND side = 'SELL' "
                "ORDER BY timestamp DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            booked_ts = float(booked["timestamp"]) if booked else 0.0
        except Exception:
            booked_ts = 0.0

        already_booked = booked_ts >= last_fill_ts - 10

        exit_reason = "sl" if exit_price < entry_price else "tp_fill"
        trade_pnl = (exit_price - entry_price) * qty if entry_price > 0 else 0.0

        if already_booked:
            # SELL row exists (e.g. booked by ensure_tp_sl) but outcome was
            # left open — close it now.
            def _close():
                from src.trade_outcome_recorder import TradeOutcomeRecorder

                TradeOutcomeRecorder(db=db).record_outcome(
                    symbol=symbol,
                    exit_price=exit_price,
                    exit_reason=exit_reason,
                    entry_id=oid,
                )

            if dry_run:
                out["closed_only"].append(f"[dry] {symbol}#{oid} close-only @ ${exit_price:.6f}")
            elif _db_retry(f"{symbol}#{oid} outcome close", _close, out["errors"]):
                out["closed_only"].append(
                    f"{symbol}#{oid}: outcome closed @ ${exit_price:.6f} ({exit_reason})"
                )
            continue

        # 5) full patch: SELL row + outcome close
        def _insert_sell():
            c = db._get_conn()
            c.execute(
                "INSERT INTO trades (symbol, side, qty, price, pnl, timestamp) "
                "VALUES (?, 'SELL', ?, ?, ?, ?)",
                (symbol, round(exit_qty, 8), exit_price, round(trade_pnl, 6), last_fill_ts),
            )
            c.commit()

        def _close_outcome():
            from src.trade_outcome_recorder import TradeOutcomeRecorder

            TradeOutcomeRecorder(db=db).record_outcome(
                symbol=symbol,
                exit_price=exit_price,
                exit_reason=exit_reason,
                entry_id=oid,
            )

        if dry_run:
            out["patched"].append(
                f"[dry] {symbol}#{oid}: SELL {exit_qty:.6f} @ ${exit_price:.6f} ({exit_reason})"
            )
            continue

        ok_sell = _db_retry(f"{symbol}#{oid} SELL trade INSERT", _insert_sell, out["errors"])
        ok_out = _db_retry(f"{symbol}#{oid} outcome close", _close_outcome, out["errors"])
        if ok_sell and ok_out:
            out["patched"].append(
                f"{symbol}#{oid}: booked SELL {exit_qty:.6f} @ ${exit_price:.6f} "
                f"({exit_reason}, pnl ${trade_pnl:.4f}) — ledger gap auto-reconciled"
            )

    return out


if __name__ == "__main__":
    import json as _json

    logging.basicConfig(level=logging.INFO)
    print(_json.dumps(reconcile_fills(), indent=2, default=str))
