"""
Drawdown Breaker - Portfolio-level circuit breaker for maximum drawdown.

Hard stop at 10% portfolio drawdown from peak equity.

WO-1046b tiered auto-reset (Leo 2026-10-12 ruling): a trip starts a graded
cooldown instead of the pre-1046b "instant re-arm on dd < 10%" behaviour
(the legacy code returned tripped=False as soon as drawdown fell back
below the hard stop — an unbuffered auto-heal with churn/chain-crash
exposure, NOT the "manual reset" the yaml comment claimed).

Grades (all kv-tunable via `drawdown_breaker_cooldown`):
  (1) earliest reset at 48h: HWM dd narrowed to < 6% (bounce confirmed)
  (2) else at 72h recheck: dd < 8% -> reset
  (3) else every 24h recheck: reset as soon as dd < 8%; no hard cap,
      each deferral escalates an alert
  (4) anti-churn: a re-trip within 72h after ANY reset is a chain crash
      -> cooldown multiplier x2 (capped, default 4)
  (5) manual fast-lane: Travis triage (bug / black swan / normal vol);
      manual override stays, audited
Rollback: kv drawdown_breaker_cooldown {"enabled": false} -> legacy
instant re-arm semantics (pre-1046b behaviour).

WO-1046f C+ gated flat auto-reset (Travis 2026-10-12 joint ruling,
accepting the WO-1046d research): while TRIPPED and FLAT (no open
positions) the tiered dd-narrowing path is structurally unreachable
(equity frozen at the trip level, dd >= hard stop). Four gates are
evaluated once per UTC day at the previous day's daily-close bar:
  G1 elapsed >= 48h since trip
  G2 BTC 24h return > -2% AND close >= 24h low x 1.02
  G3 6h realised vol <= [trip, trip+48h] peak x 0.80
  G4 2-of-3 momentum: above 24h VWAP / 4H candle momentum /
     Fear&Greed not below the trip-day value
All gates PASS -> ARM (15min human veto window, kv
drawdown_breaker_flat_veto=true) -> FIRE: trip cleared, HWM
re-anchored at current equity (chain multiplier preserved), 24h 50%
ramp, 72h anti-churn window. Re-trip within retrip_disable_hours of
a flat FIRE -> flat auto-reset disabled for the cycle (manual-only).
Rollback: kv drawdown_breaker_flat_reset {"enabled": false} -> the
flat branch is inert (pure-A manual-only semantics).

STORAGE: SQLite state.db drawdown table (trip/HWM truth) + kv table
(drawdown_breaker_cooldown_state: multiplier/chain window/events).
No JSON files are used; all state is persisted via StateDB.
"""

import logging
import math
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.exchange_client import ExchangeClient
from typing import Dict

logger = logging.getLogger(__name__)


class DrawdownBreaker:
    """Portfolio drawdown circuit breaker with 10% hard stop.

    All state is stored exclusively in the SQLite drawdown table via StateDB.
    """

    # Spike detection thresholds for HWM update rejection.
    # Reject HARD at >5x (definitely phantom), warn at >3x but allow update.
    _PHANTOM_SPIKE_HARD = 5.0  # hard reject above 5x
    _PHANTOM_SPIKE_WARN = 3.0  # warn above 3x but allow

    # Hard stop threshold (loaded from unified risk config with fallback)
    # Default: 10% drawdown from peak equity → block all new trades
    _DEFAULT_HARD_STOP_PCT = 0.10  # 10%

    try:
        from src.risk_config import get_risk_param
        HARD_STOP_PCT = get_risk_param(
            "drawdown_breaker", "hard_stop_pct", _DEFAULT_HARD_STOP_PCT
        )
    except Exception as e:
        logger.warning("drawdown_breaker.module: " + str(e))
        HARD_STOP_PCT = _DEFAULT_HARD_STOP_PCT

    # WO-1046b: tiered-reset cooldown defaults. Overrides (in order of
    # precedence): kv `drawdown_breaker_cooldown` > risk_params.yaml
    # drawdown_breaker.cooldown > these defaults.
    _COOLDOWN_DEFAULTS = {
        "enabled": True,
        "early_reset_hours": 48.0,       # (1) earliest reset point
        "early_reset_dd_pct": 6.0,       # (1) requires dd narrowed < 6%
        "recheck_hours": 72.0,           # (2) recheck point
        "recheck_dd_pct": 8.0,           # (2)/(3) reset once dd < 8%
        "recheck_interval_hours": 24.0,  # (3) subsequent recheck cadence
        "chain_window_hours": 72.0,      # (4) re-trip window after any reset
        "chain_multiplier_cap": 4,       # (4) multiplier cap (48h*4=192h)
    }

    # WO-1046f: C+ gated flat auto-reset defaults. Overrides (in order of
    # precedence): kv `drawdown_breaker_flat_reset` > risk_params.yaml
    # drawdown_breaker.flat_reset > these defaults. Gate semantics are
    # frozen to the accepted WO-1046d research (parity requirement).
    _FLAT_DEFAULTS = {
        "enabled": True,
        "g1_hours": 48.0,              # G1: min hours since trip
        "g2_ret24_min_pct": -2.0,       # G2: BTC 24h return floor (%)
        "g2_low_buffer": 1.02,          # G2: close >= 24h low * buffer
        "g3_vol_ratio_max": 0.80,       # G3: 6h vol <= peak * ratio
        "g4_need": 2,                   # G4: momentum confirmations (of 3)
        "ramp_pct": 0.5,                # first re-entries at this fraction
        "ramp_hours": 24.0,             # ramp window after FIRE
        "retrip_disable_hours": 72.0,   # re-trip in window -> manual-only
        "veto_minutes": 15,             # ARM -> FIRE human veto window
    }

    @classmethod
    def _get_flat_cfg(cls) -> Dict:
        """Merge flat defaults <- risk_params.yaml <- kv override (kv
        `drawdown_breaker_flat_reset`; {"enabled": false} rolls the whole
        branch back to pure-A manual-only semantics)."""
        cfg = dict(cls._FLAT_DEFAULTS)
        try:
            from src.risk_config import get_section
            y = (get_section("drawdown_breaker") or {}).get("flat_reset") or {}
            cfg.update({k: y[k] for k in cfg if k in y})
        except Exception:
            pass
        try:
            from src.state_db import get_state_db
            kvf = get_state_db().kv_get("drawdown_breaker_flat_reset") or {}
            if isinstance(kvf, dict):
                cfg.update({k: kvf[k] for k in cfg if k in kvf})
        except Exception:
            pass
        return cfg

    @classmethod
    def _get_cooldown_cfg(cls) -> Dict:
        """Merge defaults <- risk_params.yaml <- kv override."""
        cfg = dict(cls._COOLDOWN_DEFAULTS)
        try:
            from src.risk_config import get_section
            y = (get_section("drawdown_breaker") or {}).get("cooldown") or {}
            cfg.update({k: y[k] for k in cfg if k in y})
        except Exception:
            pass
        try:
            from src.state_db import get_state_db
            kvc = get_state_db().kv_get("drawdown_breaker_cooldown") or {}
            if isinstance(kvc, dict):
                cfg.update({k: kvc[k] for k in cfg if k in kvc})
        except Exception:
            pass
        return cfg

    # ==============================================================
    # WO-1046f: C+ gated flat auto-reset (four gates, UTC daily-close
    # checkpoint, ARM->FIRE with human veto window, re-trip disable).
    # Only reached while TRIPPED and FLAT (no open positions); the tiered
    # dd-narrowing path is structurally unreachable there (equity frozen
    # at the trip level, dd >= hard stop — WO-1046d README §1.1 mutual
    # exclusion). Gate math is a verbatim port of the frozen research
    # `_gate_eval` (deliverables/WO-1046d/backtest_wo1046d.py): the fire
    # days 2026-08-17 / 2021-05-24 / 2022-04-28 were produced by that
    # exact math and parity is an acceptance requirement.
    # ==============================================================

    def _fetch_fng_now(self) -> Optional[int]:
        """Best-effort current Fear&Greed value (sqlite-cached feed).
        None on any failure — G4 sub3 then fails closed."""
        try:
            from src.data_feed_fng import FearGreedIndex
            cur = FearGreedIndex().get_current()
            if cur and cur.get("value") is not None:
                return int(cur["value"])
        except Exception:
            pass
        return None

    def _fetch_fng_by_day(self, day_str) -> Optional[int]:
        """Fear&Greed value FOR a specific UTC day (research parity:
        the WO-1046d math compares the anchor-bar day's F&G with the
        trip day's). Deterministic — avoids the 'freshest published'
        feed-timing ambiguity at the 00:30 checkpoint. None on miss ->
        G4 sub3 fails closed."""
        try:
            from src.data_feed_fng import FearGreedIndex
            hist = FearGreedIndex().get_history(limit=14) or []
            for e in hist:
                if str(e.get("timestamp", ""))[:10] == day_str:
                    if e.get("value") is not None:
                        return int(e["value"])
                    return None
        except Exception:
            pass
        return None

    def _fetch_bars_1h(self, start_ms=None, end_ms=None, limit=40):
        """Completed BTC/USDT 1h bars (open_time ascending). Raises on
        any failure — callers treat exceptions as a data failure
        (fail-closed). Bars past the wall clock are excluded so the
        daily-close anchor is always a completed bar."""
        if self.client is None:
            raise RuntimeError("no exchange client injected")
        now_ms = int(time.time() * 1000)
        bars = self.client.get_klines(
            "BTC/USDT", "1h", limit=limit,
            start_time=start_ms, end_time=end_ms) or []
        out = []
        for b in bars:
            ot = int(b.get("open_time", 0))
            ct = int(b.get("close_time", ot + 3599999))
            if ct <= now_ms:
                out.append({
                    "open_time": ot,
                    "open": float(b["open"]),
                    "high": float(b["high"]),
                    "low": float(b["low"]),
                    "close": float(b["close"]),
                    "volume": float(b["volume"]),
                })
        if not out:
            raise RuntimeError("no completed 1h bars returned")
        out.sort(key=lambda x: x["open_time"])
        return out

    @staticmethod
    def _vol6(closes, i):
        """Population stdev of the last 6 hourly log returns ending at
        bar i — verbatim port of the WO-1046d research `_vol6`."""
        if i < 5 or i >= len(closes):
            return None
        rs = [math.log(closes[j] / closes[j - 1])
              for j in range(i - 5, i + 1)]
        m = sum(rs) / len(rs)
        return (sum((x - m) ** 2 for x in rs) / len(rs)) ** 0.5

    def _eval_flat_gates(self, fcfg, cst, tripped_at, anchor_open_ms,
                         fng_now):
        """Four C+ gates at the daily-close anchor bar — verbatim port
        of the WO-1046d research `_gate_eval` (keep bit-consistent: the
        fire-day anchors 8/17, 5/24, 4/28 in the accepted research were
        produced by this exact math). Returns None on any data failure
        (fail-closed HOLD, README §3 #4)."""
        trip_ms = int(tripped_at * 1000)
        trip_q_ms = (trip_ms // 3600000) * 3600000
        try:
            # current window: the 24h ending at the anchor bar
            cur = self._fetch_bars_1h(
                start_ms=anchor_open_ms - 24 * 3600 * 1000, limit=40)
            # G3 peak window: [trip hour, trip hour + 48h] plus 6h of
            # vol6 history before the trip bar (the research math
            # computes peak-bar vol from pre-trip closes too)
            peak_bars = self._fetch_bars_1h(
                start_ms=trip_q_ms - 6 * 3600 * 1000,
                end_ms=trip_q_ms + 49 * 3600 * 1000, limit=60)
        except Exception as e:
            logger.warning(
                "DrawdownBreaker: flat gate data fetch failed: %s", e)
            return None
        bars = {}
        for b in cur + peak_bars:
            bars[b["open_time"]] = b
        if anchor_open_ms not in bars:
            logger.warning(
                "DrawdownBreaker: daily-close anchor bar missing")
            return None
        ts = sorted(bars)
        o = [bars[t]["open"] for t in ts]
        h = [bars[t]["high"] for t in ts]
        l = [bars[t]["low"] for t in ts]
        c = [bars[t]["close"] for t in ts]
        v = [bars[t]["volume"] for t in ts]
        q = ts.index(anchor_open_ms)

        # G1 — elapsed hours since the trip bar
        g1 = (anchor_open_ms - trip_q_ms) >= (
            float(fcfg["g1_hours"]) * 3600 * 1000)
        # G2 — BTC 24h return floor + 24h low buffer
        g2 = False
        if q >= 24:
            r24 = (c[q] / c[q - 24] - 1) * 100
            low24 = min(l[q - 23: q + 1])
            g2 = (r24 > float(fcfg["g2_ret24_min_pct"])
                  and c[q] >= low24 * float(fcfg["g2_low_buffer"]))
        # G3 — 6h realised vol vs the [trip, trip+48h] peak
        v_now = self._vol6(c, q)
        peak = None
        for j, t in enumerate(ts):
            if trip_q_ms <= t <= trip_q_ms + 48 * 3600 * 1000:
                vv = self._vol6(c, j)
                if vv is not None and (peak is None or vv > peak):
                    peak = vv
        g3 = (peak is not None and v_now is not None
              and v_now <= peak * float(fcfg["g3_vol_ratio_max"]))
        # G4 — 2-of-3 momentum confirmations
        subs = [False, False, False]
        if q >= 24:
            tp_v = [((h[j] + l[j] + c[j]) / 3.0) * v[j]
                    for j in range(q - 23, q + 1)]
            vv = sum(v[q - 23: q + 1])
            if vv > 0:
                subs[0] = c[q] > sum(tp_v) / vv
        # sub2 (research-frozen semantics): walk back from the anchor
        # building 4H buckets; candle "open" = open of the bucket's
        # last hour, candle "close" = close of the bucket's first hour.
        candles = []
        cur_c = None
        j = q
        while j >= 0 and len(candles) < 2:
            bkt = ts[j] // 14400000
            if cur_c is None:
                cur_c = [bkt, o[j], c[j]]
            elif bkt == cur_c[0]:
                cur_c[2] = c[j]
            else:
                candles.append(cur_c)
                cur_c = [bkt, o[j], c[j]]
            j -= 1
        if cur_c:
            candles.append(cur_c)
        if len(candles) >= 2:
            subs[1] = ((candles[0][2] > candles[0][1])
                       or (candles[1][2] > candles[1][1])) and (
                          candles[0][2] > candles[1][2])
        # sub3 — Fear&Greed not below the trip-day value
        f_trip = cst.get("fng_at_trip")
        if fng_now is not None and f_trip is not None:
            subs[2] = fng_now >= f_trip
        g4 = sum(subs) >= int(fcfg["g4_need"])
        # current-window contiguity: position-indexed math (G2/VWAP/
        # candles/vol6) assumes 24 gapless hourly bars before the anchor
        if q < 24 or any(ts[i + 1] - ts[i] != 3600 * 1000
                        for i in range(q - 23, q)):
            logger.warning(
                "DrawdownBreaker: flat gate current window has gaps — "
                "fail-closed")
            return None
        return {
            "g1": bool(g1), "g2": bool(g2), "g3": bool(g3),
            "g4": bool(g4), "g4_subs": subs,
            "ok": bool(g1 and g2 and g3 and g4),
            "anchor_open_ms": anchor_open_ms,
            "fng_now": fng_now, "fng_at_trip": f_trip,
        }

    def _flat_vetoed(self) -> bool:
        """Manual veto: kv drawdown_breaker_flat_veto truthy. One-shot —
        consumed when read. A read failure also returns True (fail-
        closed: FIRE requires a confirmed veto check)."""
        try:
            from src.state_db import get_state_db
            db = get_state_db()
            v = db.kv_get("drawdown_breaker_flat_veto")
            if v:
                db.kv_set("drawdown_breaker_flat_veto", None)
                return True
            return False
        except Exception as e:
            logger.error("DrawdownBreaker: veto read failed: %s", e)
            return True

    def _flat_reset_eval(self, dd_pct, hwm, balance):
        """WO-1046f C+ flat auto-reset state machine. Called from
        check_drawdown ONLY while tripped AND flat (positions_open == 0).

        Returns None when the branch is disabled (kv/yaml) — the tiered
        outcome then owns the result. Otherwise returns a flat status
        dict; action == "RESET" means the FIRE transition executed
        (trip cleared, HWM re-anchored, ramp armed)."""
        fcfg = self._get_flat_cfg()
        if not fcfg.get("enabled"):
            return None
        cst = self._load_cooldown_state()
        now = time.time()
        tripped_at = self._state.get("tripped_at")

        if cst.get("flat_auto_disabled"):
            return {"state": "disabled_this_cycle"}

        # (a) ARMED pending FIRE: veto-window countdown, NOT gated on the
        # daily checkpoint. FIRE re-checks the gates on the same frozen
        # daily-close bar set (deterministic within the UTC day).
        armed_at = cst.get("flat_reset_armed_at")
        if armed_at:
            if now - armed_at > 24 * 3600:
                # stale arm (defensive — impossible while tripped+flat):
                # disarm, re-arm at the next daily checkpoint
                cst["flat_reset_armed_at"] = None
                cst["flat_anchor_open_ms"] = None
                self._save_cooldown_state(cst)
                return {"state": "armed_expired"}
            if now - armed_at < float(fcfg["veto_minutes"]) * 60:
                return {"state": "armed", "armed_at": armed_at}
            if self._flat_vetoed():
                cst["flat_reset_armed_at"] = None
                cst["flat_anchor_open_ms"] = None
                cst["flat_auto_disabled"] = True
                self._append_event(cst, {
                    "type": "flat_vetoed", "ts": now,
                    "armed_at": armed_at,
                })
                self._save_cooldown_state(cst)
                self._audit("DRAWDOWN_BREAKER_FLAT_VETOED", {
                    "armed_at": armed_at})
                msg = ("\u270b Flat auto-reset VETOED — disarmed and "
                       "disabled for this cycle; manual reset is the "
                       "only path. @Travis @Leo")
                self._outbox_alert(
                    f"dd_breaker_flat_vetoed_{int(now)}",
                    "drawdown_breaker_flat_vetoed",
                    "\u270b Flat auto-reset VETOED", msg)
                try:
                    from src.notifier import FeishuNotifier
                    FeishuNotifier().send_text(msg)
                except Exception:
                    pass
                return {"state": "vetoed_disabled"}
            anchor_ms = cst.get("flat_anchor_open_ms")
            gates = self._eval_flat_gates(
                fcfg, cst, tripped_at, anchor_ms,
                self._fetch_fng_by_day(time.strftime(
                    "%Y-%m-%d", time.gmtime((anchor_ms or 0) / 1000.0))))
            if gates is None or not gates.get("ok"):
                # data failure or gates no longer pass on re-check:
                # disarm, retry from the next daily checkpoint
                cst["flat_reset_armed_at"] = None
                cst["flat_anchor_open_ms"] = None
                self._append_event(cst, {
                    "type": "flat_armed_lapsed", "ts": now})
                self._save_cooldown_state(cst)
                return {"state": "armed_lapsed"}
            return self._flat_fire(fcfg, cst, dd_pct, hwm, balance, gates)

        # (b) daily-close checkpoint: at most one gate evaluation per UTC
        # day, anchored on the last completed bar BEFORE today's 00:00
        # UTC (yesterday's daily close — the completed-bar set is frozen
        # for the whole calendar day regardless of scan time).
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if cst.get("flat_last_gate_eval_day") == day:
            return {"state": "await_next_close"}
        today00_ms = int((now - (now % 86400)) * 1000)
        try:
            bars = self._fetch_bars_1h(
                start_ms=today00_ms - 30 * 3600 * 1000, limit=40)
        except Exception as e:
            logger.warning(
                "DrawdownBreaker: flat daily-close anchor fetch "
                "failed: %s", e)
            return {"state": "data_fail_hold"}
        anchor = None
        for b in bars:
            if b["open_time"] < today00_ms:
                anchor = b["open_time"]
        if anchor is None:
            logger.warning(
                "DrawdownBreaker: no completed daily-close anchor bar")
            return {"state": "data_fail_hold"}
        gates = self._eval_flat_gates(
            fcfg, cst, tripped_at, anchor,
            self._fetch_fng_by_day(time.strftime(
                "%Y-%m-%d", time.gmtime(anchor / 1000.0))))
        if gates is None:
            # data failure — checkpoint NOT consumed; the next call
            # retries the same day (fail-closed, README §3 #4)
            return {"state": "data_fail_hold"}
        cst["flat_last_gate_eval_day"] = day
        if gates.get("ok"):
            cst["flat_reset_armed_at"] = now
            cst["flat_anchor_open_ms"] = anchor
            cst["flat_gate_snapshot"] = gates
            self._append_event(cst, {
                "type": "flat_armed", "ts": now, "gates": gates,
                "anchor_open_ms": anchor})
            self._save_cooldown_state(cst)
            self._audit("DRAWDOWN_BREAKER_FLAT_ARMED", {
                "gates": gates, "anchor_open_ms": anchor,
                "veto_minutes": fcfg["veto_minutes"]})
            msg = (
                "\u2699\ufe0f Flat AUTO-RESET ARMED (C+ four gates PASS at "
                f"daily close {day})\n"
                f"FIRE in {fcfg['veto_minutes']}min unless vetoed — set kv "
                "drawdown_breaker_flat_veto=true to disarm. "
                "@Travis @Leo")
            self._outbox_alert(
                f"dd_breaker_flat_armed_{day}",
                "drawdown_breaker_flat_armed",
                "\u2699\ufe0f Flat auto-reset ARMED (veto window open)",
                msg)
            try:
                from src.notifier import FeishuNotifier
                FeishuNotifier().send_text(msg)
            except Exception:
                pass
            return {"state": "armed", "armed_at": now, "gates": gates}
        self._save_cooldown_state(cst)
        self._outbox_alert(
            f"dd_breaker_flat_hold_{day}",
            "drawdown_breaker_flat_hold",
            f"\u23f3 Flat gate eval HOLD {day}",
            f"\u23f3 Flat gate eval {day}: "
            f"G1={gates['g1']} G2={gates['g2']} G3={gates['g3']} "
            f"G4={gates['g4']}{gates['g4_subs']} — staying tripped; next "
            f"checkpoint at the next UTC daily close. @Travis @Leo")
        return {"state": "hold", "gates": gates}

    def _flat_fire(self, fcfg, cst, dd_pct, hwm, balance, gates):
        """Execute the FIRE transition: clear trip, re-anchor HWM at the
        current equity (chain multiplier preserved — auto resets never
        erase the anti-churn penalty, README §3 #2), arm the 72h chain
        window + ramp flag, audit + alert."""
        now = time.time()
        state = self._state
        mult = max(1, int(cst.get("cooldown_multiplier", 1) or 1))
        state["high_watermark"] = balance
        state["current_drawdown_pct"] = 0.0
        state["tripped_at"] = None
        state["reset_at"] = now
        # forensic counters (tripped_count / max_drawdown_pct) are kept:
        # FIRE is a machine reset of the TRIP state, not a human
        # "fresh start" (manual reset() clears them).
        cst["last_chain_reset_at"] = now
        cst["last_auto_reset_at"] = now
        cst["flat_last_reset_at"] = now
        cst["flat_reset_armed_at"] = None
        cst["flat_anchor_open_ms"] = None
        cst["flat_gate_snapshot"] = gates
        cst["last_escalation_ts"] = None
        cst["escalation_rounds"] = 0
        self._append_event(cst, {
            "type": "flat_auto_reset", "ts": now, "gates": gates,
            "mult": mult, "dd_at_reset": round(dd_pct, 2)})
        self._save_cooldown_state(cst)
        self._save_state()
        # ramp flag — consumed by risk_manager sizing (flat_ramp_active)
        try:
            from src.state_db import get_state_db
            get_state_db().kv_set("drawdown_breaker_flat_ramp", {
                "until_ts": now + float(fcfg["ramp_hours"]) * 3600,
                "pct": float(fcfg["ramp_pct"]),
                "ramp_hours": float(fcfg["ramp_hours"]),
            })
        except Exception as e:
            logger.error(
                "DrawdownBreaker: ramp flag write failed: %s", e)
        self._audit("DRAWDOWN_BREAKER_FLAT_AUTO_RESET", {
            "gates": gates, "multiplier": mult,
            "hwm_old": hwm, "hwm_new": balance,
            "dd_pct_at_reset": round(dd_pct, 2),
            "ramp_pct": fcfg["ramp_pct"], "ramp_hours": fcfg["ramp_hours"],
        })
        msg = (
            "\u2705 Flat AUTO-RESET FIRED (C+)\n"
            f"HWM re-anchored ${hwm:,.2f} -> ${balance:,.2f} (chain mult "
            f"x{mult} preserved); new entries re-enabled at "
            f"{float(fcfg['ramp_pct']):.0%} size for "
            f"{float(fcfg['ramp_hours']):.0f}h.\n"
            f"Re-trip within {float(fcfg['retrip_disable_hours']):.0f}h -> "
            f"manual-only for the rest of this cycle. @Travis @Leo")
        self._outbox_alert(
            f"dd_breaker_flat_reset_{int(now)}",
            "drawdown_breaker_flat_reset",
            "\u2705 Flat auto-reset FIRED (C+)", msg)
        try:
            from src.notifier import FeishuNotifier
            FeishuNotifier().send_text(msg)
        except Exception:
            pass
        logger.warning(
            "DrawdownBreaker: FLAT AUTO-RESET (cplus) dd=%.2f%% mult=x%d",
            dd_pct, mult)
        return {
            "tripped": False,
            "drawdown_pct": 0.0,
            "high_watermark": balance,
            "action": "RESET",
            "reason": (
                "Flat C+ auto-reset: four gates PASS at daily close "
                f"(G1={gates['g1']} G2={gates['g2']} G3={gates['g3']} "
                f"G4={gates['g4']}); HWM re-anchored, ramp "
                f"{float(fcfg['ramp_pct']):.0%}/"
                f"{float(fcfg['ramp_hours']):.0f}h"),
        }

    def _load_cooldown_state(self) -> Dict:
        """Runtime cooldown state from kv (multiplier / chain window /
        escalation clock / event log). Missing/corrupt -> fresh defaults."""
        try:
            from src.state_db import get_state_db
            cst = get_state_db().kv_get("drawdown_breaker_cooldown_state")
            if isinstance(cst, dict):
                return cst
        except Exception:
            pass
        return {
            "cooldown_multiplier": 1,
            "last_chain_reset_at": None,
            "last_auto_reset_at": None,
            "last_escalation_ts": None,
            "escalation_rounds": 0,
            "events": [],
        }

    def _save_cooldown_state(self, cst: Dict):
        try:
            from src.state_db import get_state_db
            get_state_db().kv_set("drawdown_breaker_cooldown_state", cst)
        except Exception as e:
            logger.error("DrawdownBreaker: cooldown state save failed: %s", e)

    @staticmethod
    def _append_event(cst: Dict, event: Dict):
        cst["events"] = (cst.get("events") or [])[-49:] + [event]

    def _audit(self, action: str, details: Dict):
        try:
            from src.state_db import get_state_db
            get_state_db().audit_log(action, details, source="drawdown_breaker")
        except Exception:
            pass

    def _outbox_alert(self, notif_id: str, msg_type: str, title: str, body: str):
        """High-priority alert through the notification outbox (idempotent
        by notif_id) so cron push + forensics both see it."""
        try:
            from src.state_db import get_state_db
            get_state_db().notification_outbox_add(notif_id, msg_type, title, body)
        except Exception:
            logger.error("DrawdownBreaker: outbox alert failed", exc_info=True)

    def _notify_tripped(self, drawdown: float, hwm: float, balance: float, mult: int):
        """(6) trip instant alert: outbox (high-priority, Travis+Leo) +
        Feishu immediate text (FIX-8 channel preserved)."""
        msg = (
            f"\U0001f6a8 DRAWDOWN BREAKER TRIPPED (high priority @Travis @Leo)\n"
            f"Drawdown: {drawdown*100:.1f}% >= 10% limit\n"
            f"HWM: ${hwm:,.2f} | equity: ${balance:,.2f}\n"
            f"Tiered cooldown started (x{mult}): earliest auto-reset 48h"
            f"{' x' + str(mult) if mult > 1 else ''} once dd < 6%.\n"
            f"Manual fast-lane: triage as bug / black-swan / normal-vol;"
            f" reset(actor=...) audits."
        )
        self._outbox_alert(
            f"dd_breaker_trip_{int(time.time())}",
            "drawdown_breaker_trip",
            "\U0001f6a8 DrawdownBreaker TRIPPED (high priority)",
            msg,
        )
        try:
            from src.notifier import FeishuNotifier
            FeishuNotifier().send_text(msg)
        except Exception:
            logger.error(
                "DrawdownBreaker: failed to send trip notification",
                exc_info=True,
            )

    def __init__(self, binance_client: Optional["ExchangeClient"] = None):
        self.client = binance_client
        self._state = self._load_state()

    def _load_state(self) -> Dict:
        """Load state from SQLite drawdown table (sole source of truth)."""
        try:
            from src.state_db import get_state_db

            db = get_state_db()
            state = db.drawdown_get()
            logger.info("DrawdownBreaker: loaded from SQLite drawdown table")
            return state
        except Exception as e:
            logger.error(f"DrawdownBreaker: failed to load from SQLite: {e}")
            # Fail-safe defaults
            return {
                "high_watermark": 0.0,
                "current_drawdown_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "tripped_at": None,
                "tripped_count": 0,
                "reset_at": None,
                "history": [],
            }

    def _save_state(self):
        """Save state to SQLite drawdown table (sole source of truth).

        Retries once on transient errors (e.g. disk I/O error on network FS),
        since drawdown state is recomputed frequently and a single lost save
        is harmless but log noise is not.
        """
        import time as _time

        from src.state_db import get_state_db

        db = get_state_db()
        for attempt in (1, 2):
            try:
                db.drawdown_set(self._state)
                return
            except Exception as e:
                if attempt == 1:
                    _time.sleep(0.5)
                    logger.warning(
                        f"DrawdownBreaker: SQLite save failed (retrying): {e}"
                    )
                else:
                    logger.error(f"DrawdownBreaker: SQLite save failed: {e}")

    def _cooldown_eval(self, dd_pct: float, hwm: float, balance: float,
                       context: str) -> Optional[Dict]:
        """WO-1046b tiered-reset state machine — evaluated on every
        check_drawdown call while a trip is outstanding.

        Returns a result dict when the cooldown decides the outcome
        (auto-RESET or stay-tripped HOLD), or None to fall through to the
        legacy instant-re-arm path (cooldown disabled / no trip).
        """
        state = self._state
        cfg = self._get_cooldown_cfg()
        if not state.get("tripped_at") or not cfg.get("enabled"):
            return None
        cst = self._load_cooldown_state()
        now = time.time()
        mult = max(1, int(cst.get("cooldown_multiplier", 1) or 1))
        elapsed_h = (now - state["tripped_at"]) / 3600.0
        early_h = float(cfg["early_reset_hours"]) * mult
        recheck_h = float(cfg["recheck_hours"]) * mult
        interval_h = float(cfg["recheck_interval_hours"]) * mult
        recheck_dd = float(cfg["recheck_dd_pct"])
        early_dd = float(cfg["early_reset_dd_pct"])

        # (1) earliest reset: elapsed >= 48h*mult AND dd narrowed < 6%
        if elapsed_h >= early_h and dd_pct < early_dd:
            return self._tiered_auto_reset(
                "tier1_early", dd_pct, mult, cst, hwm, balance)
        # (2) 72h*mult recheck: dd < 8% -> reset
        if elapsed_h >= recheck_h and dd_pct < recheck_dd:
            return self._tiered_auto_reset(
                "tier2_recheck", dd_pct, mult, cst, hwm, balance)

        # (3) still >= 8% (or inside cooldown window): stay tripped,
        # escalate an alert every interval_h once past the recheck point
        if elapsed_h >= recheck_h:
            last_esc = cst.get("last_escalation_ts")
            rounds = int(cst.get("escalation_rounds", 0) or 0)
            if last_esc is None or (now - last_esc) >= interval_h * 3600:
                cst["last_escalation_ts"] = now
                cst["escalation_rounds"] = rounds + 1
                self._append_event(cst, {
                    "type": "escalation", "ts": now,
                    "elapsed_h": round(elapsed_h, 1), "dd_pct": round(dd_pct, 2),
                    "round": rounds + 1, "mult": mult,
                })
                self._save_cooldown_state(cst)
                msg = (
                    f"\u26a0\ufe0f DrawdownBreaker cooldown DEFERRAL "
                    f"(round {rounds + 1}, mult x{mult})\n"
                    f"Elapsed {elapsed_h:.0f}h since trip, dd {dd_pct:.1f}% "
                    f">= {recheck_dd:.0f}% — auto-reset deferred.\n"
                    f"Next recheck in {interval_h:.0f}h; resets as soon as "
                    f"dd < {recheck_dd:.0f}%. @Travis @Leo"
                )
                self._outbox_alert(
                    f"dd_breaker_defer_{int(now // (interval_h * 3600))}",
                    "drawdown_breaker_deferral",
                    "\u26a0\ufe0f Breaker cooldown deferral (escalation)",
                    msg,
                )
                try:
                    from src.notifier import FeishuNotifier
                    FeishuNotifier().send_text(msg)
                except Exception:
                    pass
        return {
            "tripped": True,
            "drawdown_pct": round(dd_pct, 2),
            "high_watermark": hwm,
            "action": "HOLD",
            "reason": (
                f"Tiered cooldown active ({context}): elapsed "
                f"{elapsed_h:.1f}h/{early_h:.0f}h x{mult}, dd "
                f"{dd_pct:.1f}% (reset at dd<{early_dd:.0f}% after "
                f"{early_h:.0f}h, or dd<{recheck_dd:.0f}% after "
                f"{recheck_h:.0f}h)"
            ),
        }

    def _tiered_auto_reset(self, tier: str, dd_pct: float, mult: int,
                           cst: Dict, hwm: float, balance: float) -> Dict:
        """Execute a graded auto-reset: clear trip, open the 72h chain
        window, audit + notify."""
        state = self._state
        now = time.time()
        state["tripped_at"] = None
        state["reset_at"] = now
        cst["last_chain_reset_at"] = now
        cst["last_auto_reset_at"] = now
        cst["last_escalation_ts"] = None
        cst["escalation_rounds"] = 0
        self._append_event(cst, {
            "type": f"auto_reset_{tier}", "ts": now,
            "dd_pct": round(dd_pct, 2), "mult": mult,
        })
        self._save_cooldown_state(cst)
        self._save_state()
        self._audit("DRAWDOWN_BREAKER_AUTO_RESET", {
            "tier": tier, "dd_pct": round(dd_pct, 2), "multiplier": mult,
            "hwm": hwm, "balance": balance,
        })
        msg = (
            f"\u2705 DrawdownBreaker AUTO-RESET ({tier})\n"
            f"dd narrowed to {dd_pct:.1f}% (mult x{mult}). New entries "
            f"re-enabled; 72h anti-churn window armed. @Travis @Leo"
        )
        self._outbox_alert(
            f"dd_breaker_autoreset_{int(now)}",
            "drawdown_breaker_auto_reset",
            "\u2705 Breaker auto-reset (tiered cooldown)",
            msg,
        )
        try:
            from src.notifier import FeishuNotifier
            FeishuNotifier().send_text(msg)
        except Exception:
            pass
        logger.warning(
            "DrawdownBreaker: TIERED AUTO-RESET (%s) dd=%.2f%% mult=x%d",
            tier, dd_pct, mult,
        )
        return {
            "tripped": False,
            "drawdown_pct": round(dd_pct, 2),
            "high_watermark": hwm,
            "action": "RESET",
            "reason": f"Tiered auto-reset ({tier}): dd {dd_pct:.1f}% narrowed",
        }

    def check_drawdown(self, current_balance: float,
                      positions_open: Optional[int] = None) -> Dict:
        """Check if portfolio drawdown exceeds the 10% hard stop.

        Args:
            current_balance: Current total portfolio value in USDT
            positions_open: open position count (WO-1046f; risk_manager
                passes the live count, dust <$1 excluded). None/omitted
                (legacy callers) never evaluates the flat branch.

        Returns:
            {
                "tripped": bool,
                "drawdown_pct": float,
                "high_watermark": float,
                "action": str,  # "HOLD" / "TRIP" / "RESET" / "INIT"
                "reason": str,
            }
        """
        state = self._state
        hwm = state["high_watermark"]

        # Update high watermark
        if current_balance > hwm:
            # Spike detection: graduated response instead of hard 3x reject
            if hwm > 0:
                spike_ratio = current_balance / hwm
                if spike_ratio > self._PHANTOM_SPIKE_HARD:
                    # Hard reject: >5x is almost certainly phantom data
                    logger.warning(
                        "DrawdownBreaker: REJECTED watermark update %.2f → %.2f "
                        "(%.1fx spike > %.0fx hard limit, likely phantom)",
                        hwm, current_balance, spike_ratio, self._PHANTOM_SPIKE_HARD,
                    )
                    self._save_state()
                    return {
                        "tripped": False,
                        "drawdown_pct": (
                            round(((hwm - current_balance) / hwm) * 100, 2)
                            if hwm > 0
                            else 0
                        ),
                        "high_watermark": hwm,
                        "action": "HOLD",
                        "reason": (
                            f"Rejected phantom equity spike: {current_balance:.2f} "
                            f"> {self._PHANTOM_SPIKE_HARD}x watermark {hwm:.2f}"
                        ),
                    }
                elif spike_ratio > self._PHANTOM_SPIKE_WARN:
                    # Warn but allow: 3-5x spike in crypto is possible (alt season)
                    logger.warning(
                        "DrawdownBreaker: LARGE spike %.2f → %.2f (%.1fx), "
                        "allowing but flagging — verify manually if unexpected",
                        hwm, current_balance, spike_ratio,
                    )
                # Record the peak before resetting
                state["history"].append(
                    {
                        "hwm": hwm,
                        "new_hwm": current_balance,
                        "timestamp": time.time(),
                    }
                )
                # Trim history to last 100 entries
                state["history"] = state["history"][-100:]
            state["high_watermark"] = current_balance
            state["current_drawdown_pct"] = 0.0
            state["reset_at"] = time.time()
            self._save_state()
            # WO-1046b: a new HWM while a trip is outstanding still
            # goes through the tiered cooldown (dd=0 now; grades (1)
            # or (2) may fire if elapsed). Within the 48h window the
            # breaker stays tripped per spec — use manual fast-lane
            # if a genuine v-shaped recovery needs immediate re-arm.
            cr = self._cooldown_eval(0.0, current_balance,
                                     current_balance, "new_hwm")
            if cr is not None:
                if cr["tripped"]:
                    cr["reason"] = (
                        "New high watermark recorded; tiered cooldown "
                        "still active: " + cr["reason"]
                    )
                return cr
            return {
                "tripped": False,
                "drawdown_pct": 0.0,
                "high_watermark": current_balance,
                "action": "RESET",
                "reason": "New high watermark established",
            }

        if hwm <= 0:
            # First run, set initial watermark
            state["high_watermark"] = current_balance
            self._save_state()
            return {
                "tripped": False,
                "drawdown_pct": 0.0,
                "high_watermark": current_balance,
                "action": "INIT",
                "reason": "Initial watermark set",
            }

        # Calculate drawdown
        drawdown = (hwm - current_balance) / hwm
        state["current_drawdown_pct"] = round(drawdown * 100, 2)

        if round(drawdown * 100, 2) > state["max_drawdown_pct"]:
            state["max_drawdown_pct"] = round(drawdown * 100, 2)

        # WO-1046b: while a trip is outstanding, the tiered cooldown owns
        # the outcome even when dd has fallen back below the hard stop —
        # the legacy instant re-arm (unbuffered churn exposure) is only
        # reachable when the cooldown is disabled via kv.
        cr = self._cooldown_eval(
            round(drawdown * 100, 2), hwm, current_balance, "hard_stop_zone")
        # WO-1046f: while TRIPPED and FLAT the C+ branch owns the outcome —
        # the tiered dd-narrowing math is structurally unreachable there
        # (equity frozen at the trip level). positions_open is passed by
        # risk_manager; None/omitted = legacy callers never evaluate it.
        if positions_open == 0 and state.get("tripped_at"):
            fr = self._flat_reset_eval(
                round(drawdown * 100, 2), hwm, current_balance)
            if fr is not None and fr.get("action") == "RESET":
                self._save_state()
                return fr
            if fr is not None:
                if cr is None:
                    # cooldown rolled back while flat: synthesize the
                    # legacy HOLD shape and annotate with flat status
                    cr = {
                        "tripped": True,
                        "drawdown_pct": round(drawdown * 100, 2),
                        "high_watermark": hwm,
                        "action": "HOLD",
                        "reason": (
                            f"Breaker still tripped: "
                            f"{drawdown*100:.1f}% drawdown"
                        ),
                    }
                cr["flat_reset"] = fr
        if cr is not None:
            self._save_state()
            return cr

        # Check hard stop
        if drawdown >= self.HARD_STOP_PCT:
            if not state["tripped_at"]:
                now = time.time()
                state["tripped_at"] = now
                state["tripped_count"] = state.get("tripped_count", 0) + 1
                # (4) anti-churn: re-trip within 72h of ANY reset
                # (auto or manual) is a chain crash -> multiplier x2
                cfg = self._get_cooldown_cfg()
                mult = 1
                if cfg.get("enabled"):
                    cst = self._load_cooldown_state()
                    lcr = cst.get("last_chain_reset_at")
                    prev_mult = max(
                        1, int(cst.get("cooldown_multiplier", 1) or 1))
                    if lcr and (now - lcr) < float(
                            cfg["chain_window_hours"]) * 3600:
                        mult = min(
                            prev_mult * 2, int(cfg["chain_multiplier_cap"]))
                        self._append_event(cst, {
                            "type": "chain_crash", "ts": now,
                            "prev_mult": prev_mult, "new_mult": mult,
                        })
                    else:
                        mult = 1
                    cst["cooldown_multiplier"] = mult
                    cst["last_escalation_ts"] = None
                    cst["escalation_rounds"] = 0
                    self._save_cooldown_state(cst)
                # WO-1046f flat-cycle bookkeeping (independent of the
                # cooldown rollback switch): snapshot Fear&Greed at trip
                # (G4 sub3 anchor), reset the G3 peak cache / armed state,
                # and apply the re-trip rule — a re-trip within
                # retrip_disable_hours of a FLAT auto-reset disables flat
                # auto-reset for this cycle (manual-only); a fresh cycle
                # (beyond the chain window) re-arms it.
                fcfg = self._get_flat_cfg()
                if fcfg.get("enabled"):
                    cst2 = self._load_cooldown_state()
                    lcr2 = cst2.get("last_chain_reset_at")
                    fresh_cycle = not (
                        lcr2 and (now - lcr2)
                        < float(cfg["chain_window_hours"]) * 3600)
                    flat_last = cst2.get("flat_last_reset_at")
                    flat_retrip = (
                        flat_last is not None
                        and (now - flat_last)
                        < float(fcfg["retrip_disable_hours"]) * 3600)
                    if fresh_cycle:
                        cst2["flat_auto_disabled"] = False
                    elif flat_retrip and not cst2.get("flat_auto_disabled"):
                        cst2["flat_auto_disabled"] = True
                        self._audit("DRAWDOWN_BREAKER_FLAT_AUTO_DISABLED", {
                            "h_since_flat_reset": round(
                                (now - flat_last) / 3600.0, 1),
                        })
                        self._outbox_alert(
                            f"dd_breaker_flat_disabled_{int(now)}",
                            "drawdown_breaker_flat_disabled",
                            "\u26d4\ufe0f Flat auto-reset DISABLED this cycle",
                            "\u26d4\ufe0f Re-trip within 72h of a flat "
                            "auto-reset — flat auto-reset disabled for "
                            "this cycle, manual reset only. @Travis @Leo",
                        )
                    cst2["fng_at_trip"] = self._fetch_fng_now()
                    cst2["vol_peak_48h"] = None
                    cst2["flat_reset_armed_at"] = None
                    cst2["flat_anchor_open_ms"] = None
                    cst2["flat_gate_snapshot"] = None
                    self._save_cooldown_state(cst2)
                self._save_state()
                self._audit("DRAWDOWN_BREAKER_TRIPPED", {
                    "dd_pct": round(drawdown * 100, 2), "multiplier": mult,
                    "hwm": hwm, "balance": current_balance,
                })
                # (6) trip instant high-priority alert (outbox + Feishu)
                self._notify_tripped(drawdown, hwm, current_balance, mult)
                return {
                    "tripped": True,
                    "drawdown_pct": round(drawdown * 100, 2),
                    "high_watermark": hwm,
                    "action": "TRIP",
                    "reason": f"Hard stop triggered: {drawdown*100:.1f}% drawdown >= 10% limit",
                }
            else:
                # Already tripped, stay tripped (cooldown eval above owns
                # the graded path; reaching here means cooldown disabled)
                self._save_state()
                return {
                    "tripped": True,
                    "drawdown_pct": round(drawdown * 100, 2),
                    "high_watermark": hwm,
                    "action": "HOLD",
                    "reason": f"Breaker still tripped: {drawdown*100:.1f}% drawdown",
                }

        self._save_state()
        return {
            "tripped": False,
            "drawdown_pct": round(drawdown * 100, 2),
            "high_watermark": hwm,
            "action": "HOLD",
            "reason": f"Drawdown {drawdown*100:.1f}% within limit",
        }

    def reset(self, new_balance: Optional[float] = None,
              actor: str = "manual", reason: str = ""):
        """Manually reset the breaker (requires human confirmation).

        Only call this after investigating why drawdown occurred.
        Pass the current portfolio balance to set a correct watermark.

        WO-1046b (5) manual fast-lane: caller SHOULD pass actor + reason
        (Travis triage: system-bug / black-swan / normal-vol). The reset
        is audited (audit_log + cooldown events) and arms the same 72h
        anti-churn window as an auto-reset, so a re-trip right after a
        manual reset still counts as a chain crash (multiplier x2).
        """
        state = self._state
        if new_balance is not None and new_balance > 0:
            state["high_watermark"] = new_balance
        else:
            # No balance provided — keep existing watermark but clear tripped state
            pass
        state["current_drawdown_pct"] = 0.0
        state["tripped_at"] = None
        state["tripped_count"] = 0
        state["max_drawdown_pct"] = 0.0
        state["reset_at"] = time.time()
        # Cooldown bookkeeping: manual reset re-arms the chain window and
        # restarts the multiplier (human triage supersedes the machine's
        # escalation ladder).
        cst = self._load_cooldown_state()
        cst["last_chain_reset_at"] = time.time()
        cst["cooldown_multiplier"] = 1
        cst["last_escalation_ts"] = None
        cst["escalation_rounds"] = 0
        # WO-1046f: manual reset is the human fast-lane — it clears all
        # flat-branch state (pending ARM, this-cycle disable) and the
        # ramp flag: human triage supersedes the machine's ladder.
        cst["flat_reset_armed_at"] = None
        cst["flat_auto_disabled"] = False
        cst["flat_last_reset_at"] = None
        cst["flat_anchor_open_ms"] = None
        cst["flat_gate_snapshot"] = None
        try:
            from src.state_db import get_state_db
            get_state_db().kv_set("drawdown_breaker_flat_ramp", None)
        except Exception:
            pass
        self._append_event(cst, {
            "type": "manual_reset", "ts": time.time(),
            "actor": actor, "reason": reason,
        })
        self._save_cooldown_state(cst)
        self._save_state()
        self._audit("DRAWDOWN_BREAKER_MANUAL_RESET", {
            "actor": actor, "reason": reason, "hwm": state["high_watermark"],
        })
        logger.warning(
            "DrawdownBreaker: MANUAL RESET at %.2f (actor=%s reason=%s)",
            state["high_watermark"], actor, reason or "<none>")

    def get_status(self) -> Dict:
        """Return current drawdown status (incl. tiered-cooldown view)."""
        cfg = self._get_cooldown_cfg()
        fcfg = self._get_flat_cfg()
        cst = self._load_cooldown_state()
        tripped_at = self._state["tripped_at"]
        mult = max(1, int(cst.get("cooldown_multiplier", 1) or 1))
        elapsed_h = (
            round((time.time() - tripped_at) / 3600.0, 2)
            if tripped_at else None
        )
        return {
            "high_watermark": self._state["high_watermark"],
            "current_drawdown_pct": self._state["current_drawdown_pct"],
            "max_drawdown_pct": self._state.get("max_drawdown_pct", 0.0),
            "tripped": tripped_at is not None,
            "tripped_count": self._state.get("tripped_count", 0),
            "tripped_at": tripped_at,
            "cooldown": {
                "enabled": bool(cfg.get("enabled")),
                "multiplier": mult,
                "elapsed_h": elapsed_h,
                "early_reset_at_h": float(cfg["early_reset_hours"]) * mult,
                "recheck_at_h": float(cfg["recheck_hours"]) * mult,
                "last_auto_reset_at": cst.get("last_auto_reset_at"),
                "escalation_rounds": cst.get("escalation_rounds", 0),
            },
            "flat_reset": {
                "enabled": bool(fcfg.get("enabled")),
                "auto_disabled": bool(cst.get("flat_auto_disabled")),
                "armed_at": cst.get("flat_reset_armed_at"),
                "last_fire_at": cst.get("flat_last_reset_at"),
                "fng_at_trip": cst.get("fng_at_trip"),
                "gates_snapshot": cst.get("flat_gate_snapshot"),
            },
        }
