"""
Fundamental Scorer - Scores stocks based on fundamental metrics.
"""
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class FundamentalScorer:
    """Scores stocks on fundamental metrics (0-100)."""

    # Ideal ranges for metrics
    METRIC_RANGES = {
        'pe_ratio': {'low': 5, 'high': 30, 'ideal_low': 10, 'ideal_high': 20},
        'pb_ratio': {'low': 0.5, 'high': 10, 'ideal_low': 1, 'ideal_high': 5},
        'roe': {'low': 0, 'high': 50, 'ideal_low': 15, 'ideal_high': 30},
        'debt_equity': {'low': 0, 'high': 3, 'ideal_low': 0, 'ideal_high': 1},
        'earnings_stability': {'low': 0, 'high': 1, 'ideal_low': 0.7, 'ideal_high': 1.0},
    }

    def score(self, symbol: str, metrics: Dict[str, float] = None) -> float:
        """Score a stock on fundamentals (0-100)."""
        if not metrics:
            return 50.0  # Neutral default without data

        scores = []
        weights = {
            'pe_ratio': 0.25,
            'pb_ratio': 0.20,
            'roe': 0.25,
            'debt_equity': 0.15,
            'earnings_stability': 0.15,
        }

        for metric, value in metrics.items():
            metric_score = self._score_metric(metric, value)
            weight = weights.get(metric, 0)
            scores.append(metric_score * weight)

        total_weight = sum(weights.get(m, 0) for m in metrics)
        if total_weight == 0:
            return 50.0

        return max(0.0, min(100.0, sum(scores) / total_weight))

    def _score_metric(self, metric: str, value: float) -> float:
        """Score a single metric on 0-100 scale."""
        ranges = self.METRIC_RANGES.get(metric)
        if not ranges:
            return 50.0

        low, high = ranges['low'], ranges['high']
        ideal_low, ideal_high = ranges['ideal_low'], ranges['ideal_high']

        # Inverted metrics (lower is better)
        if metric in ('pe_ratio', 'pb_ratio', 'debt_equity'):
            # Negative values for these metrics are bad — return low score
            if value < 0:
                return 10.0
            if value <= ideal_low:
                return 90.0
            elif value <= ideal_high:
                return 70.0
            elif value <= high:
                return 40.0
            else:
                return 10.0
        else:  # Higher is better
            if value >= ideal_high:
                return 90.0
            elif value >= ideal_low:
                return 70.0
            elif value >= low:
                return 40.0
            else:
                return 10.0
