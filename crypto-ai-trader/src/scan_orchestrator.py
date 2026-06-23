"""
Scan orchestration — market scanning, research, strategy adaptation, and execution pipeline.
Extracted from main.py for maintainability.
"""

import logging
import os
from typing import Dict

from src.bear_analyst import BearAnalyst
from src.binance_client import BinanceClient  # noqa: F401 — needed for test mocking
from src.market_scanner import MarketScanner
from src.notifier import FeishuNotifier, send_signal, _append_notification
from src.paper_trader import get_trading_client, is_paper_mode
from src.pending_confirmation import clear_pending, save_pending
from src.portfolio import PortfolioManager
from src.position_optimizer import PositionOptimizer
from src.sentiment import SentimentAnalyzer
from src.trade_executor import (
    count_active_positions,
    execute_auto_trade,
    get_position_tier,
)
from src.trade_journal import TradeJournal

logger = logging.getLogger(__name__)


def cmd_scan(send_notification: bool = False):
    """Scan market for opportunities"""
    logger.info("=== Market Scanner ===")

    client = get_trading_client()
    scanner = MarketScanner(client)

    # Get top movers
    movers = scanner.get_top_movers(limit=5)
    gainers = [m for m in movers if m["direction"] == "gainer"]
    losers = [m for m in movers if m["direction"] == "loser"]
    print("\n📈 Top Gainers:")
    for g in gainers:
        vol_str = (
            f"${g.get('quote_volume', 0)/1e6:.1f}M"
            if g.get("quote_volume", 0) > 0
            else "N/A"
        )
        print(f"  {g['symbol']}: +{g['change_pct']:.2f}% (Vol: {vol_str})")

    print("\n📉 Top Losers:")
    for l in losers:
        vol_str = (
            f"${l.get('quote_volume', 0)/1e6:.1f}M"
            if l.get("quote_volume", 0) > 0
            else "N/A"
        )
        print(f"  {l['symbol']}: {l['change_pct']:.2f}% (Vol: {vol_str})")

    # Scan for opportunities
    print("\n🔍 Scanning for opportunities...")
    opportunities = scanner.scan_all()

    print(f"\n📊 Found {len(opportunities)} opportunities:")
    for opp in opportunities[:10]:
        vol_str = f"${opp['volume_24h']/1e6:.1f}M" if opp["volume_24h"] > 0 else "N/A"
        print(f"\n  {opp['symbol']} (Score: {opp['score']:.0f}/100)")
        print(f"    24h Change: {opp['price_change_24h']:.2f}%")
        print(f"    Volume: {vol_str}")
        print(f"    Signals: {', '.join(opp['signals'][:3])}")

    # Send Feishu notification if requested
    if send_notification and opportunities:
        notifier = FeishuNotifier()
        gainers = [m for m in movers if m["direction"] == "gainer"]
        losers = [m for m in movers if m["direction"] == "loser"]
        notifier.send_market_scan(opportunities, gainers, losers)
        logger.info("Feishu notification sent")

        print("=" * 50)


def _sync_from_binance(portfolio, client):
    """Sync positions and cash from Binance API to local state.

    Delegates to the canonical PortfolioManager.sync_from_binance() method
    which has proper rollback, audit logging, and phantom-trade protection.
    """
    portfolio.sync_from_binance(client)


# ===================================================================
# Step functions for cmd_cron_scan pipeline
# ===================================================================


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

    # ===== Step 2: BTC Trend & Volatility =====
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

    # ===== Step 4: Market Scan =====
    scanner.get_top_movers(limit=5)
    opportunities = scanner.scan_all()

    # held_symbols filter REMOVED — allow re-evaluation of held positions for DCA/加倉
    # Previously skipped BTC/ETH/SOL because they were already in portfolio
    acct = client.get_account()

    # Apply adapted threshold
    opportunities = [o for o in opportunities if o["score"] >= dynamic_threshold]
    logger.info(
        f"{len(opportunities)} opportunities after adapted threshold ({dynamic_threshold})"
    )

    if not opportunities:
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

    return {
        "client": client,
        "scanner": scanner,
        "notifier": notifier,
        "risk_mgr": risk_mgr,
        "researcher": researcher,
        "portfolio": portfolio,
        "opportunities": opportunities,
        "dynamic_threshold": dynamic_threshold,
        "adapted": adapted,
        "regime": regime,
        "fng": fng,
        "fng_label": fng_label,
        "btc_trend": btc_trend,
        "btc_change_24h": btc_change_24h,
        "btc_score": btc_score,
        "acct": acct,
    }


def _step_research_top_n(ctx):
    """Step 2: Risk checks, deep research on top candidates, and bear analysis.

    Performs:
    - Pre-trade risk checks (ATR, position sizing, regime filters)
    - Parallel research on top 3 candidates (news, on-chain, catalysts)
    - Bear analysis (Devil's Advocate)
    - Strategy determination (StrategyRegistry weighted voting)

    Args:
        ctx: Context dict from _step_scan_opportunities().

    Returns:
        Updated context dict with best candidate data, or None if none qualifies.
    """
    client = ctx["client"]
    risk_mgr = ctx["risk_mgr"]
    researcher = ctx["researcher"]
    notifier = ctx["notifier"]
    opportunities = ctx["opportunities"]
    dynamic_threshold = ctx["dynamic_threshold"]
    adapted = ctx["adapted"]
    regime = ctx["regime"]
    fng = ctx["fng"]
    btc_trend = ctx["btc_trend"]
    acct = ctx["acct"]

    # ===== Step 5: Risk Checks =====
    acct_balances = acct.get("balances", [])
    risk_positions = []
    for b in acct_balances:
        asset = b["asset"]
        total = float(b["free"]) + float(b["locked"])
        if asset != "USDT" and total > 0 and asset != "NTRN":
            try:
                stats = client.get_24hr_stats(asset + "USDT")
                price_val = float(stats.get("last_price", 0))
                if total * price_val >= 1.0:
                    risk_positions.append(
                        {"symbol": asset + "USDT", "value_usdt": total * price_val}
                    )
            except Exception:
                logger.error(
                    "Risk position price check failed for %s", asset, exc_info=True
                )

    # Regime-based guard: FEAR/EXTREME_FEAR — raise threshold to near-unreachable
    # (historical 0/7 wins in FEAR, 0/6 wins when BTC non-bullish)
    # Instead of hard block, set threshold to 98 so only extraordinary setups pass
    if regime in ("EXTREME_FEAR",):
        dynamic_threshold = max(dynamic_threshold, 98)
        print(
            f"REGIME_GUARD: {regime} — threshold raised to {dynamic_threshold} (historical 0% win rate)"
        )
    elif regime == "FEAR" and btc_trend != "BULLISH":
        dynamic_threshold = max(dynamic_threshold, 95)
        print(
            f"REGIME_GUARD: {regime} + BTC {btc_trend} — threshold raised to {dynamic_threshold}"
        )

    filtered = []
    for opp in opportunities:
        sym = opp["symbol"]
        atr = opp.get("atr", 0)
        if atr == 0:
            try:
                klines = client.get_klines(sym, "1h", limit=15)
                closes = [float(k["close"]) for k in klines]  # index 4 = close price
                if len(closes) >= 2:
                    trs = [
                        abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))
                    ]
                    atr = sum(trs) / len(trs) if trs else 0
            except Exception:
                logger.error(
                    "ATR calculation failed for %s, defaulting to 0", sym, exc_info=True
                )
                atr = 0

        # Determine preliminary strategy for risk check
        prelim_signals = opp.get("signals", [])
        prelim_strategy = "trend"
        if any("RSI Oversold" in s for s in prelim_signals):
            prelim_strategy = "rsi"
        elif any("Grid" in s for s in prelim_signals):
            prelim_strategy = "grid"
        elif any("VWAP" in s for s in prelim_signals):
            prelim_strategy = "vwap"
        elif any("Bollinger" in s for s in prelim_signals):
            prelim_strategy = "bollinger"

        # In fear regime, non-fear-buy strategies default to DCA
        fear_buy_ok = {"dca", "rsi", "bollinger"}
        if (
            adapted["regime"] in ("EXTREME_FEAR", "FEAR")
            and prelim_strategy not in fear_buy_ok
        ):
            prelim_strategy = "dca"

        check = risk_mgr.pre_trade_check(
            symbol=sym,
            price=opp.get("price", 0),
            atr=atr,
            positions=risk_positions,
            score=opp.get("score"),
            strategy=prelim_strategy,
        )
        if check["allowed"]:
            opp["_size_multiplier"] = check["adjustments"].get("size_multiplier", 1.0)
            filtered.append(opp)
        else:
            logger.info(
                "RiskManager: %s blocked – %s", sym, "; ".join(check["reasons"])
            )
            # Output full opportunity data even when blocked, so AI can display all fields
            score_val = opp.get("score", "N/A")
            # Derive technical score from the opportunity's 1h analysis data
            analysis_1h = opp.get("analysis", {}).get("1h", {})
            tech_val = opp.get("technical_score")
            if tech_val is None:
                # Calculate from available indicator fields
                tech_score = 0.0
                rsi = analysis_1h.get("rsi", 50)
                if rsi < 30:
                    tech_score += 25
                elif rsi < 40:
                    tech_score += 18
                elif rsi < 50:
                    tech_score += 12
                macd_hist = analysis_1h.get("macd_histogram", 0)
                if macd_hist > 0:
                    tech_score += 25
                bb_lower = analysis_1h.get("bb_lower", 0)
                current_price = analysis_1h.get("current_price", 0)
                if current_price and bb_lower and current_price < bb_lower:
                    tech_score += 20
                vwap = analysis_1h.get("vwap", 0)
                if current_price and vwap and current_price > vwap:
                    tech_score += 15
                ma7 = analysis_1h.get("ma7", 0)
                ma25 = analysis_1h.get("ma25", 0)
                ma99 = analysis_1h.get("ma99", 0)
                if ma7 > ma25 > ma99:
                    tech_score += 15
                tech_val = round(tech_score, 1)
            trend_val = opp.get("trend_score", opp.get("trend_strength", "N/A"))
            vol_val = opp.get("volume_surge", False)
            funding_val = opp.get("funding_rate", "N/A")
            if funding_val is not None and funding_val != "N/A":
                # Format funding rate with appropriate precision
                if abs(funding_val) < 0.0001:
                    funding_val = f"{funding_val*100:.6f}%"
                elif abs(funding_val) < 0.001:
                    funding_val = f"{funding_val*100:.5f}%"
                elif abs(funding_val) < 0.01:
                    funding_val = f"{funding_val*100:.4f}%"
                else:
                    funding_val = f"{funding_val*100:.2f}%"
            signals_list = opp.get("signals", [])
            print(f"RISK_BLOCKED:{sym} – {'; '.join(check['reasons'])}")
            print(
                f"  評分={score_val} 技術={tech_val} 趨勢={trend_val} 成交量={vol_val} 資金費率={funding_val} 信號={signals_list}"
            )
    opportunities = filtered

    if not opportunities:
        print("NO_OPPORTUNITIES")
        clear_pending()
        return None

    # ===== Step 6: Deep Research on Top 3 Candidates (parallel) =====
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from concurrent.futures import TimeoutError as FuturesTimeout

    top_n = opportunities[:3]
    research_results = {}  # symbol -> research dict

    logger.info(
        f"Researching top {len(top_n)} candidates: {[o['symbol'] for o in top_n]}"
    )
    t_research = _time.time()

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(researcher.research, opp["symbol"], client): opp
            for opp in top_n
        }
        try:
            for fut in as_completed(
                futures, timeout=90
            ):  # 90s covers 3 coins × 60s RESEARCH_TIMEOUT with headroom
                opp = futures[fut]
                try:
                    research_results[opp["symbol"]] = (opp, fut.result())
                except Exception as e:
                    logger.warning(f"Research failed for {opp['symbol']}: {e}")
        except FuturesTimeout:
            logger.warning(
                f"Research timeout: {len(research_results)}/{len(top_n)} completed in 90s"
            )
            for fut in futures:
                fut.cancel()

    logger.info(
        f"Research completed in {_time.time()-t_research:.1f}s for {len(research_results)} coins"
    )

    # Output all research results
    for sym, (opp, res) in research_results.items():
        adj = res["score_adjustment"]
        conf = res["confidence"]
        final = int(max(0, min(100, opp["score"] + adj)))
        print(
            f"RESEARCH: {sym} adj={adj:+.1f} score={int(opp['score'])}→{final} confidence={conf}"
        )
        print(f"  Summary: {res['sentiment_summary']}")
        if res["news"]:
            for n in res["news"][:2]:
                print(f"  📰 {n['title'][:50]} ({n['sentiment']:+.1f})")
        if res["onchain"].get("whale_activity", "UNKNOWN") != "UNKNOWN":
            print(f"  🐋 Whale: {res['onchain']['whale_activity']}")

    # Select best candidate by adjusted score
    best_sym = None
    best_adj_score = -1.0
    for sym, (opp, res) in research_results.items():
        adj_score = max(0, min(100, opp["score"] + res["score_adjustment"]))
        if adj_score > best_adj_score:
            best_adj_score = adj_score
            best_sym = sym

    if not best_sym or best_adj_score < dynamic_threshold:
        reason = (
            "no research completed"
            if not best_sym
            else f"best={int(best_adj_score)} < {dynamic_threshold}"
        )
        print(f"SCORE_BELOW_THRESHOLD: {reason} after research")
        clear_pending()
        return None

    top, research = research_results[best_sym]
    symbol = best_sym
    price = top["price"]
    score = int(top["score"])
    signals = top.get("signals", [])
    research_adj = research["score_adjustment"]
    research_confidence = research["confidence"]
    research_summary = research["sentiment_summary"]
    adjusted_score = best_adj_score

    print(f"SELECTED: {symbol} (adjusted_score={int(adjusted_score)})")

    # Re-check threshold with adjusted score
    if adjusted_score < dynamic_threshold:
        print(
            f"SCORE_BELOW_THRESHOLD: {int(adjusted_score)} < {dynamic_threshold} after research adjustment"
        )
        journal = TradeJournal()
        journal.record_decision(
            symbol=symbol, decision="BLOCKED", score=adjusted_score, research=research
        )
        clear_pending()
        return None

    # ===== Step 6b: Bear Analysis (Devil's Advocate) =====
    bear = BearAnalyst()
    bear_result = bear.analyze(
        symbol,
        {
            "score": adjusted_score,
            "technical_score": top.get("technical_score", 0),
            "rsi": top.get("analysis", {}).get("1h", {}).get("rsi", 50),
            "funding_rate": top.get("funding_rate", 0),
            "market_sentiment": fng,
            "on_chain_score": top.get("onchain_score", 50),
            "volume_surge": top.get("volume_surge", False),
            "trend_strength": top.get("trend_strength", 50),
        },
        research,
    )

    if bear_result.veto:
        print(
            f"BEAR_VETO: {symbol} – bear_score={bear_result.bear_score:.0f} vs bull_score={adjusted_score:.0f}"
        )
        for r in bear_result.reasons or []:
            print(f"  🐻 {r}")
        journal = TradeJournal()
        journal.record_decision(
            symbol=symbol,
            decision="VETOED",
            score=adjusted_score,
            bear_result=bear_result,
            research=research,
        )
        clear_pending()
        return None

    if bear_result.bear_score >= 50:
        # Reduce score by half the bear_score as penalty
        penalty = (bear_result.bear_score - 50) * 0.3
        adjusted_score = max(adjusted_score - penalty, 0)
        print(f"🐻 Bear penalty: -{penalty:.1f} → adjusted_score={adjusted_score:.1f}")
        if adjusted_score < dynamic_threshold:
            print("SCORE_BELOW_THRESHOLD after bear penalty")
            journal = TradeJournal()
            journal.record_decision(
                symbol=symbol,
                decision="BLOCKED",
                score=adjusted_score,
                bear_result=bear_result,
                research=research,
            )
            clear_pending()
            return None

    # ===== Step 7: Determine Strategy (weighted voting via StrategyRegistry) =====
    # Run all enabled strategies, pick the one with highest weighted confidence
    strategy = "score_based"  # Default when no specific strategy matches
    try:
        from src.strategy_registry import StrategyRegistry

        dca_params = adapted.get("dca_params", {})
        registry = StrategyRegistry(dca_params=dca_params)

        # Get klines for the top coin (needed by strategy classes)
        try:
            klines_raw = client.get_klines(symbol, "1h", limit=100)
            klines_data = [
                {
                    "open": float(k["open"]),
                    "high": float(k["high"]),
                    "low": float(k["low"]),
                    "close": float(k["close"]),
                    "volume": float(k["volume"]),
                }
                for k in klines_raw
            ]
        except Exception:
            logger.error("Klines fetch failed for %s", symbol, exc_info=True)
            klines_data = []

        # Get enabled strategies from adaptor
        enabled = [
            s
            for s, cfg in adapted.get("strategies", {}).items()
            if cfg.get("enabled", True)
        ]

        if klines_data and enabled:
            best = registry.select_best(symbol, klines_data, enabled)
            if best:
                strategy_name, confidence, reason, meta = best
                strategy = strategy_name
                logger.info(
                    f"StrategyRegistry: selected {strategy} (confidence={confidence:.1f}, weight={meta.get('weight', 1.0):.2f})"
                )
            else:
                # No strategy emitted BUY/SELL — fall back to first enabled strategy.
                # The 12-factor scoring system already validated this opportunity;
                # strategy name only drives SL/TP/max_hold configs, not entry logic.
                strategy = enabled[0]
                logger.info(
                    f"StrategyRegistry: no explicit BUY signal, falling back to {strategy}"
                )
    except Exception as e:
        logger.warning(f"StrategyRegistry failed: {e}")
        # Block trade when registry fails — don't fall back to score_based (0% win rate)
        print(f"STRATEGY_REGISTRY_FAILED: {e}, blocking trade")
        journal = TradeJournal()
        journal.record_decision(
            symbol=symbol, decision="BLOCKED", score=adjusted_score, research=research
        )
        clear_pending()
        return None

    # Check if strategy is enabled by adaptor
    strategy_cfg = adapted["strategies"].get(strategy)
    if strategy_cfg and not strategy_cfg["enabled"]:
        # Fall back to an enabled strategy
        for fallback in ["dca", "rsi", "bollinger", "vwap", "trend", "grid"]:
            fb_cfg = adapted["strategies"].get(fallback)
            if fb_cfg and fb_cfg["enabled"]:
                strategy = fallback
                strategy_cfg = fb_cfg
                logger.info(
                    f"Strategy {fallback} adapted to {strategy} (original was disabled)"
                )
                break

    reason = " / ".join(signals[:3]) if signals else "Multiple signals"

    # Use adapted strategy config (overrides YAML)
    if strategy_cfg:
        stop_loss_pct = strategy_cfg["sl_pct"]
        tp_levels = strategy_cfg["tp_levels"]
        max_hold = strategy_cfg["max_hold_hours"]
        size_multiplier = strategy_cfg["size_multiplier"]
    else:
        # Fallback to notifier config
        cfg = notifier.get_strategy_config(strategy)
        stop_loss_pct = cfg.get(
            "stop_loss_pct", 4.0
        )  # FIX-11: Widened from 2.0→4.0% (was triggering 71.4% of the time)
        tp_levels = cfg.get("take_profit_levels", [])
        max_hold = cfg.get("max_hold_hours", 48)
        size_multiplier = 1.0

    stop_price = price * (1 - stop_loss_pct / 100)

    # Calculate tier with adjusted score
    _, tier_label = get_position_tier(adjusted_score)
    active_pos = count_active_positions(client)
    # P1-8: fail-closed
    if active_pos < 0:
        active_pos = 0  # treat as 0 for display but the execute step will block

    ctx.update(
        {
            "symbol": symbol,
            "price": price,
            "score": score,
            "signals": signals,
            "adjusted_score": adjusted_score,
            "research": research,
            "research_adj": research_adj,
            "research_confidence": research_confidence,
            "research_summary": research_summary,
            "bear_result": bear_result,
            "strategy": strategy,
            "strategy_cfg": strategy_cfg,
            "stop_loss_pct": stop_loss_pct,
            "tp_levels": tp_levels,
            "max_hold": max_hold,
            "stop_price": stop_price,
            "active_pos": active_pos,
            "tier_label": tier_label,
            "reason": reason,
            "top": top,
            "size_multiplier": size_multiplier,  # P0-3 fix: pass strategy-level multiplier to executor
        }
    )
    return ctx


def _step_journal_results(ctx, result=None, decision="BUY"):
    """Record trade decision and execution in the trade journal.

    Args:
        ctx: Context dict with trade data.
        result: Execution result dict (from execute_auto_trade) or None for non-auto.
        decision: Decision type ('BUY', 'BLOCKED', 'VETOED').
    """
    journal = TradeJournal()

    if result and decision == "BUY":
        journal.record_trade(
            symbol=ctx["symbol"],
            side="BUY",
            price=ctx["price"],
            qty=result["qty"],
            score=ctx["adjusted_score"],
            reasons=[ctx["reason"]],
            signals=ctx["signals"],
            strategy=ctx["strategy"],
        )
        journal.record_decision(
            symbol=ctx["symbol"],
            decision=decision,
            score=ctx["adjusted_score"],
            bear_result=ctx["bear_result"],
            research=ctx["research"],
        )
    else:
        journal.record_decision(
            symbol=ctx["symbol"],
            decision=decision,
            score=ctx["adjusted_score"],
            bear_result=ctx.get("bear_result"),
            research=ctx.get("research"),
        )


def _step_execute_trades(ctx):
    """Step 3: Execute trade or present opportunity for manual confirmation.

    Performs:
    - Formats opportunity display (TP levels, research summary)
    - Auto-executes if AUTO_EXECUTE=true
    - Records trade outcome for self-learning pipeline
    - Falls back to manual confirmation flow

    Args:
        ctx: Context dict from _step_research_top_n() with best candidate data.
    """
    # ===== Step 8: Execute or Present =====
    lines = [
        f"🎯 {ctx['symbol']} (Score: {ctx['score']}→{int(ctx['adjusted_score'])} | {ctx['tier_label']})",
        f"市場: {ctx['regime']} | F&G: {ctx['fng']} ({ctx['fng_label']}) | BTC: {ctx['btc_trend']}",
        f"活躍持倉: {ctx['active_pos']}/3",
        "",
        f"策略: {ctx['strategy'].upper()} (自適應)",
        f"當前價格: ${ctx['price']:.6f}",
        "",
        "📊 建議止盈:",
    ]
    for i, tp in enumerate(ctx["tp_levels"]):
        tp_price = ctx["price"] * (1 + tp["pct"] / 100)
        prefix = "├" if i < len(ctx["tp_levels"]) - 1 else "└"
        lines.append(
            f"{prefix} TP{i+1}: +{tp['pct']}% @ ${tp_price:.6f} (賣出 {tp['size_pct']}%)"
        )

    # Add research summary
    lines.extend(
        [
            "",
            f"🔍 研究摘要: {ctx['research_summary']}",
            f"   信心度: {ctx['research_confidence']} | 評分調整: {ctx['research_adj']:+.1f}",
        ]
    )

    auto_execute = os.environ.get("AUTO_EXECUTE", "false").lower() == "true"

    # Output opportunity data BEFORE auto-execution so AI can parse all fields
    # (moved outside execution block so data is always available even on failure)
    tech_score = ctx["top"].get("technical_score", "N/A")
    trend_score = ctx["top"].get("trend_score", ctx["top"].get("trend_strength", "N/A"))
    vol_surge = ctx["top"].get("volume_surge", False)
    funding = ctx["top"].get("funding_rate")
    # Format funding rate with appropriate precision
    if funding is not None:
        # For very small values, show more decimal places
        if abs(funding) < 0.0001:
            funding_str = f"{funding*100:.6f}%"
        elif abs(funding) < 0.001:
            funding_str = f"{funding*100:.5f}%"
        elif abs(funding) < 0.01:
            funding_str = f"{funding*100:.4f}%"
        else:
            funding_str = f"{funding*100:.2f}%"
    else:
        funding_str = "N/A"
    print(
        f"OPPORTUNITY:{ctx['symbol']} 評分={int(ctx['adjusted_score'])} 技術={tech_score} 趨勢={trend_score} 成交量={vol_surge} 資金費率={funding_str} 信號={ctx['signals']}"
    )

    if auto_execute:
        logger.info(
            f"AUTO_EXECUTE enabled - executing {ctx['symbol']} trade automatically"
        )
        result = execute_auto_trade(
            symbol=ctx["symbol"],
            price=ctx["price"],
            strategy=ctx["strategy"],
            stop_loss_pct=ctx["stop_loss_pct"],
            tp_levels=ctx["tp_levels"],
            stop_price=ctx["stop_price"],
            max_hold=ctx["max_hold"],
            signals=ctx["signals"],
            reason=ctx["reason"],
            score=int(ctx["adjusted_score"]),
            cash_reserve_pct=ctx["adapted"]["global"].get("cash_reserve_pct", 30),
            max_position_pct=ctx["adapted"]["global"].get("max_position_pct", 15),
            max_total_exposure_pct=ctx["adapted"]["global"].get(
                "max_total_exposure_pct", 70
            ),
            strategy_size_multiplier=ctx.get(
                "size_multiplier", 1.0
            ),  # P0-3: pass strategy-level size_multiplier
        )
        if result["success"]:
            # Record trade in journal
            _step_journal_results(ctx, result=result, decision="BUY")

            # Phase 0: Record trade entry for self-learning pipeline
            try:
                from src.trade_outcome_recorder import TradeOutcomeRecorder

                recorder = TradeOutcomeRecorder()
                # Extract factor scores from opportunity data (best available)
                tech_score_entry = ctx["top"].get("technical_score", 0)
                trend_score_entry = ctx["top"].get(
                    "trend_score", ctx["top"].get("trend_strength", 0)
                )
                ctx["top"].get("funding_rate", 0)
                # Get all factor scores from market_scanner
                fs = ctx["top"].get("factor_scores", {})
                # Build TP percentages
                tp_pcts = [tp.get("pct", 0) for tp in ctx["tp_levels"]]
                while len(tp_pcts) < 3:
                    tp_pcts.append(0)
                entry_rowid = recorder.record_entry(
                    symbol=ctx["symbol"],
                    entry_price=result["price"],
                    qty=result["qty"],
                    score=ctx["adjusted_score"],
                    strategy=ctx["strategy"],
                    f_technical=(
                        float(fs.get("technical", tech_score_entry))
                        if isinstance(
                            fs.get("technical", tech_score_entry), (int, float)
                        )
                        else 0
                    ),
                    f_trend=(
                        float(fs.get("trend", trend_score_entry))
                        if isinstance(fs.get("trend", trend_score_entry), (int, float))
                        else 0
                    ),
                    f_volume=float(fs.get("volume", 0)),
                    f_sentiment=float(fs.get("sentiment", 0)),
                    f_price_action=float(fs.get("price_action", 0)),
                    f_obv_divergence=float(fs.get("obv_divergence", 0)),
                    f_consolidation=float(fs.get("consolidation", 0)),
                    f_bb_squeeze=float(fs.get("bb_squeeze", 0)),
                    f_rsi_divergence=float(fs.get("rsi_divergence", 0)),
                    f_onchain=float(
                        fs.get("onchain", ctx["top"].get("onchain_score", 0))
                    ),
                    f_market_sentiment=float(
                        fs.get(
                            "market_sentiment",
                            ctx["top"].get("market_sentiment_score", 0),
                        )
                    ),
                    regime=ctx["adapted"].get("regime", ""),
                    fng_score=int(ctx["fng"]),
                    fng_label=ctx["fng_label"],
                    btc_trend=ctx["btc_trend"],
                    kelly_pct=result.get("invest_pct", 0),
                    kelly_win_rate=result.get("kelly", {}).get("win_rate", 0),
                    kelly_confidence=result.get("kelly", {}).get("confidence", ""),
                    stop_loss_pct=ctx["stop_loss_pct"],
                    tp1_pct=tp_pcts[0],
                    tp2_pct=tp_pcts[1],
                    tp3_pct=tp_pcts[2],
                    max_hold_hours=ctx["max_hold"],
                    research_adj=ctx["research_adj"],
                    bear_score=(
                        ctx["bear_result"].bear_score
                        if ctx["bear_result"]
                        and hasattr(ctx["bear_result"], "bear_score")
                        else 0
                    ),
                    bear_veto=(
                        ctx["bear_result"].veto
                        if ctx["bear_result"] and hasattr(ctx["bear_result"], "veto")
                        else False
                    ),
                )
                # Store entry_rowid on position for precise outcome matching
                if entry_rowid and ctx["symbol"] in ctx["portfolio"].positions:
                    ctx["portfolio"].positions[ctx["symbol"]][
                        "entry_rowid"
                    ] = entry_rowid
            except Exception as e:
                logger.warning(f"Trade outcome entry recording failed: {e}")

            print(
                f"✅ Auto-executed {ctx['symbol']}: BUY {result['qty']} @ ${ctx['price']:.6f} | Tier: {result['tier']} | Invest: {result['invest_pct']}% | F&G: {ctx['fng']} ({ctx['fng_label']}) | Research: {ctx['research_adj']:+.1f}"
            )
            # Write signal to pending.json for heartbeat notification
            send_signal(
                signal_type="BUY",
                symbol=ctx["symbol"],
                action="OPEN",
                price=result["price"],
                quantity=result["qty"],
                reason=ctx["reason"],
                strategy=ctx["strategy"],
            )
        else:
            # Self-heal: diagnose failure at point of error
            heal_info = ""
            try:
                from src.self_healer import diagnose_and_fix

                heal = diagnose_and_fix(
                    result["error"], {"symbol": ctx["symbol"], "price": ctx["price"]}
                )
                if heal["diagnosed"]:
                    status = "✅已修復" if heal["fixed"] else "🔧待修"
                    heal_info = (
                        f"\n  {status} {heal['diagnosis']}: {heal['fix_result']}"
                    )
            except Exception:
                logger.error(
                    "Self-healer diagnosis failed for %s", ctx["symbol"], exc_info=True
                )
            print(f"❌ Auto-execute failed: {result['error']}{heal_info}")
    else:
        lines.extend(
            [
                "",
                f"🛡 止損: -{ctx['stop_loss_pct']}% @ ${ctx['stop_price']:.6f}",
                f"⏱ 最大持倉: {ctx['max_hold']}小時",
                "",
                f"💡 信號: {ctx['reason']}",
                "",
                "───" * 4,
                f"回覆 \"YES {ctx['symbol']}\" 確認下單",
            ]
        )
        print("\n".join(lines))
        logger.info(f"Scan complete: {ctx['symbol']} opportunity formatted (Phase 3)")

        save_pending(
            {
                "symbol": ctx["symbol"],
                "price": ctx["price"],
                "strategy": ctx["strategy"],
                "score": int(ctx["adjusted_score"]),
                "original_score": ctx["score"],
                "research_adjustment": ctx["research_adj"],
                "signals": ctx["signals"],
                "reason": ctx["reason"],
                "stop_loss_pct": ctx["stop_loss_pct"],
                "tp_levels": ctx["tp_levels"],
                "stop_price": ctx["stop_price"],
                "max_hold_hours": ctx["max_hold"],
            }
        )


# ===================================================================
# Main orchestrator
# ===================================================================


def cmd_cron_scan():
    """Phase 3: Scan → Score → Research → Adapt → Execute.

    Enhanced pipeline:
    1. Market scan with 6-factor scoring (existing)
    2. StrategyAdaptor: auto-adjust strategies based on F&G + BTC trend + volatility
    3. MarketResearcher: deep research on top candidates (news, on-chain, catalysts)
    4. Event-driven position adjustment (NEW)
    5. RiskManager: pre-trade risk checks
    6. Auto-execute if enabled
    """
    # P1-6: File lock to prevent concurrent scan runs
    import fcntl
    LOCK_FILE = "/tmp/crypto-trader-scan.lock"
    _lock_fd = None
    try:
        _lock_fd = open(LOCK_FILE, "w")
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        logger.warning("Another scan is already running (lock held), skipping this scan")
        if _lock_fd:
            _lock_fd.close()
        return
    try:
        ctx = _step_scan_opportunities()
        if ctx is None:
            # 即使没有机会也发通知
            _append_scan_summary(None)
            return

        ctx = _step_research_top_n(ctx)
        if ctx is None:
            _append_scan_summary(ctx)
            return

        # NEW: Event-driven position adjustment
        _step_event_driven_adjustment(ctx)

        _step_execute_trades(ctx)

        # 发送扫描摘要通知
        _append_scan_summary(ctx)
    finally:
        # P1-6: Release file lock
        if _lock_fd:
            try:
                fcntl.flock(_lock_fd, fcntl.LOCK_UN)
                _lock_fd.close()
            except (IOError, OSError):
                logger.warning("Failed to release scan lock", exc_info=True)


def _append_scan_summary(ctx):
    """Append a brief scan summary notification."""
    from datetime import datetime
    
    now = datetime.now().strftime("%H:%M")
    
    if ctx is None:
        # 扫描失败或无机会
        body = f"🔍 {now} 扫描完成\n\n❌ 未发现符合条件的机会\n市场可能极度恐慌或波动过大"
    else:
        fng = ctx.get("fng", 50)
        fng_label = ctx.get("fng_label", "Unknown")
        opportunities = ctx.get("opportunities", [])
        threshold = ctx.get("dynamic_threshold", 80)
        
        opp_count = len(opportunities)
        
        # 市场情绪图标
        if fng <= 25:
            emoji = "😱"
        elif fng <= 45:
            emoji = "😟"
        elif fng <= 55:
            emoji = "😐"
        elif fng <= 75:
            emoji = "😊"
        else:
            emoji = "🤑"
        
        body = f"🔍 {now} 扫描完成\n\n"
        body += f"{emoji} 市场情绪: {fng} ({fng_label})\n"
        body += f"📊 动态阈值: {threshold}\n"
        body += f"💡 发现机会: {opp_count}个"
        
        if opp_count > 0:
            # 显示前3个机会
            top_3 = opportunities[:3]
            body += "\n\n🏆 前3名:\n"
            for i, opp in enumerate(top_3, 1):
                symbol = opp.get("symbol", "???")
                score = opp.get("score", 0)
                body += f"  {i}. {symbol} (评分: {score:.0f})\n"
    
    _append_notification("scan_summary", "", body)


def _step_event_driven_adjustment(ctx):
    """Step 4: Adjust existing positions based on news sentiment.

    When strong news sentiment is detected for an existing position:
    - Strong bullish (score >= 8, confidence >= 0.7): Consider adding to position
    - Strong bearish (score <= 2, confidence >= 0.7): Consider reducing position or tightening stop-loss

    This creates the event-driven chain: news → sentiment → position adjustment
    """
    try:
        from src.notifier import FeishuNotifier
        from src.portfolio import PortfolioManager

        portfolio = PortfolioManager()
        _pos_list = portfolio.get_all_positions()
        positions: Dict[str, Dict] = {
            p["symbol"]: p for p in _pos_list if "symbol" in p
        }

        if not positions:
            return

        # Get research results from context
        research = ctx.get("research", {})
        if not research:
            return

        news = research.get("news", [])
        if not news:
            return

        # Calculate average sentiment score and confidence
        total_score = 0
        total_confidence = 0
        count = 0
        for article in news:
            score = article.get("sentiment_score", 5)
            confidence = article.get("sentiment_confidence", 0.5)
            total_score += score
            total_confidence += confidence
            count += 1

        if count == 0:
            return

        avg_score = total_score / count
        avg_confidence = total_confidence / count

        # Check for strong sentiment
        if avg_confidence < 0.7:
            logger.info(
                f"Event-driven: sentiment confidence too low ({avg_confidence:.2f}), skipping adjustment"
            )
            return

        symbol = ctx.get("symbol", "")
        if not symbol:
            return

        # Check if we have a position in this symbol
        position = positions.get(symbol)
        if not position:
            return

        logger.info(
            f"Event-driven: strong sentiment detected for {symbol} (score={avg_score:.1f}, confidence={avg_confidence:.2f})"
        )

        # Strong bullish sentiment (score >= 8)
        if avg_score >= 8:
            logger.info(
                f"Event-driven: strong bullish sentiment for {symbol}, considering position increase"
            )
            # Notify user about the opportunity
            notifier = FeishuNotifier()
            notifier.send_text(
                f"🟢 強烈看漲信號: {symbol}\n"
                f"情緒評分: {avg_score:.1f}/10 (信心度: {avg_confidence:.0%})\n"
                f"新聞數量: {count} 篇\n"
                f"建議: 考慮增加持倉或調整止盈"
            )

        # Strong bearish sentiment (score <= 2)
        elif avg_score <= 2:
            logger.info(
                f"Event-driven: strong bearish sentiment for {symbol}, considering position reduction"
            )
            # Notify user about the risk
            notifier = FeishuNotifier()
            notifier.send_text(
                f"🔴 強烈看跌信號: {symbol}\n"
                f"情緒評分: {avg_score:.1f}/10 (信心度: {avg_confidence:.0%})\n"
                f"新聞數量: {count} 篇\n"
                f"建議: 考慮減少持倉或收緊止損"
            )

    except Exception as e:
        logger.error(f"Event-driven adjustment failed: {e}")
