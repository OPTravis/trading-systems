#!/usr/bin/env python3
"""
Run parameter auto-optimization.

Grid search over key trading parameters, validate with walk-forward
OOS testing, and store optimized params in state.db.

Usage:
    python scripts/optimize_params.py              # Full optimization
    python scripts/optimize_params.py --dry-run    # Preview without storing
    python scripts/optimize_params.py --report     # Show current params
    python scripts/optimize_params.py --symbols SOL ETH  # Specific symbols
"""

import sys
import os
import argparse
import logging

sys.path.insert(0, os.path.expanduser("~/crypto-ai-trader"))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Parameter auto-optimization")
    parser.add_argument("--dry-run", action="store_true", help="Preview without storing")
    parser.add_argument("--report", action="store_true", help="Show current params")
    parser.add_argument("--symbols", nargs="+", default=None, help="Symbols to optimize")
    parser.add_argument("--days", type=int, default=90, help="Backtest days")
    args = parser.parse_args()

    from src.param_optimizer import ParamOptimizer, DEFAULT_PARAMS, SEARCH_SPACE

    optimizer = ParamOptimizer()

    if args.report:
        params = optimizer.get_current_params()
        print("=" * 60)
        print("當前參數")
        print("=" * 60)
        for k, v in params.items():
            default = DEFAULT_PARAMS.get(k)
            marker = " *" if default is not None and v != default else ""
            print(f"  {k:30s}: {v}{marker}")
        print(f"\n  * = 已偏離默認值")
        return

    # Run optimization
    print("=" * 60)
    print("參數自動優化")
    print("=" * 60)

    if args.dry_run:
        # Grid search only, don't store
        print("\n[DRY RUN] 正在搜索最佳參數...")
        grid_results = optimizer.grid_search(
            symbols=args.symbols,
            days=args.days,
        )

        if not grid_results:
            print("搜索失敗：無結果")
            return

        print(f"\n搜索完成：{len(grid_results)} 個組合")
        print("\nTop 5 結果:")
        for i, r in enumerate(grid_results[:5]):
            p = r["params"]
            m = r["metrics"]
            print(
                f"  #{i+1}: Sharpe={m['sharpe']:.2f} "
                f"WR={m['win_rate']:.0f}% "
                f"Return={m['total_return_pct']:+.1f}% "
                f"RSI={p['rsi_oversold']}/{p['rsi_overbought']} "
                f"SL={p['stop_loss_pct']}% TP={p['take_profit_pct']}%"
            )

        # Validate best
        best = grid_results[0]
        print(f"\n正在驗證最佳參數 (walk-forward)...")
        validation = optimizer.validate_best(best["params"], args.symbols)

        print(f"\n驗證結果: {'✅ 通過' if validation['validated'] else '❌ 失敗'}")
        print(f"  OOS Sharpe: {validation['avg_oos_sharpe']:.3f}")
        print(f"  穩健性: {validation['avg_robustness']:.0f}%")
        if not validation["validated"]:
            print(f"  原因: {validation['reason']}")

        print("\n[DRY RUN] 參數未存儲")
    else:
        # Full optimization + store
        result = optimizer.optimize_and_store(
            symbols=args.symbols,
            search_space=SEARCH_SPACE,
        )

        if result:
            print(optimizer.format_report(result))
        else:
            print("優化失敗：無有效結果")


if __name__ == "__main__":
    main()
