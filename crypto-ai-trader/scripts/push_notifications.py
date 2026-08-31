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


def _load_validate_all():
    """bug#37: CWD-independent import of the pre-report validator.

    cron shells do not guarantee CWD == repo root; when invoked from src/
    (or anywhere else) `from scripts.report_validator import ...` raises
    ModuleNotFoundError — which here would silently disable the whole
    consistency gate. Derive the repo root from __file__, never from the
    process CWD."""
    try:
        from scripts.report_validator import validate_all
        return validate_all
    except ImportError:
        repo_root = str(Path(__file__).resolve().parent.parent)
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from scripts.report_validator import validate_all
        return validate_all


def main():
    peek = "--peek" in sys.argv

    # Pending notifications
    notifs = load_json(NOTIFICATIONS_FILE)
    unpushed = [n for n in notifs if not n.get("pushed")]

    if not unpushed:
        return  # silent when nothing to push

    # bug#34: pre-report consistency gate — validate each unpushed notification
    # against DB/exchange ground truth before it goes out. Blocked ones are
    # marked (never re-pushed) and ticketed by the validator; they are NOT
    # printed. opted out only for --peek (dry inspection of the raw queue).
    if not peek:
        try:
            validate_all = _load_validate_all()
            allowed, blocked = validate_all(unpushed)
            if blocked:
                save_json(NOTIFICATIONS_FILE, notifs)  # persist blocked marks
                print(f"🛑 report_validator 阻断 {len(blocked)} 条不一致通报"
                      f"（详见 logs/report_validator_failures.jsonl）\n")
        except Exception as e:
            # fail-closed choice is made per-claim inside the validator; a
            # validator crash here must not silently kill the push path, but
            # it IS logged loudly for the self-heal pipeline to see.
            print(f"⚠️ report_validator error: {e}\n")
            allowed = unpushed
        unpushed = allowed
        if not unpushed:
            print("（其余通报已全部通过校验或为空）")
            return

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
