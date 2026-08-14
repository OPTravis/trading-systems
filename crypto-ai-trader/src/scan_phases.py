"""
Scan phases — market scanning, fear accumulation, QFL, and hash ribbon detection.
Extracted from scan_orchestrator for maintainability.
"""

import logging
import os
from typing import Dict

from src.bear_analyst import BearAnalyst
from src.binance_client import BinanceClient  # noqa: F401 — needed for test mocking
from src.market_scanner import MarketScanner
from src.notifier import FeishuNotifier, _append_notification
from src.paper_trader import get_trading_client, is_paper_mode
from src.pending_confirmation import clear_pending, save_pending  # noqa: F401
from src.portfolio import PortfolioManager
from src.position_optimizer import PositionOptimizer
from src.sentiment import SentimentAnalyzer
from src.trade_executor import count_active_positions, get_position_tier

logger = logging.getLogger(__name__)





def _sync_from_binance(portfolio, client):
    """Sync positions and cash from Binance API to local state.

    Delegates to the canonical PortfolioManager.sync_from_binance() method
    which has proper rollback, audit logging, and phantom-trade protection.
    """
    portfolio.sync_from_binance(client)



# ===================================================================
# Step functions for cmd_cron_scan pipeline
# ===================================================================


def _try_deep_value_btc(fng, client, scanner, portfolio, risk_mgr):
    """Deep Value BTC Pickup — contrarian micro-buy during extreme extended fear.

    Activates when ALL conditions met:
    - F&G <= 15 (deepest extreme fear)
    - Consecutive fear days >= 25 (persistent capitulation)
    - Available balance >= $50
    - 24h cooldown since last deep value buy

    Buys exactly $12 of BTCUSDT — small enough to be negligible risk,
    large enough to capture outsized returns when fear abates.
    Research basis: 14+ consecutive fear days → 90d avg +114.8%.

    Returns a scan context dict with synthetic opportunity, or None.
    """
    import time as _time
    from src.state_db import get_state_db

    # --- Condition 1: F&G threshold ---
    if fng > 15:
        logger.debug(f"DeepValueBTC: F&G={fng} > 15, skipping")
        return None

    # --- Condition 2: Consecutive fear days ---
    try:
        from src.sentiment import SentimentAnalyzer
        sa = SentimentAnalyzer()
        market = sa.get_market_sentiment()
        consec_fear = market.get("consecutive_fear_days", 0)
        fng_api = market.get("fear_greed", fng)  # re-fetch for freshness
    except Exception as e:
        logger.warning(f"DeepValueBTC: F&G re-fetch failed: {e}, using scan value")
        consec_fear = 0
        fng_api = fng

    # Use the fresher value
    if fng_api > 15:
        logger.info(f"DeepValueBTC: fresh F&G={fng_api} > 15, skipping")
        return None

    if consec_fear < 25:
        logger.info(
            f"DeepValueBTC: consecutive_fear={consec_fear} < 25 days, skipping"
        )
        return None

    # --- Condition 3: Balance ---
    try:
        balance = client.get_free_balance("USDT")
    except Exception as e:
        logger.warning(f"DeepValueBTC: balance check failed: {e}")
        return None

    if balance < 50:
        logger.info(f"DeepValueBTC: balance ${balance:.2f} < $50, skipping")
        return None

    # --- Condition 4: 24h cooldown ---
    db = get_state_db()
    last_buy_ts = db.kv_get("deep_value_btc_last", default=0)
    now = _time.time()
    if isinstance(last_buy_ts, (int, float)) and last_buy_ts > 0:
        elapsed = now - last_buy_ts
        if elapsed < 86400:  # 24h
            remaining = (86400 - elapsed) / 3600
            logger.info(
                f"DeepValueBTC: cooldown active, {remaining:.1f}h remaining"
            )
            return None

    # --- Condition 5: Daily cap (max 1 buy per day) ---
    today_key = _time.strftime("%Y%m%d", _time.gmtime(now + 8 * 3600))  # CST
    today_count = db.kv_get(f"deep_value_btc_{today_key}", default=0)
    if isinstance(today_count, (int, float)) and today_count >= 1:
        logger.info(f"DeepValueBTC: daily cap reached for {today_key}")
        return None

    # ===== All conditions met — execute =====
    ORDER_VALUE = 12.0  # Fixed $12 per buy
    SYMBOL = "BTCUSDT"

    try:
        price = client.get_ticker_price(SYMBOL)
    except Exception as e:
        logger.error(f"DeepValueBTC: price fetch failed: {e}")
        return None

    logger.info(
        "DeepValueBTC: TRIGGERED | F&G=%d | consec_fear=%dd | BTC=$%.2f | buy=$%.2f",
        fng_api, consec_fear, price, ORDER_VALUE,
    )
    print(
        f"🔥 DEEP_VALUE_BTC: F&G={fng_api}, fear={consec_fear}d, "
        f"BTC=${price:.2f}, buying ${ORDER_VALUE:.2f}"
    )

    # Record cooldown + daily count
    db.kv_set("deep_value_btc_last", now)
    db.kv_set(f"deep_value_btc_{today_key}", 1)
    db.audit_log(
        action="DEEP_VALUE_BTC_BUY",
        details={
            "fng": fng_api,
            "consec_fear": consec_fear,
            "symbol": SYMBOL,
            "price": price,
            "order_value": ORDER_VALUE,
        },
        source="deep_value_btc",
    )

    # Build synthetic opportunity (FeishuNotifier already imported at top)
    from src.market_researcher import MarketResearcher
    from src.strategy_adaptor import StrategyAdaptor

    adaptor = StrategyAdaptor()
    adapted = adaptor.adapt(
        fear_greed=fng_api,
        btc_trend="BEARISH",
        btc_price_change_24h=0,
    )
    adapted["global"]["score_threshold"] = 50
    adapted["global"]["cash_reserve_pct"] = 50
    adapted["global"]["max_position_pct"] = 5

    deep_opp = {
        "symbol": SYMBOL,
        "price": price,
        "score": 75,  # High conviction due to extreme conditions
        "volume_24h": 0,
        "change_24h": 0,
        "signals": [{"type": "BUY", "source": "deep_value_btc"}],
        "analysis": {"1h": {}, "deep_value": {"fng": fng_api, "consec_fear": consec_fear}},
        "fear_mode": True,
        "order_value": ORDER_VALUE,
    }

    return {
        "client": client,
        "scanner": scanner,
        "notifier": FeishuNotifier(),
        "risk_mgr": risk_mgr,
        "researcher": MarketResearcher(),
        "portfolio": portfolio,
        "opportunities": [deep_opp],
        "dynamic_threshold": 50,
        "adapted": adapted,
        "regime": "EXTREME_FEAR",
        "fng": fng_api,
        "fng_label": "Extreme Fear",
        "btc_trend": "BEARISH",
        "btc_change_24h": 0,
        "btc_score": 30,
        "acct": client.get_account(),
    }


def _try_fear_accumulation(all_opportunities, fng, client, scanner, portfolio, risk_mgr):
    """Fear Accumulation Fallback — buy small amounts of oversold coins during extreme fear.

    Only activates when:
    - F&G < 20 (extreme fear)
    - Normal scan produced 0 opportunities above threshold
    - Available balance > $50

    Selects top 2 oversold candidates by RSI (< 40) from the raw scan results.
    Uses relaxed scoring (55 threshold), small position size (5%), wide stop (15%).
    """
    try:
        from src.indicators import calculate_rsi
    except ImportError:
        # Fallback: inline RSI calculation
        def calculate_rsi(closes, period=14):
            if len(closes) < period + 1:
                return 50.0
            deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
            gains = [d if d > 0 else 0 for d in deltas]
            losses = [-d if d < 0 else 0 for d in deltas]
            avg_gain = sum(gains[:period]) / period
            avg_loss = sum(losses[:period]) / period
            for i in range(period, len(deltas)):
                avg_gain = (avg_gain * (period - 1) + gains[i]) / period
                avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            if avg_loss == 0:
                return 100.0
            rs = avg_gain / avg_loss
            return 100 - (100 / (1 + rs))

    # Check available balance
    try:
        balance = client.get_free_balance("USDT")
    except Exception as e:
        logger.warning("scan_phases._try_fear_accumulation: " + str(e))
        balance = 0
    if balance < 50:
        logger.info("Fear accumulation: balance too low ($%.2f), skipping", balance)
        return None

    # Target major coins only (high liquidity, less likely to go to zero)
    MAJOR_COINS = {"BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "DOT", "LINK"}

    # Get RSI for each candidate — check both 1h (short-term timing) and 1d (structural oversold)
    candidates = []
    for opp in all_opportunities[:15]:  # check top 15 by score
        sym = opp.get("symbol", "")
        if not sym:
            continue
        # Extract coin name (remove USDT suffix)
        coin = sym.replace("USDT", "").replace("BUSD", "")
        if coin not in MAJOR_COINS:
            continue
        try:
            rsi_1h = None
            rsi_1d = None

            # 1h RSI — short-term oversold timing
            klines_1h = client.get_klines(sym, interval="1h", limit=50)
            if klines_1h and len(klines_1h) >= 20:
                closes_1h = [float(k["close"]) for k in klines_1h]
                rsi_1h = calculate_rsi(closes_1h, 14)

            # Daily RSI — structural oversold (stronger signal for accumulation)
            klines_1d = client.get_klines(sym, interval="1d", limit=50)
            if klines_1d and len(klines_1d) >= 20:
                closes_1d = [float(k["close"]) for k in klines_1d]
                rsi_1d = calculate_rsi(closes_1d, 14)

            # Eligible if EITHER timeframe is oversold (< 40)
            # Use daily RSI as primary sort key (structural oversold is more meaningful)
            effective_rsi = min(filter(lambda x: x is not None, [rsi_1d, rsi_1h]), default=50)
            if effective_rsi < 40:
                candidates.append({
                    "symbol": sym,
                    "coin": coin,
                    "rsi": effective_rsi,
                    "rsi_1h": rsi_1h,
                    "rsi_1d": rsi_1d,
                    "score": opp.get("score", 0),
                    "price": opp.get("price", 0),
                })
        except Exception as e:
            logger.debug(f"Fear accumulation: failed to get RSI for {sym}: {e}")

    if not candidates:
        logger.info("Fear accumulation: no oversold major coins found (RSI < 40 on 1h or 1d), trying QFL")
        # ===== QFL Fallback — structural support break detection =====
        qfl_result = _try_qfl_fallback(client, fng, balance)
        if qfl_result:
            return qfl_result
        return None

    # Sort by RSI (most oversold first), pick top 2
    candidates.sort(key=lambda x: x["rsi"])
    picks = candidates[:2]

    logger.info(
        "Fear accumulation: F&G=%d, found %d oversold majors, picking %s (rsi_1d=%s, rsi_1h=%s)",
        fng, len(candidates), [p["symbol"] for p in picks],
        [f'{p.get("rsi_1d", 0):.1f}' for p in picks],
        [f'{p.get("rsi_1h", 0):.1f}' for p in picks],
    )
    print(f"FEAR_ACCUMULATION: F&G={fng}, picks={[p['symbol'] for p in picks]}")

    # Return the best pick as a synthetic scan result
    best = picks[0]
    symbol = best["symbol"]
    price = best["price"] or client.get_ticker_price(symbol)

    # Use small position size: 5% of available balance
    order_value = balance * 0.05
    if order_value < 10:
        order_value = 10  # minimum $10

    # Build a synthetic opportunity that looks like normal scan output
    # so it can flow through the rest of the pipeline
    fear_opp = {
        "symbol": symbol,
        "price": price,
        "score": 60,  # relaxed score for fear mode
        "volume_24h": 0,
        "change_24h": 0,
        "signals": [{"type": "BUY", "source": "fear_accumulation"}],
        "analysis": {"1h": {"rsi": best.get("rsi_1h")}, "1d": {"rsi": best.get("rsi_1d")}},
        "fear_mode": True,
        "order_value": order_value,
    }

    # Return a full context dict matching _step_scan_opportunities output
    # with the fear opportunity injected
    from src.market_researcher import MarketResearcher
    from src.strategy_adaptor import StrategyAdaptor
    adaptor = StrategyAdaptor()
    adapted = adaptor.adapt(
        fear_greed=fng,
        btc_trend="BEARISH",
        btc_price_change_24h=0,
    )
    # Override threshold and position sizing for fear accumulation
    adapted["global"]["score_threshold"] = 50
    adapted["global"]["cash_reserve_pct"] = 50  # keep 50% cash reserve
    adapted["global"]["max_position_pct"] = 5   # max 5% per position
    # Reduce DCA size multiplier for fear mode (conservative entry)
    if "dca" in adapted.get("strategies", {}):
        adapted["strategies"]["dca"]["size_multiplier"] = 0.5

    result = {
        "client": client,
        "scanner": scanner,
        "notifier": FeishuNotifier(),
        "risk_mgr": risk_mgr,
        "researcher": MarketResearcher(),
        "portfolio": portfolio,
        "opportunities": [fear_opp],
        "dynamic_threshold": 50,
        "adapted": adapted,
        "regime": "EXTREME_FEAR",
        "fng": fng,
        "fng_label": "Extreme Fear",
        "btc_trend": "BEARISH",
        "btc_change_24h": 0,
        "btc_score": 30,
        "acct": client.get_account(),
    }

    return result



def _try_qfl_fallback(client, fng, balance):
    """QFL (Quickfingers Luc) Fallback — structural support break detection.

    When RSI-based fear accumulation finds nothing, try QFL which detects
    panic selling that breaks below historical support with volume exhaustion.

    Only activates when F&G < 30 (panic environment).
    """
    from src.qfl_scanner import qfl_scan

    MAJOR_COINS = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
                   "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"}

    try:
        qfl_signals = qfl_scan(client, list(MAJOR_COINS), fng=fng, timeframe="4h")
    except Exception as e:
        logger.warning("QFL fallback scan failed: %s", e)
        return None

    if not qfl_signals:
        logger.info("QFL fallback: no signals found")
        return None

    # Pick the best QFL signal (highest R:R)
    best = max(qfl_signals, key=lambda s: s.get("rr_ratio", 0))
    symbol = best["symbol"]

    # Small position: 3% of balance (conservative for QFL)
    order_value = balance * 0.03
    if order_value < 10:
        order_value = 10

    price = best.get("entry", 0) or client.get_ticker_price(symbol)

    logger.info(
        "QFL fallback signal: %s support=%.4f crack_mag=%.1f%% R:R=%.1f",
        symbol, best["support_price"], best["crack_magnitude"] * 100, best["rr_ratio"],
    )

    from src.market_researcher import MarketResearcher
    from src.strategy_adaptor import StrategyAdaptor
    adaptor = StrategyAdaptor()
    adapted = adaptor.adapt(fear_greed=fng, btc_trend="BEARISH", btc_price_change_24h=0)
    adapted["global"]["score_threshold"] = 50
    adapted["global"]["cash_reserve_pct"] = 60
    adapted["global"]["max_position_pct"] = 3

    fear_opp = {
        "symbol": symbol,
        "price": price,
        "score": 65,
        "volume_24h": 0,
        "change_24h": 0,
        "signals": [{"type": "BUY", "source": "qfl_panic_bottom"}],
        "analysis": {
            "1h": {},
            "qfl": {
                "support_price": best["support_price"],
                "stop_loss": best["stop_loss"],
                "take_profit": best["take_profit"],
                "rr_ratio": best["rr_ratio"],
            },
        },
        "fear_mode": True,
        "order_value": order_value,
    }

    return {
        "client": client,
        "scanner": MarketScanner(client),
        "notifier": FeishuNotifier(),
        "risk_mgr": PortfolioManager(client),
        "researcher": MarketResearcher(),
        "portfolio": PortfolioManager(client),
        "opportunities": [fear_opp],
        "dynamic_threshold": 50,
        "adapted": adapted,
        "regime": "EXTREME_FEAR",
        "fng": fng,
        "fng_label": "Extreme Fear",
        "btc_trend": "BEARISH",
        "btc_change_24h": 0,
        "btc_score": 30,
        "acct": client.get_account(),
    }



def _try_hash_ribbon(client, portfolio, risk_mgr):
    """Hash Ribbon — miner capitulation recovery signal.

    Independent of Fear & Greed Index. Fires once when 30 DMA crosses
    above 60 DMA after a capitulation phase. Very rare (1-2x/year)
    but historically high conviction (64% profitable, >5000% avg return).

    Returns context dict for pipeline execution, or None if no signal.
    """
    from src.hash_ribbon import get_hash_ribbon_status

    try:
        status = get_hash_ribbon_status()
    except Exception as e:
        logger.warning("Hash Ribbon check failed: %s", e)
        return None

    if not status.get("signal_fired"):
        return None

    signal = status["signal"]
    logger.info(
        "Hash Ribbon BUY signal detected! ma30=%.1f EH/s, ma60=%.1f EH/s, gap=%.2f%%",
        signal["ma30_ehs"], signal["ma60_ehs"], signal["ma_gap_pct"],
    )

    # BTC only — hash ribbon is a BTC-specific signal
    symbol = "BTCUSDT"
    balance = client.get_free_balance("USDT")
    deploy_pct = signal.get("recommended_deploy_pct", 0.20)
    order_value = balance * deploy_pct

    if order_value < 10:
        logger.info("Hash Ribbon: balance too low ($%.2f), skipping", balance)
        return None

    price = client.get_ticker_price(symbol)

    from src.market_researcher import MarketResearcher
    from src.strategy_adaptor import StrategyAdaptor
    # Hash ribbon doesn't depend on fear/greed — use neutral adaptation
    adaptor = StrategyAdaptor()
    adapted = adaptor.adapt(fear_greed=50, btc_trend="NEUTRAL", btc_price_change_24h=0)
    adapted["global"]["score_threshold"] = 50  # high conviction — lower threshold
    adapted["global"]["cash_reserve_pct"] = 30  # deploy up to 20%, keep 30% reserve

    hash_opp = {
        "symbol": symbol,
        "price": price,
        "score": 70,  # high conviction
        "volume_24h": 0,
        "change_24h": 0,
        "signals": [{"type": "BUY", "source": "hash_ribbon_recovery"}],
        "analysis": {
            "1h": {},
            "hash_ribbon": {
                "ma30_ehs": signal["ma30_ehs"],
                "ma60_ehs": signal["ma60_ehs"],
                "ma_gap_pct": signal["ma_gap_pct"],
                "confidence": signal["confidence"],
                "holding_period_days": signal.get("holding_period_days", 180),
            },
        },
        "fear_mode": False,
        "order_value": order_value,
    }

    return {
        "client": client,
        "scanner": MarketScanner(client),
        "notifier": FeishuNotifier(),
        "risk_mgr": risk_mgr,
        "researcher": MarketResearcher(),
        "portfolio": portfolio,
        "opportunities": [hash_opp],
        "dynamic_threshold": 50,
        "adapted": adapted,
        "regime": "HASH_RIBBON_RECOVERY",
        "fng": 50,
        "fng_label": "Neutral (Hash Ribbon)",
        "btc_trend": "NEUTRAL",
        "btc_change_24h": 0,
        "btc_score": 50,
        "acct": client.get_account(),
    }



def _step_scan_opportunities():
    """Step 1: Market scan with sentiment, strategy adaptation, and filtering.

    Performs:
    - Binance portfolio sync
    - Market sentiment (Fear & Greed)
    - BTC trend & volatility analysis
    - Strategy adaptation (regime-based)
    - Market scan + filtering (held symbols, threshold)
    - Position optimization (smart switch)

    Returns:
        Context dict with all computed data, or None if no opportunities found.
    """
    logger.info("=== Phase 3: Scan → Research → Adapt → Execute ===")

    client = get_trading_client()
    scanner = MarketScanner(client)
    notifier = FeishuNotifier()
    sentiment = SentimentAnalyzer()

    from src.market_researcher import MarketResearcher
    from src.risk_manager import get_risk_manager
    from src.strategy_adaptor import StrategyAdaptor

    # ===== Step 0: Sync with Binance (source of truth) =====
    portfolio = PortfolioManager()
    if not is_paper_mode():
        _sync_from_binance(portfolio, client)
    else:
        logger.info("Paper mode: skipping Binance portfolio sync")

    risk_mgr = get_risk_manager(binance_client=client)
    researcher = MarketResearcher()
    adaptor = StrategyAdaptor()

    # ===== Step 1: Market Sentiment =====
    try:
        market_sent = sentiment.get_market_sentiment()
        fng = market_sent["fear_greed"]
        fng_label = market_sent["fng_classification"]
        logger.info(f"Fear & Greed: {fng} ({fng_label})")
    except Exception as e:
        logger.warning(f"Sentiment check failed: {e}")
        fng = 50
        fng_label = "Unknown"

    # ===== Step 2: Hash Ribbon Check (independent of F&G) =====
    # Hash Ribbon is a long-term macro signal that fires 1-2x/year.
    # Check early — if it fires, it overrides normal scan flow.
    hash_ribbon_result = _try_hash_ribbon(client, portfolio, risk_mgr)
    if hash_ribbon_result:
        logger.info("Hash Ribbon signal active — overriding normal scan")
        return hash_ribbon_result

    # ===== Step 3: BTC Trend & Volatility =====
    btc_trend = "NEUTRAL"
    btc_change_24h = 0.0
    btc_adx = 0.0
    btc_score = 50.0
    btc_factors = {}
    try:
        trend_data = risk_mgr.trend_filter.check_trend(client)
        btc_trend = trend_data.get("trend", "NEUTRAL")
        btc_adx = trend_data.get("adx", 0)
        btc_score = trend_data.get("score", 50)
        btc_factors = trend_data.get("factors", {})
        # Get BTC 24h change
        btc_stats = client.get_24hr_stats("BTCUSDT")
        btc_change_24h = float(btc_stats.get("price_change_pct", 0))
        logger.info(
            f"BTC: trend={btc_trend} score={btc_score:.1f} ADX={btc_adx} 24h={btc_change_24h:+.2f}%"
        )
    except Exception as e:
        logger.warning(f"BTC trend check failed: {e}")

    # ===== Step 3: Strategy Adaptation =====
    # BTC funding rate from futures API removed — this system only does SPOT.
    # fapi.binance.com is unreachable from domestic cloud without proxy and
    # is unnecessary for spot-only trading.
    btc_funding_rate = 0.0

    adapted = adaptor.adapt(
        fear_greed=fng,
        btc_trend=btc_trend,
        btc_price_change_24h=btc_change_24h,
        btc_adx=btc_adx,
        funding_rate=btc_funding_rate,
        btc_score=btc_score,
    )
    regime = adapted["regime"]
    global_cfg = adapted["global"]
    dynamic_threshold = global_cfg["score_threshold"]

    # Output strategy adaptation status
    print(
        f"STRATEGY_ADAPT: regime={regime} F&G={fng} BTC={btc_trend}({btc_score:.0f}) threshold={dynamic_threshold} funding={btc_funding_rate:+.4f}% signal={global_cfg.get('funding_signal','N/A')}"
    )
    for sname, scfg in adapted["strategies"].items():
        status = "ON" if scfg["enabled"] else "OFF"
        if scfg["enabled"]:
            print(
                f"  {sname}: {status} size={scfg['size_multiplier']*100:.0f}% SL={scfg['sl_pct']}% hold={scfg['max_hold_hours']}h"
            )
        else:
            print(f"  {sname}: {status} ({scfg['reason']})")
    # DCA regime params
    dca_p = adapted.get("dca_params", {})
    print(f"  DCA params: interval={dca_p.get('interval_hours')}h dip={dca_p.get('dip_threshold_pct')}% rounds={dca_p.get('max_dca_rounds')}")
    # BTC factor breakdown
    if btc_factors:
        print(
            f"  BTC Factors: EMA={btc_factors.get('ema_cross',0):.0f} RSI={btc_factors.get('rsi',0):.0f} MACD={btc_factors.get('macd',0):.0f} Struct={btc_factors.get('price_structure',0):.0f} Vol={btc_factors.get('volume',0):.0f}"
        )

    # ===== Step 3b: Six-Dimension Resonance =====
    dim_result = None
    try:
        from src.dimension_scorer import DimensionScorer

        dim_scorer = DimensionScorer(binance_client=client)
        dim_result = dim_scorer.score_all()
        print(dim_scorer.format_report(dim_result))

        # ===== Data Health Check =====
        unhealthy = []
        dims = dim_result.get("dimensions", {})
        for name, d in dims.items():
            signals = d.get("signals", [])
            sig_str = " ".join(str(s) for s in signals)
            if "NO_DATA" in sig_str or "no_client" in sig_str or len(signals) == 0:
                unhealthy.append(f"{name}({d.get('weight',0):.0%}): {signals}")
        if unhealthy:
            msg = "Data health WARN: " + " | ".join(unhealthy)
            logger.warning(msg)
            print(f"\n⚠️  {msg}")
        else:
            print("\n✅ Data health: all 6 dimensions reporting data")
    except Exception as e:
        logger.warning(f"Dimension scoring failed: {e}")

    # Use dimension scorer resonance to adjust score threshold
    dim_resonance = dim_result.get("resonance", "NEUTRAL") if dim_result else "NEUTRAL"
    if dim_resonance in ("STRONG_BULL", "BULL"):
        dynamic_threshold -= 5  # Lower bar when multiple dimensions align bullishly
        logger.info(
            f"Dimension resonance={dim_resonance}, lowering threshold by 5 to {dynamic_threshold}"
        )
    elif dim_resonance in ("STRONG_BEAR", "BEAR"):
        dynamic_threshold += 5  # Raise bar in bearish resonance
        logger.info(
            f"Dimension resonance={dim_resonance}, raising threshold by 5 to {dynamic_threshold}"
        )
    # Clamp threshold to reasonable range
    dynamic_threshold = max(40, min(95, dynamic_threshold))

    # ===== Step 3c: Surge Detection (Pre-Pump Characteristics) =====
    surge_result = None
    try:
        from src.surge_detector import SurgeDetector

        # Get previous F&G for delta detection
        fng_prev = None
        try:
            import sqlite3
            conn = sqlite3.connect("data/cache.db")
            rows = conn.execute(
                "SELECT value FROM fng_history ORDER BY rowid DESC LIMIT 2"
            ).fetchall()
            conn.close()
            if len(rows) >= 2:
                fng_prev = int(rows[1][0])
        except Exception:
            pass

        # Get BTC RSI
        btc_rsi_val = None
        try:
            klines = client.get_klines("BTCUSDT", "1d", limit=20)
            if klines and len(klines) >= 15:
                import pandas as pd
                from ta.momentum import RSIIndicator
                closes = [float(k[4]) for k in klines]
                rsi_indicator = RSIIndicator(pd.Series(closes), window=14)
                btc_rsi_val = float(rsi_indicator.rsi().iloc[-1])
        except Exception:
            pass

        surge_detector = SurgeDetector(binance_client=client)
        surge_result = surge_detector.detect(
            dim_result=dim_result,
            fng=fng,
            fng_prev=fng_prev,
            btc_rsi=btc_rsi_val,
        )

        if surge_result["alert_level"] != "SILENCE":
            print(f"\n{'='*50}")
            print(surge_result["summary"])
            print(f"{'='*50}")

        # ===== Surge-Adjusted Threshold =====
        # Lower the entry bar as surge signals strengthen:
        #   CONFIRMED → -12 (full conviction entries)
        #   IMMINENT  → -8  (reversal starting, good entries)
        #   ACCUMULATE→ -4  (smart money in, cautious entries)
        #   WATCH     →  0  (bottoming, don't lower bar)
        #   SILENCE   → +5  (no signals, be extra cautious)
        surge_adj = {
            "CONFIRMED": -12,
            "IMMINENT": -8,
            "ACCUMULATE": -4,
            "WATCH": 0,
            "SILENCE": 2,
        }.get(surge_result["alert_level"], 0)
        dynamic_threshold += surge_adj
        dynamic_threshold = max(40, min(95, dynamic_threshold))
        if surge_adj != 0:
            logger.info(
                f"Surge-adjusted threshold: {surge_result['alert_level']} → "
                f"{'+' if surge_adj > 0 else ''}{surge_adj} → threshold={dynamic_threshold}"
            )

        if surge_result["should_alert"]:
            from src.notifier import _append_notification
            _append_notification(
                "surge_alert",
                "",
                f"🚨 暴漲預警\n\n{surge_result['summary']}\n\n"
                f"MVRV={surge_result.get('mvrv', 'N/A')} "
                f"SOPR={surge_result.get('sopr', 'N/A')} "
                f"NUPL={surge_result.get('nupl', 'N/A')}",
            )
            logger.info(
                f"Surge alert: {surge_result['alert_level']} "
                f"(P1={surge_result['phase1_count']} "
                f"P2={surge_result['phase2_count']} "
                f"P3={surge_result['phase3_count']})"
            )
    except Exception as e:
        logger.warning(f"Surge detection failed: {e}")

    # ===== Step 4: Market Scan =====
    scanner.get_top_movers(limit=5)
    opportunities = scanner.scan_all()

    # held_symbols filter REMOVED — allow re-evaluation of held positions for DCA/加倉
    # Previously skipped BTC/ETH/SOL because they were already in portfolio
    acct = client.get_account()

    # Save threshold before time-decay for regime guard coordination
    _pre_decay_threshold = dynamic_threshold

    # ===== Time-Decay Threshold Relaxation =====
    # After 7+ consecutive days with 0 trades, progressively lower threshold
    # This prevents the system from being permanently locked out in extended Fear markets
    import json as _json
    _tracker_path = os.path.join(os.path.dirname(__file__), "..", "data", "no_signal_tracker.json")
    from datetime import datetime
    _now_iso = datetime.now().isoformat()
    try:
        if os.path.exists(_tracker_path):
            with open(_tracker_path) as _f:
                _tracker = _json.load(_f)
        else:
            _tracker = {"last_trade_date": None, "last_scan_date": None, "consecutive_no_signal_days": 0}

        _today = datetime.now().strftime("%Y-%m-%d")
        _last_scan = _tracker.get("last_scan_date")
        if _last_scan and _last_scan < _today:
            _last_dt = datetime.strptime(_last_scan, "%Y-%m-%d")
            _today_dt = datetime.strptime(_today, "%Y-%m-%d")
            _gap = (_today_dt - _last_dt).days
            _tracker["consecutive_no_signal_days"] = _tracker.get("consecutive_no_signal_days", 0) + _gap
        elif not _last_scan:
            _tracker["consecutive_no_signal_days"] = 0
        _tracker["last_scan_date"] = _today

        _no_signal_days = _tracker.get("consecutive_no_signal_days", 0)
        _time_decay_adj = 0
        if _no_signal_days >= 21:
            _time_decay_adj = -10
        elif _no_signal_days >= 14:
            _time_decay_adj = -7
        elif _no_signal_days >= 7:
            _time_decay_adj = -3

        if _time_decay_adj < 0:
            _orig_threshold = dynamic_threshold
            dynamic_threshold = max(65, dynamic_threshold + _time_decay_adj)
            print(
                f"TIME_DECAY: {_no_signal_days}d no signal → "
                f"threshold {dynamic_threshold} ({_time_decay_adj:+d}, floor 65)"
            )
            logger.info(
                f"TIME_DECAY: {_no_signal_days}d no signal → "
                f"threshold {dynamic_threshold} ({_time_decay_adj:+d}, floor 65)"
            )

    except Exception as e:
        logger.warning(f"Time-decay tracker error: {e}")
        _tracker = {}
        _no_signal_days = 0

    # Apply adapted threshold
    all_opportunities = opportunities  # save raw before filter
    opportunities = [o for o in opportunities if o["score"] >= dynamic_threshold]
    logger.info(
        f"{len(opportunities)} opportunities after adapted threshold ({dynamic_threshold})"
    )

    if not opportunities:
        # ===== Surge-Aware Entry Gating =====
        # Block fear-driven entries during Phase 1 (capitulation bottom) only.
        # Only allow entries when surge detector signals ACCUMULATE (Phase 2+)
        # or higher — meaning smart money has confirmed the bottom.
        # Exception: Deep Value BTC is always allowed (micro $12, long-term).
        surge_alert = surge_result.get("alert_level", "SILENCE") if surge_result else "SILENCE"

        # ===== Deep Value BTC Pickup (highest priority) =====
        # When F&G < 15 AND consecutive fear >= 25 days, buy small BTC.
        # Research: 14+ days extreme fear → 90d avg +114.8% return.
        # This is the deepest contrarian play — fixed $12, BTC only, 24h cooldown.
        # Always allowed regardless of surge level (negligible risk).
        deep_btc_result = _try_deep_value_btc(fng, client, scanner, portfolio, risk_mgr)
        if deep_btc_result:
            return deep_btc_result

        # ===== Fear Accumulation Fallback =====
        # SURGE GATE: Only allow fear accumulation when surge detector
        # confirms smart money is accumulating (ACCUMULATE+) or reversal
        # signals are appearing (IMMINENT/CONFIRMED).
        # During WATCH/SILENCE, the bottom isn't confirmed — buying here = catching knives.
        if fng < 20 and all_opportunities:
            if surge_alert in ("SILENCE", "WATCH"):
                logger.info(
                    f"Surge gate: BLOCKING fear_accumulation "
                    f"(alert={surge_alert}, bottom not confirmed)"
                )
            else:
                fear_result = _try_fear_accumulation(
                    all_opportunities, fng, client, scanner, portfolio, risk_mgr
                )
                if fear_result:
                    return fear_result

        print("NO_OPPORTUNITIES")
        clear_pending()
        return None

    # ===== Step 4b: Position Optimization (Smart Switch) =====
    optimizer = PositionOptimizer(
        binance_client=client, portfolio=portfolio, market_scanner=scanner
    )
    # Pass pre-computed opportunities + BTC change for smart activation (avoids redundant scan_all)
    # Filter opportunities to top 20 for optimizer input
    top_opps = sorted(opportunities, key=lambda x: x.get("score", 0), reverse=True)[:20]
    switch_decisions = optimizer.analyze_and_switch(
        dry_run=False,
        opportunities=top_opps,
        btc_change_24h=btc_change_24h,
    )
    if switch_decisions:
        for decision in switch_decisions:
            status = "EXECUTED" if decision.get("executed") else "FAILED"
            print(
                f"SWITCH_{status}: {decision['from_symbol']} -> {decision['to_symbol']} "
                f"(reason: {decision['reason']}, expected_gain: {decision['expected_gain_pct']:.2f}%)"
            )
    else:
        print("SWITCH: No switch opportunities found")

    # Store pre-time-decay threshold for regime guard coordination
    ctx_pre_decay = _pre_decay_threshold

    # ===== Write back no_signal_tracker =====
    try:
        if _tracker and "last_scan_date" in _tracker:
            _final_opps = len(opportunities) if opportunities else 0
            # If opportunities found and might lead to trades, don't increment counter
            # The counter tracks "no actionable signal" days
            with open(_tracker_path, "w") as _fw:
                _json.dump(_tracker, _fw, indent=2)
    except Exception:
        pass

    return {
        "client": client,
        "scanner": scanner,
        "notifier": notifier,
        "risk_mgr": risk_mgr,
        "researcher": researcher,
        "portfolio": portfolio,
        "opportunities": opportunities,
        "dynamic_threshold": dynamic_threshold,
        "pre_time_decay_threshold": ctx_pre_decay,
        "adapted": adapted,
        "regime": regime,
        "fng": fng,
        "fng_label": fng_label,
        "btc_trend": btc_trend,
        "btc_change_24h": btc_change_24h,
        "btc_score": btc_score,
        "acct": acct,
        "surge_result": surge_result,
    }
