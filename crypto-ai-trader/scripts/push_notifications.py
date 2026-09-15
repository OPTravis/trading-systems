#!/usr/bin/env python3
"""
Push pending notifications — reads pending_notifications.json, outputs to stdout
for cron/heartbeat pickup, and marks as pushed.

Usage:
    python3 scripts/push_notifications.py          # output and mark pushed
    python3 scripts/push_notifications.py --peek   # output without marking

bug#38: if the pre-report validator itself crashes or cannot be imported,
ALL unpushed notifications are blocked by default (fail-safe). The only
override is the explicit env switch ALLOW_UNVERIFIED_REPORT=1. --peek
remains a pure dry inspection of the raw queue (prints, marks nothing).
"""
import json
import os
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
    """bug#38: CWD-independent, cache-resilient import of the pre-report validator.

    Three-stage resolution guarantees the import succeeds regardless of:
      - CWD not being repo root (cron shells)
      - stale namespace-package cache in sys.modules (Python 3.3+ caches
        the first failed resolution of a namespace subpackage, so a naive
        retry after sys.path fixup hits the same bad entry)
      - any remaining edge case (filesystem oddities, zip-imports, etc.)

    Stage 1: plain import (fast path — works 99% of the time).
    Stage 2: clear sys.modules residue + fix sys.path + retry.
    Stage 3: importlib direct file load — immune to all packaging issues.
    """
    # Stage 1: standard import
    try:
        from scripts.report_validator import validate_all
        return validate_all
    except ImportError:
        pass

    # Stage 2: clear namespace-package cache, fix sys.path, retry
    sys.modules.pop("scripts.report_validator", None)
    sys.modules.pop("scripts", None)
    repo_root = str(Path(__file__).resolve().parent.parent)
    if repo_root in sys.path:
        sys.path.remove(repo_root)
    sys.path.insert(0, repo_root)
    try:
        from scripts.report_validator import validate_all
        return validate_all
    except ImportError:
        pass

    # Stage 3: direct file loading via importlib — guaranteed to work
    import importlib.util
    file_path = Path(__file__).resolve().parent / "report_validator.py"
    spec = importlib.util.spec_from_file_location(
        "_report_validator_direct", str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(
            f"Cannot load report_validator from {file_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.validate_all


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
            # bug#38 fail-safe: a crashed/unloadable validator means NOTHING
            # was verified — shipping unverified numbers is exactly how the
            # 15:12 escape (notif_20260831151303994385) happened. Default is
            # now BLOCK ALL; the explicit env switch below is the only way
            # through, and the loud print feeds the self-heal pipeline.
            if os.environ.get("ALLOW_UNVERIFIED_REPORT") == "1":
                print(f"⚠️ report_validator error: {e} — "
                      f"ALLOW_UNVERIFIED_REPORT=1 ⇒ 放行 {len(unpushed)} 条未验证通报\n")
                allowed = unpushed
            else:
                print(f"🛑 report_validator error: {e} — FAIL-SAFE：阻断全部 "
                      f"{len(unpushed)} 条未验证通报（人工确认后可临时设 "
                      f"ALLOW_UNVERIFIED_REPORT=1 放行）\n")
                allowed = []
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
