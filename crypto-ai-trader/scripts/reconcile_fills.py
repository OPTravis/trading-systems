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


def _sell_booked(db, symbol: str, fill_ts: float) -> bool:
    """Read-back check: was the SELL row durably written?"""
    try:
        row = db._get_conn().execute(
            "SELECT 1 FROM trades WHERE symbol = ? AND side = 'SELL' "
            "AND ABS(timestamp - ?) < 0.001 LIMIT 1",
            (symbol, fill_ts),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _outcome_closed(db, oid: int) -> bool:
    """Read-back check: is the outcome row actually closed?"""
    try:
        row = db._get_conn().execute(
            "SELECT 1 FROM trade_outcomes WHERE id = ? AND status = 'closed'",
            (oid,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _verified_write(db, write_fn, verify_fn, label: str, errors, attempts: int = 3) -> bool:
    """Run a critical ledger write THROUGH src.state_db.db_write_with_verify
    (commit + read-back verification + loud escalation via cron_failures.jsonl)
    and mirror the final failure into this job's errors[] output.
    bug#8: exception-only retry could not detect writes that commit() had
    acknowledged but the storage layer then dropped."""
    from src.state_db import db_write_with_verify

    ok = db_write_with_verify(
        db=db, write_fn=write_fn, verify_fn=verify_fn,
        label=label, attempts=attempts, backoff_sec=1.0,
    )
    if not ok:
        errors.append(
            f"{label} failed {attempts}x — exchange fill exists but "
            f"DB bookkeeping gap needs manual patch"
        )
    return ok


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

        # 3) find exit fills after this outcome's entry (FIFO across open outcomes)
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
        sells.sort(key=lambda t: float(t.get("time", 0)))

        # bug#26: support PARTIAL exits. Previously an outcome was skipped whenever
        # ANY balance of the asset remained (held_qty > 0), which broke the
        # merged-position case (e.g. a TP sells the older lot while a newer lot
        # is still open — REUSDT 15:02 10.9 TP while 10.6 new lot held).
        # Now we FIFO-allocate sell fills across open outcomes in entry order,
        # consuming up to each outcome's qty, and book/close an outcome as soon
        # as its allocated exit qty covers its entry qty.
        try:
            oq = float(qty or 0)
        except (TypeError, ValueError):
            oq = 0.0
        if oq <= 0:
            out["anomalies"].append(f"{symbol}#{oid}: zero/invalid qty")
            continue

        prior_open_qty = 0.0
        for r2 in rows:
            if r2["id"] == oid:
                break
            try:
                prior_open_qty += float(r2["qty"] or 0)
            except (TypeError, ValueError):
                pass

        remaining = oq
        allocated = []
        for t in sells:
            if remaining <= 1e-12:
                break
            tq = float(t.get("qty", 0))
            # skip fills already consumed by earlier open outcomes (FIFO)
            if tq <= prior_open_qty:
                prior_open_qty -= tq
                continue
            avail = tq - prior_open_qty
            prior_open_qty = 0.0
            take = min(avail, remaining)
            if take > 1e-12:
                allocated.append((t, take))
                remaining -= take

        exit_qty = sum(tk for _, tk in allocated)
        # bug#31 fix (2026-08-30): absolute 1e-8 tolerance was too tight -
        # exchange fills can differ from outcome qty by LOT_SIZE rounding /
        # commission residue (BANK SL 8/30: filled 141.993 vs outcome 142.0,
        # 0.005% off), leaving the outcome open forever with a repeated
        # anomaly every 5 min and no SELL booking. Use a relative 2%
        # tolerance, matching insert_sell_dedup's +/-2% semantics.
        if exit_qty < oq * 0.98:
            # outcome not yet fully closed (partial or no exit). If the asset
            # balance is gone entirely but we still have no fills, flag it.
            if held_qty <= 1e-12:
                out["anomalies"].append(
                    f"{symbol}#{oid}: position gone but insufficient SELL fills "
                    f"(exit {exit_qty}/{oq}) — check manual transfer/withdrawal"
                )
            else:
                out["skipped"].append(
                    f"{symbol}: outcome #{oid} still open (exit {exit_qty:.6f}/{oq:.6f})"
                )
            continue

        exit_price = (
            sum(tk * float(t["price"]) for t, tk in allocated) / exit_qty
            if exit_qty > 0
            else 0.0
        )
        last_fill_ts = float(allocated[-1][0].get("time", 0)) / 1000
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
        # bug#31: book the ACTUAL filled qty (exit_qty), not outcome qty
        trade_pnl = (exit_price - entry_price) * exit_qty if entry_price > 0 else 0.0

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
            elif _verified_write(
                db,
                write_fn=_close,
                verify_fn=lambda: _outcome_closed(db, oid),
                label=f"reconcile_fills {symbol}#{oid} outcome close",
                errors=out["errors"],
            ):
                out["closed_only"].append(
                    f"{symbol}#{oid}: outcome closed @ ${exit_price:.6f} ({exit_reason})"
                )
            continue

        # 5) full patch: SELL row + outcome close
        def _insert_sell():
            c = db._get_conn()
            # bug#13 fix: atomic dedup INSERT — race-proof against concurrent
            # reconcile jobs (8/22: HBAR/DOGE/POL each double-booked when
            # trailing-check and ensure_tp_sl ran in the same minute).
            # Skip insert if an equivalent SELL row already exists (same
            # symbol/side, qty within 2%, price within 1%, 1h window).
            c.execute(
                """
                INSERT INTO trades (symbol, side, qty, price, pnl, timestamp)
                SELECT ?, 'SELL', ?, ?, ?, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM trades
                    WHERE symbol = ? AND side = 'SELL'
                      AND ABS(qty - ?) <= ? * 0.02
                      AND ABS(price - ?) <= ? * 0.01
                      AND timestamp >= ? - 3600
                      AND timestamp <= ? + 3600
                )
                """,
                (symbol, round(exit_qty, 8), exit_price, round(trade_pnl, 6), last_fill_ts,
                 symbol, exit_qty, exit_qty, exit_price, exit_price,
                 last_fill_ts, last_fill_ts),
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

        ok_sell = _verified_write(
            db,
            write_fn=_insert_sell,
            verify_fn=lambda: _sell_booked(db, symbol, last_fill_ts),
            label=f"reconcile_fills {symbol}#{oid} SELL trade INSERT",
            errors=out["errors"],
        )
        ok_out = _verified_write(
            db,
            write_fn=_close_outcome,
            verify_fn=lambda: _outcome_closed(db, oid),
            label=f"reconcile_fills {symbol}#{oid} outcome close",
            errors=out["errors"],
        )
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
