"""
Research phase — LLM-powered multi-analyst research and position sizing.
Extracted from scan_orchestrator for maintainability.
"""

import logging
import os

from src.bear_analyst import BearAnalyst
from src.paper_trader import get_trading_client, is_paper_mode
from src.pending_confirmation import clear_pending, save_pending
from src.position_optimizer import PositionOptimizer
from src.notifier import FeishuNotifier, _append_notification
from src.trade_executor import count_active_positions, get_position_tier
from src.trade_journal import TradeJournal

logger = logging.getLogger(__name__)




def _filter_by_risk(opportunities, client, risk_mgr, acct, adapted,
                    regime, btc_trend, dynamic_threshold):
    """Gather risk positions, apply regime guard, and filter by risk manager.

    Returns (filtered_opportunities, dynamic_threshold, risk_positions).
    """
    # Gather existing positions for risk check
    acct_balances = acct.get("balances", [])
    risk_positions = []
    _usdt_total = 0.0
    for b in acct_balances:
        asset = b["asset"]
        total = float(b["free"]) + float(b["locked"])
        if asset == "USDT":
            _usdt_total = total
        elif total > 0 and asset != "NTRN":
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

    # Account equity = positions value + USDT cash (correct denominator for sector %)
    _account_equity = sum(p.get("value_usdt", 0) for p in risk_positions) + _usdt_total

    # Regime-based guard: FEAR/EXTREME_FEAR — raise threshold
    # Calibrated per investment advisor: 98 was too strict (effectively blocking all
    # trades in extreme fear), 85 allows high-quality signals while still filtering noise.
    has_fear_mode = any(o.get("fear_mode") for o in opportunities)
    if regime in ("EXTREME_FEAR",):
        if not has_fear_mode:
            dynamic_threshold = max(dynamic_threshold, 85)
            print(
                f"REGIME_GUARD: {regime} — threshold raised to {dynamic_threshold} (calibrated: filter noise, keep quality)"
            )
        else:
            print(f"REGIME_GUARD: {regime} — bypassed for fear accumulation mode")
    elif regime == "FEAR" and btc_trend != "BULLISH":
        dynamic_threshold = max(dynamic_threshold, 80)
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
                closes = [float(k["close"]) for k in klines]
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
            account_equity=_account_equity,
        )
        if check["allowed"]:
            opp["_size_multiplier"] = check["adjustments"].get("size_multiplier", 1.0)
            filtered.append(opp)
        else:
            logger.info(
                "RiskManager: %s blocked – %s", sym, "; ".join(check["reasons"])
            )
            _print_blocked_opp(opp, sym, check)

    return filtered, dynamic_threshold, risk_positions


def _print_blocked_opp(opp, sym, check):
    """Print diagnostic info for a risk-blocked opportunity."""
    score_val = opp.get("score", "N/A")
    analysis_1h = opp.get("analysis", {}).get("1h", {})
    tech_val = opp.get("technical_score")
    if tech_val is None:
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


def _run_deep_research(researcher, client, top_n, fng):
    """Run parallel research on top candidates, including fear-mode shortcuts.

    Returns dict: symbol -> (opportunity, research_result).
    """
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from concurrent.futures import TimeoutError as FuturesTimeout

    research_results = {}

    # Fear mode: skip heavy research, use synthetic result
    for opp in top_n:
        if opp.get("fear_mode"):
            rsi_val = opp.get("analysis", {}).get("1h", {}).get("rsi", 50)
            research_results[opp["symbol"]] = (opp, {
                "score_adjustment": 0,
                "confidence": 60,
                "sentiment_summary": f"Fear accumulation mode — RSI={rsi_val:.0f}, F&G={fng}",
                "news": [],
                "onchain": {
                    "whale_activity": "UNKNOWN",
                    "exchange_flow": "UNKNOWN",
                    "volume_trend": "UNKNOWN",
                },
            })
            logger.info("Fear mode: using synthetic research for %s", opp["symbol"])

    # Remove fear_mode candidates from normal research
    normal_top_n = [o for o in top_n if not o.get("fear_mode")]
    if normal_top_n:
        logger.info(
            f"Researching top {len(normal_top_n)} candidates: {[o['symbol'] for o in normal_top_n]}"
        )
    t_research = _time.time()

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(researcher.research, opp["symbol"], client): opp
            for opp in normal_top_n
        }
        try:
            for fut in as_completed(futures, timeout=90):
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

    return research_results


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
    opportunities, dynamic_threshold, risk_positions = _filter_by_risk(
        opportunities, client, risk_mgr, acct, adapted,
        regime, btc_trend, dynamic_threshold,
    )

    if not opportunities:
        print("NO_OPPORTUNITIES")
        clear_pending()
        return None

    # ===== Step 6: Deep Research on Top 3 Candidates (parallel) =====
    top_n = opportunities[:3]
    research_results = _run_deep_research(researcher, client, top_n, fng)

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
    # Fear mode: skip bear analysis (we're already buying fear — bear is redundant)
    is_fear_mode = top.get("fear_mode", False)
    bear = BearAnalyst()
    if is_fear_mode:
        from src.bear_analyst import BearResult
        bear_result = BearResult(
            bear_score=0,
            veto=False,
            reasons=["Fear accumulation mode — bear analysis bypassed"],
            confidence="LOW",
        )
        print(f"BEAR_BYPASS: {symbol} — fear accumulation mode, skipping bear analysis")
    else:
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
    # Fear mode: use DCA directly (skip registry for speed)
    if is_fear_mode:
        strategy = "dca"
        logger.info("Fear mode: using DCA strategy directly")
    else:
        strategy = "score_based"  # Default when no specific strategy matches
    try:
        if is_fear_mode:
            # Fear mode: skip registry, use DCA directly
            logger.info("Fear mode: skipping StrategyRegistry, using DCA")
        else:
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

    _sig_strs = []
    for s in signals[:3]:
        if isinstance(s, str):
            _sig_strs.append(s)
        elif isinstance(s, dict):
            _sig_strs.append(s.get("source", s.get("type", str(s))))
        else:
            _sig_strs.append(str(s))
    reason = " / ".join(_sig_strs) if _sig_strs else "Multiple signals"

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

    # ── Layered position sizing in fear regime ──
    # Per investment advisor: in FEAR/EXTREME_FEAR, scale position by signal quality.
    # Higher score = more conviction = larger position; lower score = probe only.
    # This replaces the old binary "all-or-nothing" approach with graduated exposure.
    _regime = adapted.get("regime", "")
    if _regime in ("EXTREME_FEAR", "FEAR"):
        if adjusted_score >= 90:
            _fear_tier_mult = 1.0   # Full conviction → full position
        elif adjusted_score >= 85:
            _fear_tier_mult = 0.80  # Strong signal → 80% position
        elif adjusted_score >= 80:
            _fear_tier_mult = 0.60  # Good signal → 60% position
        else:  # 75-80 range (just above calibrated threshold)
            _fear_tier_mult = 0.40  # Probe position → 40% only
        size_multiplier *= _fear_tier_mult
        logger.info(
            f"Layered sizing ({_regime}): score={adjusted_score:.1f} → {_fear_tier_mult:.0%} position"
        )

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
