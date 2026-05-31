"""
Strategy Exception Guard - Centralized exception handling for trading strategies.

Provides a decorator that wraps strategy methods with:
- Exception catching and logging
- Circuit breaker pattern (fail fast after N consecutive failures)
- Graceful degradation (return safe defaults on failure)

Usage:
    from src.strategy_guard import strategy_guard, CircuitOpen

    @strategy_guard(max_failures=3, cooldown_sec=300)
    def my_strategy_method(self, ...):
        ...
"""

import functools
import logging
import time
from typing import Any, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitOpen(Exception):
    """Raised when circuit breaker is open (too many failures)."""

    pass


class _CircuitBreaker:
    """Per-function circuit breaker state."""

    def __init__(self, max_failures: int = 3, cooldown_sec: int = 300):
        self.max_failures = max_failures
        self.cooldown_sec = cooldown_sec
        self.failure_count = 0
        self.last_failure_time: Optional[float] = None
        self.is_open = False

    def record_success(self):
        self.failure_count = 0
        self.is_open = False

    def record_failure(self) -> bool:
        """Record a failure. Returns True if circuit should open."""
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.max_failures:
            self.is_open = True
            return True
        return False

    def can_attempt(self) -> bool:
        """Check if we can attempt execution."""
        if not self.is_open:
            return True
        # Cooldown period passed?
        if (
            self.last_failure_time
            and (time.time() - self.last_failure_time) > self.cooldown_sec
        ):
            self.is_open = False
            self.failure_count = 0
            return True
        return False


# Global registry of circuit breakers: func_name -> _CircuitBreaker
_circuit_registry: dict[str, _CircuitBreaker] = {}


def strategy_guard(
    max_failures: int = 3,
    cooldown_sec: int = 300,
    default_return: Optional[Any] = None,
    log_level: int = logging.ERROR,
    reraise: tuple[type[Exception], ...] = (),
):
    """Decorator: wrap strategy method with exception protection.

    Args:
        max_failures: Open circuit after this many consecutive failures
        cooldown_sec: Keep circuit open for this many seconds
        default_return: Return this value on failure (if not reraising)
        log_level: Logging level for caught exceptions
        reraise: Tuple of exception types to always reraise (never catch)
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        func_name = f"{func.__module__}.{func.__qualname__}"

        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            # Get or create circuit breaker for this function
            if func_name not in _circuit_registry:
                _circuit_registry[func_name] = _CircuitBreaker(
                    max_failures, cooldown_sec
                )
            cb = _circuit_registry[func_name]

            # Check circuit breaker
            if not cb.can_attempt():
                logger.warning(f"Circuit OPEN for {func_name} — skipping execution")
                if default_return is not None:
                    return default_return
                raise CircuitOpen(f"Circuit breaker open for {func_name}")

            try:
                result = func(*args, **kwargs)
                cb.record_success()
                return result
            except Exception as e:
                # Check if we should reraise this exception type
                if isinstance(e, reraise):
                    raise

                # Record failure
                should_open = cb.record_failure()
                if should_open:
                    logger.log(
                        log_level,
                        f"Circuit OPENED for {func_name} after {cb.failure_count} failures: {e}",
                    )
                else:
                    logger.log(
                        log_level,
                        f"Strategy error in {func_name} ({cb.failure_count}/{max_failures}): {e}",
                    )

                # Return default or reraise
                if default_return is not None:
                    return default_return
                raise

        # Attach circuit breaker for external inspection
        wrapper._circuit_breaker = lambda: _circuit_registry.get(func_name)  # type: ignore[attr-defined]
        wrapper._circuit_status = lambda: {  # type: ignore[attr-defined]
            "func": func_name,
            "open": _circuit_registry.get(func_name, _CircuitBreaker()).is_open,
            "failures": getattr(_circuit_registry.get(func_name), "failure_count", 0),
        }

        return wrapper

    return decorator


def get_circuit_status() -> dict[str, dict]:
    """Get status of all circuit breakers."""
    return {
        name: {
            "open": cb.is_open,
            "failures": cb.failure_count,
            "last_failure": cb.last_failure_time,
            "max_failures": cb.max_failures,
            "cooldown_sec": cb.cooldown_sec,
        }
        for name, cb in _circuit_registry.items()
    }


def reset_circuit(func_name: str) -> bool:
    """Manually reset a circuit breaker."""
    if func_name in _circuit_registry:
        cb = _circuit_registry[func_name]
        cb.is_open = False
        cb.failure_count = 0
        cb.last_failure_time = None
        logger.info(f"Circuit breaker reset: {func_name}")
        return True
    return False


def reset_all_circuits():
    """Reset all circuit breakers."""
    for name, cb in _circuit_registry.items():
        cb.is_open = False
        cb.failure_count = 0
        cb.last_failure_time = None
    logger.info("All circuit breakers reset")
