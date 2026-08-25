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
        _step_execute_trades(ctx)
        _append_scan_summary(ctx)
    finally:
        if _lock_fd:
            try:
                fcntl.flock(_lock_fd, fcntl.LOCK_UN)
                _lock_fd.close()
            except (IOError, OSError):
                logger.warning("Failed to release scan lock", exc_info=True)



def _bull_phase2_status_line() -> str:
    """Return BULL Phase 2 regime + capture ratio line for scan report.
    Returns empty string if Phase 2 modules not initialised."""
    try:
        from src.state_db import StateDB
        from src.bull_regime import BullRegimeDetector
        from src.capture_tracker import CaptureTracker
        db = StateDB()
        det = BullRegimeDetector(db=db)
        state = det.load_state()
        if not state.last_eval_ts:
            return ""  # Phase 2 not initialised yet
        line = det.format_report_line()
        ct = CaptureTracker(db)
        info = ct.current()
        if info:
            l = info["latest"]
            line += (
                f"\n📊 BTC Capture: {l['capture_ratio']:.1%}"
                f" (paper {l['paper_return']:+.2%} vs BTC {l['btc_bh_return']:+.2%},"
                f" {info['days_elapsed']:.0f}d)"
            )
        return line
    except Exception:
        return ""


def _append_scan_summary(ctx):
    """Append a brief scan summary notification."""
    from datetime import datetime

    now = datetime.now().strftime("%H:%M")

    if ctx is None:
        body = f"🔍 {now} 扫描完成\n\n❌ 未发现符合条件的机会\n市场可能极度恐慌或波动过大"
        bull_line = _bull_phase2_status_line()
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
    bull_line = _bull_phase2_status_line()
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
