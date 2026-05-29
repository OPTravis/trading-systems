"""
On-Chain Data Provider — whale detection + exchange flow analysis.

Uses free public APIs:
- Blockchain.info: BTC whale transactions
- Binance public API: large trade detection from recent trades
- DeFiLlama: TVL changes (already integrated via OnChainAgent)

Provides a 0-100 score for on-chain activity per symbol.
"""

import logging
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class OnChainDataProvider:
    """Fetch and analyze on-chain data for whale detection."""

    def __init__(self, binance_client=None):
        if binance_client is None:
            from src.binance_client import BinanceClient
            binance_client = BinanceClient(testnet=False)
        self._client = binance_client
        self._cache = {}
        self._cache_ttl = 300  # 5 min cache

    def get_whale_trades(self, symbol: str, min_usdt: float = 10000) -> List[Dict]:
        """Detect large trades from recent Binance trades.

        Uses GET /api/v3/trades (public, no key needed).
        Returns trades > min_usdt value.
        """
        try:
            trades = self._client.get_trades(symbol=symbol, limit=1000)
            whale_trades = []
            for t in trades:
                price = float(t.get("price", 0))
                qty = float(t.get("qty", 0))
                value = price * qty
                if value >= min_usdt:
                    whale_trades.append({
                        "price": price,
                        "qty": qty,
                        "value_usdt": round(value, 2),
                        "is_buyer_maker": t.get("isBuyerMaker", False),
                        "time": t.get("time", 0),
                    })
            return whale_trades
        except Exception as e:
            logger.debug(f"Whale trade detection failed for {symbol}: {e}")
            return []

    def analyze_symbol(self, symbol: str) -> Optional[Dict]:
        """Analyze on-chain activity for a symbol.

        Returns:
            {
                "score": 0-100 (higher = more bullish on-chain activity),
                "whale_buys": int (number of large buy trades),
                "whale_sells": int (number of large sell trades),
                "whale_buy_volume": float (total USDT value of whale buys),
                "whale_sell_volume": float (total USDT value of whale sells),
                "net_flow": float (buy_volume - sell_volume, positive = bullish),
            }
        """
        cache_key = f"onchain_{symbol}"
        now = time.time()
        if cache_key in self._cache:
            cached_time, cached_data = self._cache[cache_key]
            if now - cached_time < self._cache_ttl:
                return cached_data

        whale_trades = self.get_whale_trades(symbol, min_usdt=5000)

        if not whale_trades:
            return None

        # Separate buys and sells
        # isBuyerMaker=True means seller is taker (market sell)
        # isBuyerMaker=False means buyer is taker (market buy)
        buys = [t for t in whale_trades if not t["is_buyer_maker"]]
        sells = [t for t in whale_trades if t["is_buyer_maker"]]

        buy_volume = sum(t["value_usdt"] for t in buys)
        sell_volume = sum(t["value_usdt"] for t in sells)
        net_flow = buy_volume - sell_volume

        # Compute score
        score = self._compute_score(
            whale_buys=len(buys),
            whale_sells=len(sells),
            buy_volume=buy_volume,
            sell_volume=sell_volume,
            net_flow=net_flow,
        )

        result = {
            "score": round(score, 1),
            "whale_buys": len(buys),
            "whale_sells": len(sells),
            "whale_buy_volume": round(buy_volume, 2),
            "whale_sell_volume": round(sell_volume, 2),
            "net_flow": round(net_flow, 2),
        }

        self._cache[cache_key] = (now, result)
        return result

    def _compute_score(
        self,
        whale_buys: int,
        whale_sells: int,
        buy_volume: float,
        sell_volume: float,
        net_flow: float,
    ) -> float:
        """Compute 0-100 on-chain score.

        Components:
        - Net flow (50%): positive = bullish
        - Whale count ratio (30%): more buys than sells = bullish
        - Volume ratio (20%): more buy volume = bullish
        """
        # 1. Net flow score (50%)
        # $0 → 50, +$100K → 80, -$100K → 20
        total_vol = buy_volume + sell_volume
        if total_vol > 0:
            flow_ratio = net_flow / total_vol  # -1 to +1
            flow_score = 50 + flow_ratio * 30  # 20-80
        else:
            flow_score = 50

        # 2. Whale count ratio (30%)
        total_whales = whale_buys + whale_sells
        if total_whales > 0:
            count_ratio = whale_buys / total_whales
            count_score = count_ratio * 100
        else:
            count_score = 50

        # 3. Volume ratio (20%)
        if total_vol > 0:
            vol_ratio = buy_volume / total_vol
            vol_score = vol_ratio * 100
        else:
            vol_score = 50

        score = (
            0.50 * flow_score
            + 0.30 * count_score
            + 0.20 * vol_score
        )

        return max(0, min(100, score))
