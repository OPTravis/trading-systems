#!/usr/bin/env python3
"""WO-1044-D: GREED score-threshold calibration from historical signal distribution.

Aggregates the last N days (default 90) of candidate signal scores from:
  1. state.db `decisions.score`      — research/execution decision scores
  2. state.db `trade_outcomes.score` — recorded entry scores
  3. state.db `notification_outbox` scan_summary bodies — "評分/评分: NN" regex
     (top-3 per round whenever opportunities passed the scanner)

Computes distribution percentiles and persists them to state.db kv
`threshold_calibration`. strategy_adaptor reads `p75` (fallback: map
default 75) as the GREED regime score threshold. Re-runnable: scores
accumulate as the system trades; each run refreshes the kv row.

Known bias (documented in the WO-1044 impact doc): recorded scores are
threshold-survivors (right tail), so the calibrated bar stays
conservative — it unlocks entry without dropping to the noise band.

Usage:
  python3 scripts/calibrate_threshold.py             # print only
  python3 scripts/calibrate_threshold.py --write     # persist to kv
  python3 scripts/calibrate_threshold.py --window 60 --write
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, ".")

STATE_DB = "/root/trading-state/state.db"
SCORE_RE = re.compile(r"評分[:：]\s*(\d{1,3})|评分[:：]\s*(\d{1,3})")


def _pct(sorted_vals, p):
    """Linear-interpolated percentile of an ascending list."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return round(float(sorted_vals[0]), 2)
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f, c = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return round(sorted_vals[f] + (k - f) * (sorted_vals[c] - sorted_vals[f]), 2)


def collect(window_days: int, db_path: str):
    cutoff_ts = time.time() - window_days * 86400
    scores, sources = [], {}

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT timestamp, score FROM decisions WHERE timestamp >= ? "
            "AND score IS NOT NULL AND score > 0",
            (cutoff_ts,),
        ).fetchall()
        vals = [float(r[1]) for r in rows]
        sources["decisions"] = len(vals)
        scores.extend(vals)

        rows = conn.execute(
            "SELECT entry_time, score FROM trade_outcomes WHERE entry_time >= ? "
            "AND score IS NOT NULL AND score > 0",
            (cutoff_ts,),
        ).fetchall()
        vals = [float(r[1]) for r in rows]
        sources["trade_outcomes"] = len(vals)
        scores.extend(vals)

        rows = conn.execute(
            "SELECT created_ts, body FROM notification_outbox "
            "WHERE msg_type='scan_summary' AND created_ts >= ?",
            (cutoff_ts,),
        ).fetchall()
        vals = []
        for _, body in rows:
            for m in SCORE_RE.finditer(body or ""):
                v = m.group(1) or m.group(2)
                if v and 0 < float(v) <= 100:
                    vals.append(float(v))
        sources["scan_summary_regex"] = len(vals)
        scores.extend(vals)
    finally:
        conn.close()
    return scores, sources


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=90)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--db", default=STATE_DB)
    args = ap.parse_args()

    scores, sources = collect(args.window, args.db)
    if not scores:
        print("no scores found in window — nothing to calibrate")
        return 1

    s = sorted(scores)
    calib = {
        "p25": _pct(s, 25),
        "p50": _pct(s, 50),
        "p75": _pct(s, 75),
        "p90": _pct(s, 90),
        "n": len(s),
        "min": round(s[0], 2),
        "max": round(s[-1], 2),
        "window_days": args.window,
        "sources": sources,
        "computed_at": datetime.now().isoformat(timespec="seconds"),
    }

    print(json.dumps(calib, ensure_ascii=False, indent=2))
    print(
        f"\nGREED threshold := P75 = {calib['p75']} "
        f"(strategy_adaptor falls back to 75 when kv absent)"
    )

    if args.write:
        conn = sqlite3.connect(args.db)
        try:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("threshold_calibration", json.dumps(calib, ensure_ascii=False)),
            )
            conn.commit()
            print(f"\nwritten to kv threshold_calibration ({args.db})")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
