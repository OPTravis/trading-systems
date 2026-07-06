"""
Correlation Risk Manager - Prevents high-correlation position stacking.

During market stress, altcoins correlate >0.8 with BTC. Holding BTC+ETH+SOL
provides no diversification — they all crash together.

This module:
1. Computes 45-day return correlation matrix from daily closes
2. Blocks new positions if correlation with existing holdings exceeds threshold
3. Enforces maximum portfolio correlation (average pairwise correlation)
"""

import logging
import time
from typing import Any, Dict, List, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Correlation thresholds
MAX_PAIRWISE_CORR = 0.70  # Block if new symbol correlates >0.7 with any holding
MAX_PORTFOLIO_CORR = 0.60  # Block if average pairwise correlation >0.6
MIN_HISTORY_DAYS = 15  # Minimum days of price history required
CACHE_TTL_SECONDS = 3600  # Recompute correlations every hour


# Class-level cache: persists across instances within the same process.
# Each entry stores its own timestamp so TTL is per-key.
_CLASS_CACHE: Dict[str, Any] = {}


class CorrelationRiskManager:
    """Monitor and enforce correlation limits for portfolio diversification."""

    def __init__(self, binance_client):
        self.client = binance_client
        # Share class-level cache across instances (each scan creates a new RiskManager)
        self._cache = _CLASS_CACHE

    def _get_price_history(self, symbol: str, days: int = 45) -> List[float]:
        """Fetch daily closing prices for correlation calculation."""
        try:
            # Handle both get_klines() (DataFeed) and klines() (BinanceSpotClient)
            client = self.client
            symbol_usdt = f"{symbol}USDT"
            interval = "1d"

            if hasattr(client, "get_klines"):
                klines = client.get_klines(symbol_usdt, interval, limit=days)
            elif hasattr(client, "klines"):
                raw = client.klines(symbol_usdt, interval, limit=days)
                # Convert raw list format to dict format
                klines = [
                    {
                        "open_time": k[0],
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                        "close_time": k[6],
                    }
                    for k in raw
                ]
            else:
                logger.warning(
                    f"CorrelationRisk: client has no klines method for {symbol}"
                )
                return []

            if not klines or len(klines) < MIN_HISTORY_DAYS:
                return []
            return [k["close"] for k in klines]
        except Exception as e:
            logger.warning(f"Failed to get price history for {symbol}: {e}")
            return []

    def _compute_correlation(
        self, prices_a: List[float], prices_b: List[float]
    ) -> float:
        """Compute Pearson correlation between two price series."""
        if len(prices_a) != len(prices_b) or len(prices_a) < MIN_HISTORY_DAYS:
            return 0.0
        # Use log returns for stationarity
        returns_a = np.diff(np.log(prices_a))
        returns_b = np.diff(np.log(prices_b))
        if len(returns_a) < 2 or np.std(returns_a) == 0 or np.std(returns_b) == 0:
            return 0.0
        corr = np.corrcoef(returns_a, returns_b)[0, 1]
        return float(corr) if not np.isnan(corr) else 0.0

    def _build_correlation_matrix(
        self, symbols: List[str]
    ) -> Tuple[Dict[str, Dict[str, float]], Dict[str, List[float]]]:
        """Build correlation matrix for a list of symbols.

        Uses in-memory cache with CACHE_TTL_SECONDS TTL to avoid
        redundant API calls and computation.

        Returns:
            (correlation_dict, price_history_dict)
        """
        now = time.time()
        cache_key = ",".join(sorted(symbols))

        # Check cache (per-key TTL)
        cached_entry = self._cache.get(cache_key)
        if cached_entry and (now - cached_entry["ts"]) < CACHE_TTL_SECONDS:
            logger.debug("CorrelationRisk: cache hit for %s", cache_key)
            return cached_entry["matrix"], cached_entry["histories"]

        # Fetch all price histories
        histories = {}
        for sym in symbols:
            hist = self._get_price_history(sym, days=45)
            if len(hist) >= MIN_HISTORY_DAYS:
                histories[sym] = hist

        # Compute pairwise correlations
        corr_matrix: Dict[str, Dict[str, float]] = {}
        symbols_with_data = list(histories.keys())
        for i, sym_a in enumerate(symbols_with_data):
            corr_matrix[sym_a] = {}
            for sym_b in symbols_with_data:
                if sym_a == sym_b:
                    corr_matrix[sym_a][sym_b] = 1.0
                else:
                    corr_matrix[sym_a][sym_b] = self._compute_correlation(
                        histories[sym_a], histories[sym_b]
                    )

        # Update cache (per-key timestamp)
        self._cache[cache_key] = {
            "matrix": corr_matrix,
            "histories": histories,
            "ts": now,
        }
        logger.debug("CorrelationRisk: cache updated for %s (%d symbols)", cache_key, len(symbols))

        return corr_matrix, histories

    def check_new_position(self, new_symbol: str, current_positions: List[str]) -> Dict:
        """Check if adding new_symbol would violate correlation limits.

        Args:
            new_symbol: Symbol to potentially add (e.g., 'SOL')
            current_positions: List of currently held symbols (e.g., ['BTC', 'ETH'])

        Returns:
            {
                allowed: bool,
                reason: str,
                correlations: {symbol: corr_value},  # correlations with existing positions
                avg_correlation: float,
                max_correlation: float,
            }
        """
        if not current_positions:
            return {
                "allowed": True,
                "reason": "No existing positions",
                "correlations": {},
                "avg_correlation": 0.0,
                "max_correlation": 0.0,
            }

        # Same-symbol DCA: adding to an existing position is not a
        # diversification concern — correlation check only makes sense
        # for *different* assets. Allow with reduced size for safety.
        if new_symbol in current_positions:
            other_positions = [p for p in current_positions if p != new_symbol]
            if not other_positions:
                logger.info(
                    f"CorrelationRisk: {new_symbol} already held — "
                    f"DCA add allowed (same-symbol, no other positions)"
                )
                return {
                    "allowed": True,
                    "reason": f"{new_symbol} already held — DCA add (same-symbol exempt)",
                    "correlations": {new_symbol: 1.0},
                    "avg_correlation": 0.0,
                    "max_correlation": 0.0,
                    "size_multiplier": 0.8,  # slight reduction for safety
                }
            # Has other positions too — check correlation with those, skip self
            all_symbols = list(set(other_positions + [new_symbol]))
            corr_matrix, histories = self._build_correlation_matrix(all_symbols)
            correlations = {}
            max_corr = 0.0
            for pos in other_positions:
                if pos in corr_matrix.get(new_symbol, {}):
                    corr = corr_matrix[new_symbol][pos]
                    correlations[pos] = round(corr, 3)
                    max_corr = max(max_corr, abs(corr))
            blocked_by_pair = [
                f"{p} ({c:.2f})"
                for p, c in correlations.items()
                if abs(c) > MAX_PAIRWISE_CORR
            ]
            if blocked_by_pair:
                return {
                    "allowed": False,
                    "reason": f"High correlation with other holdings: {', '.join(blocked_by_pair)}. Limit={MAX_PAIRWISE_CORR}",
                    "correlations": correlations,
                    "avg_correlation": round(max_corr, 3),
                    "max_correlation": round(max_corr, 3),
                    "size_multiplier": 1.0,
                }
            logger.info(
                f"CorrelationRisk: {new_symbol} DCA add — "
                f"corr with other holdings OK (max={max_corr:.2f})"
            )
            return {
                "allowed": True,
                "reason": f"{new_symbol} already held — DCA add (same-symbol exempt, other corr max={max_corr:.2f})",
                "correlations": correlations,
                "avg_correlation": round(max_corr, 3),
                "max_correlation": round(max_corr, 3),
                "size_multiplier": 0.8,  # slight reduction for safety
            }

        # Include new symbol in correlation calculation
        all_symbols = list(set(current_positions + [new_symbol]))
        corr_matrix, histories = self._build_correlation_matrix(all_symbols)

        if new_symbol not in corr_matrix:
            logger.warning(
                f"CorrelationRisk: insufficient data for {new_symbol}, fail-open with reduced size ×0.5 (P1)"
            )
            return {
                "allowed": True,
                "reason": f"Insufficient price history for {new_symbol} — fail-open with ×0.5 size",
                "correlations": {},
                "avg_correlation": 0.0,
                "max_correlation": 0.0,
                "size_multiplier": 0.5,
            }

        # Check pairwise correlations with existing positions
        correlations = {}
        max_corr = 0.0
        for pos in current_positions:
            if pos in corr_matrix.get(new_symbol, {}):
                corr = corr_matrix[new_symbol][pos]
                correlations[pos] = round(corr, 3)
                max_corr = max(max_corr, abs(corr))

        # Check if any pairwise correlation exceeds threshold
        blocked_by_pair = []
        for pos, corr in correlations.items():
            if abs(corr) > MAX_PAIRWISE_CORR:
                blocked_by_pair.append(f"{pos} ({corr:.2f})")

        # Calculate portfolio average correlation if added
        portfolio_symbols = current_positions + [new_symbol]
        portfolio_corrs = []
        for i, sym_a in enumerate(portfolio_symbols):
            for sym_b in portfolio_symbols[i + 1 :]:
                if sym_a in corr_matrix and sym_b in corr_matrix.get(sym_a, {}):
                    portfolio_corrs.append(abs(corr_matrix[sym_a][sym_b]))

        avg_corr = (
            sum(portfolio_corrs) / len(portfolio_corrs) if portfolio_corrs else 0.0
        )

        # Decision
        if blocked_by_pair:
            reason = f"High correlation with: {', '.join(blocked_by_pair)}. Limit={MAX_PAIRWISE_CORR}"
            allowed = False
            size_multiplier = 1.0
        elif avg_corr > MAX_PORTFOLIO_CORR and len(current_positions) >= 2:
            reason = f"Portfolio avg correlation would be {avg_corr:.2f}. Limit={MAX_PORTFOLIO_CORR}"
            allowed = False
            size_multiplier = 1.0
        else:
            reason = f"Max pairwise corr={max_corr:.2f}, portfolio avg={avg_corr:.2f}"
            allowed = True
            size_multiplier = 1.0

        return {
            "allowed": allowed,
            "reason": reason,
            "correlations": correlations,
            "avg_correlation": round(avg_corr, 3),
            "max_correlation": round(max_corr, 3),
            "size_multiplier": size_multiplier,
        }

    def get_portfolio_correlation_summary(self, positions: List[str]) -> Dict:
        """Get correlation summary for current portfolio (no new position)."""
        if len(positions) < 2:
            return {
                "avg_correlation": 0.0,
                "max_correlation": 0.0,
                "pairs": [],
            }

        corr_matrix, _ = self._build_correlation_matrix(positions)
        pairs = []
        corrs = []
        for i, sym_a in enumerate(positions):
            for sym_b in positions[i + 1 :]:
                if sym_a in corr_matrix and sym_b in corr_matrix.get(sym_a, {}):
                    corr = corr_matrix[sym_a][sym_b]
                    pairs.append(
                        {
                            "pair": f"{sym_a}-{sym_b}",
                            "correlation": round(corr, 3),
                        }
                    )
                    corrs.append(abs(corr))

        return {
            "avg_correlation": round(sum(corrs) / len(corrs), 3) if corrs else 0.0,
            "max_correlation": round(max(corrs), 3) if corrs else 0.0,
            "pairs": pairs,
        }
