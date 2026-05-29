"""
Financial news feed via Jina Reader and NewsAPI.
"""
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

NEWSAPI_BASE = "https://newsapi.org/v2"
JINA_READER_BASE = "https://r.jina.ai"


class NewsFeed:
    """
    Financial news aggregation using NewsAPI and Jina Reader.
    Provides symbol-specific and general market news.
    """

    def __init__(self, newsapi_key: Optional[str] = None):
        self.newsapi_key = newsapi_key or os.environ.get("NEWSAPI_KEY", "")
        if not self.newsapi_key:
            logger.warning("No NewsAPI key configured – news fetch will be limited")

    def get_news(self, symbol: str, days: int = 7) -> list[dict]:
        """
        Get recent news articles mentioning a stock symbol.

        Args:
            symbol: Ticker symbol.
            days: Look-back window in days.

        Returns:
            List of dicts with keys: title, description, url, source,
            published_at, sentiment_hint.
        """
        from_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        logger.info("Fetching news for %s (last %d days)", symbol, days)

        articles = []

        # NewsAPI
        if self.newsapi_key:
            try:
                resp = requests.get(
                    f"{NEWSAPI_BASE}/everything",
                    params={
                        "q": symbol,
                        "from": from_date,
                        "sortBy": "publishedAt",
                        "language": "en",
                        "pageSize": 30,
                        "apiKey": self.newsapi_key,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
                for a in data.get("articles", []):
                    articles.append({
                        "title": a.get("title", ""),
                        "description": a.get("description", ""),
                        "url": a.get("url", ""),
                        "source": a.get("source", {}).get("name", ""),
                        "published_at": a.get("publishedAt", ""),
                        "content": a.get("content", ""),
                    })
            except Exception as exc:
                logger.error("NewsAPI error for %s: %s", symbol, exc)

        # Jina Reader fallback – scrape financial news page
        if not articles:
            try:
                resp = requests.get(
                    f"{JINA_READER_BASE}/https://finance.yahoo.com/quote/{symbol}/news",
                    timeout=15,
                )
                if resp.ok:
                    text = resp.text[:5000]
                    articles.append({
                        "title": f"Yahoo Finance news for {symbol}",
                        "description": text[:500],
                        "url": f"https://finance.yahoo.com/quote/{symbol}/news",
                        "source": "Yahoo Finance (via Jina)",
                        "published_at": datetime.utcnow().isoformat(),
                        "content": text,
                    })
            except Exception as exc:
                logger.error("Jina reader error for %s: %s", symbol, exc)

        return articles

    def get_market_news(self, limit: int = 20) -> list[dict]:
        """
        Get general market/financial news.

        Returns:
            List of news article dicts.
        """
        logger.info("Fetching market news (limit=%d)", limit)
        articles = []

        if self.newsapi_key:
            try:
                resp = requests.get(
                    f"{NEWSAPI_BASE}/top-headlines",
                    params={
                        "category": "business",
                        "language": "en",
                        "pageSize": min(limit, 100),
                        "apiKey": self.newsapi_key,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
                for a in data.get("articles", []):
                    articles.append({
                        "title": a.get("title", ""),
                        "description": a.get("description", ""),
                        "url": a.get("url", ""),
                        "source": a.get("source", {}).get("name", ""),
                        "published_at": a.get("publishedAt", ""),
                        "content": a.get("content", ""),
                    })
            except Exception as exc:
                logger.error("NewsAPI market news error: %s", exc)

        return articles[:limit]

    def get_news_for_sentiment(self, symbol: str, days: int = 3) -> list[str]:
        """
        Get news headlines for sentiment analysis (lightweight).

        Returns:
            List of headline strings.
        """
        articles = self.get_news(symbol, days=days)
        headlines = []
        for a in articles:
            title = a.get("title", "")
            if title:
                headlines.append(title)
        return headlines
