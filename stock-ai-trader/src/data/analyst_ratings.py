"""
Analyst ratings and price targets from Financial Modeling Prep.
"""
import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"


class AnalystRatings:
    """
    Analyst recommendations and price targets from FMP.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("FMP_API_KEY", "")
        self._cache: dict = {}

    def _get(self, endpoint: str, params: Optional[dict] = None) -> any:
        p = params or {}
        p["apikey"] = self.api_key
        resp = requests.get(f"{FMP_BASE_URL}/{endpoint}", params=p, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_ratings(self, symbol: str, limit: int = 10) -> list[dict]:
        """
        Get recent analyst ratings/recommendations.

        Returns:
            List of dicts with keys: date, analyst, rating, action, price_target.
        """
        cache_key = f"ratings|{symbol}|{limit}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        logger.info("Fetching analyst ratings for %s", symbol)
        data = self._get(f"grade/{symbol}", {"limit": limit})
        ratings = []
        for item in data:
            ratings.append({
                "date": item.get("date", ""),
                "analyst": item.get("gradingCompany", ""),
                "rating": item.get("newGrade", ""),
                "previous": item.get("previousGrade", ""),
                "action": item.get("action", ""),
            })
        self._cache[cache_key] = ratings
        return ratings

    def get_price_targets(self, symbol: str) -> dict:
        """
        Get analyst price target consensus.

        Returns:
            Dict with keys: target_high, target_low, target_mean,
            target_median, number_of_analysts.
        """
        cache_key = f"targets|{symbol}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        logger.info("Fetching price targets for %s", symbol)
        data = self._get(f"analyst-estimates/{symbol}", {"limit": 1})
        if not data:
            return {}

        latest = data[0] if isinstance(data, list) else data
        result = {
            "target_high": float(latest.get("estimatedRevenueHigh", 0) or 0),
            "target_low": float(latest.get("estimatedRevenueLow", 0) or 0),
            "target_mean": float(latest.get("estimatedRevenueAvg", 0) or 0),
            "target_median": float(latest.get("estimatedRevenueMedian", 0) or 0),
            "number_of_analysts": int(latest.get("numberAnalystEstimatedRevenue", 0) or 0),
        }

        # Also try the dedicated price-target endpoint
        try:
            pt_data = self._get(f"price-target/{symbol}")
            if pt_data and isinstance(pt_data, list):
                latest_pt = pt_data[0]
                result.update({
                    "target_high": float(latest_pt.get("targetHigh", result["target_high"]) or result["target_high"]),
                    "target_low": float(latest_pt.get("targetLow", result["target_low"]) or result["target_low"]),
                    "target_mean": float(latest_pt.get("targetMean", result["target_mean"]) or result["target_mean"]),
                    "target_median": float(latest_pt.get("targetMedian", result["target_median"]) or result["target_median"]),
                    "number_of_analysts": int(latest_pt.get("numberOfAnalysts", result["number_of_analysts"]) or result["number_of_analysts"]),
                })
        except Exception:
            pass  # price-target endpoint may not be on free tier

        self._cache[cache_key] = result
        return result
