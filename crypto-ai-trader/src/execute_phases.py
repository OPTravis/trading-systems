"""
Execute phases — trade execution, journaling, and event-driven adjustments.
Extracted from scan_orchestrator for maintainability.
"""

import logging
import os

from src.notifier import FeishuNotifier, send_signal, _append_notification
from src.paper_trader import get_trading_client, is_paper_mode
from src.pending_confirmation import clear_pending, save_pending
from src.trade_executor import execute_auto_trade, _RISK_MAX_ACTIVE_POSITIONS
from src.trade_journal import TradeJournal

logger = logging.getLogger(__name__)





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
        f"活躍持倉: {ctx['active_pos']}/{_RISK_MAX_ACTIVE_POSITIONS}",
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
            order_value=ctx.get("top", {}).get("order_value"),  # DeepValueBTC / Fear Acc
            surge_alert_level=(
                ctx.get("surge_result", {}).get("alert_level", "SILENCE")
                if ctx.get("surge_result")
                else "SILENCE"
            ),
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

                _explore_tag = " 🔬EXPLORATION" if result.get("is_exploration") else ""
                print(
                    f"✅ Auto-executed {ctx['symbol']}: BUY {result['qty']} @ ${ctx['price']:.6f} | Invest: {result['invest_pct']}% | F&G: {ctx['fng']} ({ctx['fng_label']}) | Research: {ctx['research_adj']:+.1f}{_explore_tag}"
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
