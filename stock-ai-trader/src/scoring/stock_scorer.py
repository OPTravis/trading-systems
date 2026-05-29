"""
Stock Scorer - Multi-dimensional stock scoring with IC dynamic weighting.
"""
from dataclasses import dataclass
from typing import Dict, Optional
import logging

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
    quality: float = 0.0
    value: float = 0.0
    weights: Dict[str, float] = None

    def __post_init__(self):
        if self.weights is None:
            self.weights = {}


class StockScorer:
    """Multi-dimensional stock scoring with IC-based dynamic factor weights."""

    def __init__(self, ic_tracker=None, fundamental_scorer=None, sentiment_scorer=None):
        self.ic_tracker = ic_tracker
        self.fundamental_scorer = fundamental_scorer
        self.sentiment_scorer = sentiment_scorer
        # Default equal weights
        self._default_weights = {
            'technical': 0.20,
            'fundamental': 0.20,
            'momentum': 0.15,
            'sentiment': 0.15,
            'quality': 0.15,
            'value': 0.15,
        }

    def _get_weights(self) -> Dict[str, float]:
        """Get factor weights, preferring IC-tracker weights if available."""
        if self.ic_tracker:
            weights = self.ic_tracker.get_weights()
            if weights:
                # Normalize to sum to 1
                total = sum(weights.values())
                if total > 0:
                    return {k: v / total for k, v in weights.items()}
        return self._default_weights.copy()

    def score_stock(self, symbol: str, market_data: dict = None) -> StockScore:
        """Score a stock across all dimensions (0-100 scale)."""
        market_data = market_data or {}

        technical = self._score_technical(symbol, market_data)
        fundamental = self._score_fundamental(symbol)
        momentum = self._score_momentum(symbol, market_data)
        sentiment = self._score_sentiment(symbol)
        quality = self._score_quality(symbol)
        value = self._score_value(symbol)

        weights = self._get_weights()
        composite = (
            technical * weights.get('technical', 0) +
            fundamental * weights.get('fundamental', 0) +
            momentum * weights.get('momentum', 0) +
            sentiment * weights.get('sentiment', 0) +
            quality * weights.get('quality', 0) +
            value * weights.get('value', 0)
        )

        return StockScore(
            symbol=symbol,
            composite=min(100.0, max(0.0, composite)),
            technical=technical,
            fundamental=fundamental,
            momentum=momentum,
            sentiment=sentiment,
            quality=quality,
            value=value,
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

    def _score_quality(self, symbol: str) -> float:
        """Score based on quality metrics (ROE, margins, stability)."""
        return 50.0  # Placeholder - populated by fundamental data

    def _score_value(self, symbol: str) -> float:
        """Score based on valuation metrics."""
        return 50.0  # Placeholder - populated by fundamental data
