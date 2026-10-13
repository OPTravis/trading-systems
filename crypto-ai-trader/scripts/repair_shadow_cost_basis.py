#!/usr/bin/env python3
"""WO-1051: repair ledger_shadow_positions cost pollution (存量修复).

Bug: pre-fix SELL cut net_qty but kept the full cost_basis on the row;
a later re-BUY merged into the stale cost -> inflated avg_entry_price
(TIAUSDT 10/13: 1.2404 vs true 0.6214). pnl_live_monitor read the
polluted avg and raised a false -49.9% NEAR_SL alert.

Repair method — anchored replay:
  For each shadow row, anchor at the BUY event whose ts == opened_at
  (the INSERT that created the current row), replay BUY/SELL events
  forward with CORRECT proportional-cost semantics, and compare.
  * replay qty == live qty  (validated 16/16 on 10/13 live data)
  * avg repair   : rows whose close-reopen merge polluted the avg
    (NEARUSDT 14.0781->5.2017, PYTHUSDT 0.1712->0.0854,
     TIAUSDT 1.2404->0.6214)
  * cost repair  : every row violating the invariant
    cost_basis == net_qty * avg_entry_price (12 stale-cost rows)
  * rows with no anchor (bootstrap baseline): per-unit avg is trusted,
    cost repaired to qty*avg only.

Safety: DRYRUN by default; writes only with PROD_WRITES_ALLOWED=1.
Only touches ledger_shadow_positions (reconciliation book); never the
live portfolio / trades / kv cash.
"""
import os
import sqlite3
import sys
import time

DB_PATH = os.environ.get("STATE_DB", "/root/trading-state/state.db")
EPS = 1e-9
AVG_TOL = 5e-5     # avg comparison tolerance
COST_TOL = 1e-4    # cost comparison tolerance
QTY_TOL = 1e-6     # replay-vs-live qty must match to auto-repair

# Audit trail key written once repairs are applied (kv, for the worklog).
AUDIT_KEY = "wo1051:shadow_cost_repair"


def _ts(t):
    import datetime
    return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")


def plan_repair(conn):
    """Return (repairs, flags): repairs = [{symbol, old_avg, new_avg,
    old_cost, new_cost, kind}], flags = [manual-review strings]."""
    repairs, flags = [], []
    rows = conn.execute(
        "SELECT * FROM ledger_shadow_positions ORDER BY symbol"
    ).fetchall()
    for row in rows:
        sym = row["symbol"]
        opened = float(row["opened_at"] or 0)
        evs = conn.execute(
            "SELECT ts, type, qty, price FROM ledger_events "
            "WHERE symbol=? AND type IN ('BUY','SELL') ORDER BY ts, id",
            (sym,),
        ).fetchall()
        live_qty = float(row["net_qty"])
        live_avg = float(row["avg_entry_price"])
        live_cost = float(row["cost_basis"])

        anchor = None
        for i, e in enumerate(evs):
            if e["type"] == "BUY" and abs(e["ts"] - opened) < 1.0:
                anchor = i
                break
        if anchor is None:
            # bootstrap-baseline row: trust per-unit avg, fix cost only
            true_avg = live_avg
            true_cost = live_qty * live_avg
            kind = "bootstrap_cost_only"
        else:
            qty = cost = 0.0
            for e in evs[anchor:]:
                if e["type"] == "BUY":
                    cost += e["qty"] * e["price"]
                    qty += e["qty"]
                else:
                    nq = qty - e["qty"]
                    if nq <= EPS:
                        qty = cost = 0.0
                    else:
                        ratio = min(
                            max(e["qty"] / qty if qty > 0 else 1.0, 0.0), 1.0
                        )
                        cost *= (1.0 - ratio)
                        qty = nq
            true_avg = cost / qty if qty > EPS else 0.0
            true_cost = cost
            if abs(qty - live_qty) > QTY_TOL:
                flags.append(
                    f"{sym}: replay qty {qty:.6f} != live {live_qty:.6f} "
                    f"— NOT repaired, manual review"
                )
                continue
            kind = "anchored_replay"

        need_avg = abs(true_avg - live_avg) > AVG_TOL
        need_cost = abs(true_cost - live_cost) > COST_TOL
        if not (need_avg or need_cost):
            continue
        repairs.append({
            "symbol": sym, "kind": kind,
            "old_avg": live_avg, "new_avg": true_avg,
            "old_cost": live_cost, "new_cost": true_cost,
            "qty": live_qty,
        })
    return repairs, flags


def main():
    dryrun = os.environ.get("PROD_WRITES_ALLOWED") != "1"
    if not os.path.exists(DB_PATH):
        print(f"FATAL: db not found: {DB_PATH}")
        return 2
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    repairs, flags = plan_repair(conn)

    print(f"=== WO-1051 shadow cost repair ({'DRYRUN' if dryrun else 'APPLY'}) ===")
    if not repairs and not flags:
        print("nothing to repair — book is consistent")
        return 0
    for r in repairs:
        avg_note = (
            f"avg {r['old_avg']:.4f} -> {r['new_avg']:.4f}"
            if abs(r["old_avg"] - r["new_avg"]) > AVG_TOL else "avg unchanged"
        )
        print(f"  {r['symbol']:12s} [{r['kind']:19s}] qty={r['qty']:.6f} "
              f"{avg_note}, cost {r['old_cost']:.4f} -> {r['new_cost']:.4f}")
    for f in flags:
        print(f"  !! {f}")

    if dryrun:
        print(f"\nDRYRUN: {len(repairs)} row(s) would be repaired. "
              f"Re-run with PROD_WRITES_ALLOWED=1 to apply.")
        return 0

    cur = conn.cursor()
    applied = []
    for r in repairs:
        cur.execute(
            """UPDATE ledger_shadow_positions
               SET avg_entry_price = ?, cost_basis = ?
               WHERE symbol = ?""",
            (r["new_avg"], r["new_cost"], r["symbol"]),
        )
        if cur.rowcount != 1:
            print(f"FATAL: update failed for {r['symbol']} — aborting")
            conn.rollback()
            return 3
        applied.append(r)
    audit = {
        "applied_at": _ts(time.time()),
        "rows": [
            {"symbol": r["symbol"], "kind": r["kind"],
             "old_avg": round(r["old_avg"], 8), "new_avg": round(r["new_avg"], 8),
             "old_cost": round(r["old_cost"], 8), "new_cost": round(r["new_cost"], 8)}
            for r in applied
        ],
    }
    import json
    cur.execute(
        "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
        (AUDIT_KEY, json.dumps(audit, ensure_ascii=False), time.time()),
    )
    conn.commit()
    print(f"\nAPPLIED: {len(applied)} row(s) repaired; audit trail in "
          f"kv '{AUDIT_KEY}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
