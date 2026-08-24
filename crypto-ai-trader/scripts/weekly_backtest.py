#!/usr/bin/env python3
"""
Weekly Multi-Symbol Backtest — runs every Monday 09:30.

Outputs a compact Feishu-formatted report of backtest results
for the current top traded symbols. Compares returns, win rates,
Sharpe/Calmar ratios. Flags any strategy degradation.

2026-08-24 (bug#25): run TWO modes to mirror live deployment.
- LIVE mode (trend filter ON): BTC 200SMA gate, same as trade_executor.
  Degradation alerts are judged on THIS mode. Symbols with < 10 trades
  are exempt from degradation judgment (trend-gate periods naturally
  produce few trades; judging on tiny samples causes false alerts).
- ALPHA mode (trend filter OFF): raw strategy health reference only,
  never triggers alerts. Kept to detect strategy alpha decay that the
  trend gate would mask.

Rationale: since 2026-08-03, live trading is protected by the BTC 200SMA
trend filter; the old trend-OFF-only backtest kept alerting on bear-market
segments that live would never trade (3 consecutive weekly false alarms
8/10-8/24 while live was profitable).

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
MIN_TRADES_FOR_DEGRADATION = 10


def evaluate_degradation(individual: dict, min_trades: int = MIN_TRADES_FOR_DEGRADATION):
    """Judge degradation per symbol.

    Returns (degradation: list[str], low_sample: list[str]).
    Symbols with < min_trades trades are listed as low_sample (exempt).
    Degradation criteria: return < -5% AND max drawdown > 15%.
    """
    degradation, low_sample = [], []
    for sym, r in individual.items():
        if "error" in r:
            continue
        sym_clean = sym.replace("USDT", "")
        ret = r.get("total_return_pct", 0)
        dd = r.get("max_drawdown_pct", 0)
        trades = r.get("total_trades", 0)
        if trades < min_trades:
            low_sample.append(f"{sym_clean}: {trades} trades（趨勢過濾期，豁免判定）")
            continue
        if ret < -5 and dd > 15:
            degradation.append(f"{sym_clean}: {ret:+.1f}% (DD {dd:.1f}%)")
    return degradation, low_sample


def format_section(individual: dict, summary: dict, title: str, note: str = ""):
    lines = [f"## {title}"]
    if note:
        lines.append(f"（{note}）")
    total_trades = 0
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
        lines.append(
            f"- **{sym_clean}**: {ret:+.2f}% | WinRate {wr:.0f}% | "
            f"Sharpe {sr:.2f} | Calmar {cr:.2f} | "
            f"DD {dd:.1f}% | {trades} trades | PF {pf}"
        )
    total_return = summary.get("total_return_pct", 0)
    total_pnl = summary.get("total_pnl_usdt", 0)
    overall_wr = summary.get("win_rate", 0)
    overall_pf = summary.get("profit_factor", 0)
    lines.append(
        f"**組合合計**: {total_return:+.2f}% (PnL ${total_pnl:+,.2f}) | "
        f"{total_trades} trades | WR {overall_wr:.0f}% | PF {overall_pf}"
    )
    return lines, total_trades


def main():
    client = BinanceClient(testnet=False)
    engine = BacktestEngine(binance_client=client, initial_capital=10000)

    # LIVE mode: trend filter ON — mirrors live deployment
    live_result = engine.run_multi(
        symbols=SYMBOLS,
        interval="1h",
        days=DAYS,
        enable_trend_filter=True,
        enable_trailing_stop=True,
    )
    # ALPHA mode: trend filter OFF — raw strategy health reference
    alpha_result = engine.run_multi(
        symbols=SYMBOLS,
        interval="1h",
        days=DAYS,
        enable_trend_filter=False,
        enable_trailing_stop=True,
    )

    live_summary = live_result.get("summary", {})
    live_individual = live_result.get("individual", {})
    alpha_summary = alpha_result.get("summary", {})
    alpha_individual = alpha_result.get("individual", {})

    degradation, low_sample = evaluate_degradation(live_individual)

    # Build report
    lines = []
    lines.append("## 週度策略回測報告")
    lines.append(f"- 期間: {DAYS} 天 | 1h K 線 | $10,000 初始資金/幣")
    lines.append(f"- 時間: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("---")
    live_lines, live_total_trades = format_section(
        live_individual, live_summary,
        "績效總覽（實盤同款：BTC 200SMA 趨勢過濾 ON）",
        "退化警報以此版為準",
    )
    lines.extend(live_lines)

    if low_sample:
        lines.append("---")
        lines.append("## 低樣本（趨勢過濾期，豁免退化判定）")
        for s in low_sample:
            lines.append(f"- {s}")

    lines.append("---")
    alpha_lines, _ = format_section(
        alpha_individual, alpha_summary,
        "裸策略 alpha 參考（趨勢過濾 OFF）",
        "只反映策略本身健康度，唔觸發警報",
    )
    lines.extend(alpha_lines)

    if degradation:
        lines.append("---")
        lines.append("## ⚠️ 策略退化警報（實盤同款模擬）")
        for d in degradation:
            lines.append(f"- {d}")
        lines.append("- 建議: 跑 walk-forward 驗證是否需要調整閾值")

    report = "\n".join(lines)
    print(report)

    # Write status file for post-run monitoring
    status = {
        "pipeline": "weekly_backtest",
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": "trend_on_live",
        "total_return_pct": live_summary.get("total_return_pct", 0),
        "total_trades": live_total_trades,
        "degradation": degradation,
        "low_sample": low_sample,
        "has_degradation": len(degradation) > 0,
        "all_ok": len(degradation) == 0,
        "alpha_reference": {
            "total_return_pct": alpha_summary.get("total_return_pct", 0),
            "note": "trend OFF, reference only, not used for alerts",
        },
    }
    status_file = Path.home() / "trading-systems" / "crypto-ai-trader" / "logs" / "weekly_backtest_status.json"
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(status, indent=2), encoding="utf-8")

    # Exit non-zero on degradation so wrapper can detect
    if status["has_degradation"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
