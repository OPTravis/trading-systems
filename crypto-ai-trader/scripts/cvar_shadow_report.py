#!/usr/bin/env python3
"""
CVaR overlay stage-1 shadow report (P7 tail batch).

Reads kv cvar:shadow_log — the rolling observation log written by
StrategyAdaptor.adapt via CVaRRiskManager.log_shadow_observation — and
prints the stage-1 comparison table: what position_scale WOULD have
been if the CVaR overlay were active, vs live behaviour (scale pinned
at 1.0, zero mainline change).

Usage:
    python3 scripts/cvar_shadow_report.py [--db PATH] [--json]

Pure read-only observation tool — stage 2 activation awaits Leo's
ruling; this script never writes anything.
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
DEFAULT_DB = "/root/trading-state/state.db"
SHADOW_LOG_KEY = "cvar:shadow_log"


def load_log(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM kv WHERE key = ?", (SHADOW_LOG_KEY,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return []
    try:
        val = json.loads(row[0])
    except json.JSONDecodeError:
        print(f"error: kv {SHADOW_LOG_KEY} is not valid JSON", file=sys.stderr)
        return []
    return val if isinstance(val, list) else []


def _pct(sorted_vals, q):
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def _fmt(v):
    return "?" if v is None else f"{v:g}"


def build_stats(log):
    scales = sorted(
        e["scale_if_active"] for e in log
        if isinstance(e.get("scale_if_active"), (int, float))
    )
    levels = {}
    for e in log:
        lv = e.get("risk_level") or "unknown"
        levels[lv] = levels.get(lv, 0) + 1
    ts = [e["ts"] for e in log if e.get("ts")]
    n_down = sum(1 for s in scales if s < 1.0)
    n_up = sum(1 for s in scales if s > 1.0)
    avg = sum(scales) / len(scales) if scales else None
    return {
        "n_observations": len(log),
        "n_with_scale": len(scales),
        "first_ts": min(ts) if ts else None,
        "last_ts": max(ts) if ts else None,
        "scale_min": scales[0] if scales else None,
        "scale_p25": _pct(scales, 0.25),
        "scale_median": _pct(scales, 0.50),
        "scale_p75": _pct(scales, 0.75),
        "scale_max": scales[-1] if scales else None,
        "scale_avg": avg,
        "avg_position_delta_pct": (avg - 1.0) * 100 if avg is not None else None,
        "n_scale_down": n_down,
        "n_scale_up": n_up,
        "pct_scale_down": 100.0 * n_down / len(scales) if scales else None,
        "pct_scale_up": 100.0 * n_up / len(scales) if scales else None,
        "risk_levels": levels,
        "recent": log[-5:][::-1],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB,
                    help=f"path to state.db (default {DEFAULT_DB})")
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON instead of a table")
    args = ap.parse_args()

    log = load_log(args.db)
    if not log:
        print(f"no shadow observations in kv {SHADOW_LOG_KEY} ({args.db})")
        return 0

    s = build_stats(log)

    if args.json:
        print(json.dumps(s, indent=2, ensure_ascii=False))
        return 0

    span = ""
    if s["first_ts"] and s["last_ts"]:
        span = (f"{datetime.fromtimestamp(s['first_ts'], CST):%m-%d %H:%M}"
                f" ~ {datetime.fromtimestamp(s['last_ts'], CST):%m-%d %H:%M} CST")
    print("== CVaR overlay stage-1 shadow report ==")
    print(f"observations : {s['n_observations']}  ({span})")
    print(f"scale if active  min/p25/med/p75/max: "
          f"{_fmt(s['scale_min'])} / {_fmt(s['scale_p25'])} / "
          f"{_fmt(s['scale_median'])} / {_fmt(s['scale_p75'])} / "
          f"{_fmt(s['scale_max'])}   (avg {_fmt(s['scale_avg'])})")
    print(f"would scale DOWN (<1.0): {s['n_scale_down']} "
          f"({s['pct_scale_down']:.1f}% of observations)"
          if s["pct_scale_down"] is not None else "would scale DOWN: n/a")
    print(f"would scale UP   (>1.0): {s['n_scale_up']} "
          f"({s['pct_scale_up']:.1f}% of observations)"
          if s["pct_scale_up"] is not None else "would scale UP: n/a")
    if s["avg_position_delta_pct"] is not None:
        print(f"avg position delta if activated: "
              f"{s['avg_position_delta_pct']:+.2f}%")
    print(f"risk levels: {s['risk_levels']}")
    print("\nrecent observations (newest first):")
    for e in s["recent"]:
        t = datetime.fromtimestamp(e.get("ts", 0), CST).strftime("%m-%d %H:%M:%S")
        print(f"  {t}  scale_if_active={_fmt(e.get('scale_if_active'))}"
              f"  level={e.get('risk_level')}"
              f"  cvar_95={_fmt(e.get('cvar_95'))}"
              f"  n_pos={e.get('n_positions')}  n_samples={e.get('n_samples')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
