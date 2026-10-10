#!/usr/bin/env python3
"""
Daily Learning Pipeline — WO-1039 lightweight daily tier.

Bridges the weekly learning cadence (scripts/learning_pipeline.py, Sunday
09:00) with per-trade realtime channels (bandit / Phase 2A rolling stats /
Phase 2B PF per-scan). Runs two LIGHT steps daily; the heavy param
optimization stays weekly-only by design.

Steps:
1. Factor weight learning  (gated: see guardrails below)
2. Concept drift detection (report-only, never acts)

Guardrails (WO-1039 acceptance: min-sample / epsilon / audit / rollback):
- MIN_RECENT_TRADES: needs >= 5 closed trades in the last 7d before the
  daily weight learning even runs (fresh evidence required; the global
  MIN_TRADES=10 inside OnlineLearner still applies on top).
- WEIGHT_EPS: if every factor changes by less than 0.5 (absolute weight
  points) the new weights are NOT written (no-op learning round).
- AUDIT: every accepted change appends {ts, trigger, old, new} to
  logs/learning_audit.jsonl (never silent).
- ROLLBACK: `--rollback-last` restores the most recent audited old
  weights back into kv (and appends a rollback audit row).

Run via cron (no_agent) Mon-Sat 08:30 (Sunday 09:00 keeps the full
weekly pipeline). API budget: 0 external calls — DB-only compute.
"""
import argparse
import json
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

LOGS = PROJECT / "logs"
STATUS_FILE = LOGS / "daily_learning_status.json"
AUDIT_FILE = LOGS / "learning_audit.jsonl"

MIN_RECENT_TRADES = 5      # closed trades in the last 7d required to learn
WEIGHT_EPS = 0.5           # max per-factor delta below which nothing is written
RECENT_WINDOW_SEC = 7 * 86400


def _recent_closed_trades(db) -> int:
    row = db._get_conn().execute(
        "SELECT COUNT(*) FROM trade_outcomes "
        "WHERE exit_time IS NOT NULL AND exit_time >= ?",
        (time.time() - RECENT_WINDOW_SEC,)).fetchone()
    return int(row[0]) if row else 0


def _append_audit(entry: dict) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _load_audit() -> list:
    if not AUDIT_FILE.exists():
        return []
    rows = []
    for line in AUDIT_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def step_weight_learning(db, dry_run: bool = False) -> dict:
    """Gated daily weight learning. Returns a step report dict."""
    from src.online_learner import OnlineLearner

    recent = _recent_closed_trades(db)
    if recent < MIN_RECENT_TRADES:
        return {
            "step": "weight_learning", "status": "skipped",
            "reason": f"recent_7d_trades {recent} < {MIN_RECENT_TRADES}",
            "recent_7d_trades": recent,
        }

    learner = OnlineLearner(db=db)
    old_weights = learner.get_current_weights()
    result = learner.compute_optimal_weights()
    if not result:
        return {
            "step": "weight_learning", "status": "skipped",
            "reason": "insufficient total trades (OnlineLearner MIN_TRADES)",
            "recent_7d_trades": recent,
        }

    new_weights = result["weights"]
    max_delta = max(
        (abs(new_weights.get(f, 0) - old_weights.get(f, 0))
         for f in new_weights), default=0.0)
    if max_delta < WEIGHT_EPS:
        return {
            "step": "weight_learning", "status": "no_change",
            "reason": f"max factor delta {max_delta:.3f} < eps {WEIGHT_EPS}",
            "recent_7d_trades": recent,
        }

    if not dry_run:
        db.kv_set("learned_factor_weights", new_weights)
        _append_audit({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "trigger": "daily_learning",
            "action": "weights_written",
            "old": old_weights,
            "new": new_weights,
            "max_delta": round(max_delta, 3),
            "recent_7d_trades": recent,
        })
    return {
        "step": "weight_learning",
        "status": "would_write" if dry_run else "ok",
        "recent_7d_trades": recent,
        "max_delta": round(max_delta, 3),
        "old": old_weights, "new": new_weights,
    }


def step_concept_drift() -> dict:
    """Report-only drift detection (same call as weekly pipeline step 2)."""
    try:
        from src.concept_drift import ConceptDriftDetector

        d = ConceptDriftDetector()
        r = d.detect_drift()
        return {
            "step": "concept_drift", "status": "ok",
            "drift_severity": (r or {}).get("severity", "none"),
            "drift_signals": (r or {}).get("drift_signals"),
            "recommendation": (r or {}).get("recommendation", ""),
        }
    except Exception as e:  # drift is informational — never fail the round
        return {"step": "concept_drift", "status": "error", "error": str(e)[:200]}


def rollback_last(db) -> dict:
    """Restore the most recent 'weights_written' audit row's old weights."""
    rows = [r for r in _load_audit() if r.get("action") == "weights_written"]
    if not rows:
        return {"status": "error", "reason": "no audited weight writes to roll back"}
    last = rows[-1]
    db.kv_set("learned_factor_weights", last["old"])
    _append_audit({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "trigger": "rollback",
        "action": "rollback_applied",
        "rolled_back_ts": last["ts"],
        "restored": last["old"],
    })
    return {"status": "ok", "restored": last["old"], "rolled_back_ts": last["ts"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="WO-1039 daily learning tier")
    parser.add_argument("--rollback-last", action="store_true",
                        help="restore the most recent audited weight change")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would happen, write nothing")
    args = parser.parse_args()

    from src.state_db import get_state_db
    db = get_state_db()

    if args.rollback_last:
        if args.dry_run:
            rows = [r for r in _load_audit() if r.get("action") == "weights_written"]
            print(json.dumps({"would_rollback": rows[-1] if rows else None}, indent=2))
            return 0
        result = rollback_last(db)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["status"] == "ok" else 1

    started = time.time()
    steps = []

    wl = step_weight_learning(db, dry_run=args.dry_run)
    steps.append(wl)

    steps.append(step_concept_drift())

    summary = {
        "pipeline": "daily_learning",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "steps": steps,
        "total_elapsed_sec": round(time.time() - started, 1),
        "all_ok": all(s.get("status") in
                      ("ok", "skipped", "no_change", "would_write")
                      for s in steps),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if not args.dry_run:
        LOGS.mkdir(parents=True, exist_ok=True)
        STATUS_FILE.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    # guardrail skips / no_change are healthy outcomes — informational
    # steps never fail the cron round; always exit 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
