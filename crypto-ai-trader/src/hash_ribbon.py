"""
Hash Ribbon — Miner Capitulation Signal.

Detects Bitcoin miner capitulation via hash rate moving average crossover:
- 30 DMA < 60 DMA → capitulation phase (miners shutting down)
- 30 DMA crosses above 60 DMA → recovery signal (BUY)

Data source: mempool.space API (daily hash rate, ~1 year history)
Signal frequency: 1-2 times per year, extremely high conviction.

Reference: Axel Adler Jr (axeladlerjr.com), historically 64% profitable
with only 1 false signal (Aug 2022).
"""

import json
import logging
import os
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Cache file for hash rate data (avoid hitting API on every scan)
_CACHE_DIR = "/tmp/crypto_cache"
_CACHE_FILE = os.path.join(_CACHE_DIR, "hashrate_cache.json")
_CACHE_TTL = 86400  # 24 hours — hash rate updates daily


def _get_cached_hashrate() -> Optional[List[Dict]]:
    """Read cached hash rate data if fresh enough."""
    try:
        if not os.path.exists(_CACHE_FILE):
            return None
        with open(_CACHE_FILE, "r") as f:
            cache = json.load(f)
        if time.time() - cache.get("fetched_at", 0) > _CACHE_TTL:
            return None
        return cache.get("data")
    except Exception as e:
        logger.warning("hash_ribbon._get_cached_hashrate: " + str(e))
        return None


def _save_cache(data: List[Dict]) -> None:
    """Save hash rate data to cache."""
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_CACHE_FILE, "w") as f:
            json.dump({"fetched_at": time.time(), "data": data}, f)
    except Exception as e:
        logger.debug("Hash ribbon: cache save failed: %s", e)


def fetch_hashrate_data() -> Optional[List[Dict]]:
    """Fetch daily hash rate data from mempool.space API.

    Returns list of {timestamp, avgHashrate} or None on failure.
    Uses 24h cache to avoid redundant API calls.
    """
    # Try cache first
    cached = _get_cached_hashrate()
    if cached:
        logger.debug("Hash ribbon: using cached hash rate data (%d points)", len(cached))
        return cached

    try:
        import urllib.request
        url = "https://mempool.space/api/v1/mining/hashrate/1y"
        req = urllib.request.Request(url, headers={"User-Agent": "crypto-ai-trader/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        hashrates = data.get("hashrates", [])
        if not hashrates:
            logger.warning("Hash ribbon: empty hashrate data from API")
            return None

        _save_cache(hashrates)
        logger.info("Hash ribbon: fetched %d daily hash rate points", len(hashrates))
        return hashrates

    except Exception as e:
        logger.warning("Hash ribbon: failed to fetch hash rate: %s", e)
        return None


def _moving_average(values: List[float], period: int) -> List[Optional[float]]:
    """Calculate simple moving average. Returns None for indices < period-1."""
    result = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(None)
        else:
            window = values[i - period + 1: i + 1]
            result.append(sum(window) / len(window))
    return result


def calculate_hash_ribbons(hashrate_data: List[Dict]) -> List[Dict]:
    """Calculate 30-day and 60-day hash rate moving averages.

    Returns list of {timestamp, hash_rate, ma30, ma60, in_capitulation}.
    """
    if not hashrate_data or len(hashrate_data) < 60:
        return []

    # Sort by timestamp ascending
    sorted_data = sorted(hashrate_data, key=lambda x: x["timestamp"])

    # Extract hash rate values (convert from H/s to EH/s for readability)
    rates = [d["avgHashrate"] / 1e18 for d in sorted_data]  # EH/s
    timestamps = [d["timestamp"] for d in sorted_data]

    ma30 = _moving_average(rates, 30)
    ma60 = _moving_average(rates, 60)

    result = []
    for i in range(len(sorted_data)):
        entry = {
            "timestamp": timestamps[i],
            "hash_rate_ehs": rates[i],
            "ma30": ma30[i],
            "ma60": ma60[i],
            "in_capitulation": (ma30[i] is not None and ma60[i] is not None
                                and ma30[i] < ma60[i]),
        }
        result.append(entry)

    return result


def detect_signal(ribbons: List[Dict]) -> Optional[Dict]:
    """Detect Hash Ribbon buy signal (recovery from capitulation).

    Signal fires when:
    1. Previous bar: ma30 < ma60 (was in capitulation)
    2. Current bar: ma30 >= ma60 (recovery crossover)

    Returns signal dict or None.
    """
    if not ribbons or len(ribbons) < 2:
        return None

    # Need at least 60 data points for valid MAs
    valid = [r for r in ribbons if r["ma30"] is not None and r["ma60"] is not None]
    if len(valid) < 2:
        return None

    prev = valid[-2]
    curr = valid[-1]

    # Check for recovery crossover
    was_capitulating = prev["in_capitulation"]
    is_recovering = curr["ma30"] >= curr["ma60"]

    if was_capitulating and is_recovering:
        # Calculate signal strength metrics
        ma_gap = (curr["ma30"] - curr["ma60"]) / curr["ma60"] * 100  # % gap
        hash_growth = (curr["hash_rate_ehs"] - prev["hash_rate_ehs"]) / prev["hash_rate_ehs"] * 100

        signal = {
            "type": "hash_ribbon_buy",
            "timestamp": curr["timestamp"],
            "ma30_ehs": curr["ma30"],
            "ma60_ehs": curr["ma60"],
            "ma_gap_pct": ma_gap,
            "hash_rate_ehs": curr["hash_rate_ehs"],
            "hash_growth_pct": hash_growth,
            "confidence": "high",  # historically 64% profitable, avg >5000% to cycle peak
            "recommended_deploy_pct": 0.20,  # 20% of available capital
            "holding_period_days": 180,  # 6-18 months
        }
        logger.info(
            "Hash Ribbon BUY signal: ma30=%.1f EH/s, ma60=%.1f EH/s, gap=%.2f%%",
            curr["ma30"], curr["ma60"], ma_gap,
        )
        return signal

    # Check if currently in capitulation (for informational purposes)
    if curr["in_capitulation"]:
        days_in = sum(1 for r in valid[-30:] if r["in_capitulation"])
        logger.debug("Hash Ribbon: IN CAPITULATION (%d recent days), waiting for recovery", days_in)

    return None


def get_hash_ribbon_status() -> Dict:
    """Get current Hash Ribbon status for reporting.

    Returns {status, ma30, ma60, capitulating, signal_fired, ...}
    """
    result = {
        "status": "unavailable",
        "capitulating": None,
        "signal_fired": False,
    }

    hashrate_data = fetch_hashrate_data()
    if not hashrate_data:
        return result

    ribbons = calculate_hash_ribbons(hashrate_data)
    if not ribbons:
        return result

    valid = [r for r in ribbons if r["ma30"] is not None and r["ma60"] is not None]
    if not valid:
        return result

    curr = valid[-1]
    result.update({
        "status": "active",
        "ma30_ehs": round(curr["ma30"], 2),
        "ma60_ehs": round(curr["ma60"], 2),
        "current_hashrate_ehs": round(curr["hash_rate_ehs"], 2),
        "capitulating": curr["in_capitulation"],
        "timestamp": curr["timestamp"],
    })

    signal = detect_signal(ribbons)
    if signal:
        result["signal_fired"] = True
        result["signal"] = signal

    return result
