"""
BULL regime detector — production version of the walk-forward state machine.

5 states:
  DEEP_BEAR  — BTC < SMA200*0.85 or F&G 7d avg < 25
  FEAR       — F&G crash exit / bearish but not deep
  NEUTRAL    — no bull signal, no extreme fear
  MILD_BULL  — one of (BTC>SMA200+5%, F&G>60) but not all three
  CONFIRMED_BULL — BTC>SMA200+5% AND F&G 7d>60 AND BTC ADX>25 for 2 consecutive 4H closes

Demotion (from CONFIRMED_BULL) is ASYMMETRIC — wider thresholds, immediate:
  1. BTC daily close < SMA200 (no +5% buffer)
  2. F&G single-day < 50 OR single-day drop > 15 pts
  3. BTC 4H ADX < 20 for 6 consecutive bars

Entry requires 2 consecutive 4H closes satisfying all three strict conditions.

State is persisted to StateDB kv store under key 'bull_regime_state'.
A transition log is appended to the 'bull_regime_log' table.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Constants (mirrors bull_walkforward.py) ─────────────────────────────────
BTC_SMA_BUFFER = 0.05        # entry: close > SMA200 * 1.05
FNG_THRESHOLD = 60           # entry: 7d avg > 60
BTC_ADX_THRESHOLD = 25.0     # entry: ADX > 25
ADX_THRESHOLD_EXIT = 20.0    # exit: ADX < 20
ADX_EXIT_CONSEC = 6          # exit: 6 consecutive bars below
REGIME_CONFIRM_BARS = 2      # entry: 2 consecutive 4H bars
FNG_EXIT_LOW = 50            # exit: single-day < 50
FNG_EXIT_DROP = 15           # exit: single-day drop > 15
DEEP_BEAR_BUFFER = 0.85      # BTC < SMA200 * 0.85 → DEEP_BEAR
FEAR_FNG_AVG = 25            # 7d avg < 25 → FEAR

VALID_STATES = ("DEEP_BEAR", "FEAR", "NEUTRAL", "MILD_BULL", "CONFIRMED_BULL")

STATE_EMOJI = {
    "DEEP_BEAR": "🔴",
    "FEAR": "🟠",
    "NEUTRAL": "⚪",
    "MILD_BULL": "🟡",
    "CONFIRMED_BULL": "🟢",
}

STATE_CN = {
    "DEEP_BEAR": "深度熊市",
    "FEAR": "恐懼",
    "NEUTRAL": "中性",
    "MILD_BULL": "溫和牛市",
    "CONFIRMED_BULL": "確認牛市",
}


@dataclass
class RegimeState:
    """Serializable regime state."""
    regime: str = "NEUTRAL"
    confirm_count: int = 0
    btc_above_sma: bool = False
    fng_ok: bool = False
    btc_adx_ok: bool = False
    last_4h_ts: int = 0
    last_eval_ts: int = 0
    adx_below20_count: int = 0
    prev_fng: Optional[int] = None
    # Transition tracking
    last_transition_ts: int = 0
    last_transition_from: str = ""
    last_transition_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RegimeState":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _fng_7d_avg(fng_history: Dict[int, int], ts_ms: int) -> Optional[float]:
    """Average F&G over the 7 days preceding ts_ms (inclusive of current day)."""
    day_sec = 86400
    ts_sec = ts_ms // 1000
    today = ts_sec - (ts_sec % day_sec)
    vals: List[int] = []
    for i in range(7):
        day = today - i * day_sec
        v = fng_history.get(day)
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    return sum(vals) / len(vals)


def evaluate_regime(
    state: RegimeState,
    btc_daily_close: float,
    btc_sma200: float,
    btc_4h_close: float,
    btc_adx: Optional[float],
    fng_history: Dict[int, int],
    ts_ms: int,
    fng_threshold_override: Optional[int] = None,
) -> Tuple[RegimeState, Optional[Dict[str, Any]]]:
    """Evaluate regime at a given 4H bar. Returns (updated_state, transition_event).

    transition_event is None if no state change, otherwise a dict with:
      from, to, reason, ts, btc_close, btc_sma200, fng_avg, adx, conditions
    """
    old_regime = state.regime

    if btc_sma200 is None or btc_sma200 <= 0:
        return state, None

    # Entry conditions (strict)
    btc_above_entry = btc_daily_close > btc_sma200 * (1 + BTC_SMA_BUFFER)
    fng_thr = fng_threshold_override if fng_threshold_override else FNG_THRESHOLD
    fng_avg = _fng_7d_avg(fng_history, ts_ms)
    fng_ok_entry = fng_avg is not None and fng_avg > fng_thr
    btc_adx_ok_entry = btc_adx is not None and btc_adx > BTC_ADX_THRESHOLD

    # Exit conditions (wider / hysteresis)
    btc_below_sma_exit = btc_daily_close < btc_sma200

    ts_sec = ts_ms // 1000
    today = ts_sec - (ts_sec % 86400)
    fng_today = fng_history.get(today)
    fng_yesterday = fng_history.get(today - 86400)

    fng_crash_exit = False
    if fng_today is not None and fng_today < FNG_EXIT_LOW:
        fng_crash_exit = True
    if (fng_today is not None and fng_yesterday is not None
            and fng_yesterday - fng_today > FNG_EXIT_DROP):
        fng_crash_exit = True

    if btc_adx is not None and btc_adx < ADX_THRESHOLD_EXIT:
        state.adx_below20_count += 1
    else:
        state.adx_below20_count = 0
    adx_exit = state.adx_below20_count >= ADX_EXIT_CONSEC

    # Track conditions for UI/log
    state.btc_above_sma = btc_above_entry
    state.fng_ok = fng_ok_entry
    state.btc_adx_ok = btc_adx_ok_entry
    state.last_4h_ts = ts_ms
    state.last_eval_ts = int(time.time() * 1000)
    state.prev_fng = fng_today

    transition_reason = ""

    if state.regime == "CONFIRMED_BULL":
        if btc_below_sma_exit or fng_crash_exit or adx_exit:
            # Determine demotion target
            if btc_4h_close < btc_sma200 * DEEP_BEAR_BUFFER:
                new_regime = "DEEP_BEAR"
            elif fng_today is not None and fng_today < FEAR_FNG_AVG:
                new_regime = "FEAR"
            elif btc_4h_close < btc_sma200 * DEEP_BEAR_BUFFER:
                new_regime = "DEEP_BEAR"
            elif btc_above_entry or fng_ok_entry:
                new_regime = "MILD_BULL"
            else:
                new_regime = "NEUTRAL"

            reasons = []
            if btc_below_sma_exit:
                reasons.append(f"BTC日收${btc_daily_close:,.0f}<SMA200${btc_sma200:,.0f}")
            if fng_crash_exit:
                reasons.append(f"F&G崩跌(today={fng_today},yesterday={fng_yesterday})")
            if adx_exit:
                reasons.append(f"ADX<20連續{state.adx_below20_count}根")
            transition_reason = "; ".join(reasons)

            state.regime = new_regime
            state.confirm_count = 0
            state.adx_below20_count = 0
    else:
        all_three = btc_above_entry and fng_ok_entry and btc_adx_ok_entry
        if all_three:
            state.confirm_count = min(state.confirm_count + 1, REGIME_CONFIRM_BARS + 2)
        else:
            state.confirm_count = 0

        if state.confirm_count >= REGIME_CONFIRM_BARS:
            state.regime = "CONFIRMED_BULL"
            state.adx_below20_count = 0
            transition_reason = (
                f"BTC>SMA200+5%(${btc_daily_close:,.0f}>${btc_sma200*1.05:,.0f}) & "
                f"F&G7d={fng_avg:.1f}>60 & ADX={btc_adx:.1f}>25 "
                f"連續{state.confirm_count}根確認"
            )
        else:
            if btc_4h_close < btc_sma200 * DEEP_BEAR_BUFFER:
                state.regime = "DEEP_BEAR"
            elif fng_avg is not None and fng_avg < FEAR_FNG_AVG:
                state.regime = "FEAR"
            elif btc_above_entry or fng_ok_entry:
                state.regime = "MILD_BULL"
            else:
                state.regime = "NEUTRAL"

    # Build transition event
    transition = None
    if state.regime != old_regime:
        state.last_transition_ts = int(time.time() * 1000)
        state.last_transition_from = old_regime
        state.last_transition_reason = transition_reason or f"{old_regime}→{state.regime}"
        transition = {
            "from": old_regime,
            "to": state.regime,
            "reason": transition_reason or f"{old_regime}→{state.regime}",
            "ts": state.last_transition_ts,
            "bar_ts": ts_ms,
            "btc_close": btc_daily_close,
            "btc_sma200": btc_sma200,
            "fng_avg": round(fng_avg, 1) if fng_avg is not None else None,
            "fng_today": fng_today,
            "adx": round(btc_adx, 2) if btc_adx is not None else None,
            "conditions": {
                "btc_above_sma": btc_above_entry,
                "fng_ok": fng_ok_entry,
                "adx_ok": btc_adx_ok_entry,
                "adx_below_count": state.adx_below20_count,
                "confirm_count": state.confirm_count,
            },
        }
        logger.info(
            f"[BULL_REGIME] {STATE_EMOJI.get(old_regime,'')}{old_regime} → "
            f"{STATE_EMOJI.get(state.regime,'')}{state.regime} | {transition_reason}"
        )

    return state, transition


class BullRegimeDetector:
    """Production regime detector with StateDB persistence."""

    KV_KEY = "bull_regime_state"
    TABLE = "bull_regime_log"

    def __init__(self, db=None, client=None):
        self.db = db
        self.client = client
        self._state: Optional[RegimeState] = None
        self._ensure_table()

    def _ensure_table(self):
        if self.db is None:
            return
        with self.db._get_conn() as conn:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    bar_ts INTEGER NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    reason TEXT,
                    btc_close REAL,
                    btc_sma200 REAL,
                    fng_avg REAL,
                    fng_today INTEGER,
                    adx REAL,
                    conditions_json TEXT
                )
            """)
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self.TABLE}_ts ON {self.TABLE}(ts)"
            )
            conn.commit()

    def load_state(self) -> RegimeState:
        if self._state is not None:
            return self._state
        if self.db is not None:
            raw = self.db.kv_get(self.KV_KEY)
            if raw:
                try:
                    self._state = RegimeState.from_dict(json.loads(raw))
                    return self._state
                except (json.JSONDecodeError, TypeError):
                    logger.warning("[BULL_REGIME] Failed to parse saved state, resetting")
        self._state = RegimeState()
        return self._state

    def save_state(self, state: RegimeState):
        self._state = state
        if self.db is not None:
            self.db.kv_set(self.KV_KEY, json.dumps(state.to_dict()))

    def record_transition(self, t: Dict[str, Any]):
        if self.db is None or t is None:
            return
        with self.db._get_conn() as conn:
            conn.execute(
                f"""INSERT INTO {self.TABLE}
                    (ts, bar_ts, from_state, to_state, reason,
                     btc_close, btc_sma200, fng_avg, fng_today, adx, conditions_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    t["ts"], t["bar_ts"], t["from"], t["to"], t["reason"],
                    t.get("btc_close"), t.get("btc_sma200"),
                    t.get("fng_avg"), t.get("fng_today"), t.get("adx"),
                    json.dumps(t.get("conditions", {})),
                ),
            )
            conn.commit()

    def get_transitions(self, limit: int = 50) -> List[Dict]:
        if self.db is None:
            return []
        with self.db._get_conn() as conn:
            rows = conn.execute(
                f"""SELECT ts, from_state, to_state, reason, btc_close,
                           fng_avg, adx
                    FROM {self.TABLE} ORDER BY ts DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_time_in_state(self) -> Dict[str, Any]:
        """Get how long we've been in the current state."""
        state = self.load_state()
        now = int(time.time() * 1000)
        since = state.last_transition_ts or state.last_eval_ts or now
        hours = (now - since) / 3_600_000
        return {
            "regime": state.regime,
            "since_ts": since,
            "hours_in_state": round(hours, 1),
            "last_transition_from": state.last_transition_from,
            "last_transition_reason": state.last_transition_reason,
        }

    def format_report_line(self) -> str:
        """One-line regime status for scan reports."""
        state = self.load_state()
        tinfo = self.get_time_in_state()
        emoji = STATE_EMOJI.get(state.regime, "⚪")
        cn = STATE_CN.get(state.regime, state.regime)
        hours = tinfo["hours_in_state"]
        if hours < 48:
            time_str = f"{hours:.0f}h"
        else:
            time_str = f"{hours/24:.1f}d"

        cond_bits = []
        cond_bits.append("BTC✓" if state.btc_above_sma else "BTC✗")
        cond_bits.append("FNG✓" if state.fng_ok else "FNG✗")
        cond_bits.append("ADX✓" if state.btc_adx_ok else "ADX✗")
        if state.regime == "CONFIRMED_BULL":
            cond_bits.append(f"確認{state.confirm_count}根")
        else:
            cond_bits.append(f"計數{state.confirm_count}/{REGIME_CONFIRM_BARS}")

        return (
            f"{emoji} BULL體制: {cn} ({state.regime}) | 持續{time_str} | "
            + " ".join(cond_bits)
        )
