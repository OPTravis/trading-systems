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
        """Sentiment scoring with negation-aware keyword matching.

        Handles negation patterns like "not bullish", "no surge", "far from moon"
        by flipping positive keywords to negative when preceded by a negation word
        within 3 tokens.
        """
        if not text:
            return 0.0

        import re

        text_lower = text.lower()
        words = re.findall(r'\w+', text_lower)

        # Negation words that flip the meaning of the next keyword
        negation_words = {
            "not", "no", "never", "neither", "nor", "barely", "hardly",
            "scarcely", "seldom", "without", "nobody", "nothing", "nowhere",
            "cannot", "cant", "wont", "dont", "isnt", "arent", "wasnt",
            "werent", "hasnt", "havent", "hadnt", "doesnt", "didnt",
            "lack", "lacking", "lacks", "fail", "fails", "failed", "failing",
            "far", "away", "unlikely", "unlikely",
        }

        # Positive keywords
        positive = [
            "bullish", "buy", "up", "gain", "rise", "surge", "pump",
            "growth", "positive", "rally", "high", "highs", "moon",
            "adoption", "partnership", "launch", "upgrade", "breakout",
        ]

        # Negative keywords
        negative = [
            "bearish", "sell", "down", "drop", "fall", "crash", "dump",
            "loss", "negative", "decline", "low", "lows", "hack", "ban",
            "regulation", "fraud", "scam", "risk", "warning",
        ]

        score = 0.0

        for i, word in enumerate(words):
            # Check if any of the preceding 3 words is a negation
            is_negated = False
            for j in range(max(0, i - 3), i):
                if words[j] in negation_words:
                    is_negated = True
                    break

            if word in positive:
                score += -0.1 if is_negated else 0.1
            elif word in negative:
                # Negating a negative word makes it positive, but this is
                # weaker than direct positive — use +0.05
                score += 0.05 if is_negated else -0.1

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
            # P0: Fall back to cached F&G value instead of hardcoded 50
            # Prevents a single API blip from flipping EXTREME_FEAR→NEUTRAL
            cached_fng = self._get_cached_fng()
            if cached_fng is not None:
                logger.warning(f"Using cached F&G={cached_fng} (API failed)")
                fng_value = cached_fng
                fng_label = self._fng_label(cached_fng)
            else:
                logger.warning("No cached F&G available, defaulting to 50")
                fng_value = 50
                fng_label = "Neutral"
            return {
                "sentiment_score": round((fng_value - 50) / 50, 2),
                "sentiment_label": self._fng_label(fng_value),
                "fear_greed": fng_value,
                "fng_classification": fng_label,
                "consecutive_fear_days": 0,
                "consecutive_greed_days": 0,
                "signal": "NO_DATA",
                "timestamp": datetime.now().isoformat(),
            }

    def _get_cached_fng(self) -> Optional[int]:
        """Read the most recent F&G value from the SQLite cache.

        Falls back to data_feed_fng's cache database. Returns None if
        no cached data is available.
        """
        try:
            from src.data_feed_base import CACHE_DB
            import sqlite3
            conn = sqlite3.connect(CACHE_DB, timeout=5)
            row = conn.execute(
                "SELECT value FROM fng_history ORDER BY date DESC LIMIT 1"
            ).fetchone()
            conn.close()
            if row:
                return int(row[0])
        except Exception as e:
            logger.debug(f"F&G cache read failed: {e}")
        return None

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
