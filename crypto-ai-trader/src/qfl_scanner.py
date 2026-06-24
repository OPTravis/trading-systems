"""
QFL (Quickfingers Luc) Panic Bottom-Fishing Scanner.

Detects structural support breaks during extreme fear and identifies
volume exhaustion for high-probability reversal entries.

Core logic:
1. Find historical support levels from OHLCV pivots
2. Detect "crack" — decisive break below support (3-5% within 4 bars)
3. Verify volume exhaustion (current sell volume < 30% of peak)
4. Confirm panic environment (F&G < 30)
"""

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def find_support_levels(
    klines: List[Dict],
    min_touches: int = 2,
    tolerance_pct: float = 0.02,
) -> List[Dict]:
    """Find support levels from OHLCV data using pivot detection.

    A support level is a price zone where price has bounced at least
    `min_touches` times (low within `tolerance_pct` of each other).

    Returns list of dicts: {price, touch_count, last_touch_idx}
    """
    if not klines or len(klines) < 20:
        return []

    lows = [float(k["low"]) for k in klines]
    n = len(lows)

    # Find local minima (pivots)
    pivots = []
    for i in range(2, n - 2):
        if lows[i] <= lows[i - 1] and lows[i] <= lows[i - 2] and \
           lows[i] <= lows[i + 1] and lows[i] <= lows[i + 2]:
            pivots.append({"price": lows[i], "idx": i})

    if not pivots:
        return []

    # Cluster nearby pivots into support zones
    supports = []
    used = set()
    for i, p in enumerate(pivots):
        if i in used:
            continue
        zone_prices = [p["price"]]
        zone_indices = [p["idx"]]
        used.add(i)
        for j, q in enumerate(pivots):
            if j in used:
                continue
            if abs(p["price"] - q["price"]) / p["price"] < tolerance_pct:
                zone_prices.append(q["price"])
                zone_indices.append(q["idx"])
                used.add(j)

        if len(zone_prices) >= min_touches:
            supports.append({
                "price": sum(zone_prices) / len(zone_prices),
                "touch_count": len(zone_prices),
                "last_touch_idx": max(zone_indices),
                "strength": len(zone_prices),  # more touches = stronger
            })

    # Sort by strength (most tested first)
    supports.sort(key=lambda x: x["strength"], reverse=True)
    return supports


def detect_crack(
    klines: List[Dict],
    support: Dict,
    min_magnitude_pct: float = 0.03,
    max_bars: int = 4,
) -> Optional[Dict]:
    """Detect if support has been decisively broken.

    A "crack" requires:
    - Price closes below support by >= min_magnitude_pct
    - The break happened within max_bars recent candles
    - Break candle has above-average volume (1.5x+)

    Returns crack info dict or None.
    """
    if not klines or len(klines) < 10:
        return None

    support_price = support["price"]
    recent = klines[-max_bars:]

    # Calculate average volume from earlier candles
    vol_candles = klines[-20:-max_bars] if len(klines) > 20 + max_bars else klines[:-max_bars]
    if not vol_candles:
        return None
    avg_vol = sum(float(k.get("volume", 0)) for k in vol_candles) / len(vol_candles)
    if avg_vol <= 0:
        return None

    for candle in recent:
        close = float(candle.get("close", 0))
        low = float(candle.get("low", 0))
        volume = float(candle.get("volume", 0))

        # Check decisive break: close below support
        if close >= support_price:
            continue

        magnitude = (support_price - close) / support_price
        if magnitude < min_magnitude_pct:
            continue

        # Check volume confirmation (above average)
        if volume < avg_vol * 1.2:
            continue

        return {
            "support_price": support_price,
            "crack_close": close,
            "crack_low": low,
            "magnitude": magnitude,
            "volume_ratio": volume / avg_vol,
            "crack_idx": len(klines) - 1,
        }

    return None


def check_volume_exhaustion(
    klines: List[Dict],
    crack_info: Dict,
    exhaustion_ratio: float = 0.30,
) -> bool:
    """Check if selling volume has exhausted after the crack.

    Volume exhaustion means: the most recent candle's volume is less
    than `exhaustion_ratio` of the crack candle's volume.

    This indicates sellers are running out of steam — potential reversal.
    """
    if not klines or not crack_info:
        return False

    crack_idx = crack_info.get("crack_idx", 0)
    if crack_idx < 0 or crack_idx >= len(klines):
        return False

    crack_volume = float(klines[crack_idx].get("volume", 0))
    if crack_volume <= 0:
        return False

    # Check if volume is declining after crack
    # Look at candles after the crack
    post_crack = klines[crack_idx + 1:]
    if not post_crack:
        return False  # No confirmation yet

    # Most recent volume vs crack volume
    latest_vol = float(post_crack[-1].get("volume", 0))
    return latest_vol < crack_volume * exhaustion_ratio


def calculate_qfl_targets(
    crack_info: Dict,
    atr_value: float,
) -> Tuple[float, float, float]:
    """Calculate QFL entry, stop loss, and take profit.

    Returns: (entry_price, stop_loss, take_profit)
    - Entry: crack low + 0.5% (slightly above the capitulation low)
    - Stop loss: crack low - 1x ATR
    - Take profit: original support level (now becomes resistance)
    """
    crack_low = crack_info["crack_low"]
    support_price = crack_info["support_price"]

    entry = crack_low * 1.005  # 0.5% above crack low
    stop_loss = crack_low - atr_value
    take_profit = support_price  # support-turned-resistance

    return entry, stop_loss, take_profit


def qfl_scan(
    client,
    symbols: List[str],
    fng: int,
    timeframe: str = "4h",
    lookback: int = 100,
) -> List[Dict]:
    """Main QFL scan entry point.

    Scans multiple symbols for QFL signals during panic conditions.

    Args:
        client: ExchangeClient instance
        symbols: List of trading symbols to scan
        fng: Current Fear & Greed Index
        timeframe: Kline timeframe (default 4h for QFL)
        lookback: Number of candles to fetch

    Returns:
        List of QFL signal dicts with entry/stop/target info.
    """
    from src.indicators import Indicators

    # QFL only works in fear/panic
    if fng > 30:
        logger.debug("QFL: F&G=%d > 30, skipping (not in panic)", fng)
        return []

    signals = []

    for symbol in symbols:
        try:
            klines = client.get_klines(symbol, interval=timeframe, limit=lookback)
            if not klines or len(klines) < 30:
                continue

            # Step 1: Find support levels
            supports = find_support_levels(klines, min_touches=2)
            if not supports:
                continue

            # Step 2: Check each support for crack
            for support in supports[:5]:  # check top 5 strongest supports
                crack = detect_crack(klines, support)
                if not crack:
                    continue

                # Step 3: Check volume exhaustion
                if not check_volume_exhaustion(klines, crack):
                    continue

                # Step 4: Calculate targets
                closes = [float(k["close"]) for k in klines]
                try:
                    atr = Indicators.atr(klines, period=14)
                except Exception:
                    atr = closes[-1] * 0.02  # fallback: 2% of price

                entry, stop_loss, take_profit = calculate_qfl_targets(crack, atr)

                # Calculate risk/reward
                risk = abs(entry - stop_loss)
                reward = abs(take_profit - entry)
                rr_ratio = reward / risk if risk > 0 else 0

                if rr_ratio < 1.5:
                    continue  # Skip poor R:R setups

                signal = {
                    "symbol": symbol,
                    "source": "qfl",
                    "support_price": support["price"],
                    "support_touches": support["touch_count"],
                    "crack_magnitude": crack["magnitude"],
                    "volume_exhaustion": True,
                    "entry": entry,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "rr_ratio": rr_ratio,
                    "fng": fng,
                }
                signals.append(signal)
                logger.info(
                    "QFL signal: %s support=%.4f crack_mag=%.1f%% R:R=%.1f",
                    symbol, support["price"], crack["magnitude"] * 100, rr_ratio,
                )
                break  # one signal per symbol

        except Exception as e:
            logger.debug("QFL scan failed for %s: %s", symbol, e)

    return signals
