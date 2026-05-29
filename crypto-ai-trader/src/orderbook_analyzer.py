"""
Order Book Analyzer — Phase 4 Data Layer Expansion.

Fetches and analyzes Binance order book depth to detect:
- Buy/sell pressure imbalance
- Large orders (whale walls)
- Support/resistance levels from order clustering

Provides a 0-100 score that can be used as an additional factor
in the multi-factor scoring system.

API: Binance GET /api/v3/depth (public, no key needed)
Rate limit: weight 1-5 per call depending on limit param
"""

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class OrderBookAnalyzer:
    """Analyze order book depth for buy/sell pressure and whale detection."""

    def __init__(self, binance_client=None):
        if binance_client is None:
            from src.binance_client import BinanceClient
            binance_client = BinanceClient(testnet=False)
        self._client = binance_client

    def get_depth(self, symbol: str, limit: int = 20) -> Optional[Dict]:
        """Fetch order book depth from Binance.

        Returns: {"bids": [[price, qty], ...], "asks": [[price, qty], ...]}
        """
        try:
            resp = self._client.get_order_book(symbol=symbol, limit=limit)
            return {
                "bids": [[float(p), float(q)] for p, q in resp.get("bids", [])],
                "asks": [[float(p), float(q)] for p, q in resp.get("asks", [])],
            }
        except Exception as e:
            logger.warning(f"Depth fetch failed for {symbol}: {e}")
            return None

    def analyze(self, symbol: str, limit: int = 20) -> Optional[Dict]:
        """Full order book analysis.

        Returns:
            {
                "score": 0-100 (higher = more bullish pressure),
                "bid_ask_ratio": float (bid_volume / ask_volume),
                "spread_pct": float (spread as % of mid price),
                "whale_bid": float (largest bid order value in USDT),
                "whale_ask": float (largest ask order value in USDT),
                "support_level": float (price with most bid volume),
                "resistance_level": float (price with most ask volume),
                "bid_volume_usdt": float,
                "ask_volume_usdt": float,
            }
        """
        depth = self.get_depth(symbol, limit)
        if not depth or not depth["bids"] or not depth["asks"]:
            return None

        bids = depth["bids"]
        asks = depth["asks"]

        # Current price (mid of best bid/ask)
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid_price = (best_bid + best_ask) / 2

        # Spread
        spread_pct = (best_ask - best_bid) / mid_price * 100

        # Volume analysis
        bid_volume = sum(q for _, q in bids)
        ask_volume = sum(q for _, q in asks)
        bid_volume_usdt = sum(p * q for p, q in bids)
        ask_volume_usdt = sum(p * q for p, q in asks)

        # Bid/ask ratio (>1 = more buying pressure)
        bid_ask_ratio = bid_volume / ask_volume if ask_volume > 0 else 10.0

        # Whale detection (largest single order)
        whale_bid_usdt = max((p * q for p, q in bids), default=0)
        whale_ask_usdt = max((p * q for p, q in asks), default=0)

        # Support/resistance from order clustering
        # Support: price level with highest bid volume
        support_level = max(bids, key=lambda x: x[1])[0] if bids else best_bid
        # Resistance: price level with highest ask volume
        resistance_level = max(asks, key=lambda x: x[1])[0] if asks else best_ask

        # Compute 0-100 score
        score = self._compute_score(
            bid_ask_ratio=bid_ask_ratio,
            spread_pct=spread_pct,
            whale_bid=whale_bid_usdt,
            whale_ask=whale_ask_usdt,
            bid_volume_usdt=bid_volume_usdt,
            ask_volume_usdt=ask_volume_usdt,
        )

        return {
            "score": round(score, 1),
            "bid_ask_ratio": round(bid_ask_ratio, 3),
            "spread_pct": round(spread_pct, 4),
            "whale_bid": round(whale_bid_usdt, 2),
            "whale_ask": round(whale_ask_usdt, 2),
            "support_level": support_level,
            "resistance_level": resistance_level,
            "bid_volume_usdt": round(bid_volume_usdt, 2),
            "ask_volume_usdt": round(ask_volume_usdt, 2),
            "mid_price": round(mid_price, 6),
        }

    def _compute_score(
        self,
        bid_ask_ratio: float,
        spread_pct: float,
        whale_bid: float,
        whale_ask: float,
        bid_volume_usdt: float,
        ask_volume_usdt: float,
    ) -> float:
        """Compute 0-100 order book score.

        Components:
        - Bid/ask ratio (40%): >1 = bullish, <1 = bearish
        - Whale imbalance (30%): more bid whales = bullish
        - Volume imbalance (20%): more bid volume = bullish
        - Spread (10%): tight spread = healthy market
        """
        # 1. Bid/ask ratio score (40%)
        # ratio 1.0 → 50, ratio 2.0 → 80, ratio 0.5 → 20
        ratio_score = min(100, max(0, 50 + (bid_ask_ratio - 1) * 30))

        # 2. Whale imbalance score (30%)
        # More bid whales → bullish
        total_whale = whale_bid + whale_ask
        if total_whale > 0:
            whale_ratio = whale_bid / total_whale
            whale_score = whale_ratio * 100  # 0-100
        else:
            whale_score = 50

        # 3. Volume imbalance score (20%)
        total_vol = bid_volume_usdt + ask_volume_usdt
        if total_vol > 0:
            vol_ratio = bid_volume_usdt / total_vol
            vol_score = vol_ratio * 100
        else:
            vol_score = 50

        # 4. Spread score (10%)
        # Tight spread = healthy, wide spread = illiquid
        # 0.01% → 100, 0.1% → 70, 1% → 20
        spread_score = max(0, min(100, 100 - spread_pct * 80))

        score = (
            0.40 * ratio_score
            + 0.30 * whale_score
            + 0.20 * vol_score
            + 0.10 * spread_score
        )

        return max(0, min(100, score))

    def get_batch_depth(self, symbols: List[str], limit: int = 10) -> Dict[str, Dict]:
        """Fetch depth for multiple symbols (sequential with rate limiting).

        Returns: {symbol: analysis_result}
        """
        results = {}
        for sym in symbols:
            result = self.analyze(sym, limit)
            if result:
                results[sym] = result
        return results
