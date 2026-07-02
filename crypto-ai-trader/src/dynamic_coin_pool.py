"""
Dynamic Coin Pool Module
Filters coins by volume, market cap, and liquidity to build a dynamic
universe for scanning. Avoids stablecoins, wrapped tokens, leverage tokens,
and applies dynamic filters for price action and trade activity.
"""

import logging
import re
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from .exchange_client import ExchangeClient
from .risk_manager import SectorExposure

logger = logging.getLogger(__name__)

# Tokens to always exclude from the pool
STABLECOINS = {"USDCUSDT", "TUSDUSDT", "BUSDUSDT", "FDUSDUSDT", "DAIUSDT", "USDPUSDT"}

WRAPPED_TOKENS = {
    "WETHUSDT",
    "WBTCUSDT",
    "WBNBUSDT",
    "WMATICUSDT",
    "WAVAXUSDT",
    "WFTMUSDT",
    "WLINKUSDT",
}

# Leverage token suffixes/patterns
_LEVERAGE_TOKEN_RE = re.compile(r"(UP|DOWN|BULL|BEAR)[0-9]*USDT$")


class DynamicCoinPool:
    """Build and filter a dynamic coin pool for trading signal scanning.

    The pool is rebuilt on each call to ``build_pool()`` using live 24hr stats
    from Binance, applying volume, price, and activity filters.  Downstream
    methods like ``get_sector_filtered_pool()`` and ``get_momentum_pool()``
    further refine the pool for specific strategies.
    """

    def __init__(self, client: "ExchangeClient"):
        self.client = client

    # ------------------------------------------------------------------
    # 1. Core pool builder
    # ------------------------------------------------------------------

    def build_pool(
        self,
        min_volume_usd: float = 5_000_000,
        max_coins: int = 40,
        exclude_stablecoins: bool = True,
        min_price: float = 0.01,
        min_trades: int = 10_000,
        price_change_min: float = -25.0,
        price_change_max: float = 30.0,
    ) -> List[Dict]:
        """Fetch all USDT pairs and return a filtered, ranked pool.

        Args:
            min_volume_usd: Minimum 24h quote volume in USDT.
            max_coins: Maximum number of coins to return (top-N by volume).
            exclude_stablecoins: Remove stablecoin pairs from the pool.
            min_price: Minimum last price to avoid dust / low-value tokens.
            min_trades: Minimum number of trades in 24h (liquidity proxy).
            price_change_min: Lower bound for 24h price change %.
            price_change_max: Upper bound for 24h price change %.

        Returns:
            List of dicts sorted by volume descending, each with:
                symbol, volume_24h, price, price_change_24h, rank
        """
        raw = self.client.get_24hr_stats()
        if not raw:
            logger.warning("DynamicCoinPool: no 24hr stats returned from client")
            return []

        pool: List[Dict] = []

        for ticker in raw:
            symbol = ticker.get("symbol", "").replace("/", "")

            # --- Static exclusion filters ---

            # Only USDT pairs
            if not symbol.endswith("USDT"):
                continue

            # Stablecoins
            if exclude_stablecoins and symbol in STABLECOINS:
                continue

            # Wrapped tokens (duplicates of BTC/ETH)
            if symbol in WRAPPED_TOKENS:
                continue

            # Leverage tokens (UP/DOWN/BULL/BEAR suffixes)
            if _LEVERAGE_TOKEN_RE.match(symbol):
                continue

            # --- Quantitative filters ---

            volume = ticker.get("quote_volume", 0)
            price = ticker.get("last_price", 0)
            price_change = ticker.get("price_change_pct", 0)

            if volume < min_volume_usd:
                continue

            if price < min_price:
                continue

            if price_change < price_change_min or price_change > price_change_max:
                continue

            # Trades count — field may be missing if the client didn't extract it
            trades = ticker.get("trades")
            if trades is not None and trades <= min_trades:
                continue

            pool.append(
                {
                    "symbol": symbol,
                    "volume_24h": volume,
                    "price": price,
                    "price_change_24h": price_change,
                    "trades": trades,
                }
            )

        # Sort by volume descending and assign ranks
        pool.sort(key=lambda c: c["volume_24h"], reverse=True)

        for rank, coin in enumerate(pool, start=1):
            coin["rank"] = rank

        # Trim to max_coins
        if len(pool) > max_coins:
            pool = pool[:max_coins]

        logger.info(
            "DynamicCoinPool: built pool with %d coins (min_vol=$%.0f, max=%d)",
            len(pool),
            min_volume_usd,
            max_coins,
        )
        return pool

    # ------------------------------------------------------------------
    # 2. Sector-aware priority adjustment
    # ------------------------------------------------------------------

    def get_sector_filtered_pool(
        self,
        pool: List[Dict],
        positions: List[Dict],
        sector_limits: Optional[Dict] = None,
        account_equity: Optional[float] = None,
    ) -> List[Dict]:
        """Annotate pool coins with sector priority based on current exposure.

        Uses :class:`SectorExposure` to compute per-sector utilization.  If a
        sector is at >80 % of its limit, coins in that sector are downgraded
        from ``high`` → ``medium`` → ``low``.  This does **not** hard-block
        coins — it merely adds a ``sector_priority`` field for downstream
        scoring logic.

        Args:
            pool: Output from :meth:`build_pool`.
            positions: Current positions with ``symbol`` and ``value_usdt``.
            sector_limits: Optional override of ``{sector: max_pct}``.
                           Falls back to ``SectorExposure.MAX_SECTOR_PCT`` (30).
            account_equity: Total account value including USDT cash.  When
                provided, used as denominator for sector % (correct behaviour).

        Returns:
            Pool with added ``sector_priority`` field on each coin dict.
        """
        if not pool:
            return pool

        exposure = SectorExposure()
        check_result = exposure.check(positions, account_equity=account_equity)
        details = check_result.get("details", {})
        default_limit = sector_limits or {}

        # Determine which sectors are "near limit" (>80 % utilised)
        near_limit_sectors: set = set()
        blocked_sectors: set = set(check_result.get("blocked_sectors", []))

        for sector, info in details.items():
            limit = default_limit.get(
                sector, info.get("limit_pct", SectorExposure.MAX_SECTOR_PCT)
            )
            pct = info.get("pct", 0)
            if pct >= limit:
                blocked_sectors.add(sector)
            elif pct >= limit * 0.8:
                near_limit_sectors.add(sector)

        for coin in pool:
            symbol = coin["symbol"]
            sector = exposure.classify_position(symbol)

            if sector in blocked_sectors:
                priority = "low"
            elif sector in near_limit_sectors:
                priority = "medium"
            else:
                priority = "high"

            coin["sector_priority"] = priority
            coin["sector"] = sector

        if near_limit_sectors or blocked_sectors:
            logger.info(
                "DynamicCoinPool: near-limit sectors=%s, blocked=%s",
                near_limit_sectors,
                blocked_sectors,
            )

        return pool

    # ------------------------------------------------------------------
    # 3. Momentum sub-pool
    # ------------------------------------------------------------------

    def get_momentum_pool(
        self,
        pool: List[Dict],
        price_change_floor: float = -5.0,
        min_consistency_days: int = 3,
    ) -> List[Dict]:
        """Filter pool to coins showing sustained momentum.

        A coin qualifies when:
        * 24h price change is above *price_change_floor* (not crashing).
        * It has maintained high volume for at least *min_consistency_days*
          (verified via 3-day average volume from daily klines).

        Each qualifying coin receives a ``momentum_score`` (0–100) based on
        volume consistency and price action strength.

        Args:
            pool: Output from :meth:`build_pool`.
            price_change_floor: Minimum 24h price change % to qualify.
            min_consistency_days: Days of sustained high volume required.

        Returns:
            Subset of *pool* with added ``momentum_score`` field.
        """
        if not pool:
            return []

        # Pre-filter by 24h price change
        candidates = [
            c for c in pool if c.get("price_change_24h", -999) > price_change_floor
        ]

        if not candidates:
            logger.info("DynamicCoinPool: no momentum candidates after price filter")
            return []

        momentum_pool: List[Dict] = []
        {c["symbol"] for c in pool}
        top_volume_threshold = pool[-1]["volume_24h"] if pool else 0

        for coin in candidates:
            symbol = coin["symbol"]
            avg_3d_volume = self._get_avg_volume(symbol, days=min_consistency_days)

            # If we couldn't get multi-day data, still include the coin
            # but assign a lower consistency score.
            if avg_3d_volume is None:
                volume_consistent = False
                consistency_bonus = 25  # partial credit for being in today's top
            else:
                # Check if 3-day average volume is at least 60% of today's volume
                # and still above a reasonable threshold (40% of pool floor)
                volume_consistent = (
                    avg_3d_volume >= coin["volume_24h"] * 0.6
                    and avg_3d_volume >= top_volume_threshold * 0.4
                )
                consistency_bonus = 50 if volume_consistent else 10

            # --- Momentum score calculation (0–100) ---
            # Component 1: Volume consistency (0–50)
            vol_score = consistency_bonus

            # Component 2: Price action strength (0–30)
            # Positive change is good; strong positive (5–20%) is ideal
            pc = coin.get("price_change_24h", 0)
            if 5 <= pc <= 20:
                price_score = 30
            elif 0 <= pc < 5:
                price_score = 20
            elif -5 <= pc < 0:
                price_score = 10
            else:
                price_score = 5

            # Component 3: Volume rank bonus (0–20)
            # Higher ranked coins get a bonus
            rank = coin.get("rank", len(pool))
            if rank <= 10:
                rank_score = 20
            elif rank <= 20:
                rank_score = 15
            elif rank <= 30:
                rank_score = 10
            else:
                rank_score = 5

            momentum_score = vol_score + price_score + rank_score

            coin_out = dict(coin)
            coin_out["momentum_score"] = min(momentum_score, 100)
            coin_out["avg_3d_volume"] = avg_3d_volume
            coin_out["volume_consistent"] = volume_consistent

            momentum_pool.append(coin_out)

        # Sort by momentum score descending
        momentum_pool.sort(key=lambda c: c["momentum_score"], reverse=True)

        logger.info(
            "DynamicCoinPool: momentum pool has %d coins (from %d candidates)",
            len(momentum_pool),
            len(candidates),
        )
        return momentum_pool

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_avg_volume(self, symbol: str, days: int = 3) -> Optional[float]:
        """Fetch daily klines and compute average quote-volume over *days*.

        Returns ``None`` if data is unavailable (fail-open).
        """
        try:
            klines = self.client.get_klines(symbol, "1d", limit=days + 1)
            if not klines or len(klines) < days:
                return None

            # Use the most recent `days` candles (excluding the current
            # incomplete one if applicable)
            volumes = [k["quote_volume"] for k in klines[-days:]]
            if not volumes or all(v == 0 for v in volumes):
                return None

            return sum(volumes) / len(volumes)
        except Exception as e:
            logger.debug(
                "DynamicCoinPool: failed to get avg volume for %s: %s", symbol, e
            )
            return None
