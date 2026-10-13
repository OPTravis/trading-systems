"""WO-1046h (P4 / S6): HMM rolling score — hit-rate downweight + divergence retrain.

Why: the HMM regime detector's verdicts (BEAR_TREND / BULL_TREND / ...) feed
strategy_adaptor risk overlays, but nothing ever scored whether those verdicts
had predictive power on realized PnL. A stale/wrong HMM kept killing trend
strategies through bull legs (the WO-1044 six-week deadlock family). This
module closes the loop:

1.  rolling_hit_rate(window=30d) — direction-consistency between the HMM
    regime verdict in force on entry_date and the realized win/loss of closed
    trades. Bull-ish verdict + win, or bear-ish verdict + loss => hit.
2.  divergence_days — consecutive days where bull_tier == CONFIRMED_BULL
    (BTC SMA gate) while the HMM says BEAR_TREND. >= 10 days forces a
    retrain trigger (should_retrain third condition).
3.  compute_hmm_score() — one-shot summary persisted to kv
    ``hmm_rolling_score`` so the weekly review (and the WO-1046g
    core/satellite enable signal (b)) can read it without re-deriving.

Log-only first (WO-1046h spec ③): kv ``hmm_scoring_config.enabled`` defaults
to False — the scorer records *what it would do* (downweight_would /
retrain_would) without changing live behaviour. Flipping the kv switch
activates the downweight in strategy_adaptor.

Data model (deliberately append-only, no back-mutation of history rows):
-   kv ``hmm_daily_verdicts``: {"YYYY-MM-DD": {"hmm": <regime>, "bull_tier":
    <tier>}} — one row per calendar day, refreshed by every scan (idempotent).
-   trade_outcomes.context_json gains ``hmm_regime`` on NEW entries
    (execute_phases passes adapted["hmm_regime"]); for older rows the daily
    verdict table is the join key via entry_date.

Backfill: scripts/backfill_hmm_verdicts.py mined cron-scan logs for the
2026-10-08..13 window (pre-launch history; earlier logs rotated away).
"""

import json
import logging
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ── kv keys ──────────────────────────────────────────────────────────────
KV_DAILY_VERDICTS = "hmm_daily_verdicts"
KV_ROLLING_SCORE = "hmm_rolling_score"
KV_SCORING_CONFIG = "hmm_scoring_config"

# Regime polarity: which verdicts are "bull-ish" (long-friendly) vs
# "bear-ish" (defensive). Used for the direction-consistency hit test.
_BULLISH = {"BULL_TREND", "RANGE_BOUND"}
_BEARISH = {"BEAR_TREND", "HIGH_VOL"}

# Downweight defaults (kv-overridable). enabled=False = log-only observation.
DEFAULTS = {
    "enabled": False,            # WO-1046h ③: observe first, activate via kv
    "hit_rate_threshold": 0.45,  # below this the HMM verdicts get downweighted
    "downweight_factor": 0.5,    # how much HMM advice shrinks toward neutral
    "min_sample": 10,            # fewer closed samples => no verdict (noise)
    "window_days": 30,
    "divergence_retrain_days": 10,   # >= N consecutive divergence days
    "divergence_retrain_enabled": False,  # log-only until activated
}


def get_config(db) -> Dict:
    """Merged scoring config: DEFAULTS <- kv override (hot-tunable)."""
    cfg = dict(DEFAULTS)
    try:
        raw = db.kv_get(KV_SCORING_CONFIG)
        if isinstance(raw, dict):
            for k in DEFAULTS:
                if k in raw:
                    v = raw[k]
                    if isinstance(DEFAULTS[k], bool):
                        cfg[k] = bool(v)
                    elif isinstance(DEFAULTS[k], (int, float)) and not isinstance(DEFAULTS[k], bool):
                        try:
                            cfg[k] = type(DEFAULTS[k])(v)
                        except (TypeError, ValueError):
                            pass
                    else:
                        cfg[k] = v
    except Exception:
        logger.warning("hmm_scoring: config read failed, using defaults",
                       exc_info=True)
    return cfg


# ── daily verdict ledger ─────────────────────────────────────────────────

def record_daily_verdict(db, bull_tier: Optional[str], hmm_regime: Optional[str],
                         date_str: Optional[str] = None) -> None:
    """Idempotently record today's (bull_tier, hmm_regime) verdict pair.

    Called from every scan; repeated calls on the same day keep the freshest
    verdict for that date (the last scan of the day wins — matches how the
    HMM re-predicts on new data).
    """
    if not bull_tier and not hmm_regime:
        return
    if date_str is None:
        date_str = time.strftime("%Y-%m-%d", time.localtime())
    try:
        ledger = db.kv_get(KV_DAILY_VERDICTS)
        if not isinstance(ledger, dict):
            ledger = {}
        today = ledger.get(date_str) or {}
        if bull_tier:
            today["bull_tier"] = bull_tier
        if hmm_regime:
            today["hmm"] = hmm_regime
        ledger[date_str] = today
        # cap the ledger at 400 days — rolling window never needs more
        if len(ledger) > 400:
            for d in sorted(ledger)[:-400]:
                ledger.pop(d, None)
        db.kv_set(KV_DAILY_VERDICTS, ledger)
    except Exception:
        logger.warning("hmm_scoring.record_daily_verdict failed", exc_info=True)


def _load_verdicts(db) -> Dict[str, Dict]:
    ledger = db.kv_get(KV_DAILY_VERDICTS)
    return ledger if isinstance(ledger, dict) else {}


# ── rolling hit rate ─────────────────────────────────────────────────────

def _verdict_for_date(verdicts: Dict[str, Dict], date_str: str) -> Optional[str]:
    row = verdicts.get(date_str)
    if row and row.get("hmm"):
        return str(row["hmm"]).upper()
    return None


def rolling_hit_rate(db, window_days: int = 30,
                     now: Optional[float] = None) -> Dict:
    """Direction-consistency of the HMM verdict vs realized closed-trade PnL.

    Hit = entry-day HMM verdict is bull-ish (BULL_TREND/RANGE_BOUND) and the
    trade won, OR bear-ish (BEAR_TREND/HIGH_VOL) and it lost. Entries with no
    verdict coverage (missing daily ledger row / context tag) are skipped,
    not counted as misses.

    Returns {"hit_rate": float|None, "sample_size": int, "window_days": int,
             "hits": int}. hit_rate is None when there are zero samples
    (caller applies min_sample from config).
    """
    now = now if now is not None else time.time()
    cutoff = now - window_days * 86400
    verdicts = _load_verdicts(db)
    hits = 0
    samples = 0
    try:
        rows = db.outcomes_get_closed(limit=500)
    except Exception:
        logger.warning("hmm_scoring.rolling_hit_rate: outcomes read failed",
                       exc_info=True)
        return {"hit_rate": None, "sample_size": 0, "window_days": window_days,
                "hits": 0}

    for r in rows:
        exit_ts = r.get("exit_time") or r.get("updated_at") or 0
        if not exit_ts or exit_ts < cutoff:
            continue
        # Prefer the per-entry context tag (new rows); fall back to the
        # daily ledger joined on entry_date (backfilled / older rows).
        hmm = None
        try:
            ctx = json.loads(r.get("context_json") or "{}")
            hmm = (ctx.get("hmm_regime") or "").upper() or None
        except Exception:
            pass
        if not hmm:
            hmm = _verdict_for_date(verdicts, r.get("entry_date") or "")
        if not hmm:
            continue
        if hmm not in _BULLISH and hmm not in _BEARISH:
            continue
        is_win = bool(r.get("is_win"))
        bull_ish = hmm in _BULLISH
        if bull_ish == is_win:
            hits += 1
        samples += 1

    hit_rate = (hits / samples) if samples else None
    return {"hit_rate": hit_rate, "sample_size": samples,
            "window_days": window_days, "hits": hits}


# ── divergence tracking ──────────────────────────────────────────────────

def divergence_days(db, now: Optional[float] = None) -> Dict:
    """Consecutive calendar days of HMM×bull_regime divergence.

    Divergence (WO-1044-A conflict definition): bull_tier == CONFIRMED_BULL
    while HMM regime == BEAR_TREND. Counts the trailing run ending on the
    most recent ledger day; a non-divergent day breaks the streak.

    Returns {"divergence_days": int, "first_date": str|None,
             "last_date": str|None}.
    """
    ledger = _load_verdicts(db)
    if not ledger:
        return {"divergence_days": 0, "first_date": None, "last_date": None}

    def _is_divergent(day: Dict) -> bool:
        bull = (day.get("bull_tier") or "").upper()
        hmm = (day.get("hmm") or "").upper()
        return bull == "CONFIRMED_BULL" and hmm == "BEAR_TREND"

    days = sorted(ledger)
    streak = 0
    first = None
    for d in reversed(days):
        if _is_divergent(ledger[d]):
            streak += 1
            first = d
        else:
            break
    return {
        "divergence_days": streak,
        "first_date": first,
        "last_date": days[-1] if days else None,
    }


# ── downweight application (strategy_adaptor hook) ───────────────────────

def apply_downweight(db, hmm_adjustments: Dict, hit_rate: Optional[float],
                     cfg: Optional[Dict] = None) -> Dict:
    """Shrink HMM overlay advice toward neutral by ``downweight_factor``.

    Semantics — every HMM recommendation contracts proportionally toward the
    "no advice" anchor:
      * score_threshold_adj: adj * factor          (BEAR +10 -> +5)
      * position_scale (preferred): 1 + (scale-1) * factor   (1.2 -> 1.1)
      * avoid_strategies: flagged ``avoid_downgraded_to_half_size`` — the
        caller (strategy_adaptor) downgrades the hard disable to half size,
        mirroring the WO-1044-A arbitration precedent (a low-hit-rate HMM
        has not earned the kill vote).

    Only mutates adjustments when cfg.enabled AND hit_rate is not None.
    Returns {"adjustments": dict, "audit": {...}}.
    """
    cfg = cfg or get_config(db)
    factor = float(cfg.get("downweight_factor", 0.5))
    out = dict(hmm_adjustments)
    audit = {"applied": False, "factor": factor}

    if not cfg.get("enabled"):
        # log-only: report what WOULD happen, change nothing
        audit["would"] = True
        return {"adjustments": out, "audit": audit}

    if hit_rate is None:
        return {"adjustments": out, "audit": audit}

    audit["applied"] = True
    if "score_threshold_adj" in out and out["score_threshold_adj"]:
        out["score_threshold_adj"] = int(round(
            out["score_threshold_adj"] * factor))
    scale = out.get("position_scale", 1.0)
    if isinstance(scale, (int, float)) and scale != 1.0:
        out["position_scale"] = round(1 + (scale - 1) * factor, 3)
    if out.get("avoid_strategies"):
        out["avoid_downgraded_to_half_size"] = True
    return {"adjustments": out, "audit": audit}


# ── one-shot score (weekly-review / enable-signal source) ───────────────

def compute_hmm_score(db, now: Optional[float] = None,
                      persist: bool = True) -> Dict:
    """Full HMM health snapshot; persisted to kv ``hmm_rolling_score``.

    Fields the weekly review reads (and the WO-1046g core/satellite enable
    signal (b) binds to): hit_rate / sample_size / downweight_would /
    divergence_days / retrain_would.
    """
    cfg = get_config(db)
    window = int(cfg.get("window_days", 30))
    hr = rolling_hit_rate(db, window_days=window, now=now)
    div = divergence_days(db, now=now)

    threshold = float(cfg.get("hit_rate_threshold", 0.45))
    min_sample = int(cfg.get("min_sample", 10))
    hr_val = hr["hit_rate"]
    enough = hr["sample_size"] >= min_sample

    downweight_active = bool(
        cfg.get("enabled") and hr_val is not None and enough
        and hr_val < threshold)
    downweight_would = bool(
        hr_val is not None and enough and hr_val < threshold)

    retrain_days = int(cfg.get("divergence_retrain_days", 10))
    retrain_would = div["divergence_days"] >= retrain_days
    retrain_active = bool(cfg.get("divergence_retrain_enabled") and retrain_would)

    score = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(
            now if now is not None else time.time())),
        "hit_rate": hr_val,
        "sample_size": hr["sample_size"],
        "window_days": window,
        "hit_rate_threshold": threshold,
        "min_sample": min_sample,
        "downweight_would": downweight_would,
        "downweight_active": downweight_active,
        "enabled": bool(cfg.get("enabled")),
        "divergence_days": div["divergence_days"],
        "divergence_first_date": div["first_date"],
        "divergence_retrain_days": retrain_days,
        "retrain_would": retrain_would,
        "retrain_active": retrain_active,
    }
    if persist:
        try:
            db.kv_set(KV_ROLLING_SCORE, score)
        except Exception:
            logger.warning("hmm_scoring: score persist failed", exc_info=True)

    if downweight_would:
        mode = "ACTIVE" if downweight_active else "WOULD (log-only)"
        logger.info(
            "HMM downweight %s: hit_rate=%.3f < %.2f over %d samples "
            "(factor=%.2f)",
            mode, hr_val, threshold, hr["sample_size"],
            cfg.get("downweight_factor", 0.5))
    if retrain_would:
        mode = "ACTIVE" if retrain_active else "WOULD (log-only)"
        logger.info(
            "HMM divergence retrain %s: %d consecutive days "
            "(>= %d, since %s)",
            mode, div["divergence_days"], retrain_days,
            div["first_date"] or "?")
    return score
