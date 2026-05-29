"""
Stock Scorer - Multi-dimensional stock scoring with IC dynamic weighting.
"""
from dataclasses import dataclass
from typing import Dict, Optional
import logging

import yaml

logger = logging.getLogger(__name__)


@dataclass
class StockScore:
    """Composite stock score with dimension breakdown."""
    symbol: str
    composite: float  # 0-100
    technical: float = 0.0
    fundamental: float = 0.0
    momentum: float = 0.0
    sentiment: float = 0.0
    quality: Optional[float] = None
    value: Optional[float] = None
    weights: Dict[str, float] = None

    def __post_init__(self):
        if self.weights is None:
            self.weights = {}


class StockScorer:
    """Multi-dimensional stock scoring with IC-based dynamic factor weights."""

    def __init__(self, ic_tracker=None, fundamental_scorer=None, sentiment_scorer=None, feature_store=None):
        self.ic_tracker = ic_tracker
        self.fundamental_scorer = fundamental_scorer
        self.sentiment_scorer = sentiment_scorer
        self.feature_store = feature_store
        # Default equal weights
        self._default_weights = {
            'technical': 0.20,
            'fundamental': 0.20,
            'momentum': 0.15,
            'sentiment': 0.15,
            'quality': 0.15,
            'value': 0.15,
        }
        # Load per-symbol strategy allocation
        self._strategy_allocation = self._load_strategy_allocation()

    @staticmethod
    def _load_strategy_allocation() -> dict:
        """Load per-symbol strategy weights from config/strategy_allocation.yaml."""
        from pathlib import Path
        config_path = Path(__file__).resolve().parent.parent.parent / "config" / "strategy_allocation.yaml"
        if not config_path.exists():
            return {}
        try:
            with open(config_path) as f:
                data = yaml.safe_load(f)
            return data.get("symbols", {})
        except Exception:
            return {}

    def _get_weights(self, symbol: str = None) -> Dict[str, float]:
        """Get factor weights: per-symbol strategy allocation > IC-tracker > defaults."""
        # 1. Per-symbol strategy allocation (highest priority)
        if symbol and symbol in self._strategy_allocation:
            alloc = self._strategy_allocation[symbol]
            weights = alloc.get("weights", {})
            if weights:
                total = sum(weights.values())
                if total > 0:
                    return {k: v / total for k, v in weights.items()}

        # 2. IC-tracker weights
        if self.ic_tracker:
            weights = self.ic_tracker.get_weights()
            if weights:
                total = sum(weights.values())
                if total > 0:
                    return {k: v / total for k, v in weights.items()}

        # 3. Default equal weights
        return self._default_weights.copy()

    def _get_feature_store_scores(self, symbol: str) -> dict:
        """Get latest factor scores from FeatureStore for a symbol."""
        if not self.feature_store:
            return {}
        try:
            df = self.feature_store.get_factor_values(
                date=None,  # latest
                symbols=[symbol],
            )
            if df.empty:
                return {}
            # Pivot to factor_name → value
            latest = df.groupby("factor_name")["value"].last()
            return latest.to_dict()
        except Exception:
            return {}

    def score_stock(self, symbol: str, market_data: dict = None) -> StockScore:
        """Score a stock across all dimensions (0-100 scale)."""
        market_data = market_data or {}

        # Try feature store first (real computed factors)
        fs_scores = self._get_feature_store_scores(symbol)

        technical = fs_scores.get("technical", self._score_technical(symbol, market_data))
        fundamental = self._score_fundamental(symbol)
        momentum = fs_scores.get("momentum", self._score_momentum(symbol, market_data))
        sentiment = self._score_sentiment(symbol)
        quality = fs_scores.get("quality", self._score_quality(symbol))
        value = fs_scores.get("value_score", self._score_value(symbol))

        # Collect factor scores; None means "no data — skip this factor"
        scores = {
            'technical': technical,
            'fundamental': fundamental,
            'momentum': momentum,
            'sentiment': sentiment,
            'quality': quality,
            'value': value,
        }

        weights = self._get_weights(symbol=symbol)

        # Build a set of factors that have real scores (not None)
        active_factors = {k for k, v in scores.items() if v is not None}
        skipped_factors = {k for k in scores if k not in active_factors}

        # Redistribute weight from skipped factors proportionally among active ones
        if skipped_factors and active_factors:
            skipped_weight = sum(weights.get(k, 0) for k in skipped_factors)
            active_weight = sum(weights.get(k, 0) for k in active_factors)
            if active_weight > 0:
                logger.info(
                    "Redistributing weight %.4f from skipped factors %s to active factors %s",
                    skipped_weight, skipped_factors, active_factors,
                )
                weights = {
                    k: (weights.get(k, 0) + skipped_weight * weights.get(k, 0) / active_weight)
                    if k in active_factors else 0.0
                    for k in weights
                }
            else:
                logger.warning(
                    "No active weight to redistribute — skipped factors %s have all the weight; "
                    "falling back to equal weights for active factors",
                    skipped_factors,
                )
                n_active = len(active_factors)
                weights = {k: (1.0 / n_active if k in active_factors else 0.0) for k in weights}

        # Compute composite using only active (non-None) factors
        composite = sum(
            (scores[k] or 0.0) * weights.get(k, 0)
            for k in scores
            if k in active_factors
        )

        return StockScore(
            symbol=symbol,
            composite=min(100.0, max(0.0, composite)),
            technical=technical,
            fundamental=fundamental,
            momentum=momentum,
            sentiment=sentiment,
            quality=quality if quality is not None else 0.0,
            value=value if value is not None else 0.0,
            weights=weights,
        )

    def _score_technical(self, symbol: str, data: dict) -> float:
        """Score based on technical indicators (RSI, MACD, Bollinger, etc.)."""
        score = 50.0  # neutral default
        rsi = data.get('rsi')
        macd_signal = data.get('macd_signal', 0)
        bb_position = data.get('bb_position', 0.5)  # 0=lower, 1=upper

        if rsi is not None:
            if rsi < 30:
                score += 20  # oversold
            elif rsi > 70:
                score -= 20  # overbought

        if macd_signal > 0:
            score += 10
        elif macd_signal < 0:
            score -= 10

        # Bollinger band position
        if bb_position < 0.2:
            score += 10
        elif bb_position > 0.8:
            score -= 10

        return max(0.0, min(100.0, score))

    def _score_fundamental(self, symbol: str) -> float:
        """Score based on fundamentals."""
        if self.fundamental_scorer:
            return self.fundamental_scorer.score(symbol)
        return 50.0

    def _score_momentum(self, symbol: str, data: dict) -> float:
        """Score based on price momentum."""
        score = 50.0
        ret_5d = data.get('return_5d', 0)
        ret_20d = data.get('return_20d', 0)
        rel_volume = data.get('relative_volume', 1.0)

        # Positive momentum scores higher
        score += min(20, ret_5d * 100)
        score += min(15, ret_20d * 50)
        # Volume confirmation
        if rel_volume > 1.5 and ret_5d > 0:
            score += 10
        return max(0.0, min(100.0, score))

    def _score_sentiment(self, symbol: str) -> float:
        """Score based on sentiment."""
        if self.sentiment_scorer:
            return self.sentiment_scorer.score(symbol)
        return 50.0

    def _score_quality(self, symbol: str) -> Optional[float]:
        """Score based on quality metrics (ROE, margins, stability).

        Returns None when no real data is available so the composite
        scorer can skip this factor and redistribute its weight.
        """
        return None  # No data available — signal caller to skip this factor

    def _score_value(self, symbol: str) -> Optional[float]:
        """Score based on valuation metrics.

        Returns None when no real data is available so the composite
        scorer can skip this factor and redistribute its weight.
        """
        return None  # No data available — signal caller to skip this factor
