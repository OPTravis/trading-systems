#!/usr/bin/env python3
"""
Push pending notifications — reads pending_notifications.json, outputs to stdout
for cron/heartbeat pickup, and marks as pushed.

Usage:
    python3 scripts/push_notifications.py          # output and mark pushed
    python3 scripts/push_notifications.py --peek   # output without marking
"""
import json
import sys
from pathlib import Path

SIGNALS_DIR = Path(__file__).parent.parent / "signals"
NOTIFICATIONS_FILE = SIGNALS_DIR / "pending_notifications.json"
MESSAGES_FILE = SIGNALS_DIR / "messages.json"


def load_json(path):
    if not path.exists():
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return []


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def main():
    peek = "--peek" in sys.argv

    # Pending notifications
    notifs = load_json(NOTIFICATIONS_FILE)
    unpushed = [n for n in notifs if not n.get("pushed")]

    if not unpushed:
        return  # silent when nothing to push

    print(f"📢 {len(unpushed)} 条待推送通知:\n")
    for n in unpushed:
        ts = n["timestamp"][:16].replace("T", " ")
        print(f"[{ts}]")
        print(n["body"])
        print()

    if not peek:
        # Mark as pushed
        for n in notifs:
            if not n.get("pushed"):
                n["pushed"] = True
        save_json(NOTIFICATIONS_FILE, notifs)

        # Also mark messages.json
        msgs = load_json(MESSAGES_FILE)
        for m in msgs:
            if not m.get("notified"):
                m["notified"] = True
        save_json(MESSAGES_FILE, msgs)

        print("✅ 已标记为已推送")


if __name__ == "__main__":
    main()
