"""WO-1046h one-shot backfill: mine cron-scan logs for daily HMM×bull_tier
verdicts and seed kv ``hmm_daily_verdicts``.

Pre-launch history is limited to whatever the rotated logs still hold
(cron-scan.log.old 2026-10-08..12, cron-scan.log 2026-10-12..13). Everything
earlier rotated away — the ledger self-heals forward from every scan once
record_daily_verdict is live, so this backfill is best-effort seed data.

Usage:
    python3 -m scripts.backfill_hmm_verdicts [--db /root/trading-state/state.db]
        [--log logs/cron-scan.log.old] [--log logs/cron-scan.log] [--dry-run]
"""
import argparse
import re
import sys
from collections import defaultdict

sys.path.insert(0, ".")

HMM_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2},\d+ \[INFO\] "
    r"StrategyAdaptor: .*?HMM=(\w+)")
BULL_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2},\d+ \[INFO\] "
    r"BTC trend gate: .*?→ (CONFIRMED_BULL|MILD_BULL|NEUTRAL_FEAR|BEAR)")


def mine(log_paths):
    """Per day, the LAST verdict seen in log order wins (day-end state).

    Files must be given oldest-first so cross-file ordering holds
    (log.old precedes log by rotation).
    """
    hmm_daily = {}   # day -> verdict (last seen)
    bull_daily = {}
    for path in log_paths:
        try:
            with open(path, errors="ignore") as fh:
                for line in fh:
                    m = HMM_RE.match(line)
                    if m:
                        hmm_daily[m.group(1)] = m.group(2)
                    b = BULL_RE.match(line)
                    if b:
                        bull_daily[b.group(1)] = b.group(2)
        except FileNotFoundError:
            print(f"  (skip missing {path})")
    return hmm_daily, bull_daily


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/root/trading-state/state.db")
    ap.add_argument("--log", action="append", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logs = args.log or ["logs/cron-scan.log.old", "logs/cron-scan.log"]
    hmm_daily, bull_daily = mine(logs)

    from src.state_db import StateDB
    db = StateDB(args.db)
    ledger = db.kv_get("hmm_daily_verdicts")
    ledger = ledger if isinstance(ledger, dict) else {}

    added = 0
    all_days = sorted(set(hmm_daily) | set(bull_daily))
    for day in all_days:
        row = ledger.setdefault(day, {})
        # Seed only fields the live ledger has not written yet (live data
        # is fresher than a static log mine — never overwrite it).
        if "hmm" not in row and day in hmm_daily:
            row["hmm"] = hmm_daily[day]
        if "bull_tier" not in row and day in bull_daily:
            row["bull_tier"] = bull_daily[day]
        added += 1

    print(f"days mined: {len(all_days)} (hmm={len(hmm_daily)}, bull={len(bull_daily)})")
    for day in all_days:
        print(f"  {day}: hmm={ledger[day].get('hmm')} bull={ledger[day].get('bull_tier')}")

    if args.dry_run:
        print("(dry-run, no write)")
        return
    db.kv_set("hmm_daily_verdicts", ledger)
    print(f"kv hmm_daily_verdicts written ({added} days)")

    # Post-write sanity: divergence streak
    from src.hmm_scoring import divergence_days, compute_hmm_score
    print("divergence:", divergence_days(db))
    print("score:", compute_hmm_score(db))


if __name__ == "__main__":
    main()
