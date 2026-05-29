"""
Unified Data Feed Layer for Crypto AI Trader.

Provides three data sources with SQLite caching:
  - FearGreedIndex:  Crypto Fear & Greed Index (alternative.me API)
  - NewsFeed:        CryptoCompare News API
  - FundingRate:     Binance Futures Funding Rate

All feeds are orchestrated by DataFeedManager which offers a single
get_market_snapshot() call and graceful per-feed error isolation.

Sub-modules:
  - data_feed_base.py     Shared utilities (SQLite helpers, constants)
  - data_feed_fng.py      FearGreedIndex
  - data_feed_news.py     NewsFeed
  - data_feed_funding.py  FundingRate
  - data_feed_oi.py       OpenInterest
  - data_feed_onchain.py  DeFiLlamaOnChain
  - data_feed_scorer.py   ScoringDataAggregator
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

# Re-export all sub-module classes for backward compatibility
# so that `from src.data_feed import FearGreedIndex` still works.
from src.data_feed_base import _get_conn, _init_tables, CACHE_DB  # noqa: F401
from src.data_feed_fng import FearGreedIndex  # noqa: F401
from src.data_feed_news import NewsFeed  # noqa: F401
from src.data_feed_funding import FundingRate  # noqa: F401
from src.data_feed_oi import OpenInterest  # noqa: F401
from src.data_feed_onchain import DeFiLlamaOnChain  # noqa: F401
from src.data_feed_scorer import ScoringDataAggregator  # noqa: F401

logger = logging.getLogger(__name__)


# ===================================================================
# DataFeedManager  (unified entry point)
# ===================================================================

class DataFeedManager:
    """Orchestrates all data feeds with per-feed error isolation.

    Usage::

        mgr = DataFeedManager()
        snapshot = mgr.get_market_snapshot()
    """

    def __init__(self) -> None:
        self.fng = FearGreedIndex()
        self.news = NewsFeed()
        self.funding = FundingRate()
        self.oi = OpenInterest()
        self.scorer = ScoringDataAggregator(self.funding, self.oi)
        self.onchain = DeFiLlamaOnChain()
        logger.info("DataFeedManager initialised (FNG + News + Funding + OI + Scorer + OnChain)")

    # ------------------------------------------------------------------
    def get_market_snapshot(self) -> Dict[str, Any]:
        """Collect a full market snapshot from all feeds.

        Individual feed failures are caught and logged; the snapshot
        still includes data from the feeds that succeeded.

        Returns:
            dict with keys:
                - fear_greed:      current F&G value dict or None
                - btc_price:       current BTC/USDT price (from Binance) or None
                - funding:         funding summary dict
                - news_p1:         list of P1 (high-impact) articles
                - news_p2:         list of P2 articles
                - onchain_score:   0-100 on-chain health score or None
                - timestamp:       ISO timestamp of snapshot
        """
        snapshot: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # 1. Fear & Greed Index
        try:
            snapshot["fear_greed"] = self.fng.get_current()
        except Exception as e:
            logger.error("FearGreedIndex failed in snapshot: %s", e)
            snapshot["fear_greed"] = None

        # 2. BTC price (from Binance public REST)
        try:
            snapshot["btc_price"] = self._get_btc_price()
        except Exception as e:
            logger.error("BTC price fetch failed in snapshot: %s", e)
            snapshot["btc_price"] = None

        # 3. Funding rates
        try:
            snapshot["funding"] = self.funding.get_funding_summary()
        except Exception as e:
            logger.error("FundingRate failed in snapshot: %s", e)
            snapshot["funding"] = None

        # 4. News (last 24h)
        try:
            articles = self.news.get_crypto_news(limit=50)
            classified = self.news.classify_news(articles)
            snapshot["news_p1"] = classified["P1"]
            snapshot["news_p2"] = classified["P2"]
        except Exception as e:
            logger.error("NewsFeed failed in snapshot: %s", e)
            snapshot["news_p1"] = []
            snapshot["news_p2"] = []

        # 5. Market sentiment (funding + OI based scoring)
        try:
            btc_sent = self.scorer.get_symbol_sentiment("BTCUSDT")
            eth_sent = self.scorer.get_symbol_sentiment("ETHUSDT")
            snapshot["market_sentiment"] = {
                "BTCUSDT": btc_sent,
                "ETHUSDT": eth_sent,
                "summary": (
                    f"BTC sentiment: {btc_sent.get('sentiment_score', 0):.1f} "
                    f"(funding: {btc_sent.get('funding_rate', 0):.6f}, "
                    f"OI change: {btc_sent.get('oi_change_pct', 'N/A')}%), "
                    f"ETH sentiment: {eth_sent.get('sentiment_score', 0):.1f} "
                    f"(funding: {eth_sent.get('funding_rate', 0):.6f}, "
                    f"OI change: {eth_sent.get('oi_change_pct', 'N/A')}%)"
                ),
            }
        except Exception as e:
            logger.error("Market sentiment scoring failed in snapshot: %s", e)
            snapshot["market_sentiment"] = None

        # 6. On-chain score (DeFiLlama TVL changes)
        try:
            snapshot["onchain_score"] = self.onchain.get_onchain_score()
        except Exception as e:
            logger.error("OnChain score failed in snapshot: %s", e)
            snapshot["onchain_score"] = None

        return snapshot

    # ------------------------------------------------------------------
    @staticmethod
    def _get_btc_price() -> Optional[float]:
        """Fetch current BTC/USDT price from Binance public API."""
        try:
            resp = requests.get(
                "https://api3.binance.com/api/v3/ticker/price",
                params={"symbol": "BTCUSDT"},
                timeout=5,
            )
            resp.raise_for_status()
            return float(resp.json()["price"])
        except Exception as e:
            logger.error("Failed to fetch BTC price: %s", e)
            return None

    # ------------------------------------------------------------------
    def close(self) -> None:
        """Release resources held by sub-feeds."""
        try:
            self.funding.close()
        except Exception:
            logger.error("Failed to close funding rate session", exc_info=True)
        try:
            self.oi.close()
        except Exception:
            logger.error("Failed to close open interest session", exc_info=True)

    # ------------------------------------------------------------------
    def get_btc_dominance(self) -> Optional[Dict[str, Any]]:
        """Fetch BTC.D (Bitcoin dominance) from CoinGecko free API.

        Returns: {btc_dominance: float, eth_dominance: float, timestamp: str}
        or None on failure.
        """
        try:
            resp = requests.get(
                "https://api.coingecko.com/api/v3/global",
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            btc_d = data.get("market_cap_percentage", {}).get("btc", 0)
            eth_d = data.get("market_cap_percentage", {}).get("eth", 0)
            return {
                "btc_dominance": btc_d,
                "eth_dominance": eth_d,
                "btc_d_change_24h": data.get("market_cap_change_percentage_24h_usd", 0),
            }
        except Exception as e:
            logger.warning("Failed to fetch BTC dominance: %s", e)
            return None


# ===================================================================
# Quick self-test
# ===================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(name)s - %(levelname)s - %(message)s")

    mgr = DataFeedManager()
    snap = mgr.get_market_snapshot()

    import json
    print(json.dumps(snap, indent=2, default=str))
    mgr.close()
