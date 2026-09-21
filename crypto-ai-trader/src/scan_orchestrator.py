"""
Scan orchestration — pipeline coordinator.

Actual step implementations live in:
  - scan_phases:     market scan, sentiment, strategy adaptation, fear/QFL/hash-ribbon fallbacks
  - research_phase:  multi-analyst LLM research and position sizing
  - execute_phases:  trade execution, journaling, event-driven adjustment

This module provides the public API (cmd_scan, cmd_cron_scan) and re-exports
all names that external code and tests may reference.
"""

import os
import logging

# ── Re-export names for backward compatibility ────────────────────────────────
# Tests mock these at src.scan_orchestrator.XXX — keeping them here ensures
# those patches still work for cmd_scan which creates objects directly.
from src.bear_analyst import BearAnalyst  # noqa: F401
from src.binance_client import BinanceClient  # noqa: F401
from src.market_scanner import MarketScanner  # noqa: F401
from src.notifier import FeishuNotifier, send_signal, _append_notification  # noqa: F401
from src.paper_trader import get_trading_client, is_paper_mode  # noqa: F401
from src.pending_confirmation import clear_pending, save_pending  # noqa: F401
from src.portfolio import PortfolioManager  # noqa: F401
from src.position_optimizer import PositionOptimizer  # noqa: F401
from src.sentiment import SentimentAnalyzer  # noqa: F401
from src.trade_executor import (  # noqa: F401
    count_active_positions,
    execute_auto_trade,
    get_position_tier,
)
from src.trade_journal import TradeJournal  # noqa: F401

# Step functions (moved to sub-modules)
from src.scan_phases import (
    _sync_from_binance,
    _try_fear_accumulation,
    _try_qfl_fallback,
    _try_hash_ribbon,
    _step_scan_opportunities,
)
from src.research_phase import _step_research_top_n
from src.execute_phases import (
    _step_journal_results,
    _step_execute_trades,
    _step_event_driven_adjustment,
)

logger = logging.getLogger(__name__)


def cmd_scan(send_notification: bool = False):
    """Scan market for opportunities (interactive/manual mode)."""
    logger.info("=== Market Scanner ===")

    client = get_trading_client()
    scanner = MarketScanner(client)

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

    print("\n🔍 Scanning for opportunities...")
    opportunities = scanner.scan_all()

    print(f"\n📊 Found {len(opportunities)} opportunities:")
    for opp in opportunities[:10]:
        vol_str = f"${opp['volume_24h']/1e6:.1f}M" if opp["volume_24h"] > 0 else "N/A"
        print(f"\n  {opp['symbol']} (Score: {opp['score']:.0f}/100)")
        print(f"    24h Change: {opp['price_change_24h']:.2f}%")
        print(f"    Volume: {vol_str}")
        print(f"    Signals: {', '.join(opp['signals'][:3])}")

    if send_notification and opportunities:
        notifier = FeishuNotifier()
        gainers = [m for m in movers if m["direction"] == "gainer"]
        losers = [m for m in movers if m["direction"] == "loser"]
        notifier.send_market_scan(opportunities, gainers, losers)
        logger.info("Feishu notification sent")

        print("=" * 50)


def cmd_cron_scan():
    """Phase 3: Scan → Score → Research → Adapt → Execute.

    Enhanced pipeline:
    1. Market scan with 6-factor scoring
    2. StrategyAdaptor: auto-adjust strategies based on F&G + BTC trend + volatility
    3. MarketResearcher: deep research on top candidates
    4. Event-driven position adjustment
    5. RiskManager: pre-trade risk checks
    6. Auto-execute if enabled
    """
    import fcntl

    LOCK_FILE = os.environ.get("SCAN_LOCK_FILE", "/tmp/crypto-trader-scan.lock")
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
            _append_scan_summary(None)
            return

        ctx = _step_research_top_n(ctx)
        if ctx is None:
            _append_scan_summary(ctx)
            return

        _step_event_driven_adjustment(ctx)
        if _step_kv_preflight(ctx):
            _step_execute_trades(ctx)
        else:
            logger.warning(
                "kv_preflight: FAIL — skipping new entries this scan")
        _step_reconcile_portfolio(ctx)
        _step_defense_sweep(ctx)
        _step_evolve_strategies(ctx)
        _append_scan_summary(ctx)
    finally:
        if _lock_fd:
            try:
                fcntl.flock(_lock_fd, fcntl.LOCK_UN)
                _lock_fd.close()
            except (IOError, OSError):
                logger.warning("Failed to release scan lock", exc_info=True)



def _step_evolve_strategies(ctx):
    """Phase 2B: per-scan strategy auto-switch evaluation.

    Runs the dual-window PF channel + dca guardrail on every cron-scan
    (rolling stats are refreshed per-trade by Phase 2A). The weekly
    WR channel in online_learner is unchanged. Fail-safe: any error
    here never blocks the scan pipeline or trading.
    """
    try:
        from src.strategy_evolver import StrategyEvolver

        evolver = StrategyEvolver()
        pf_changes = evolver.evaluate_pf_channel()
        for c in pf_changes:
            logger.info(
                "STRATEGY_EVOLVED: %s %s — %s",
                c.get("action"), c.get("strategy"), c.get("reason"),
            )
    except Exception:
        logger.warning("evolve step failed (non-fatal)", exc_info=True)


def _step_reconcile_portfolio(ctx):
    """2026-09-17 bridge blind-spot fix: book OCO passive fills.

    Detects DB-vs-exchange drift (same-round or cross-round), books the
    missing exchange-side SELL legs into trades (orderId-idempotent), and
    prints bridge-visible log lines so reside_scan/latest.json picks them
    up. Fail-open: exchange/API errors skip the round silently.
    """
    try:
        from src.portfolio_reconciler import reconcile_portfolio_drift

        client = ctx.get("client")
        portfolio = ctx.get("portfolio")
        if client is None or portfolio is None:
            return
        db = getattr(portfolio, "_db", None)
        if db is None:
            return
        booked = reconcile_portfolio_drift(client, db)
        if booked:
            logger.info(
                "reconcile: booked %d OCO fill(s): %s",
                len(booked),
                ", ".join(f"{b['symbol']} {b['qty']}@{b['price']}" for b in booked),
            )

        # P0-1 (設計 v1.1 §四/§五): dust reaper + health self-report.
        # dust_reaper defaults to report-only (kv DUST_REAPER_MODE); never
        # raises into the pipeline.
        try:
            from src.dust_reaper import run as dust_reaper_run
            from src.health_report import run as health_report_run
            portfolio = ctx.get("portfolio") or portfolio
            dust_summary = dust_reaper_run(client, portfolio)
            health_report_run(client, portfolio, dust_summary=dust_summary)
            logger.info(
                "dust_reaper: mode=%s positions=%d candidates=%d watch=%d",
                dust_summary.get("mode"), dust_summary.get("positions", 0),
                dust_summary.get("liquidate_candidates", 0),
                dust_summary.get("watch", 0),
            )
        except Exception:
            logger.warning("dust/health step failed (non-fatal)", exc_info=True)
    except Exception:
        logger.warning("reconcile step failed (non-fatal)", exc_info=True)


def _step_kv_preflight(ctx) -> bool:
    """P0-2 defense item 3: KV/state freshness gate before new entries.

    Returns True when the execute step may run. Any preflight failure
    (including an internal exception — a broken gate must fail closed)
    returns False so no new positions are opened on stale/corrupt state.
    """
    portfolio = ctx.get("portfolio")
    db = getattr(portfolio, "_db", None) if portfolio is not None else None
    if db is None:
        logger.warning("kv_preflight: no db handle — skipping new entries")
        return False
    try:
        from src.kv_preflight import run as kv_preflight_run

        result = kv_preflight_run(db)
        return bool(result.get("ok"))
    except Exception:
        logger.warning("kv_preflight: internal error — skipping new entries",
                       exc_info=True)
        return False


def _step_defense_sweep(ctx):
    """P0-2 defense items 1/2: stuck-order monitor + circuit tiers.

    Runs after reconcile/dust/health so tier evaluation sees the freshest
    booked state. Both sub-steps are individually fail-open (non-fatal).
    """
    client = ctx.get("client")
    portfolio = ctx.get("portfolio")
    if client is None or portfolio is None:
        return
    try:
        from src.stuck_order_monitor import run as stuck_order_run

        stuck = stuck_order_run(client)
        if stuck.get("stuck"):
            logger.warning("stuck_order_monitor: %s", stuck)
    except Exception:
        logger.warning("stuck order monitor failed (non-fatal)",
                       exc_info=True)
    try:
        from src.circuit_tiers import evaluate_and_act

        tiers = evaluate_and_act(client, portfolio)
        if tiers.get("tier", 0) > 0 or tiers.get("action") not in (
                "HOLD", None):
            logger.warning("circuit_tiers: %s", tiers)
    except Exception:
        logger.warning("circuit tiers step failed (non-fatal)",
                       exc_info=True)


def _bull_phase2_status_line(opportunities=None) -> str:
    """Run BULL Phase 2 paper scan and return report section.
    Returns empty string if Phase 2 not initialised."""
    try:
        from src.state_db import StateDB
        db = StateDB()
        # Check if Phase 2 is initialised (capture tracker start_ts > 0)
        import json
        raw = db.kv_get("capture_tracker_state")
        if not raw:
            return ""
        state = json.loads(raw)
        if not state.get("initialised"):
            return ""
        # Run paper scan
        from scripts.bull_paper_scan import run_paper_scan
        result = run_paper_scan(scanner_opportunities=opportunities)
        return result["report"]
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"BULL paper scan error: {e}")
        return f"⚠️ BULL paper scan error: {e}"


def _append_scan_summary(ctx):
    """Append a brief scan summary notification."""
    from datetime import datetime

    now = datetime.now().strftime("%H:%M")

    if ctx is None:
        body = f"🔍 {now} 扫描完成\n\n❌ 未发现符合条件的机会\n市场可能极度恐慌或波动过大"
        bull_line = _bull_phase2_status_line(ctx.get("opportunities") if ctx else None)
        if bull_line:
            body += f"\n\n{bull_line}"
        _append_notification("scan_summary", "", body)
        return

    fng = ctx.get("fng", 50)
    fng_label = ctx.get("fng_label", "Unknown")
    opportunities = ctx.get("opportunities", [])
    threshold = ctx.get("dynamic_threshold", 80)
    opp_count = len(opportunities)

    if fng <= 25:
        emoji = "😱"
    elif fng <= 45:
        emoji = "😟"
    elif fng < 55:
        emoji = "😐"
    elif fng < 75:
        emoji = "😊"
    else:
        emoji = "🤑"

    body = f"🔍 {now} 扫描完成\n\n"
    body += f"{emoji} 市场情绪: {fng} ({fng_label})\n"
    body += f"📊 动态阈值: {threshold}\n"

    # Append surge detection info
    surge = ctx.get("surge_result")
    if surge and surge.get("alert_level", "SILENCE") != "SILENCE":
        surge_emoji = {
            "WATCH": "🔵", "ACCUMULATE": "🟡",
            "IMMINENT": "🔴", "CONFIRMED": "🚀",
        }.get(surge["alert_level"], "⚪")
        body += f"{surge_emoji} 暴涨预警: {surge['alert_level']}"
        body += f" (P1={surge['phase1_count']} P2={surge['phase2_count']} P3={surge['phase3_count']})\n"
        # Show top phase 3 signals if any
        if surge["phase3_signals"]:
            body += f"  🔥 {surge['phase3_signals'][0]}\n"
        elif surge["phase2_signals"]:
            body += f"  🐋 {surge['phase2_signals'][0]}\n"
        elif surge["phase1_signals"]:
            body += f"  📊 {surge['phase1_signals'][0]}\n"
    # Phase 2 BULL regime status (if initialised)
    bull_line = _bull_phase2_status_line(ctx.get("opportunities") if ctx else None)
    if bull_line:
        body += f"\n\n{bull_line}"

    body += f"\n💡 发现机会: {opp_count}个"

    if opp_count > 0:
        top_3 = opportunities[:3]
        body += "\n\n🏆 前3名:\n"
        for i, opp in enumerate(top_3, 1):
            symbol = opp.get("symbol", "???")
            score = opp.get("score", 0)
            body += f"  {i}. {symbol} (评分: {score:.0f})\n"

    _append_notification("scan_summary", "", body)
