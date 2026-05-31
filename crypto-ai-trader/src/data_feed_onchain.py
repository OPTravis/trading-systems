"""
DeFiLlama on-chain data feed.

Fetches DeFiLlama chain TVL data and produces a 0-100 on-chain health score.

Design:
- Parallel fetching via ThreadPoolExecutor (7 chains concurrently)
- Retry with exponential backoff per chain (2 retries, 1s/2s delays)
- In-memory cache with 1-hour TTL for graceful degradation
- 10s per-request timeout (down from 15s)
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

# Cache TTL in seconds
CACHE_TTL = 3600  # 1 hour

# Retry config
MAX_RETRIES = 2
RETRY_BASE_DELAY = 1.0  # seconds, doubles each retry
REQUEST_TIMEOUT = 10  # seconds per request


class DeFiLlamaOnChain:
    """Fetch DeFiLlama chain TVL data and produce a 0-100 on-chain health score.

    Uses /v2/historicalChainTvl/{chain} to compute 1-day TVL change %
    across major chains. No auth required.
    """

    BASE = "https://api.llama.fi"
    MAJOR_CHAINS = [
        "Ethereum",
        "BSC",
        "Arbitrum",
        "Base",
        "Solana",
        "Avalanche",
        "Polygon",
    ]

    def __init__(self) -> None:
        self._cache: Dict[str, float] = {}
        self._cache_ts: float = 0.0

    def _fetch_chain_tvl_change(self, chain: str) -> Optional[float]:
        """Fetch 1-day TVL change % for a single chain with retry."""
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = requests.get(
                    f"{self.BASE}/v2/historicalChainTvl/{chain}",
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                if len(data) < 2:
                    return None
                tvl_now = data[-1]["tvl"]
                tvl_yesterday = data[-2]["tvl"]
                if tvl_yesterday <= 0:
                    return None
                return (tvl_now - tvl_yesterday) / tvl_yesterday * 100
            except Exception:
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2**attempt)
                    time.sleep(delay)
                else:
                    logger.warning(
                        "DeFiLlama TVL fetch failed for %s after %d attempts",
                        chain,
                        MAX_RETRIES + 1,
                    )
                    return None
        return None  # unreachable, satisfies type checker

    def get_chain_tvl_changes(self) -> Dict[str, float]:
        """Return {chain_name: tvl_change_24h_pct} for major chains.

        Uses concurrent fetching. Updates cache on success.
        Returns cached data if all fetches fail and cache is valid.
        """
        results: Dict[str, float] = {}

        with ThreadPoolExecutor(max_workers=len(self.MAJOR_CHAINS)) as pool:
            futures = {
                pool.submit(self._fetch_chain_tvl_change, chain): chain
                for chain in self.MAJOR_CHAINS
            }
            for future in as_completed(futures, timeout=30):
                chain = futures[future]
                try:
                    chg = future.result()
                    if chg is not None:
                        results[chain] = chg
                except Exception:
                    logger.warning("Unexpected error fetching %s TVL", chain)

        if results:
            self._cache = results
            self._cache_ts = time.time()
            return results

        # All fetches failed — try cache
        if self._cache and (time.time() - self._cache_ts) < CACHE_TTL:
            logger.info(
                "Using cached DeFiLlama data (%d chains, age %.0fs)",
                len(self._cache),
                time.time() - self._cache_ts,
            )
            return self._cache

        return results

    def get_onchain_score(self) -> float:
        """Compute a 0-100 on-chain health score.

        Logic:
        - Aggregate TVL change across major chains
        - Positive aggregate change → bullish on-chain (50-100)
        - Negative aggregate change → bearish on-chain (0-50)
        - Extreme changes (>±10%) capped at edges
        """
        changes = self.get_chain_tvl_changes()
        if not changes:
            return 50.0  # neutral on failure

        avg_change = sum(changes.values()) / len(changes)
        score = 50.0 + (avg_change * 5.0)
        return max(0.0, min(100.0, score))
