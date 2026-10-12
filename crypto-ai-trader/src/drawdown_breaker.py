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

STORAGE: SQLite state.db drawdown table (trip/HWM truth) + kv table
(drawdown_breaker_cooldown_state: multiplier/chain window/events).
No JSON files are used; all state is persisted via StateDB.
"""

import logging
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

    def check_drawdown(self, current_balance: float) -> Dict:
        """Check if portfolio drawdown exceeds the 10% hard stop.

        Args:
            current_balance: Current total portfolio value in USDT

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
        }
