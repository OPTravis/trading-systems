#!/usr/bin/env python3
"""P0-2 one-off ledger correction — stale SELL backfills (2026-09-21).

The 9/20 16:31 reconcile round booked five stale legs (INJ id=58..61,
SUI id=62/63) plus ZAMA id=68 (fixed earlier under P0-1.5) because
_book_missing_sells had no time window and no per-leg cap. This script
removes the fabricated rows, backfills the two real SELL fills the
ledger is missing, and verifies (not rewrites) the correct 9/19 INJ row
and the already-applied ZAMA fix.

Idempotent: every step checks current DB state first, so re-runs and
the earlier manual ZAMA fix are handled cleanly.
Default mode is DRY-RUN (prints the diff); pass --apply to write.
"""
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

DB = "/root/trading-state/state.db"
CST = timezone(timedelta(hours=8))


def epoch(y, mo, d, h, mi, s):
    return datetime(y, mo, d, h, mi, s, tzinfo=CST).timestamp()


# --- the six fabricated backfill rows (DELETE if present) -------------------
FABRICATED = {
    58: ("INJUSDT", "SELL", 3.13, 5.091, "2981368351"),   # 5/13 stale leg
    59: ("INJUSDT", "SELL", 0.99, 5.798, "3162425417"),   # 9/15 dup of id=6
    60: ("INJUSDT", "SELL", 1.9, 5.466, "3165195491"),    # 9/16 dup of id=7
    61: ("INJUSDT", "SELL", 3.35, 6.547, "3170937513"),   # 9/19 dup of id=43
    62: ("SUIUSDT", "SELL", 6.7, 0.9355, "8370533427"),   # 4/23 stale leg
    63: ("SUIUSDT", "SELL", 5.4, 0.9331, "8372669858"),   # 4/23 stale leg
    68: ("ZAMAUSDT", "SELL", 443.0, 0.03178, "98217977"), # 4/14 (P0-1.5)
}

# --- the two real fills the ledger is missing (INSERT if absent) -----------
BACKFILL = [
    dict(symbol="INJUSDT", side="SELL", qty=7.45, price=7.492,
         pnl=round(7.45 * (7.492 - 8.058), 6),           # -4.2167
         oid="3175955775", ts=epoch(2026, 9, 20, 11, 14, 49)),
    dict(symbol="SUIUSDT", side="SELL", qty=6.9, price=0.8214,
         pnl=round(6.9 * (0.8214 - 0.8623), 6),           # -0.28221
         oid="8874168544", ts=epoch(2026, 9, 20, 13, 18, 16)),
    dict(symbol="ZAMAUSDT", side="SELL", qty=64.0, price=0.08782890625,
         pnl=-0.40657, oid="233705997",
         ts=epoch(2026, 9, 20, 22, 42, 24)),              # P0-1.5 backfill
]

# --- rows that must be verified present and correct (NEVER rewritten) ------
VERIFY = [
    (43, "INJUSDT", "SELL", 3.35, 6.547, "3170937513"),  # 9/19 real close
    (37, "INJUSDT", "BUY", 3.35, 6.608, None),           # 9/19 real entry
    (51, "SUIUSDT", "BUY", 6.9, 0.8623, None),           # 9/20 real entry
    (54, "INJUSDT", "BUY", 7.45, 8.058, None),           # 9/20 real entry
    (65, "ZAMAUSDT", "BUY", 64.0, 0.0941815625, None),   # 9/20 real entry
]

SYMBOLS = ("SUIUSDT", "INJUSDT", "ZAMAUSDT")


def pnl_sum(conn, symbol):
    row = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE symbol = ?",
        (symbol,)).fetchone()
    return row[0]


def main(apply: bool):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    print(f"=== P0-2 ledger correction {'APPLY' if apply else 'DRY-RUN'} ===")
    for sym in SYMBOLS:
        print(f"  before: {sym:9s} realized pnl = {pnl_sum(conn, sym):+.6f}")

    plan_del, plan_ins = [], []
    for tid, (sym, side, qty, px, oid) in FABRICATED.items():
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (tid,)).fetchone()
        if row is None:
            print(f"  [skip] id={tid} already gone")
            continue
        exp = (sym, side, qty, px, oid)
        got = (row["symbol"], row["side"], row["qty"], row["price"],
               row["client_order_id"])
        if got != exp:
            print(f"  [ABORT] id={tid} content mismatch!\n    expect {exp}\n    got   {got}")
            conn.close()
            sys.exit(2)
        plan_del.append((tid, got, row["pnl"]))

    for b in BACKFILL:
        row = conn.execute(
            "SELECT id FROM trades WHERE client_order_id = ?",
            (b["oid"],)).fetchone()
        if row:
            print(f"  [skip] {b['symbol']} oid={b['oid']} already booked (id={row['id']})")
            continue
        plan_ins.append(b)

    print(f"\n--- plan: {len(plan_del)} DELETE, {len(plan_ins)} INSERT ---")
    for tid, got, pnl in plan_del:
        print(f"  DELETE id={tid}: {got[0]} {got[1]} {got[2]} @ {got[3]} pnl={pnl:+.6f} (oid {got[4]})")
    for b in plan_ins:
        print(f"  INSERT {b['symbol']} {b['side']} {b['qty']} @ {b['price']} "
              f"pnl={b['pnl']:+.6f} (oid {b['oid']}, "
              f"{datetime.fromtimestamp(b['ts'], tz=CST):%m-%d %H:%M:%S})")

    print("\n--- verify correct rows (no rewrite) ---")
    ok = True
    for tid, sym, side, qty, px, oid in VERIFY:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (tid,)).fetchone()
        if row is None:
            print(f"  [MISSING] id={tid} {sym} {side} — expected!")
            ok = False
            continue
        got = (row["symbol"], row["side"], row["qty"])
        tag = "ok" if got == (sym, side, qty) else "MISMATCH"
        print(f"  id={tid}: {got} @ {row['price']} pnl={row['pnl']:+.6f} [{tag}]")
    p43 = conn.execute("SELECT pnl FROM trades WHERE id = 43").fetchone()
    if p43 and abs(p43[0] - (-0.20435)) > 0.001:
        print(f"  [WARN] id=43 pnl {p43[0]:+.6f} deviates from -0.20435")

    if not apply:
        print("\nDRY-RUN only — re-run with --apply to write.")
        conn.close()
        return

    backup = f"/Coze/Drive/Crypto_Trading_Monitor/backups/state.db.20260921_pre_p02_sui_inj_fix"
    import shutil
    shutil.copy(DB, backup)
    print(f"\nbackup written: {backup}")

    evidence = {
        "ticket": "P0-2 stale SELL backfill correction",
        "deleted": [dict(id=t, row=g, pnl=p) for t, g, p in plan_del],
        "inserted": plan_ins,
        "verified": [list(v) for v in VERIFY],
        "source": "scripts/oneoff/p0_2_fix_stale_sell_backfills.py",
    }
    for tid, _, _ in plan_del:
        conn.execute("DELETE FROM trades WHERE id = ?", (tid,))
    for b in plan_ins:
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, timestamp, "
            "client_order_id) VALUES (?,?,?,?,?,?,?)",
            (b["symbol"], b["side"], b["qty"], b["price"], b["pnl"], b["ts"], b["oid"]))
    conn.execute(
        "INSERT INTO audit_log (timestamp, action, details, old_value, new_value, source) "
        "VALUES (?,?,?,?,?,?)",
        (time.time(), "TRADES_P02_STALE_SELL_FIX",
         json.dumps(evidence, ensure_ascii=False, indent=1),
         json.dumps({str(t): g for t, g, _ in plan_del}, ensure_ascii=False),
         json.dumps({b["oid"]: b for b in plan_ins}, ensure_ascii=False),
         "p0-2_oneoff_script"))
    conn.commit()

    print("\n--- after ---")
    total_delta = 0.0
    for sym in SYMBOLS:
        now_sum = pnl_sum(conn, sym)
        print(f"  after:  {sym:9s} realized pnl = {now_sum:+.6f}")
    rows = {r["symbol"]: r["qty"] for r in conn.execute(
        "SELECT symbol, COUNT(*) qty FROM trades WHERE symbol IN (?,?,?) "
        "GROUP BY symbol", SYMBOLS)}
    print(f"  rows per symbol: {rows}")
    aid = conn.execute(
        "SELECT id FROM audit_log WHERE action='TRADES_P02_STALE_SELL_FIX' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    print(f"  audit_log id={aid[0]} TRADES_P02_STALE_SELL_FIX")
    conn.close()


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
