"""
DeFiLlama Data Feed — via llama-data-skill CLI wrapper.

Fetches on-chain data from DeFiLlama through the Coze data-provider gateway:
- Stablecoin supply trends (USDT/USDC capital inflow/outflow)
- DEX volume overview (market activity signal)
- Chain TVL changes (replaces deprecated /v2/historicalChainTvl)

All calls go through the skill CLI wrapper which handles auth injection.
Fallback: returns None/empty on failure, won't break the scoring pipeline.

Design:
- Subprocess calls with 15s timeout per operation
- In-memory cache with 30-min TTL (stale data better than slow scans)
- Graceful degradation: any failure → return None, dimension scorer uses neutral
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Path to llama-data-skill CLI wrapper
# Priority: 1) LLAMA_CLI env var, 2) relative path from project root
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_LLAMA_CLI = os.path.join(
    _PROJECT_ROOT, "..", "..", ".skills", "skill_llama-data-skill", "bin", "_cli_wrapper.py"
)
LLAMA_CLI = os.environ.get("LLAMA_CLI", _DEFAULT_LLAMA_CLI)

# Cache TTL
CACHE_TTL = 1800  # 30 minutes
REQUEST_TIMEOUT = 20  # seconds per subprocess call

# Stablecoins to track (top 2 by market cap)
TRACKED_STABLECOINS = ["USDT", "USDC"]

# Stablecoins to check for depeg (only pure USD pegs, not yield-bearing)
DEPEG_CHECK_SYMBOLS = {"USDT", "USDC", "DAI", "FDUSD", "USDS", "TUSD", "BUSD", "PYUSD", "GUSD"}

# Major chains for TVL tracking
MAJOR_CHAINS = ["Ethereum", "BSC", "Arbitrum", "Base", "Solana"]


class LlamaDataFeed:
    """Fetch DeFiLlama data via llama-data-skill CLI."""

    def __init__(self) -> None:
        self._cache: Dict[str, Any] = {}
        self._cache_ts: Dict[str, float] = {}

    def _call_llama(self, operation: str, params: Optional[Dict] = None) -> Optional[Any]:
        """Call a llama CLI operation and return parsed JSON."""
        cmd = ["python3", LLAMA_CLI, "call", operation]
        if params:
            for k, v in params.items():
                cmd.extend(["--param", f"{k}={v}"])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=REQUEST_TIMEOUT,
            )
            if result.returncode != 0:
                logger.warning(
                    "llama CLI %s failed (exit=%d): %s",
                    operation,
                    result.returncode,
                    result.stderr[:200] if result.stderr else "no stderr",
                )
                return None
            return json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            logger.warning("llama CLI %s timed out after %ds", operation, REQUEST_TIMEOUT)
            return None
        except json.JSONDecodeError as e:
            logger.warning("llama CLI %s returned invalid JSON: %s", operation, e)
            return None
        except Exception as e:
            logger.warning("llama CLI %s unexpected error: %s", operation, e)
            return None

    def _get_cached(self, key: str) -> Optional[Any]:
        """Return cached value if still fresh."""
        ts = self._cache_ts.get(key, 0)
        if time.time() - ts < CACHE_TTL and key in self._cache:
            return self._cache[key]
        return None

    def _set_cache(self, key: str, value: Any) -> None:
        self._cache[key] = value
        self._cache_ts[key] = time.time()

    # ------------------------------------------------------------------
    # Stablecoin Supply
    # ------------------------------------------------------------------
    def get_stablecoin_supply(self) -> Optional[Dict[str, Any]]:
        """Get USDT/USDC supply data.

        Returns:
            {
                "total_circulating_usd": float,
                "usdt_circulating": float,
                "usdc_circulating": float,
                "supply_change_24h_pct": float (estimated),
                "depeg_alerts": list,
            }
        or None on failure.
        """
        cached = self._get_cached("stablecoins")
        if cached is not None:
            return cached

        data = self._call_llama("stablecoins", {"includePrices": "true"})
        if not data:
            return None

        try:
            # Data is a dict with 'peggedAssets' key (not a flat list)
            stablecoins = data.get("peggedAssets", []) if isinstance(data, dict) else data
            if not stablecoins:
                return None

            result = {
                "total_circulating_usd": 0,
                "usdt_circulating": 0,
                "usdc_circulating": 0,
                "usdt_change_day": 0,
                "usdc_change_day": 0,
                "depeg_alerts": [],
            }

            for coin in stablecoins:
                if not isinstance(coin, dict):
                    continue
                symbol = coin.get("symbol", "").upper()
                name = coin.get("name", "").upper()

                # Extract circulating supply
                circulating_data = coin.get("circulating", {})
                if isinstance(circulating_data, dict):
                    pegged_usd = circulating_data.get("peggedUSD", 0) or 0
                elif isinstance(circulating_data, (int, float)):
                    pegged_usd = circulating_data
                else:
                    pegged_usd = 0

                result["total_circulating_usd"] += pegged_usd

                # Extract previous day circulating for change calculation
                prev_day_data = coin.get("circulatingPrevDay", {})
                prev_day_usd = 0
                if isinstance(prev_day_data, dict):
                    prev_day_usd = prev_day_data.get("peggedUSD", 0) or 0

                # Track specific stablecoins (match exactly by symbol only)
                if symbol == "USDT":
                    result["usdt_circulating"] = pegged_usd
                    if prev_day_usd > 0:
                        result["usdt_change_day"] = round((pegged_usd - prev_day_usd) / prev_day_usd * 100, 2)
                elif symbol == "USDC":
                    result["usdc_circulating"] = pegged_usd
                    if prev_day_usd > 0:
                        result["usdc_change_day"] = round((pegged_usd - prev_day_usd) / prev_day_usd * 100, 2)

                # Check for depeg alerts — only for known pure-USD stablecoins
                # Yield-bearing tokens (USYC, USDY, etc.) naturally trade >$1
                price = coin.get("price")
                if (
                    price
                    and isinstance(price, (int, float))
                    and symbol in DEPEG_CHECK_SYMBOLS
                    and pegged_usd > 100_000_000
                ):
                    if abs(price - 1.0) > 0.01:  # >1% depeg
                        result["depeg_alerts"].append({
                            "name": coin.get("name", "unknown"),
                            "symbol": symbol,
                            "price": price,
                            "circulating_usd": pegged_usd,
                            "deviation_pct": round((price - 1.0) * 100, 2),
                        })

            self._set_cache("stablecoins", result)
            return result

        except Exception as e:
            logger.warning("Failed to parse stablecoin data: %s", e)
            return None

    # ------------------------------------------------------------------
    # DEX Volume
    # ------------------------------------------------------------------
    def get_dex_volume(self) -> Optional[Dict[str, Any]]:
        """Get DEX volume overview.

        Returns:
            {
                "total_24h_usd": float,
                "total_7d_usd": float,
                "change_24h_pct": float,
                "top_dexes": list of {name, volume_24h},
            }
        or None on failure.
        """
        cached = self._get_cached("dex_volume")
        if cached is not None:
            return cached

        data = self._call_llama("overview-dexs", {
            "excludeTotalDataChart": "true",
            "excludeTotalDataChartBreakdown": "true",
        })
        if not data:
            return None

        try:
            protocols = data.get("protocols", [])
            total_24h = data.get("total24h", 0) or 0
            total_7d = data.get("total7d", 0) or 0

            # Top 5 DEXes by volume
            top_dexes = []
            for p in sorted(protocols, key=lambda x: x.get("total24h", 0) or 0, reverse=True)[:5]:
                top_dexes.append({
                    "name": p.get("name", "unknown"),
                    "volume_24h": p.get("total24h", 0) or 0,
                })

            # Calculate 7d avg to estimate change
            avg_7d_daily = total_7d / 7 if total_7d > 0 else 0
            change_pct = ((total_24h - avg_7d_daily) / avg_7d_daily * 100) if avg_7d_daily > 0 else 0

            result = {
                "total_24h_usd": total_24h,
                "total_7d_usd": total_7d,
                "change_24h_pct": round(change_pct, 1),
                "top_dexes": top_dexes,
            }
            self._set_cache("dex_volume", result)
            return result

        except Exception as e:
            logger.warning("Failed to parse DEX volume data: %s", e)
            return None

    # ------------------------------------------------------------------
    # Chain TVL (replaces deprecated /v2/historicalChainTvl)
    # ------------------------------------------------------------------
    def get_chain_tvl(self) -> Optional[Dict[str, float]]:
        """Get TVL change % for major chains.

        Uses chain-tvl-history for each chain to compute 1d change.
        Returns: {chain_name: change_1d_pct} or None on failure.
        """
        cached = self._get_cached("chain_tvl")
        if cached is not None:
            return cached

        result: Dict[str, float] = {}
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _fetch_chain_change(chain: str) -> Optional[tuple]:
            data = self._call_llama("chain-tvl-history", {"chain": chain})
            if not data or not isinstance(data, list) or len(data) < 2:
                return None
            try:
                tvl_now = float(data[-1].get("tvl", 0))
                tvl_prev = float(data[-2].get("tvl", 0))
                if tvl_prev > 0:
                    return (chain, (tvl_now - tvl_prev) / tvl_prev * 100)
            except (ValueError, TypeError, KeyError):
                pass
            return None

        try:
            with ThreadPoolExecutor(max_workers=len(MAJOR_CHAINS)) as pool:
                futures = {
                    pool.submit(_fetch_chain_change, chain): chain
                    for chain in MAJOR_CHAINS
                }
                for future in as_completed(futures, timeout=45):
                    try:
                        res = future.result()
                        if res:
                            result[res[0]] = round(res[1], 2)
                    except Exception as e:
                        logger.warning("data_feed_llama.get_chain_tvl: " + str(e))
                        pass

            if result:
                self._set_cache("chain_tvl", result)
            return result if result else None

        except Exception as e:
            logger.warning("Failed to fetch chain TVL data: %s", e)
            return None

    # ------------------------------------------------------------------
    # Combined summary for dimension scorer
    # ------------------------------------------------------------------
    def get_onchain_summary(self) -> Dict[str, Any]:
        """Get a combined on-chain summary for dimension scoring.

        Returns dict with all available on-chain signals.
        """
        stablecoins = self.get_stablecoin_supply()
        dex_vol = self.get_dex_volume()
        chain_tvl = self.get_chain_tvl()

        summary: Dict[str, Any] = {
            "stablecoin": stablecoins,
            "dex_volume": dex_vol,
            "chain_tvl": chain_tvl,
            "data_quality": "full" if all([stablecoins, dex_vol, chain_tvl]) else "partial",
        }

        return summary
