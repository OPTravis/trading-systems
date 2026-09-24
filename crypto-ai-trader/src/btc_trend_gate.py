"""WO-0924-z 1A-2: BTC trend gate, extracted from trade_executor.

Neutral home so kelly_sizer and trade_executor can both import it
at module level — kills the kelly_sizer <-> trade_executor cycle.
"""
import logging
import time

logger = logging.getLogger(__name__)

_btc_trend_cache = {
    "timestamp": 0, "multiplier": 1.0, "tier": "CONFIRMED_BULL",
    "btc_price": 0, "sma_100": 0, "sma_200": 0, "sma_200_slope_pct": 0,
}
_BTC_TREND_CACHE_TTL = 3600  # 1 hour


# ── P0-B (2026-08-26): New-position halt flag ────────────────────────
# Set via env NEW_POSITIONS_HALTED (default "1"). While True, execute_auto_trade
# BLOCKS all regular new LIVE positions. The Kelly deadlock already zeros sizing,
# this is a double safety net in case statistics roll out of the 8/8 cap.


def _check_btc_trend() -> tuple:
    """Tiered BTC trend gate — returns position size multiplier.

    Returns:
        (multiplier: float, info: dict)
        multiplier: 0.0 (block), 0.3 (warmup), 0.5 (transition), 1.0 (full)
        info: btc_price, sma_100, sma_200, sma_200_slope_pct, deviation_pct, tier
    """
    global _btc_trend_cache

    now = time.time()
    if now - _btc_trend_cache["timestamp"] < _BTC_TREND_CACHE_TTL:
        return (
            _btc_trend_cache["multiplier"],
            {
                "btc_price": _btc_trend_cache["btc_price"],
                "sma_100": _btc_trend_cache["sma_100"],
                "sma_200": _btc_trend_cache["sma_200"],
                "sma_200_slope_pct": _btc_trend_cache["sma_200_slope_pct"],
                "tier": _btc_trend_cache["tier"],
                "cached": True,
            },
        )

    try:
        from src.binance_client import BinanceClient

        client = BinanceClient(testnet=False)
        klines = client.get_klines("BTCUSDT", "1d", limit=210)
        if len(klines) < 200:
            logger.warning(
                f"BTC trend gate: insufficient data ({len(klines)}/200), allowing trades"
            )
            return 1.0, {"error": "insufficient_data", "bars": len(klines)}

        closes = [k["close"] for k in klines]
        btc_price = closes[-1]
        sma_200 = sum(closes[-200:]) / 200
        sma_100 = sum(closes[-100:]) / 100

        # 200 SMA slope: 14-day change rate (%)
        sma_200_14d_ago = sum(closes[-214:-14]) / 200 if len(closes) >= 214 else sma_200
        sma_200_slope_pct = ((sma_200 / sma_200_14d_ago) - 1) * 100

        deviation_pct = ((btc_price / sma_200) - 1) * 100

        # ── Tier classification ──
        if btc_price < sma_100 and sma_200_slope_pct < -0.1:
            # DEEP_BEAR: below 100 SMA AND 200 SMA still declining
            multiplier = 0.0
            tier = "DEEP_BEAR"
        elif btc_price >= sma_200 and sma_200_slope_pct >= 0:
            # CONFIRMED_BULL: above 200 SMA with flat/rising slope
            multiplier = 1.0
            tier = "CONFIRMED_BULL"
        elif btc_price >= sma_100:
            # TRANSITION: between 100 and 200 SMA
            # Proximity check: if within 5% of 200 SMA, use 0.3 warmup
            if abs(deviation_pct) < 5.0:
                multiplier = 0.3
                tier = "PROXIMITY_WARMUP"
            else:
                multiplier = 0.5
                tier = "TRANSITION"
        else:
            # Below 100 SMA but 200 SMA slope >= 0 (rare)
            multiplier = 0.3
            tier = "PROXIMITY_WARMUP"

        _btc_trend_cache = {
            "timestamp": now,
            "multiplier": multiplier,
            "tier": tier,
            "btc_price": btc_price,
            "sma_100": sma_100,
            "sma_200": sma_200,
            "sma_200_slope_pct": sma_200_slope_pct,
        }

        logger.info(
            f"BTC trend gate: BTC=${btc_price:,.0f} | 100SMA=${sma_100:,.0f} "
            f"200SMA=${sma_200:,.0f} (slope {sma_200_slope_pct:+.2f}%/14d) | "
            f"{deviation_pct:+.1f}% from 200SMA → {tier} ({multiplier:.1f}x)"
        )
        return multiplier, {
            "btc_price": btc_price,
            "sma_100": sma_100,
            "sma_200": sma_200,
            "sma_200_slope_pct": round(sma_200_slope_pct, 4),
            "deviation_pct": round(deviation_pct, 2),
            "tier": tier,
            "cached": False,
        }
    except Exception as e:
        logger.warning(f"BTC trend gate check failed: {e}, allowing trades (fail open)")
        return 1.0, {"error": str(e)}
