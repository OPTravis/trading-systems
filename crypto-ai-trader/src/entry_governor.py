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

# WO-1053: NEUTRAL-regime fresh-entry rate cap. Rolling 24h window (not
# calendar-day — the 10/13-14 wave evidence is a 3.3-entries/day pace
# crossing midnight), stacked with the calendar-day cap above; the
# stricter one binds. BULL-side buckets stay on the global 3/day only.
NEUTRAL_ENTRY_CAP = 2
NEUTRAL_WINDOW_S = 24 * 3600.0
_MARKET_REGIME_KEY = "gov:market_regime"      # scan-chain snapshot dict
_NEUTRAL_ENTRIES_KEY = "gov:neutral_entries"  # [epoch_ts, ...] tagged fills


def _db():
    from src.state_db import get_state_db
    return get_state_db()


def _kv_get_migrated(key_new, key_old):
    """WO-0924-z 1A-1: read new gov: key, fall back to the legacy
    spelling once and self-heal the migration in place."""
    db = _db()
    val = db.kv_get(key_new)
    if val is None:
        val = db.kv_get(key_old)
        if val is not None:
            db.kv_set(key_new, val)
            try:
                db.kv_remove(key_old)
            except Exception:
                pass
    return val


def _today(now=None):
    return time.strftime("%Y%m%d", time.localtime(now or time.time()))


def _market_regime(db, regime_hint=None):
    """WO-1053: the market regime an entry decision is being made under.

    Primary source: the executor-threaded value — exactly the F&G bucket
    this round's threshold was adapted under (StrategyAdaptor.
    _determine_regime: <=60 NEUTRAL / <80 GREED / ...), i.e. the regime
    notion the 10/13-14 stop-loss wave evidence used (all five failed
    entries recorded context regime NEUTRAL at F&G 57). Fallback: the
    scan chain's persisted snapshot (kv gov:market_regime, refreshed
    every round).

    Deliberately NOT bull_regime: during the wave bull_regime sat in
    MILD_BULL (10/13 14:53) / CONFIRMED_BULL (10/14 00:12) and has not
    visited NEUTRAL since 9/21 — a bull_regime==NEUTRAL gate would have
    blocked none of the five failed entries.

    Returns upper-case label, or None when unknown (gate skips — the
    governor's fail-open contract).
    """
    if regime_hint:
        r = str(regime_hint).strip().upper()
        return r or None
    try:
        snap = db.kv_get(_MARKET_REGIME_KEY) or {}
        r = str(snap.get("regime") or "").strip().upper()
        return r or None
    except Exception:
        return None


def _neutral_window_count(db, now):
    """NEUTRAL-tagged fresh entries inside the rolling 24h window.

    kv-tagged list first, then the cold-start backstop rebuilt from
    trade_outcomes' recorded entry regimes (the kv list starts empty
    whenever the governor is redeployed; real same-window entries keep
    the cap honest — same over-block contract as the daily cap's
    trades floor)."""
    cutoff = now - NEUTRAL_WINDOW_S
    kv_count = 0
    try:
        raw = db.kv_get(_NEUTRAL_ENTRIES_KEY) or []
        ts_list = []
        for x in raw:
            try:
                ts_list.append(float(x))
            except (TypeError, ValueError):
                continue
        kv_count = len([t for t in ts_list if t > cutoff])
    except Exception:
        kv_count = 0
    backstop = 0
    try:
        for row in db.outcomes_regime_since(cutoff):
            if row["regime"] == "NEUTRAL":
                backstop += 1
    except Exception:
        backstop = 0
    return max(kv_count, backstop)


def check_entry(symbol, *, size_mult=1.0, now=None, market_regime=None):
    """Pre-entry gate. Returns dict(ok, reason, gate)."""
    now = now if now is not None else time.time()
    try:
        db = _db()
        cd = _kv_get_migrated("gov:cooldown:" + symbol,
                              "entry_cooldown:" + symbol) or {}
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
        count = int(_kv_get_migrated(
            "gov:entry_count:" + _today(now),
            "entry_count:" + _today(now)) or 0)
        # WO-0924-z P1-2: cold-start backstop — the kv counter starts
        # empty whenever the governor first goes live (or is rede-
        # ployed); real same-day BUYs booked in trades keep the cap
        # honest. Conservative: DCA adds count too (over-block, never
        # under-block).
        try:
            import datetime as _dt
            _day0 = _dt.datetime.fromtimestamp(
                now).replace(hour=0, minute=0, second=0,
                             microsecond=0).timestamp()
            # P4: StateDB trades-store reader (SQL moved verbatim)
            count = max(count, db.trades_count_buys_since(_day0))
        except Exception:
            pass  # trades unreadable — fall back to kv counter
        if count >= DAILY_ENTRY_CAP:
            return {
                "ok": False, "gate": "daily_entry_cap",
                "reason": (
                    "ENTRY_BLOCKED: daily fresh-entry cap %d reached "
                    "(entry_count:%s=%d)"
                    % (DAILY_ENTRY_CAP, _today(now), count)),
            }
        # WO-1053: NEUTRAL-regime fresh-entry rate cap (rolling 24h x
        # 2 entries). Stacks with the calendar-day cap above — whichever
        # is stricter binds. Other buckets (GREED/EXTREME_GREED, FEAR
        # side) are unaffected: the wave evidence is specific to the
        # NEUTRAL F&G bucket (5 failed entries at F&G 57, 83% failure).
        if _market_regime(db, market_regime) == "NEUTRAL":
            n = _neutral_window_count(db, now)
            if n >= NEUTRAL_ENTRY_CAP:
                return {
                    "ok": False, "gate": "neutral_entry_cap",
                    "reason": (
                        "ENTRY_BLOCKED: neutral fresh-entry cap %d "
                        "reached (%d NEUTRAL entries in last 24h)"
                        % (NEUTRAL_ENTRY_CAP, n)),
                }
        flag = _kv_get_migrated("gov:fallback:" + symbol,
                                "entry_fallback:" + symbol) or {}
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


def note_entry(symbol, now=None, market_regime=None):
    """Bump the daily counter — call exactly once per FILLED fresh BUY.

    WO-1053: when the fill happened under the NEUTRAL market regime,
    also tag its timestamp into the rolling NEUTRAL window so the
    check_entry gate above counts it (BULL/FEAR-side fills never enter
    the window — regime switching mid-window is handled by the tag,
    not by the current snapshot)."""
    now = now if now is not None else time.time()
    try:
        db = _db()
        key = "gov:entry_count:" + _today(now)
        db.kv_set(key, int(_kv_get_migrated(
            key, "entry_count:" + _today(now)) or 0) + 1)
    except Exception:
        logger.warning("entry_governor: note_entry failed for %s",
                       symbol, exc_info=True)
    try:
        db = _db()
        if _market_regime(db, market_regime) != "NEUTRAL":
            return
        raw = db.kv_get(_NEUTRAL_ENTRIES_KEY) or []
        ts_list = []
        for x in raw:
            try:
                ts_list.append(float(x))
            except (TypeError, ValueError):
                continue
        ts_list = [t for t in ts_list if t > now - NEUTRAL_WINDOW_S]
        ts_list.append(float(now))
        del ts_list[:-64]  # hard bound; window prune keeps it tiny
        db.kv_set(_NEUTRAL_ENTRIES_KEY, ts_list)
        logger.info("entry_governor: %s fresh entry tagged NEUTRAL "
                    "(%d in rolling 24h window, cap %d)", symbol,
                    len(ts_list), NEUTRAL_ENTRY_CAP)
    except Exception:
        logger.warning("entry_governor: note_entry NEUTRAL tag failed "
                       "for %s", symbol, exc_info=True)


def note_loss_exit(symbol, pnl, now=None):
    """Stamp the cooldown clock — call when a losing exit is booked."""
    if pnl is None or float(pnl) >= 0:
        return  # winners rotate freely; only losses cool down
    now = now if now is not None else time.time()
    try:
        _db().kv_set("gov:cooldown:" + symbol,
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
        _db().kv_set("gov:fallback:" + symbol, {"ts": now})
    except Exception:
        logger.warning("entry_governor: note_fallback failed for %s",
                       symbol, exc_info=True)
