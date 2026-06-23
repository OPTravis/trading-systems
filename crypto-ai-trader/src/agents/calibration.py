"""
Outcome-Based Score Calibration for Specialist Agents.

Tracks historical (predicted_score, actual_pnl_pct) pairs for each agent.
After collecting enough outcomes, computes a calibration factor that adjusts
future scores based on historical accuracy.

Storage: StateDB kv store with key pattern ``agent_calibration:{agent_name}``
"""

import json
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Minimum number of outcomes before calibration is applied
MIN_OUTCOMES = 20

# Agent-to-factor mappings for extracting agent-specific scores from trade outcomes.
# Keys are agent names (used in StateDB keys), values are factor names
# stored in the trade_outcomes.factors_json column.
AGENT_FACTOR_MAP: Dict[str, List[str]] = {
    "technical_agent": ["technical", "price_action", "bb_squeeze", "rsi_divergence"],
    "trend_agent": ["trend"],
    "volume_agent": ["volume"],
    "sentiment_agent": ["sentiment"],
    "onchain_agent": ["onchain"],
    "market_sentiment_agent": ["market_sentiment"],
    "prepump_agent": ["obv_divergence", "consolidation"],
}


def get_outcome_history(
    agent_name: str,
    db=None,
) -> List[Tuple[float, float]]:
    """Retrieve stored outcome history for *agent_name*.

    Returns:
        List of (predicted_score_0_1, pnl_pct) pairs, or empty list.
    """
    if db is None:
        from src.state_db import get_state_db

        db = get_state_db()
    key = f"agent_calibration:{agent_name}"
    data = db.kv_get(key, None)
    if data and isinstance(data, dict) and "outcomes" in data:
        return [tuple(pair) for pair in data["outcomes"]]
    return []


def update_calibration(
    agent_name: str,
    predicted_score_0_1: float,
    pnl_pct: float,
    db=None,
) -> Optional[float]:
    """Record a new outcome and recompute calibration factor.

    Args:
        agent_name: Agent identifier (e.g. ``"technical_agent"``).
        predicted_score_0_1: The score the agent assigned, normalized to 0-1.
        pnl_pct: Actual net PnL percentage the trade produced.
        db: Optional StateDB instance.

    Returns:
        Updated calibration factor (clamped to [0.5, 2.0]), or ``None``
        when fewer than ``MIN_OUTCOMES`` outcomes have been recorded.
    """
    if db is None:
        from src.state_db import get_state_db

        db = get_state_db()

    key = f"agent_calibration:{agent_name}"
    data = db.kv_get(key, None)
    if data and isinstance(data, dict) and "outcomes" in data:
        outcomes = data["outcomes"]
    else:
        outcomes = []

    outcomes.append([predicted_score_0_1, pnl_pct])

    calibration_factor = None
    if len(outcomes) >= MIN_OUTCOMES:
        calibration_factor = _compute_calibration_factor(outcomes)

    record = {
        "outcomes": outcomes,
        "count": len(outcomes),
        "calibration_factor": calibration_factor,
    }
    db.kv_set(key, record)
    return calibration_factor


def _compute_calibration_factor(
    outcomes: List,
) -> float:
    """Derive a calibration factor from outcome pairs.

    Formula::

        calibration = mean(pnl | score > 0.7) / mean(pnl | score < 0.3)

    If any denominator is missing, defaults to 1.0 (no-op).
    """
    high = [pnl for score, pnl in outcomes if score > 0.7]
    low = [pnl for score, pnl in outcomes if score < 0.3]

    mean_high = sum(high) / len(high) if high else None
    mean_low = sum(low) / len(low) if low else None

    if mean_high is None or mean_low is None:
        return 1.0

    if abs(mean_low) < 1e-9:
        return 2.0 if mean_high > 0 else 1.0

    return mean_high / mean_low


def apply_calibration(
    agent_name: str,
    raw_score: float,
    db=None,
) -> float:
    """Apply stored calibration to a raw score.

    Args:
        agent_name: Agent identifier.
        raw_score: Raw score in the 0-100 range.
        db: Optional StateDB instance.

    Returns:
        Adjusted score (still 0-100), unchanged when fewer than
        ``MIN_OUTCOMES`` outcomes are available.
    """
    try:
        if db is None:
            from src.state_db import get_state_db

            db = get_state_db()

        key = f"agent_calibration:{agent_name}"
        data = db.kv_get(key, None)

        if not data or not isinstance(data, dict):
            return raw_score
        if data.get("count", 0) < MIN_OUTCOMES:
            return raw_score
        if "calibration_factor" not in data or data["calibration_factor"] is None:
            return raw_score

        factor = max(0.5, min(2.0, data["calibration_factor"]))
        normalized = raw_score / 100.0
        adjusted = normalized * factor
        return max(0.0, min(100.0, adjusted * 100.0))
    except Exception as e:
        logger.debug(f"apply_calibration for {agent_name} failed: {e}")
        return raw_score


def record_agent_outcome(
    agent_name: str,
    raw_score: float,
    pnl_pct: float,
    db=None,
) -> Optional[float]:
    """Convenience wrapper: record outcome and recompute calibration.

    Args:
        agent_name: Agent identifier.
        raw_score: Raw score the agent produced (0-100 range).
        pnl_pct: Actual net PnL percentage.
        db: Optional StateDB instance.

    Returns:
        Updated calibration factor or ``None``.
    """
    return update_calibration(
        agent_name=agent_name,
        predicted_score_0_1=max(0.0, min(1.0, raw_score / 100.0)),
        pnl_pct=pnl_pct,
        db=db,
    )


def record_agent_outcomes_from_trade(
    factors_json: str,
    pnl_pct: float,
    db=None,
) -> Dict[str, Optional[float]]:
    """Record outcomes for all agents from a closed trade's factors JSON.

    Args:
        factors_json: JSON string from trade_outcomes.factors_json.
        pnl_pct: Actual net PnL percentage of the trade.
        db: Optional StateDB instance.

    Returns:
        Dict mapping agent name to its updated calibration factor.
    """
    if not factors_json:
        return {}

    try:
        factors = json.loads(factors_json) if isinstance(factors_json, str) else factors_json
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"record_agent_outcomes_from_trade: bad factors_json")
        return {}

    results: Dict[str, Optional[float]] = {}
    for agent_name, factor_keys in AGENT_FACTOR_MAP.items():
        scores = [factors[k] for k in factor_keys if k in factors]
        if not scores:
            continue
        avg_score = sum(scores) / len(scores)
        factor = record_agent_outcome(
            agent_name=agent_name,
            raw_score=avg_score,
            pnl_pct=pnl_pct,
            db=db,
        )
        results[agent_name] = factor

    return results
