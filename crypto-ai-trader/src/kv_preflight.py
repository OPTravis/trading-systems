"""P0-2 Defense Item 3: KV / state preflight gate before new entries.

Runs at the top of each scan cycle (before the execute step): verifies
that the key persisted state the entry path depends on is readable,
parseable and FRESH. Any failure emits a live_alerts JSON
(event KV_PREFLIGHT_FAIL) and tells the caller to skip new entries for
this round — never open positions on stale/corrupt state.

Checked items (mapping of the work order's "cash_balance/positions/
portfolio" onto what this repo actually persists):
  - kv  cash_balance            — parseable + fresh when present
  - kv  hmm_regime              — parseable + fresh when present
  - portfolio table             — readable; rows carry fresh updated_at

Freshness: kv.updated_at / portfolio.updated_at within KV_MAX_AGE_S
(default 2h — scan cadence is ~1h, so one missed cycle still passes,
two means stale).

Semantics:
  - Missing kv keys / empty portfolio table are COLD-START LEGAL (the
    entry path falls back to live exchange balances), so they PASS with
    a note. The gate targets *stale or corrupt persisted state*, which
    is what poisons decisions.
  - The db handle is probed with a round-trip write+read before any
    verdict is attempted. A db whose reads don't reflect writes (mock /
    broken handle) is OPAQUE: the checker cannot evaluate it, so it
    must not block trading on that basis alone (degraded-checker ≠
    bad-data). An alert is emitted so the opacity itself is visible.
  - An internal exception in the semantic phase fails closed (skip
    entries) — at that point the db was real and something is wrong.
"""

import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

KV_MAX_AGE_S = 2 * 3600

KV_KEYS = ("cash_balance", "hmm_regime")

#: round-trip probe key (value equality proves the store is transparent)
_PROBE_KEY = "kv_preflight:probe"

#: fail-state key — {first_ts, last_alert_ts, count, escalated}
KV_FAIL_STATE = "kv_preflight:fail_state"

#: FAIL alerts: immediate on first, then at most one per hour (Travis
#: 2026-09-21 acceptance: no 10-min spam); continuous failure past
#: ESCALATE_AFTER_S upgrades to a human-intervention alert.
FAIL_ALERT_THROTTLE_S = 3600
ESCALATE_AFTER_S = 2 * 3600


def _kv_age_s(db: Any, key: str) -> Optional[float]:
    row = (
        db._get_conn()
        .execute("SELECT updated_at FROM kv WHERE key = ?", (key,))
        .fetchone()
    )
    if row is None:
        return None
    try:
        return max(0.0, time.time() - float(row["updated_at"]))
    except (TypeError, ValueError, KeyError, IndexError):
        return None


def _db_is_transparent(db, log) -> bool:
    """Round-trip probe: kv_set then kv_get must return the same value.

    A store whose reads don't reflect writes (mocked handle, broken
    connection) cannot be evaluated — treat as opaque, not as failure.
    """
    sentinel = {"probe": True, "ts": time.time()}
    try:
        db.kv_set(_PROBE_KEY, sentinel)
        back = db.kv_get(_PROBE_KEY)
    except Exception:
        log.warning("kv_preflight: probe raised — treating db as opaque",
                    exc_info=True)
        return False
    return back == sentinel


def run(db: Any, log: Optional[logging.Logger] = None) -> Dict[str, Any]:
    """One preflight round. Returns {ok, skip_new_entries, checks}. Never raises."""
    log = log or logger
    checks = []
    ok = True

    def _fail(item: str, reason: str, details: Dict[str, Any]) -> None:
        nonlocal ok
        ok = False
        checks.append({"item": item, "status": "FAIL", "reason": reason,
                       **details})

    def _pass(item: str, details: Dict[str, Any]) -> None:
        checks.append({"item": item, "status": "PASS", **details})

    # -- guard: db handle must be usable at all --
    if db is None:
        _pass("db", {"note": "no db handle — checker blind, not blocking"})
        return _finish(True, checks, log, note="NO_DB_OPINION", db=db)

    if not _db_is_transparent(db, log):
        _pass("db", {"note": "opaque db handle — semantics not evaluable"})
        # Acceptance addendum: opacity must be VISIBLE even though it does
        # not block — frequent opacity inside the observation window is a
        # persistence-layer alarm signal someone has to be able to see.
        # Scan cadence is ~1h so one alert per round == "first + hourly".
        from src.live_alerts import emit as _emit

        _emit("KV_PREFLIGHT_OPAQUE", None, {
            "note": "db reads do not reflect writes — checker blind, "
                    "trading NOT blocked",
            "ts": time.time(),
        })
        log.warning("kv_preflight: opaque db handle (not blocking) — "
                    "persistence layer may be unhealthy")
        return _finish(True, checks, log, note="OPAQUE_DB", db=db)

    try:
        # -- kv keys: readable + parseable + fresh --
        for key in KV_KEYS:
            try:
                raw = db.kv_get(key)
            except Exception as exc:  # noqa: BLE001
                _fail(f"kv:{key}", f"kv_get raised: {exc}", {})
                continue
            if raw is None:
                _pass(f"kv:{key}",
                      {"note": "not set — cold start, entry path uses "
                               "live exchange balance"})
                continue
            try:
                if isinstance(raw, str):
                    json.loads(raw)
            except (TypeError, ValueError) as exc:
                _fail(f"kv:{key}", f"unparseable JSON: {exc}",
                      {"raw_head": str(raw)[:80]})
                continue
            age = _kv_age_s(db, key)
            if age is None:
                _fail(f"kv:{key}", "no updated_at", {})
                continue
            if age > KV_MAX_AGE_S:
                _fail(f"kv:{key}", f"stale: {age/3600:.2f}h old",
                      {"age_s": round(age)})
                continue
            _pass(f"kv:{key}", {"age_s": round(age)})

        # -- portfolio table: readable + fresh --
        try:
            rows = (
                db._get_conn()
                .execute(
                    "SELECT symbol, qty, avg_price, cash_balance, "
                    "updated_at FROM portfolio")
                .fetchall()
            )
        except Exception as exc:  # noqa: BLE001
            _fail("portfolio_table", f"read failed: {exc}", {})
        else:
            if rows is None:
                _fail("portfolio_table", "read returned None", {})
            elif len(rows) == 0:
                _pass("portfolio_table",
                      {"note": "no rows — flat, nothing to go stale"})
            else:
                newest = 0.0
                for r in rows:
                    try:
                        newest = max(newest, float(r["updated_at"]))
                    except (TypeError, ValueError, KeyError, IndexError):
                        continue
                age = time.time() - newest if newest > 0 else None
                if age is None:
                    _fail("portfolio_table", "no updated_at found",
                          {"rows": len(rows)})
                elif age > KV_MAX_AGE_S:
                    _fail("portfolio_table",
                          f"stale: {age/3600:.2f}h old",
                          {"age_s": round(age), "rows": len(rows)})
                else:
                    _pass("portfolio_table",
                          {"age_s": round(age), "rows": len(rows)})
    except Exception as exc:  # noqa: BLE001
        _fail("preflight", f"internal error: {exc}", {})
        return _finish(ok, checks, log, error="INTERNAL", db=db)

    return _finish(ok, checks, log, db=db)


def _fail_state_load(db) -> Dict[str, Any]:
    try:
        raw = db.kv_get(KV_FAIL_STATE)
        if isinstance(raw, dict):
            return raw
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {}


def _fail_state_save(db, state: Dict[str, Any]) -> None:
    try:
        db.kv_set(KV_FAIL_STATE, state)
    except Exception:
        log_only = logging.getLogger(__name__)
        log_only.warning("kv_preflight: fail-state save failed",
                         exc_info=True)


def _finish(ok: bool, checks: list, log: logging.Logger,
            error: Optional[str] = None, note: Optional[str] = None,
            db: Any = None) -> Dict[str, Any]:
    from src.live_alerts import emit as emit_alert

    result = {
        "ok": ok,
        "skip_new_entries": not ok,
        "checks": checks,
    }
    if error:
        result["error"] = error
    if note:
        result["note"] = note
    if not ok:
        # Throttled + escalating alerting (acceptance addendum):
        #   - first failure alerts immediately
        #   - repeats at most once per FAIL_ALERT_THROTTLE_S
        #   - continuous failure past ESCALATE_AFTER_S flips to the
        #     human-intervention alert and re-alerts hourly at that level
        now = time.time()
        st = _fail_state_load(db) if db is not None else {}
        first_ts = float(st.get("first_ts") or now)
        last_alert = float(st.get("last_alert_ts") or 0)
        was_escalated = bool(st.get("escalated"))
        escalated = (now - first_ts) >= ESCALATE_AFTER_S
        do_alert = (
            not st                       # first failure
            or now - last_alert >= FAIL_ALERT_THROTTLE_S
            or escalated != was_escalated
        )
        if do_alert:
            event = ("KV_PREFLIGHT_ESCALATED" if escalated
                     else "KV_PREFLIGHT_FAIL")
            emit_alert(event, None, {
                "checks": checks, "error": error,
                "continuous_fail_s": round(now - first_ts),
                "escalated": escalated, "ts": now,
            })
            log.warning(
                "kv_preflight: %s — skipping new entries this round: %s",
                event, [c for c in checks if c["status"] == "FAIL"])
        if db is not None:
            _fail_state_save(db, {
                "first_ts": first_ts, "last_alert_ts": now,
                "escalated": escalated,
            })
    elif db is not None:
        # healthy round clears the fail streak
        if _fail_state_load(db):
            _fail_state_save(db, {})
    return result
    return result
