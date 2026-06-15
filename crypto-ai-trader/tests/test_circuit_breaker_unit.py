"""
Unit tests for circuit_breaker.py — mock-based, no network.

Covers:
  - Thread-safe singleton get_circuit_breaker() (P0-4)
  - record_failure escalation to CONSECUTIVE_FAILURES_MAX → trip
  - record_success resets failure count
  - check_drawdown trips at DRAWDOWN_TRIP_PCT
  - is_tripped auto-recovery after TRIP_DURATION_SEC
  - reset() clears all state
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src import circuit_breaker
from src.circuit_breaker import (
    CircuitBreaker,
    CONSECUTIVE_FAILURES_MAX,
    FAILURE_WINDOW_SEC,
    TRIP_DURATION_SEC,
    DRAWDOWN_TRIP_PCT,
    get_circuit_breaker,
)


@pytest.fixture(autouse=True)
def _reset_cb_singleton():
    """Reset module-level singleton between tests."""
    circuit_breaker._cb_instance = None
    yield
    circuit_breaker._cb_instance = None


def _make_cb():
    """Create a CircuitBreaker with mocked StateDB (avoids real DB)."""
    with patch.object(CircuitBreaker, "_load_state", lambda self: None), \
         patch.object(CircuitBreaker, "_save_state", lambda self: None):
        return CircuitBreaker()


# ────────────────────────────────────────────────────────────
# Thread-safe singleton (P0-4)
# ────────────────────────────────────────────────────────────

class TestCircuitBreakerSingleton:

    def test_get_circuit_breaker_returns_same_instance(self):
        """Multiple calls return the same singleton."""
        with patch.object(CircuitBreaker, "_load_state", lambda self: None), \
             patch.object(CircuitBreaker, "_save_state", lambda self: None):
            cb1 = get_circuit_breaker()
            cb2 = get_circuit_breaker()
            assert cb1 is cb2

    def test_thread_safe_singleton(self):
        """P0-4: Concurrent threads get exactly one instance."""
        with patch.object(CircuitBreaker, "_load_state", lambda self: None), \
             patch.object(CircuitBreaker, "_save_state", lambda self: None):
            instances = []
            barrier = threading.Barrier(CONSECUTIVE_FAILURES_MAX)

            def _get():
                barrier.wait()
                instances.append(get_circuit_breaker())

            threads = [threading.Thread(target=_get) for _ in range(CONSECUTIVE_FAILURES_MAX)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert all(inst is instances[0] for inst in instances)
            # All CONSECUTIVE_FAILURES_MAX threads got the same object
            assert len(set(id(i) for i in instances)) == 1


# ────────────────────────────────────────────────────────────
# record_failure → trip
# ────────────────────────────────────────────────────────────

class TestRecordFailure:

    def test_below_threshold_not_tripped(self):
        cb = _make_cb()
        for i in range(CONSECUTIVE_FAILURES_MAX - 1):
            cb.record_failure(f"source_{i}")
        assert cb.is_tripped() is False

    def test_at_threshold_trips(self):
        """Reaching CONSECUTIVE_FAILURES_MAX consecutive failures → trip."""
        cb = _make_cb()
        for i in range(CONSECUTIVE_FAILURES_MAX):
            cb.record_failure(f"source_{i}")
        assert cb.is_tripped() is True

    def test_failure_count_increments(self):
        cb = _make_cb()
        cb.record_failure("api")
        cb.record_failure("api")
        status = cb.get_status()
        assert status["failure_count"] == 2

    def test_failure_window_expiry_resets_count(self):
        """Failures outside the window should reset count."""
        cb = _make_cb()
        cb.record_failure("api")
        assert cb.get_status()["failure_count"] == 1

        # Simulate window expiry
        cb._first_failure_ts = time.time() - (FAILURE_WINDOW_SEC + 1)
        cb.record_failure("api")
        # The window expired, so _first_failure_ts was reset and count started at 1
        assert cb.get_status()["failure_count"] == 1


# ────────────────────────────────────────────────────────────
# record_success resets
# ────────────────────────────────────────────────────────────

class TestRecordSuccess:

    def test_success_resets_failure_count(self):
        cb = _make_cb()
        cb.record_failure("api")
        cb.record_failure("api")
        assert cb.get_status()["failure_count"] == 2

        cb.record_success()
        assert cb.get_status()["failure_count"] == 0
        assert cb.get_status()["first_failure_ts"] is None

    def test_success_after_near_trip_prevents_trip(self):
        """4 failures → success → 4 more failures should not trip."""
        cb = _make_cb()
        for i in range(CONSECUTIVE_FAILURES_MAX - 1):
            cb.record_failure(f"src_{i}")
        cb.record_success()
        for i in range(CONSECUTIVE_FAILURES_MAX - 1):
            cb.record_failure(f"src_{i}")
        assert cb.is_tripped() is False


# ────────────────────────────────────────────────────────────
# check_drawdown
# ────────────────────────────────────────────────────────────

class TestCheckDrawdown:

    def test_drawdown_below_threshold_no_trip(self):
        cb = _make_cb()
        with patch("src.state_db.get_state_db") as mock_get_db:
            db = MagicMock()
            db.drawdown_get.return_value = {"high_watermark": 10000}
            mock_get_db.return_value = db
            # 15% drawdown < 20% threshold
            result = cb.check_drawdown(8500.0)
            assert result is False

    def test_drawdown_above_threshold_trips(self):
        """Drawdown >= DRAWDOWN_TRIP_PCT → indefinite trip."""
        cb = _make_cb()
        with patch("src.state_db.get_state_db") as mock_get_db:
            db = MagicMock()
            db.drawdown_get.return_value = {"high_watermark": 10000}
            mock_get_db.return_value = db
            # 25% drawdown > 20% threshold
            result = cb.check_drawdown(7500.0)
            assert result is True
            # Tripped indefinitely (tripped_until=None means manual reset)
            assert cb.get_status()["tripped_until"] is None

    def test_drawdown_at_exact_threshold_trips(self):
        cb = _make_cb()
        with patch("src.state_db.get_state_db") as mock_get_db:
            db = MagicMock()
            db.drawdown_get.return_value = {"high_watermark": 10000}
            mock_get_db.return_value = db
            # exactly 20% drawdown
            result = cb.check_drawdown(8000.0)
            assert result is True

    def test_no_high_watermark_no_trip(self):
        """No high watermark → can't compute drawdown → no trip."""
        cb = _make_cb()
        with patch("src.state_db.get_state_db") as mock_get_db:
            db = MagicMock()
            db.drawdown_get.return_value = {"high_watermark": 0}
            mock_get_db.return_value = db
            result = cb.check_drawdown(5000.0)
            assert result is False

    def test_drawdown_db_error_no_trip(self):
        """DB error during drawdown check → no crash, no trip."""
        cb = _make_cb()
        with patch("src.state_db.get_state_db", side_effect=Exception("DB error")):
            result = cb.check_drawdown(5000.0)
            assert result is False


# ────────────────────────────────────────────────────────────
# is_tripped auto-recovery
# ────────────────────────────────────────────────────────────

class TestIsTrippedAutoRecovery:

    def test_timed_trip_expires(self):
        """Timed trip (from record_failure) auto-recovers after duration."""
        cb = _make_cb()
        # Force a trip
        for i in range(CONSECUTIVE_FAILURES_MAX):
            cb.record_failure(f"src_{i}")
        assert cb.is_tripped() is True

        # Fast-forward past trip duration
        status = cb.get_status()
        cb._tripped_until = time.time() - 1  # expired
        assert cb.is_tripped() is False

    def test_indefinite_trip_requires_manual_reset(self):
        """Drawdown trip (tripped_until=None) never auto-recovers."""
        cb = _make_cb()
        cb._trip_reason = "Drawdown 25% — manual reset required"
        cb._tripped_until = None  # indefinite
        assert cb.is_tripped() is True
        # Even after a long time
        cb._tripped_until = None
        assert cb.is_tripped() is True


# ────────────────────────────────────────────────────────────
# reset()
# ────────────────────────────────────────────────────────────

class TestReset:

    def test_reset_clears_all_state(self):
        cb = _make_cb()
        cb.record_failure("a")
        cb.record_failure("b")
        cb._trip_reason = "test trip"
        cb._tripped_until = time.time() + 1000

        cb.reset()
        status = cb.get_status()
        assert status["failure_count"] == 0
        assert status["first_failure_ts"] is None
        assert status["tripped_until"] is None
        assert status["trip_reason"] == ""

    def test_reset_allows_trading_after_trip(self):
        cb = _make_cb()
        for i in range(CONSECUTIVE_FAILURES_MAX):
            cb.record_failure(f"src_{i}")
        assert cb.is_tripped() is True

        cb.reset()
        assert cb.is_tripped() is False
