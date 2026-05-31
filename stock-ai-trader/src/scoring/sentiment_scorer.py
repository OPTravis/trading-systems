"""
Sentiment Scorer - News + social sentiment scoring.
"""

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class SentimentScorer:
    """Scores stocks on sentiment from multiple sources (0-100)."""

    def __init__(self, news_client=None, social_client=None):
        self.news_client = news_client
        self.social_client = social_client

    def score(self, symbol: str, sentiment_data: Optional[Dict] = None) -> float:
        """Score a stock on sentiment (0-100)."""
        if not sentiment_data:
            return 50.0  # Neutral default

        weights = {
            "news_sentiment": 0.40,
            "analyst_ratings": 0.35,
            "insider_trading": 0.25,
        }

        news_score = self._score_news(sentiment_data.get("news_sentiment", 0))
        analyst_score = self._score_analyst_ratings(
            sentiment_data.get("analyst_ratings", {})
        )
        insider_score = self._score_insider_trading(
            sentiment_data.get("insider_trades", [])
        )

        composite = (
            news_score * weights["news_sentiment"]
            + analyst_score * weights["analyst_ratings"]
            + insider_score * weights["insider_trading"]
        )

        return max(0.0, min(100.0, composite))

    def _score_news(self, sentiment: float) -> float:
        """Score news sentiment (-1 to 1 -> 0 to 100)."""
        return 50 + (sentiment * 50)

    def _score_analyst_ratings(self, ratings: Dict) -> float:
        """Score analyst consensus."""
        if not ratings:
            return 50.0

        buy = ratings.get("buy", 0)
        hold = ratings.get("hold", 0)
        sell = ratings.get("sell", 0)
        total = buy + hold + sell
        if total == 0:
            return 50.0

        return (buy / total) * 100

    def _score_insider_trading(self, trades: list) -> float:
        """Score insider trading activity."""
        if not trades:
            return 50.0

        buys = sum(1 for t in trades if t.get("type") == "buy")
        sells = sum(1 for t in trades if t.get("type") == "sell")
        net = buys - sells

        if net > 0:
            return min(100, 50 + net * 10)
        elif net < 0:
            return max(0, 50 + net * 10)
        return 50.0
