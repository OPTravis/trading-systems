#!/usr/bin/env python3
"""
Weekly Multi-Symbol Backtest — runs every Monday 09:30.

Outputs a compact Feishu-formatted report of backtest results
for the current top traded symbols. Compares returns, win rates,
Sharpe/Calmar ratios. Flags any strategy degradation.

Exit: stdout for cron delivery. Empty = silent.
"""
import sys
import os
import json
import logging
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/crypto-ai-trader"))

logging.basicConfig(level=logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("src").setLevel(logging.WARNING)

from src.backtest import BacktestEngine
from src.binance_client import BinanceClient

# Top symbols to backtest (adjust as portfolio evolves)
SYMBOLS = ["SOLUSDT", "ETHUSDT", "AVAXUSDT", "BNBUSDT", "LINKUSDT"]
DAYS = 90


def main():
    client = BinanceClient(testnet=False)
    engine = BacktestEngine(binance_client=client, initial_capital=10000)

    # Use run_multi for correct portfolio-level aggregation
    result = engine.run_multi(
        symbols=SYMBOLS,
        interval="1h",
        days=DAYS,
        enable_trend_filter=False,
        enable_trailing_stop=True,
    )

    summary = result.get("summary", {})
    individual = result.get("individual", {})

    # Build report
    lines = []
    lines.append("## 週度策略回測報告")
    lines.append(f"- 期間: {DAYS} 天 | 1h K 線 | $10,000 初始資金/幣")
    lines.append(f"- 時間: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("---")
    lines.append("## 績效總覽")

    total_trades = 0
    degradation = []

    for sym, r in individual.items():
        if "error" in r:
            lines.append(f"- **{sym}**: ❌ {r.get('error', 'unknown')}")
            continue

        sym_clean = sym.replace("USDT", "")
        ret = r.get("total_return_pct", 0)
        wr = r.get("win_rate", 0)
        sr = r.get("sharpe_ratio", 0)
        cr = r.get("calmar_ratio", 0)
        dd = r.get("max_drawdown_pct", 0)
        trades = r.get("total_trades", 0)
        pf = r.get("profit_factor", 0)

        total_trades += trades

        # Flag degradation: negative return + high drawdown
        if ret < -5 and dd > 15:
            degradation.append(f"{sym_clean}: {ret:+.1f}% (DD {dd:.1f}%)")

        lines.append(
            f"- **{sym_clean}**: {ret:+.2f}% | WinRate {wr:.0f}% | "
            f"Sharpe {sr:.2f} | Calmar {cr:.2f} | "
            f"DD {dd:.1f}% | {trades} trades | PF {pf}"
        )

    # Use summary for correct total return (weighted by PnL, not % sum)
    lines.append("---")
    total_return = summary.get("total_return_pct", 0)
    total_pnl = summary.get("total_pnl_usdt", 0)
    total_final = summary.get("total_final_equity", 0)
    total_initial = summary.get("total_initial_capital", 0)
    overall_wr = summary.get("win_rate", 0)
    overall_pf = summary.get("profit_factor", 0)
    lines.append(f"**組合合計**: {total_return:+.2f}% (PnL ${total_pnl:+,.2f}) | {total_trades} trades | WR {overall_wr:.0f}% | PF {overall_pf}")

    if degradation:
        lines.append("---")
        lines.append("## ⚠️ 策略退化警報")
        for d in degradation:
            lines.append(f"- {d}")
        lines.append("- 建議: 跑 walk-forward 驗證是否需要調整閾值")

    report = "\n".join(lines)
    print(report)

    # Write status file for post-run monitoring
    status = {
        "pipeline": "weekly_backtest",
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "total_return_pct": total_return,
        "total_trades": total_trades,
        "degradation": degradation,
        "has_degradation": len(degradation) > 0,
        "all_ok": len(degradation) == 0,
    }
    status_file = Path.home() / "trading-systems" / "crypto-ai-trader" / "logs" / "weekly_backtest_status.json"
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(status, indent=2), encoding="utf-8")

    # Exit non-zero on degradation so wrapper can detect
    if status["has_degradation"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
