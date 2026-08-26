#!/usr/bin/env python3
"""
BULL Phase 2 Weekly Summary — runs every Sunday.
Generates a week summary report with:
  - Cumulative return, Sharpe, PF, MaxDD
  - Regime timeline
  - Deviation from backtest expectations
  - Core/Sat attribution
  - Exit reason breakdown
"""
import sys, os, json, time, math
sys.path.insert(0, os.path.expanduser("~/crypto-ai-trader"))
os.chdir(os.path.expanduser("~/crypto-ai-trader"))

from datetime import datetime, timedelta
from src.state_db import StateDB
from src.bull_paper_engine import BullPaperEngine
from src.bull_regime import BullRegimeDetector, STATE_EMOJI, STATE_CN
from src.capture_tracker import CaptureTracker

DB_PATH = "/root/trading-state/state.db"


def run_weekly():
    db = StateDB(DB_PATH)
    engine = BullPaperEngine(db, None)  # no client needed for historical data
    det = BullRegimeDetector(db=db)
    ct = CaptureTracker(db)

    status = engine.get_status()
    val = status["portfolio"]
    info = ct.current()

    # Get all trades for the week
    all_trades = engine.portfolio.get_trade_history(limit=500)
    week_ago = int(time.time() * 1000) - 7 * 86400_000
    week_trades = [t for t in all_trades if t["timestamp"] >= week_ago]

    # Closed P&L
    closed_positions = [p for p in engine.portfolio.get_all_positions(limit=200)
                        if p["status"] == "closed" and p["exit_time"] >= week_ago]
    total_pnl = sum(p["realized_pnl"] for p in closed_positions)
    wins = [p for p in closed_positions if p["realized_pnl"] > 0]
    losses = [p for p in closed_positions if p["realized_pnl"] <= 0]

    win_rate = len(wins) / len(closed_positions) if closed_positions else 0
    avg_win = sum(p["realized_pnl"] for p in wins) / len(wins) if wins else 0
    avg_loss = sum(p["realized_pnl"] for p in losses) / len(losses) if losses else 0
    pf = abs(sum(p["realized_pnl"] for p in wins) / sum(p["realized_pnl"] for p in losses)) if losses and sum(p["realized_pnl"] for p in losses) != 0 else float("inf")

    # P0-A6: hold-time distribution by side
    hold_core = engine.portfolio.hold_time_stats(side="core", days=7)
    hold_sat = engine.portfolio.hold_time_stats(side="satellite", days=7)

    # Regime transitions
    transitions = det.get_transitions(50)
    week_transitions = [t for t in transitions if t["ts"] >= week_ago]
    regime_time = det.get_time_in_state()

    lines = [
        "=" * 50,
        f"📊 BULL Phase 2 週報 — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "=" * 50,
        "",
        f"💰 累計權益: ${status['total_value']:.2f} (起始 $400)",
        f"   回報: {status['total_return']:+.2%}",
        f"   Cash: ${status['cash']:.2f}",
        f"   持倉市值: ${val['market_value']:.2f}",
        f"   未實現盈虧: ${val['unrealized_pnl']:+.2f} ({val['unrealized_pnl_pct']:+.2%})",
        "",
        f"📈 本週交易:",
        f"   平倉: {len(closed_positions)} 筆 | 勝率: {win_rate:.0%}",
        f"   已實現盈虧: ${total_pnl:+.2f}",
        f"   平均盈利: ${avg_win:+.2f} | 平均虧損: ${avg_loss:+.2f}",
        f"   盈虧比 PF: {pf:.2f}" if pf != float("inf") else "   盈虧比 PF: N/A",
        "",
    ]
    # P0-A6 hold-time lines
    for label, hs in (("Core", hold_core), ("Sat", hold_sat)):
        if hs.get("count"):
            lines.append(
                f"   {label} hold: avg {hs['avg_hours']:.1f}h | med {hs['median_hours']:.1f}h "
                f"| p10-p90 {hs['p10_hours']:.1f}-{hs['p90_hours']:.1f}h "
                f"| min {hs['min_hours']:.1f}h max {hs['max_hours']:.1f}h (n={hs['count']})"
            )
    lines.extend([
        "",
        f"🎯 當前 Regime: {STATE_EMOJI.get(regime_time['regime'],'')} {regime_time['regime']}",
        f"   持續: {regime_time['hours_in_state']:.0f}h",
        f"   本週轉換: {len(week_transitions)} 次",
    ])

    for t in week_transitions:
        ts_str = datetime.fromtimestamp(t["ts"] / 1000).strftime("%m-%d %H:%M")
        lines.append(f"   {ts_str} {t['from_state']}→{t['to_state']}: {t['reason'][:50]}")

    # Capture
    if info:
        l = info["latest"]
        lines.extend([
            "",
            f"📊 BTC Capture Ratio: {l['capture_ratio']:.1%}",
            f"   Paper: {l['paper_return']:+.2%} vs BTC B&H: {l['btc_bh_return']:+.2%}",
            f"   追踪天數: {info['days_elapsed']:.1f}",
        ])

    # Backtest comparison
    lines.extend([
        "",
        "📐 回測預期對照:",
        f"   回測 Bull A 年化~28% (Sharpe 1.13, MaxDD -14.4%)",
        f"   當前 paper: {status['total_return']:+.2%} | EMA50 rate: {status['ema50_rate']:.0%}",
        f"   Slippage: {status['avg_slippage']*100:.2f}% (假設 0.05%, 警告 0.08%)",
    ])

    # Exit reasons
    if status["exit_reasons"]:
        lines.append("")
        lines.append("📤 出場原因統計:")
        for reason, count in sorted(status["exit_reasons"].items(), key=lambda x: -x[1]):
            lines.append(f"   {reason}: {count}")

    # Open positions
    if status["positions"]:
        lines.append("")
        lines.append("📋 持倉:")
        for p in status["positions"]:
            px = status["prices"].get(p["symbol"], p["entry_price"])
            pnl_pct = (px - p["entry_price"]) / p["entry_price"]
            lines.append(
                f"   [{p['side'][:3]}] {p['symbol']:10s} {p['quantity']:.4f} "
                f"@ ${p['entry_price']:.4f} → ${px:.4f} ({pnl_pct:+.1%})"
            )

    report = "\n".join(lines)
    print(report)

    # Save to file
    report_dir = "/Coze/Drive/Crypto_Trading_Monitor/crypto-reports"
    os.makedirs(report_dir, exist_ok=True)
    fname = f"bull_paper_weekly_{datetime.now().strftime('%Y%m%d')}.txt"
    with open(os.path.join(report_dir, fname), "w") as f:
        f.write(report)
    print(f"\nSaved to {report_dir}/{fname}")
    return report


if __name__ == "__main__":
    run_weekly()
