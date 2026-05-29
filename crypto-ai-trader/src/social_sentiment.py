"""
Social Sentiment Analyzer — community sentiment scoring.

Uses free public APIs:
- CoinGecko: community sentiment (up/down votes, Reddit, Twitter stats)
- Alternative.me: Fear & Greed Index (already integrated)
- Binance: recent trades volume as proxy for market attention

Provides a 0-100 score for social sentiment per symbol.
"""

import logging
import time
import json
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class SocialSentimentAnalyzer:
    """Fetch and analyze social sentiment for crypto symbols."""

    def __init__(self):
        self._cache = {}
        self._cache_ttl = 600  # 10 min cache

    def get_coingecko_sentiment(self, symbol: str) -> Optional[Dict]:
        """Get community sentiment from CoinGecko (free, no key).

        Uses the /coins/{id} endpoint with community data.
        """
        # Map common symbols to CoinGecko IDs
        SYMBOL_TO_ID = {
            "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
            "SOL": "solana", "AVAX": "avalanche-2", "LINK": "chainlink",
            "DOT": "polkadot", "ADA": "cardano", "MATIC": "matic-network",
            "OP": "optimism", "ARB": "arbitrum", "DOGE": "dogecoin",
            "XRP": "ripple", "TON": "the-open-network", "NEAR": "near",
            "FIL": "filecoin", "ATOM": "cosmos", "UNI": "uniswap",
            "AAVE": "aave", "MKR": "maker", "LDO": "lido-dao",
            "ENA": "ethena", "WLD": "worldcoin-wld", "TRX": "tron",
            "TAO": "bittensor", "SAHARA": "sahara-ai",
        }

        base = symbol.replace("USDT", "").replace("BUSD", "")
        coin_id = SYMBOL_TO_ID.get(base.upper())
        if not coin_id:
            return None

        cache_key = f"cg_{coin_id}"
        now = time.time()
        if cache_key in self._cache:
            cached_time, cached_data = self._cache[cache_key]
            if now - cached_time < self._cache_ttl:
                return cached_data

        try:
            import urllib.request
            url = f"https://api.coingecko.com/api/v3/coins/{coin_id}?localization=false&tickers=false&market_data=false&community_data=true&developer_data=false"
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())

            community = data.get("community_data", {})
            sentiment_up = data.get("sentiment_votes_up_percentage", 50)
            sentiment_down = data.get("sentiment_votes_down_percentage", 50)

            result = {
                "sentiment_up_pct": sentiment_up,
                "sentiment_down_pct": sentiment_down,
                "twitter_followers": community.get("twitter_followers", 0),
                "reddit_subscribers": community.get("reddit_subscribers", 0),
                "reddit_active_accounts": community.get("reddit_accounts_active_48h", 0),
            }

            self._cache[cache_key] = (now, result)
            return result
        except Exception as e:
            logger.debug(f"CoinGecko sentiment failed for {coin_id}: {e}")
            return None

    def analyze(self, symbol: str) -> Optional[Dict]:
        """Full social sentiment analysis for a symbol.

        Returns:
            {
                "score": 0-100 (higher = more positive sentiment),
                "sentiment_up_pct": float (% positive votes),
                "twitter_followers": int,
                "reddit_active": int,
                "source": "coingecko",
            }
        """
        cg = self.get_coingecko_sentiment(symbol)
        if not cg:
            return None

        # Compute score from sentiment votes
        up = cg["sentiment_up_pct"]
        down = cg["sentiment_down_pct"]
        total = up + down

        if total > 0:
            sentiment_ratio = up / total
        else:
            sentiment_ratio = 0.5

        # Social activity bonus
        twitter = cg.get("twitter_followers", 0)
        reddit = cg.get("reddit_active", 0)

        # Activity score: more followers/active = more attention
        # Cap at reasonable levels
        activity_bonus = 0
        if twitter > 100000:
            activity_bonus += 5
        if reddit > 1000:
            activity_bonus += 5

        score = sentiment_ratio * 90 + activity_bonus + 5  # 5-100

        return {
            "score": round(max(0, min(100, score)), 1),
            "sentiment_up_pct": round(up, 1),
            "sentiment_down_pct": round(down, 1),
            "twitter_followers": twitter,
            "reddit_active": reddit,
            "source": "coingecko",
        }
