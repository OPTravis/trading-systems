"""
Fear & Greed Index data feed.

Fetches and caches the Crypto Fear & Greed Index from alternative.me API.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from src.data_feed_base import FNG_TTL, FNG_URL, _get_conn

logger = logging.getLogger(__name__)


class FearGreedIndex:
    """Fetch and cache the Crypto Fear & Greed Index.

    Data source: https://api.alternative.me/fng/
    """

    def __init__(self) -> None:
        self._url = FNG_URL

    # ------------------------------------------------------------------
    def get_current(self) -> Optional[Dict[str, Any]]:
        """Return the latest F&G value.

        Returns:
            dict with keys: value (int), classification (str), timestamp (str)
            or None on failure.
        """
        history = self.get_history(limit=1)
        if history:
            return history[0]
        return None

    # ------------------------------------------------------------------
    def get_history(self, limit: int = 30) -> List[Dict[str, Any]]:
        """Return cached F&G history, fetching new data if cache is stale.

        Args:
            limit: Number of days to return (max 1000, API default 30).

        Returns:
            List of dicts with keys: value, classification, timestamp (date str).
        """
        if not self._is_cache_fresh():
            self._fetch_and_cache()

        return self._read_cache(limit)

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _is_cache_fresh(self) -> bool:
        """Check whether the cached data is within TTL."""
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT fetched_at FROM fng_history ORDER BY date DESC LIMIT 1"
            ).fetchone()
            if not row:
                return False
            fetched = datetime.fromisoformat(row["fetched_at"])
            return (datetime.now(timezone.utc) - fetched).total_seconds() < FNG_TTL
        except Exception as e:
            logger.warning("FNG cache freshness check failed: %s", e)
            return False
        finally:
            conn.close()

    def _fetch_and_cache(self) -> None:
        """Fetch latest F&G data from API and upsert into SQLite."""
        try:
            resp = requests.get(self._url, params={"limit": 30, "format": "json"}, timeout=10)  # type: ignore[arg-type]
            resp.raise_for_status()
            data = resp.json().get("data", [])
            if not data:
                logger.warning("FNG API returned empty data")
                return

            now = datetime.now(timezone.utc).isoformat()
            conn = _get_conn()
            try:
                for item in data:
                    date_str = datetime.fromtimestamp(
                        int(item["timestamp"]), tz=timezone.utc
                    ).strftime("%Y-%m-%d")
                    conn.execute(
                        """INSERT INTO fng_history (date, value, classification, fetched_at)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(date) DO UPDATE SET
                               value = excluded.value,
                               classification = excluded.classification,
                               fetched_at = excluded.fetched_at""",
                        (
                            date_str,
                            int(item["value"]),
                            item["value_classification"],
                            now,
                        ),
                    )
                conn.commit()
                logger.debug("FNG cache updated with %d entries", len(data))
            finally:
                conn.close()
        except Exception as e:
            logger.error("Failed to fetch F&G Index: %s", e)

    def _read_cache(self, limit: int) -> List[Dict[str, Any]]:
        """Read the most recent *limit* rows from cache."""
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT date, value, classification FROM fng_history ORDER BY date DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {
                    "value": row["value"],
                    "classification": row["classification"],
                    "timestamp": row["date"],
                }
                for row in rows
            ]
        except Exception as e:
            logger.error("Failed to read FNG cache: %s", e)
            return []
        finally:
            conn.close()
