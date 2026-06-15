"""
Global Circuit Breaker — system-wide safety halt for crypto trading.

Trips under these conditions:
1. Consecutive API failures >= 5 within 10 minutes → 30min pause
2. Total drawdown from ATH >= 20% → indefinite pause until manual reset
3. Unusual portfolio state (negative cash, ghost positions > 3)

Persisted to StateDB so cron runs pick up the tripped state.

Usage:
    from src.circuit_breaker import CircuitBreaker
    cb = CircuitBreaker()

    if cb.is_tripped():
        return  # stop all trading

    try:
        do_trade()
    except Exception:
        cb.record_failure()

    cb.record_success()
"""

import logging
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ── Hardcoded defaults (fallback if config file is missing) ──
_DEFAULT_CONSECUTIVE_FAILURES_MAX = 5
_DEFAULT_FAILURE_WINDOW_SEC = 600
_DEFAULT_TRIP_DURATION_SEC = 1800
_DEFAULT_DRAWDOWN_TRIP_PCT = 20.0
_DEFAULT_MAX_GHOST_POSITIONS = 3

# ── Load from unified risk config (with fallback to defaults) ──
try:
    from src.risk_config import get_risk_param
    CONSECUTIVE_FAILURES_MAX = get_risk_param(
        "circuit_breaker", "consecutive_failures_max", _DEFAULT_CONSECUTIVE_FAILURES_MAX
    )
    FAILURE_WINDOW_SEC = get_risk_param(
        "circuit_breaker", "failure_window_sec", _DEFAULT_FAILURE_WINDOW_SEC
    )
    TRIP_DURATION_SEC = get_risk_param(
        "circuit_breaker", "trip_duration_sec", _DEFAULT_TRIP_DURATION_SEC
    )
    DRAWDOWN_TRIP_PCT = get_risk_param(
        "circuit_breaker", "drawdown_trip_pct", _DEFAULT_DRAWDOWN_TRIP_PCT
    )
    MAX_GHOST_POSITIONS = get_risk_param(
        "circuit_breaker", "max_ghost_positions", _DEFAULT_MAX_GHOST_POSITIONS
    )
except Exception:
    # Fallback to all defaults if risk_config module itself fails to import
    CONSECUTIVE_FAILURES_MAX = _DEFAULT_CONSECUTIVE_FAILURES_MAX
    FAILURE_WINDOW_SEC = _DEFAULT_FAILURE_WINDOW_SEC
    TRIP_DURATION_SEC = _DEFAULT_TRIP_DURATION_SEC
    DRAWDOWN_TRIP_PCT = _DEFAULT_DRAWDOWN_TRIP_PCT
    MAX_GHOST_POSITIONS = _DEFAULT_MAX_GHOST_POSITIONS


class CircuitBreaker:
    """System-wide safety halt when anomalies accumulate."""

    def __init__(self):
        self._lock = threading.Lock()
        self._failure_count = 0
        self._first_failure_ts: Optional[float] = None
        self._tripped_until: Optional[float] = None
        self._trip_reason: str = ""
        # Load persisted state
        self._load_state()

    # ── Persistence ──

    def _load_state(self):
        """Load circuit breaker state from StateDB kv store."""
        try:
            from src.state_db import get_state_db

            db = get_state_db()
            state = db.kv_get("circuit_breaker:state", {})
            if state:
                self._failure_count = state.get("failure_count", 0)
                self._first_failure_ts = state.get("first_failure_ts")
                self._tripped_until = state.get("tripped_until")
                self._trip_reason = state.get("trip_reason", "")
        except Exception as e:
            logger.warning(f"CircuitBreaker: failed to load state: {e}")

    def _save_state(self):
        """Persist circuit breaker state to StateDB."""
        try:
            from src.state_db import get_state_db

            db = get_state_db()
            db.kv_set(
                "circuit_breaker:state",
                {
                    "failure_count": self._failure_count,
                    "first_failure_ts": self._first_failure_ts,
                    "tripped_until": self._tripped_until,
                    "trip_reason": self._trip_reason,
                },
            )
        except Exception as e:
            logger.warning(f"CircuitBreaker: failed to save state: {e}")

    # ── Public API ──

    def is_tripped(self) -> bool:
        """Check if trading should be halted. Returns True=halted, False=OK."""
        with self._lock:
            now = time.time()

            # Indefinite pause (e.g. 20% drawdown) — requires manual reset
            if self._tripped_until is None and self._trip_reason:
                logger.warning(
                    f"CircuitBreaker: TRIPPED — {self._trip_reason} "
                    f"(manual reset required)"
                )
                return True

            # Check if timed trip has expired
            if self._tripped_until and now >= self._tripped_until:
                self._reset_unlocked()
                return False

            if self._tripped_until and now < self._tripped_until:
                remaining = int((self._tripped_until - now) / 60)
                logger.warning(
                    f"CircuitBreaker: TRIPPED — {self._trip_reason} "
                    f"(resumes in {remaining}min)"
                )
                return True

            return False

    def _trip(self, reason: str):
        """Internal: trip the circuit breaker (called with lock held)."""
        self._tripped_until = time.time() + TRIP_DURATION_SEC
        self._trip_reason = reason
        self._save_state()
        logger.critical(
            f"CircuitBreaker: TRIPPED — {reason} "
            f"(resumes in {TRIP_DURATION_SEC//60}min)"
        )

    def record_failure(self, source: str = ""):
        """Record an API or trade failure. May trigger circuit breaker."""
        with self._lock:
            now = time.time()

            # Reset failure window if expired
            if (
                self._first_failure_ts
                and (now - self._first_failure_ts) > FAILURE_WINDOW_SEC
            ):
                self._failure_count = 0
                self._first_failure_ts = None

            if self._first_failure_ts is None:
                self._first_failure_ts = now

            self._failure_count += 1
            logger.warning(
                f"CircuitBreaker: failure #{self._failure_count} from '{source}' "
                f"(window: {(now - self._first_failure_ts):.0f}s)"
            )

            if self._failure_count >= CONSECUTIVE_FAILURES_MAX:
                self._trip(
                    self._trip_reason
                    or f"{self._failure_count} consecutive failures in {FAILURE_WINDOW_SEC}s"
                )

            self._save_state()

    def record_success(self):
        """Reset failure counter on successful operation."""
        with self._lock:
            if self._failure_count > 0:
                logger.info(
                    f"CircuitBreaker: success — resetting failure counter ({self._failure_count}→0)"
                )
            self._failure_count = 0
            self._first_failure_ts = None
            self._save_state()

    def check_drawdown(self, current_equity: float) -> bool:
        """Check for drawdown-based trip.

        Args:
            current_equity: Total portfolio value in USDT

        Returns True if trading is blocked by drawdown.
        """
        with self._lock:
            try:
                from src.state_db import get_state_db

                db = get_state_db()
                dd_state = db.drawdown_get()
                high_watermark = dd_state.get("high_watermark", 0)
                if high_watermark > 0:
                    dd_pct = (high_watermark - current_equity) / high_watermark * 100
                    if dd_pct >= DRAWDOWN_TRIP_PCT:
                        self._tripped_until = None  # indefinite pause
                        self._trip_reason = f"Drawdown {dd_pct:.1f}% >= {DRAWDOWN_TRIP_PCT}% — manual reset required"
                        self._save_state()
                        logger.critical(f"CircuitBreaker: {self._trip_reason}")
                        return True
            except Exception as e:
                logger.warning(f"CircuitBreaker: drawdown check failed: {e}")
            return False

    def _reset_unlocked(self):
        """Internal reset without acquiring lock (called when lock already held)."""
        self._failure_count = 0
        self._first_failure_ts = None
        self._tripped_until = None
        self._trip_reason = ""
        self._save_state()
        logger.info("CircuitBreaker: auto-reset — trip expired, trading resumed")

    def reset(self):
        """Manually reset the circuit breaker."""
        with self._lock:
            self._reset_unlocked()
            logger.info("CircuitBreaker: manually reset — trading resumed")

    def get_status(self) -> Dict:
        """Return current status for monitoring."""
        with self._lock:
            return {
                "failure_count": self._failure_count,
                "first_failure_ts": self._first_failure_ts,
                "tripped_until": self._tripped_until,
                "trip_reason": self._trip_reason,
            }


# Singleton
_cb_instance: Optional[CircuitBreaker] = None
_cb_singleton_lock = threading.Lock()


def get_circuit_breaker() -> CircuitBreaker:
    """Get singleton CircuitBreaker instance (thread-safe)."""
    global _cb_instance
    if _cb_instance is None:
        with _cb_singleton_lock:
            if _cb_instance is None:  # double-checked locking
                _cb_instance = CircuitBreaker()
    return _cb_instance
