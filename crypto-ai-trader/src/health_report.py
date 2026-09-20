"""System health self-report — P0-1 (設計 v1.1 §五簡版).

Three-tier disposition on top of per-round signals:
  L1 self-healed, silent — rate-limit backoffs, transient network retries.
     Only counted, never alerted (weekly digest material).
  L2 degraded, announced — API reachable but something is off (filter-read
     failures, rising 429 pressure). live_alerts SYSTEM_DEGRADED; no human
     action required.
  L3 capital-safety, escalated — unprotectable-and-unliquidatable positions,
     repeated liquidation failures, persistent API outage. Stops nothing by
     itself (drawdown breaker owns the kill), but ALWAYS alerts
     SYSTEM_ESCALATED for human eyes.

Inputs: dust_reaper summary (per round) + binance client rate stats
(_binance_sdk_client.RATE_STATS) + a live get_account ping.
Fail-safe: never raises into the scan pipeline.
"""

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# thresholds (module-level for tests)
FILTER_FAIL_L2 = 2          # ≥2 symbols failing filter reads → L2
RATE_429_L2 = 10            # ≥10 rate-limit events since boot → L2
LIQ_FAIL_L3 = 2             # ≥2 liquidation failures in one round → L3
UNPROTECTABLE_L3 = 1        # ≥1 unprotectable+unsellable (watch) position → L3 note
KV_LAST_ESC_TS = "health_last_escalated_ts"
KV_LAST_DEG_TS = "health_last_degraded_ts"
ESCALATE_THROTTLE_S = 4 * 3600   # L3 re-alert at most every 4h
DEGRADE_THROTTLE_S = 4 * 3600    # L2 same


def _emit(event_type: str, details: Dict[str, Any]) -> None:
    try:
        from src.live_alerts import emit as emit_alert
        emit_alert(event_type, "SYSTEM", details)
    except Exception:
        pass


def _throttled(db, key: str, throttle_s: float) -> bool:
    if db is None:
        return False
    try:
        last = float(db.kv_get(key, 0) or 0)
        if time.time() - last < throttle_s:
            return False
        db.kv_set(key, time.time())
        return True
    except Exception:
        return False


def run(client, portfolio, dust_summary: Optional[Dict[str, Any]] = None,
        log: Optional[logging.Logger] = None) -> Dict[str, Any]:
    """One health round. Returns {level: 'L0'|'L1'|'L2'|'L3', signals: {...}}."""
    log = log or logger
    dust = dust_summary or {}
    db = getattr(portfolio, "_db", None)

    signals: Dict[str, Any] = {
        "api_ok": True,
        "rate_events": {},
        "filter_failures": int(dust.get("filter_failures", 0)),
        "liquidation_failures": int(dust.get("liquidation_failures", 0)),
        "watch_positions": int(dust.get("watch", 0)),
        "unprotected_candidates": int(dust.get("liquidate_candidates", 0)),
    }

    # rate-limit accounting from the SDK client (if present)
    try:
        from src import _binance_sdk_client as _sdk
        signals["rate_events"] = dict(getattr(_sdk, "RATE_STATS", {}) or {})
    except Exception:
        pass

    # API liveness ping
    try:
        acct = client.get_account()
        if not acct or not acct.get("balances"):
            signals["api_ok"] = False
    except Exception:
        signals["api_ok"] = False
        log.warning("health_report: get_account ping failed", exc_info=True)

    total_rate = sum(signals["rate_events"].values())

    # --- classify ----------------------------------------------------------
    l3_reasons = []
    l2_reasons = []

    if not signals["api_ok"]:
        l3_reasons.append("api_down")
    if signals["liquidation_failures"] >= LIQ_FAIL_L3:
        l3_reasons.append("liquidation_failures=%d" % signals["liquidation_failures"])
    if l3_reasons:
        level = "L3"
    elif signals["filter_failures"] >= FILTER_FAIL_L2:
        l2_reasons.append("filter_failures=%d" % signals["filter_failures"])
        level = "L2"
    elif total_rate >= RATE_429_L2:
        l2_reasons.append("rate_events=%d" % total_rate)
        level = "L2"
    elif total_rate > 0:
        level = "L1"   # self-healed backoffs — silent
    else:
        level = "L0"

    signals["l2_reasons"] = l2_reasons
    signals["l3_reasons"] = l3_reasons

    # --- notify ------------------------------------------------------------
    if level == "L2" and _throttled(db, KV_LAST_DEG_TS, DEGRADE_THROTTLE_S):
        _emit("SYSTEM_DEGRADED", {"level": level, "signals": signals})
        log.warning("health_report: L2 DEGRADED — %s", "; ".join(l2_reasons))
    elif level == "L3" and _throttled(db, KV_LAST_ESC_TS, ESCALATE_THROTTLE_S):
        _emit("SYSTEM_ESCALATED", {"level": level, "signals": signals})
        log.error("health_report: L3 ESCALATED — %s", "; ".join(l3_reasons))
    elif level == "L3":
        # throttled but still capital-safety → log every round regardless
        log.error("health_report: L3 persisting — %s", "; ".join(l3_reasons))

    # watch positions are an L3 *note* (unprotectable & unsellable capital),
    # reported without escalating the whole system to L3
    if signals["watch_positions"] > 0 and _throttled(
        db, KV_LAST_ESC_TS + "_watch", ESCALATE_THROTTLE_S
    ):
        _emit(
            "SYSTEM_ESCALATED",
            {"level": "L3-note", "reason": "unprotectable_unsellable_watch",
             "watch_positions": signals["watch_positions"]},
        )

    return {"level": level, "signals": signals}
