"""WO-0924-x: entry frequency governor — pure risk-control layer.

Three gates, learned from the 9/24 12:21-13:11 incident (3 fresh
entries in 50 minutes via FRESH_ENTRY_FALLBACK under mild drawdown,
plus UNI re-entered 15h after its SL exit):

  1. loss-exit cooldown — a symbol that just left with a loss is
     blocked for COOLDOWN_HOURS (default 24h);
  2. daily entry cap — at most DAILY_ENTRY_CAP fresh BUY entries per
     calendar day (counter keyed by local date, natural rollover);
  3. fallback under drawdown — a FRESH_ENTRY_FALLBACK entry (runner-up
     score, taken only because the held best is duplicate-blocked) is
     refused while the sizing multiplier is reduced (<1.0, e.g. mild
     StepwiseDrawdown ×0.7): the fallback exists to not waste a
     signal, not to sweep runners-up against the drawdown.

Pure risk layer: no thresholds, floors or strategy parameters are
touched here. Gates run BEFORE a BUY order is placed; counters are
bumped only after fills/booking (no phantom counts on rejects).

kv layout (StateDB kv store, json values):
  entry_cooldown:<sym> = {"ts": float, "pnl": float}
  entry_count:<date>   = int            # <date> = YYYYMMDD local
  entry_fallback:<sym> = {"ts": float}  # 30-min TTL bridge from the
                                        # research phase to the executor
"""
import logging
import time

logger = logging.getLogger(__name__)

COOLDOWN_HOURS = 24.0
DAILY_ENTRY_CAP = 3
FALLBACK_FLAG_TTL = 30 * 60.0  # one cron round + pending-confirm window


def _db():
    from src.state_db import get_state_db
    return get_state_db()


def _today(now=None):
    return time.strftime("%Y%m%d", time.localtime(now or time.time()))


def check_entry(symbol, *, size_mult=1.0, now=None):
    """Pre-entry gate. Returns dict(ok, reason, gate)."""
    now = now if now is not None else time.time()
    try:
        db = _db()
        cd = db.kv_get("entry_cooldown:" + symbol) or {}
        if cd.get("ts") and now - float(cd["ts"]) < COOLDOWN_HOURS * 3600:
            return {
                "ok": False, "gate": "loss_exit_cooldown",
                "reason": (
                    "ENTRY_BLOCKED: %s left with a loss %.1fh ago "
                    "(pnl %+.2f) — %.1fh cooldown active"
                    % (symbol, (now - float(cd["ts"])) / 3600,
                       float(cd.get("pnl") or 0),
                       COOLDOWN_HOURS)),
            }
        count = int(db.kv_get("entry_count:" + _today(now)) or 0)
        if count >= DAILY_ENTRY_CAP:
            return {
                "ok": False, "gate": "daily_entry_cap",
                "reason": (
                    "ENTRY_BLOCKED: daily fresh-entry cap %d reached "
                    "(entry_count:%s=%d)"
                    % (DAILY_ENTRY_CAP, _today(now), count)),
            }
        flag = db.kv_get("entry_fallback:" + symbol) or {}
        if (flag.get("ts") and now - float(flag["ts"]) <= FALLBACK_FLAG_TTL
                and float(size_mult) < 1.0):
            return {
                "ok": False, "gate": "fallback_under_drawdown",
                "reason": (
                    "ENTRY_BLOCKED: FRESH_ENTRY_FALLBACK entry refused "
                    "under reduced sizing (size_mult=%.2f) — fallback "
                    "exists to not waste a signal, not to sweep "
                    "runners-up in a drawdown" % float(size_mult)),
            }
    except Exception:
        # governor itself must never break trading — fail-open, but loud
        logger.warning("entry_governor: check failed for %s — "
                       "fail-open", symbol, exc_info=True)
    return {"ok": True, "reason": None, "gate": None}


def note_entry(symbol, now=None):
    """Bump the daily counter — call exactly once per FILLED fresh BUY."""
    now = now if now is not None else time.time()
    try:
        db = _db()
        key = "entry_count:" + _today(now)
        db.kv_set(key, int(db.kv_get(key) or 0) + 1)
    except Exception:
        logger.warning("entry_governor: note_entry failed for %s",
                       symbol, exc_info=True)


def note_loss_exit(symbol, pnl, now=None):
    """Stamp the cooldown clock — call when a losing exit is booked."""
    if pnl is None or float(pnl) >= 0:
        return  # winners rotate freely; only losses cool down
    now = now if now is not None else time.time()
    try:
        _db().kv_set("entry_cooldown:" + symbol,
                     {"ts": now, "pnl": float(pnl)})
        logger.info("entry_governor: %s loss exit (pnl %+.4f) — %.0fh "
                    "re-entry cooldown stamped", symbol, float(pnl),
                    COOLDOWN_HOURS)
    except Exception:
        logger.warning("entry_governor: note_loss_exit failed for %s",
                       symbol, exc_info=True)


def note_fallback(symbol, now=None):
    """Bridge: research phase flags a fallback pick; the executor's
    gate reads it within FALLBACK_FLAG_TTL."""
    now = now if now is not None else time.time()
    try:
        _db().kv_set("entry_fallback:" + symbol, {"ts": now})
    except Exception:
        logger.warning("entry_governor: note_fallback failed for %s",
                       symbol, exc_info=True)
