"""
DeFiLlama on-chain data feed.

Fetches DeFiLlama chain TVL data and produces a 0-100 on-chain health score.

Design:
- Parallel fetching via ThreadPoolExecutor (7 chains concurrently)
- Retry with exponential backoff per chain (2 retries, 1s/2s delays)
- In-memory cache with 1-hour TTL for graceful degradation
- 10s per-request timeout (down from 15s)
- WO-1017: disk-persisted per-chain cache with a 2h fallback TTL.
  Callers (dimension_scorer, price_predictor) build a fresh instance per
  scan round, so an instance-scoped memory cache can never carry a value
  across rounds -- the 7 empty rounds on 10/5 (13:52-19:52) hit exactly
  that hole. The last known-good values now live in a JSON file and are
  served when an entire fetch round comes back empty and the values are
  younger than FALLBACK_TTL; beyond it the dimension goes empty and the
  WARN stays.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

# Cache TTL in seconds
CACHE_TTL = 3600  # 1 hour (in-memory)

# WO-1017: fallback window for the disk-persisted last-known-good values
FALLBACK_TTL = 7200  # 2 hours; beyond this the dimension goes empty

# Retry config
MAX_RETRIES = 2
RETRY_BASE_DELAY = 1.0  # seconds, doubles each retry
REQUEST_TIMEOUT = 10  # seconds per request

import json
import os

# WO-1017: cross-round persistence (callers create a fresh instance every
# scan round; /root/trading-state already hosts the runtime state files)
CACHE_FILE = os.environ.get(
    "ONCHAIN_TVL_CACHE_PATH", "/root/trading-state/onchain_tvl_cache.json"
)


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
        # WO-1017: per-chain last-known-good {chain: {"chg": float, "ts": ts}}
        self._cache: Dict[str, Dict] = self._load_disk_cache()
        self._cache_ts: float = 0.0

    # ---- WO-1017: disk persistence helpers (fail-open) ----

    def _load_disk_cache(self) -> Dict[str, Dict]:
        try:
            with open(CACHE_FILE) as f:
                data = json.load(f)
            chains = data.get("chains", {})
            if isinstance(chains, dict) and all(
                isinstance(v, dict) and "chg" in v and "ts" in v
                for v in chains.values()
            ):
                return chains
            logger.warning("DeFiLlama cache file malformed, ignoring: %s", CACHE_FILE)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("DeFiLlama cache read failed (%s), ignoring", e)
        return {}

    def _persist_disk_cache(self) -> None:
        try:
            with open(CACHE_FILE, "w") as f:
                json.dump({"chains": self._cache, "written": time.time()}, f)
        except Exception as e:
            logger.warning("DeFiLlama cache write failed: %s", e)

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
        fetched: set = set()

        # WO-1010 (10/3): a single slow/failing chain must not discard the
        # chains that already finished. The worst-case single-chain time is
        # (MAX_RETRIES+1) * REQUEST_TIMEOUT + backoff = 33s, which the old
        # as_completed timeout=30 could not cover -- the resulting
        # TimeoutError propagated past the partial `results` and left the
        # whole on-chain dimension (25% weight) empty for 5h+ on 10/2-10/3
        # while 6 of 7 chains had actually answered.
        with ThreadPoolExecutor(max_workers=len(self.MAJOR_CHAINS)) as pool:
            futures = {
                pool.submit(self._fetch_chain_tvl_change, chain): chain
                for chain in self.MAJOR_CHAINS
            }
            try:
                for future in as_completed(futures, timeout=45):
                    chain = futures[future]
                    fetched.add(chain)
                    try:
                        chg = future.result()
                        if chg is not None:
                            results[chain] = chg
                    except Exception:
                        logger.warning("Unexpected error fetching %s TVL", chain)
            except TimeoutError:
                # keep whatever finished; name what did not (WARN, not silent)
                missing = sorted(set(self.MAJOR_CHAINS) - fetched)
                logger.warning(
                    "DeFiLlama chain TVL partial: %d/%d chains fetched "
                    "before pool timeout; missing: %s",
                    len(results), len(self.MAJOR_CHAINS), ", ".join(missing),
                )

        missing_after = sorted(set(self.MAJOR_CHAINS) - set(results))
        if missing_after:
            logger.warning(
                "DeFiLlama chain TVL degraded: no data for %d chain(s): %s "
                "(scoring on the remaining %d)",
                len(missing_after), ", ".join(missing_after), len(results),
            )

        if results:
            # WO-1017: merge into the per-chain last-known-good store and
            # persist, so a later empty round (possibly in a NEW process)
            # can fall back. Partial rounds keep their WO-1010 semantics
            # (no stale backfill here) but still refresh their own chains.
            now = time.time()
            for chain, chg in results.items():
                self._cache[chain] = {"chg": chg, "ts": now}
            self._cache_ts = now
            self._persist_disk_cache()
            return results

        # All fetches failed — fall back to last-known-good values
        # (memory first, disk-backed), per chain, within FALLBACK_TTL.
        now = time.time()
        stale = {
            chain: entry["chg"]
            for chain, entry in self._cache.items()
            if now - entry.get("ts", 0) < FALLBACK_TTL
        }
        if stale:
            logger.warning(
                "DeFiLlama fetch round EMPTY for all %d chains — serving "
                "last-known-good TVL for %d chain(s) within %ds TTL",
                len(self.MAJOR_CHAINS), len(stale), FALLBACK_TTL,
            )
            return stale

        logger.warning(
            "DeFiLlama fetch round EMPTY and no fresh fallback within "
            "%ds TTL — on-chain dimension goes empty this round",
            FALLBACK_TTL,
        )
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
