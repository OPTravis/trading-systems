#!/usr/bin/env python3
"""
Run online learning to compute optimal factor weights.

Reads closed trade outcomes, computes correlations, and stores
new weights in state.db. Can be run manually or via cron.

Usage:
    python scripts/learn_weights.py           # Run learning
    python scripts/learn_weights.py --dry-run # Preview without storing
    python scripts/learn_weights.py --report  # Show current weights + stats
"""

import sys
import os
import json
import argparse

sys.path.insert(0, os.path.expanduser("~/crypto-ai-trader"))


def main():
    parser = argparse.ArgumentParser(description="Online factor weight learning")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without storing")
    parser.add_argument("--report", action="store_true", help="Show current weights and stats")
    args = parser.parse_args()

    from src.online_learner import OnlineLearner, FACTOR_NAMES, DEFAULT_WEIGHTS
    from src.trade_outcome_recorder import TradeOutcomeRecorder

    learner = OnlineLearner()
    recorder = TradeOutcomeRecorder()

    if args.report:
        # Show current state
        weights = learner.get_current_weights()
        summary = recorder.get_summary()
        stats = recorder.get_factor_stats()

        print("=" * 60)
        print("當前因子權重")
        print("=" * 60)
        for f in FACTOR_NAMES:
            default = DEFAULT_WEIGHTS[f]
            current = weights[f]
            marker = " *" if abs(current - default) > 0.1 else ""
            print(f"  {f:20s}: {current:5.1f}% (default {default:5.1f}%){marker}")

        print(f"\n{'=' * 60}")
        print("交易統計")
        print("=" * 60)
        print(f"  開放交易: {summary['open_trades']}")
        print(f"  閉合交易: {summary['closed_trades']}")
        print(f"  勝率: {summary['win_rate']}%")
        print(f"  平均 PnL: {summary['avg_pnl_pct']:+.2f}%")

        if stats:
            print(f"\n{'=' * 60}")
            print("因子相關性")
            print("=" * 60)
            for factor, stat in stats["factors"].items():
                corr = stat["correlation"]
                indicator = "🟢" if corr > 0.1 else "🔴" if corr < -0.1 else "⚪"
                print(
                    f"  {indicator} {factor:20s}: r={corr:+.4f} "
                    f"(贏={stat['avg_winner']:.0f} 輸={stat['avg_loser']:.0f})"
                )
        return

    # Run learning
    print("正在學習因子權重...")
    result = learner.compute_optimal_weights()

    if not result:
        print("學習數據不足（需要至少 10 筆閉合交易）")
        # Show how many we have
        summary = recorder.get_summary()
        print(f"  目前: {summary['closed_trades']} 筆閉合交易")
        return

    # Show results
    print(learner.format_report(result))

    if args.dry_run:
        print("\n[DRY RUN] 權重未儲存")
        print("\n建議新權重:")
        for f, w in result["weights"].items():
            default = DEFAULT_WEIGHTS[f]
            diff = w - default
            print(f"  {f:20s}: {w:5.1f}% ({diff:+.1f}%)")
    else:
        # Store
        changes = learner.learn_and_store()
        if changes and changes.get("changes"):
            print(f"\n✅ 權重已更新（{len(changes['changes'])} 項變更）")
        else:
            print("\n✅ 權重無顯著變化")


if __name__ == "__main__":
    main()
