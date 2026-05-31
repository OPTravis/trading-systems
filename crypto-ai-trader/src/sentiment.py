"""
Sentiment Analyzer - News and Social Media Analysis
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger(__name__)


def tavily_search(query: str, count: int = 10) -> Optional[Dict]:
    """Search using Tavily API"""
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        logger.warning("TAVILY_API_KEY not set")
        return None

    try:
        response = requests.post(
            "https://api.tavily.com/search",
            json={"query": query, "api_key": api_key, "max_results": count},
            timeout=10,
        )
        return response.json()
    except Exception as e:
        logger.error(f"Tavily search failed: {e}")
        return None


class SentimentAnalyzer:
    """Analyze market sentiment from news and social media"""

    def __init__(self):
        self.cache = {}
        self.cache_ttl = 300  # 5 minutes

    def analyze_coin(self, symbol: str) -> Dict:
        """Analyze sentiment for a specific coin"""
        coin_name = symbol.replace("USDT", "")

        # Get news
        news = self._get_news(coin_name)

        # Analyze sentiment
        sentiment_score = self._calculate_sentiment(news)

        return {
            "symbol": symbol,
            "coin_name": coin_name,
            "sentiment_score": sentiment_score,  # -1 to +1
            "sentiment_label": self._sentiment_label(sentiment_score),
            "news_count": len(news),
            "news": news[:5],  # Top 5 news
            "timestamp": datetime.now().isoformat(),
        }

    def analyze_batch(self, symbols: List[str]) -> List[Dict]:
        """Analyze sentiment for multiple coins"""
        results = []
        for symbol in symbols:
            try:
                result = self.analyze_coin(symbol)
                results.append(result)
            except Exception as e:
                logger.warning(f"Failed to analyze {symbol}: {e}")
        return results

    def _get_news(self, coin_name: str) -> List[Dict]:
        """Get latest news for a coin"""
        try:
            # Search for news using Tavily
            query = f"{coin_name} cryptocurrency news today"
            results_raw = tavily_search(query, count=10)
            results = []
            if results_raw and "results" in results_raw:
                for r in results_raw["results"]:
                    results.append(
                        {
                            "title": r.get("title", ""),
                            "description": r.get("content", ""),
                            "url": r.get("url", ""),
                        }
                    )

            # Add sentiment scores
            for r in results:
                r["score"] = self._score_sentiment(r.get("description", ""))

            return results
        except Exception as e:
            logger.error(f"Failed to get news: {e}")
            return []

    def _calculate_sentiment(self, news: List[Dict]) -> float:
        """Calculate overall sentiment from news"""
        if not news:
            return 0.0

        total = sum(n.get("score", 0) for n in news)
        return total / len(news)

    def _score_sentiment(self, text: str) -> float:
        """Simple sentiment scoring based on keywords"""
        if not text:
            return 0.0

        text_lower = text.lower()

        # Positive keywords
        positive = [
            "bullish",
            "buy",
            "up",
            "gain",
            "rise",
            "surge",
            "pump",
            "growth",
            "positive",
            " rally",
            "high",
            "highs",
            "moon",
            "adoption",
            "partnership",
            "launch",
            "upgrade",
            "breakout",
        ]

        # Negative keywords
        negative = [
            "bearish",
            "sell",
            "down",
            "drop",
            "fall",
            "crash",
            "dump",
            "loss",
            "negative",
            "decline",
            "low",
            "lows",
            "hack",
            "ban",
            "regulation",
            "fraud",
            "scam",
            "risk",
            "warning",
        ]

        score = 0.0
        for word in positive:
            if word in text_lower:
                score += 0.1
        for word in negative:
            if word in text_lower:
                score -= 0.1

        return max(-1.0, min(1.0, score))

    def _sentiment_label(self, score: float) -> str:
        """Convert score to label"""
        if score >= 0.5:
            return "Very Bullish"
        elif score >= 0.2:
            return "Bullish"
        elif score >= -0.2:
            return "Neutral"
        elif score >= -0.5:
            return "Bearish"
        else:
            return "Very Bearish"

    def get_market_sentiment(self) -> Dict:
        """Get overall crypto market sentiment via Fear & Greed Index API.

        Now includes persistence tracking: how many consecutive days CFGI
        has been below 25 (research report: 10+ days = 90d +114.8%).
        """
        try:
            # Fetch 30 days for persistence calculation
            resp = requests.get(
                "https://api.alternative.me/fng/?limit=30&format=json", timeout=5
            )
            data_list = resp.json()["data"]
            latest = data_list[0]
            fng_value = int(latest["value"])
            fng_label = latest["value_classification"]

            # Persistence: count consecutive days below/above thresholds
            consecutive_fear = 0
            consecutive_greed = 0
            for d in data_list:
                v = int(d["value"])
                if v < 25:
                    consecutive_fear += 1
                else:
                    break
            for d in data_list:
                v = int(d["value"])
                if v > 75:
                    consecutive_greed += 1
                else:
                    break

            # Research-based signal
            signal = "NEUTRAL"
            if consecutive_fear >= 14:
                signal = "STRONG_REVERSAL_BUY"  # 90d +114.8%
            elif consecutive_fear >= 10:
                signal = "REVERSAL_BUY"  # adjusted threshold
            elif consecutive_greed >= 7:
                signal = "OVERBOUGHT_WARNING"

            # Convert to our score: 0-100 -> -1 to +1
            sentiment_score = (fng_value - 50) / 50

            return {
                "sentiment_score": round(sentiment_score, 2),
                "sentiment_label": self._fng_label(fng_value),
                "fear_greed": fng_value,
                "fng_classification": fng_label,
                "consecutive_fear_days": consecutive_fear,
                "consecutive_greed_days": consecutive_greed,
                "signal": signal,
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as e:
            logger.error(f"Failed to get Fear & Greed Index: {e}")
            return {
                "sentiment_score": 0,
                "sentiment_label": "Unknown",
                "fear_greed": 50,
                "fng_classification": "Neutral",
                "consecutive_fear_days": 0,
                "consecutive_greed_days": 0,
                "signal": "NO_DATA",
                "timestamp": datetime.now().isoformat(),
            }

    def _fng_label(self, value: int) -> str:
        """Map F&G value to actionable label"""
        if value <= 25:
            return "Extreme Fear"
        if value <= 45:
            return "Fear"
        if value <= 55:
            return "Neutral"
        if value <= 75:
            return "Greed"
        return "Extreme Greed"
