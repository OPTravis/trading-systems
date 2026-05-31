"""
News feed data module.

Fetches and classifies crypto news from CryptoCompare API.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests

from src.app_secrets import CRYPTO_SECRETS, load_secret_file
from src.data_feed_base import NEWS_TTL, NEWS_URL, P1_KEYWORDS, _get_conn

logger = logging.getLogger(__name__)


class NewsFeed:
    """Fetch and classify crypto news from CryptoCompare.

    API: https://min-api.cryptocompare.com/data/v2/news/
    """

    def __init__(self) -> None:
        self._api_key: Optional[str] = self._load_api_key()
        self._base_url = NEWS_URL

    # ------------------------------------------------------------------
    @staticmethod
    def _load_api_key() -> Optional[str]:
        """Load CRYPTOCOMPARE_API_KEY from environment or secrets file."""
        key = os.environ.get("CRYPTOCOMPARE_API_KEY")
        if key:
            return key
        secrets = load_secret_file(CRYPTO_SECRETS)
        return secrets.get("CRYPTOCOMPARE_API_KEY")

    # ------------------------------------------------------------------
    def get_crypto_news(
        self,
        categories: Optional[List[str]] = None,
        exclude_categories: Optional[List[str]] = None,
        ts_sym: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Fetch recent crypto news with optional filtering.

        Args:
            categories:          Include only these categories (e.g. ['BTC', 'ETH']).
            exclude_categories:  Exclude these categories.
            ts_sym:              Filter by symbol on CryptoCompare side (e.g. 'BTC').
            limit:               Max number of articles to return.

        Returns:
            List of article dicts, sorted newest first.
        """
        # Return cached if fresh
        cached = self._read_cache(limit=limit)
        if cached and self._is_cache_fresh():
            return self._filter_articles(cached, categories, exclude_categories)[:limit]

        # Fetch from API
        articles = self._fetch_from_api(
            categories=categories,
            exclude_categories=exclude_categories,
            ts_sym=ts_sym,
            limit=limit,
        )
        if articles:
            self._write_cache(articles)

        # Filter out articles older than 24h
        cutoff = time.time() - 86400
        articles = [a for a in articles if a.get("published_on", 0) >= cutoff]

        return articles[:limit]

    # ------------------------------------------------------------------
    def classify_news(
        self, articles: List[Dict[str, Any]]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Classify articles into priority tiers.

        P1: high-impact (regulation, hacks, ETFs, etc.)
        P2: everything else.

        Args:
            articles: List of article dicts (must contain 'title' and/or 'body').
        Returns:
            {"P1": [...], "P2": [...]}
        """
        p1: List[Dict[str, Any]] = []
        p2: List[Dict[str, Any]] = []

        for article in articles:
            text = (
                (article.get("title") or "") + " " + (article.get("body") or "")
            ).lower()

            is_p1 = any(kw in text for kw in P1_KEYWORDS)
            if is_p1:
                p1.append(article)
            else:
                p2.append(article)

        return {"P1": p1, "P2": p2}

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _fetch_from_api(
        self,
        categories: Optional[List[str]] = None,
        exclude_categories: Optional[List[str]] = None,
        ts_sym: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Call the CryptoCompare News API."""
        params: Dict[str, Any] = {"lTs": 0}
        if categories:
            params["categories"] = ",".join(categories)
        if exclude_categories:
            params["excludeCategories"] = ",".join(exclude_categories)
        if ts_sym:
            params["tsSym"] = ts_sym.upper()
        if limit:
            params["limit"] = limit

        url = self._base_url
        if self._api_key:
            url += f"&api_key={self._api_key}"

        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            raw_articles = data.get("Data", [])
            logger.debug("CryptoCompare returned %d articles", len(raw_articles))
            return raw_articles
        except Exception as e:
            logger.error("Failed to fetch CryptoCompare news: %s", e)
            return []

    def _is_cache_fresh(self) -> bool:
        """Check if the news cache has entries fetched within TTL."""
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT MAX(fetched_at) AS latest FROM news_cache"
            ).fetchone()
            if not row or row["latest"] is None:
                return False
            return (time.time() - row["latest"]) < NEWS_TTL
        except Exception as e:
            logger.warning("News cache freshness check failed: %s", e)
            return False
        finally:
            conn.close()

    def _write_cache(self, articles: List[Dict[str, Any]]) -> None:
        """Upsert articles into SQLite cache, pruning entries older than 7 days."""
        conn = _get_conn()
        try:
            now = time.time()
            cutoff = now - (7 * 86400)
            for a in articles:
                article_id = a.get("id")
                published_on = a.get("published_on", 0)
                if not article_id:
                    continue
                conn.execute(
                    """INSERT INTO news_cache (id, published_on, title, categories, body, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                           published_on = excluded.published_on,
                           title = excluded.title,
                           categories = excluded.categories,
                           body = excluded.body,
                           fetched_at = excluded.fetched_at""",
                    (
                        article_id,
                        published_on,
                        a.get("title"),
                        a.get("categories"),
                        a.get("body"),
                        now,
                    ),
                )
            # Prune old entries
            conn.execute("DELETE FROM news_cache WHERE published_on < ?", (cutoff,))
            conn.commit()
            logger.debug("News cache updated with %d articles", len(articles))
        except Exception as e:
            logger.error("Failed to write news cache: %s", e)
        finally:
            conn.close()

    def _read_cache(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Read cached news articles sorted by published_on desc."""
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT id, published_on, title, categories, body FROM news_cache "
                "ORDER BY published_on DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "published_on": row["published_on"],
                    "title": row["title"],
                    "categories": row["categories"],
                    "body": row["body"],
                }
                for row in rows
            ]
        except Exception as e:
            logger.error("Failed to read news cache: %s", e)
            return []
        finally:
            conn.close()

    @staticmethod
    def _filter_articles(
        articles: List[Dict[str, Any]],
        categories: Optional[List[str]] = None,
        exclude_categories: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Filter articles by categories (case-insensitive substring match)."""
        result = articles
        if categories:
            cats_lower = [c.lower() for c in categories]
            result = [
                a
                for a in result
                if any(c in (a.get("categories") or "").lower() for c in cats_lower)
            ]
        if exclude_categories:
            exc_lower = [c.lower() for c in exclude_categories]
            result = [
                a
                for a in result
                if not any(c in (a.get("categories") or "").lower() for c in exc_lower)
            ]
        return result
