#!/usr/bin/env python3
"""WO-0926 bug1: notification outbox CLI (consumer side).

The chat consumer (hourly cron ticket / main agent) uses this to fetch
undelivered notifications and mark them delivered once they have actually
reached the chat. Until marked, reside_scan re-attaches them to every
latest.json round (cross-round redelivery).

Usage:
  python3 scripts/notify_outbox.py --pending [--json] [--limit N]
  python3 scripts/notify_outbox.py --mark-delivered NOTIF_ID [NOTIF_ID ...]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.state_db import get_state_db  # noqa: E402

DEFAULT_DB = "/root/trading-state/state.db"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--pending", action="store_true",
                    help="list undelivered notifications (oldest first)")
    ap.add_argument("--mark-delivered", nargs="+", metavar="NOTIF_ID",
                    help="mark notifications as delivered")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output")
    args = ap.parse_args()

    if not args.pending and not args.mark_delivered:
        ap.error("nothing to do: pass --pending and/or --mark-delivered")

    os.environ.setdefault("STATE_DB_PATH", args.db)
    db = get_state_db()

    if args.mark_delivered:
        n = db.notification_outbox_mark_delivered(args.mark_delivered)
        if args.json:
            print(json.dumps({"marked_delivered": n}))
        else:
            print(f"marked delivered: {n}")

    if args.pending:
        rows = db.notification_outbox_pending(limit=args.limit)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        elif not rows:
            print("no undelivered notifications")
        else:
            for r in rows:
                ts = time_str(r["created_ts"])
                print(f"[{r['notif_id']}] {ts} ({r['type']}) {r['title']}")
                for line in (r.get("body") or "").splitlines():
                    print(f"    {line}")
    return 0


def time_str(ts):
    import time as _t

    try:
        return _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(float(ts)))
    except (TypeError, ValueError):
        return str(ts)


if __name__ == "__main__":
    raise SystemExit(main())
