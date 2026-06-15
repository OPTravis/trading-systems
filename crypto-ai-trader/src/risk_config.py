"""
Unified Risk Parameters Loader.

Loads all risk control parameters from config/risk_params.yaml.
Provides graceful fallback to hardcoded defaults if the config
file is missing or a specific key is absent.

Usage:
    from src.risk_config import get_risk_param, load_risk_config

    # Get a single parameter
    max_failures = get_risk_param('circuit_breaker', 'consecutive_failures_max', 5)

    # Load an entire section
    cb_config = load_risk_config().get('circuit_breaker', {})

Design principles:
    - Never raises on missing file/key — always falls back to defaults.
    - Config is cached via lru_cache for performance.
    - Each consuming module defines its own hardcoded defaults,
      so removing this file or risk_params.yaml does not break anything.
"""

import logging
import os
from functools import lru_cache
from typing import Any, Dict

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "config",
    "risk_params.yaml",
)


@lru_cache(maxsize=1)
def load_risk_config() -> Dict[str, Any]:
    """Load the full risk parameters configuration.

    Returns:
        Dict with all sections from risk_params.yaml.
        Returns empty dict if file is missing or unreadable.
    """
    try:
        with open(_CONFIG_PATH, "r") as f:
            config = yaml.safe_load(f)
            if config is None:
                logger.warning("risk_params.yaml is empty — using module defaults")
                return {}
            logger.debug("risk_params.yaml loaded successfully")
            return config
    except FileNotFoundError:
        logger.debug("risk_params.yaml not found — using module defaults")
        return {}
    except yaml.YAMLError as e:
        logger.warning("risk_params.yaml parse error: %s — using module defaults", e)
        return {}
    except Exception as e:
        logger.warning("Failed to load risk_params.yaml: %s — using module defaults", e)
        return {}


def get_risk_param(section: str, key: str, default: Any = None) -> Any:
    """Get a specific parameter from the risk config.

    Args:
        section: Top-level section name (e.g. 'circuit_breaker').
        key: Parameter key within the section.
        default: Fallback value if section or key is missing.

    Returns:
        The parameter value, or default if not found.
    """
    config = load_risk_config()
    return config.get(section, {}).get(key, default)


def get_section(section: str) -> Dict[str, Any]:
    """Get an entire section from the risk config.

    Args:
        section: Top-level section name.

    Returns:
        Dict of parameters for that section, or empty dict.
    """
    return load_risk_config().get(section, {})


# ── Convenience getters for each module ──

def get_circuit_breaker_config() -> Dict[str, Any]:
    """Get circuit_breaker section."""
    return get_section("circuit_breaker")


def get_daily_loss_config() -> Dict[str, Any]:
    """Get daily_loss_breaker section."""
    return get_section("daily_loss_breaker")


def get_drawdown_breaker_config() -> Dict[str, Any]:
    """Get drawdown_breaker section."""
    return get_section("drawdown_breaker")


def get_stepwise_drawdown_config() -> Dict[str, Any]:
    """Get stepwise_drawdown section."""
    return get_section("stepwise_drawdown")


def get_trade_executor_config() -> Dict[str, Any]:
    """Get trade_executor section."""
    return get_section("trade_executor")
