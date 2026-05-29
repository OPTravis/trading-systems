#!/usr/bin/env python3
"""
Verify Phase 2 Parameter Auto-Optimizer.

Tests (logic only, no real backtests):
1. Default params retrieval
2. Param storage/retrieval
3. Grid combination generation
4. Validation thresholds
5. Report formatting
"""

import sys
import os
import json
import time
import tempfile

sys.path.insert(0, os.path.expanduser("~/crypto-ai-trader"))


def test_param_optimizer():
    """Verification of param optimizer logic."""

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db_path = f.name

    try:
        from src.state_db import StateDB
        from src.param_optimizer import (
            ParamOptimizer, DEFAULT_PARAMS, SEARCH_SPACE,
            MIN_SHARPE, MIN_OOS_WIN_RATE, MIN_OOS_ROBUSTNESS, MIN_TRADES,
        )

        db = StateDB(db_path=temp_db_path)
        optimizer = ParamOptimizer(db=db)

        # 1. Default params
        params = optimizer.get_current_params()
        assert params == DEFAULT_PARAMS, f"Expected defaults, got {params}"
        assert len(params) == 7, f"Expected 7 params, got {len(params)}"
        print("✅ 1. Default params: OK")

        # 2. Store and retrieve
        test_params = {
            "rsi_oversold": 30,
            "rsi_overbought": 70,
            "stop_loss_pct": 4.0,
            "take_profit_pct": 10.0,
            "score_threshold": 60,
            "trailing_activation_atr": 2.0,
            "trailing_distance_atr": 0.3,
        }
        conn = db._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
            ("optimized_params", json.dumps(test_params), time.time()),
        )
        conn.commit()

        retrieved = optimizer.get_current_params()
        assert retrieved == test_params, f"Expected stored params, got {retrieved}"
        print("✅ 2. Param storage/retrieval: OK")

        # 3. Grid combination generation
        from itertools import product
        keys = list(SEARCH_SPACE.keys())
        values = list(SEARCH_SPACE.values())
        all_combos = list(product(*values))
        expected_count = 1
        for v in values:
            expected_count *= len(v)
        assert len(all_combos) == expected_count, \
            f"Expected {expected_count} combos, got {len(all_combos)}"
        # Each combo should have 4 values (one per param)
        for combo in all_combos[:5]:
            assert len(combo) == 4, f"Expected 4 values per combo, got {len(combo)}"
        print(f"✅ 3. Grid combinations: OK ({len(all_combos)} combos)")

        # 4. Validation thresholds
        assert MIN_SHARPE == 0.5
        assert MIN_OOS_WIN_RATE == 40.0
        assert MIN_OOS_ROBUSTNESS == 33.0
        assert MIN_TRADES == 5
        print("✅ 4. Validation thresholds: OK")

        # 5. Report formatting (with mock result)
        mock_result = {
            "status": "optimized",
            "best_params": test_params,
            "best_metrics": {
                "sharpe": 1.2,
                "win_rate": 55.0,
                "total_return_pct": 8.5,
                "max_drawdown_pct": -12.3,
                "n_trades": 15,
                "profit_factor": 1.8,
            },
            "validation": {
                "validated": True,
                "reason": "OK",
                "avg_oos_sharpe": 0.8,
                "avg_robustness": 66.7,
                "total_trades": 12,
                "oos_results": {
                    "SOL": {"oos_sharpe": 0.9, "robustness_pct": 67},
                    "ETH": {"oos_sharpe": 0.7, "robustness_pct": 67},
                },
            },
            "old_params": DEFAULT_PARAMS,
            "changes": ["rsi_oversold: 35 → 30", "take_profit_pct: 8.0 → 10.0"],
            "grid_results_count": 256,
            "timestamp": time.time(),
        }
        report = optimizer.format_report(mock_result)
        assert "參數自動優化報告" in report
        assert "已優化並存儲" in report
        assert "Sharpe" in report
        assert "OOS" in report
        assert "變更" in report
        print("✅ 5. Report formatting: OK")

        # 6. Validation failure report
        fail_result = {
            "status": "validation_failed",
            "best_params": test_params,
            "best_metrics": {"sharpe": 0.3, "win_rate": 30, "total_return_pct": -5},
            "validation": {
                "validated": False,
                "reason": "OOS Sharpe 0.30 < 0.5; OOS robustness 20% < 33%",
            },
            "old_params": DEFAULT_PARAMS,
        }
        fail_report = optimizer.format_report(fail_result)
        assert "驗證失敗" in fail_report
        assert "OOS Sharpe" in fail_report
        print("✅ 6. Validation failure report: OK")

        # 7. SEARCH_SPACE has all expected params
        expected_keys = {"rsi_oversold", "rsi_overbought", "stop_loss_pct", "take_profit_pct"}
        assert set(SEARCH_SPACE.keys()) == expected_keys
        for k, v in SEARCH_SPACE.items():
            assert len(v) >= 3, f"{k} should have at least 3 values, got {len(v)}"
        print("✅ 7. Search space: OK")

        print("\n" + "=" * 50)
        print("ALL PHASE 2 VERIFICATIONS PASSED")
        print("=" * 50)
        return True

    finally:
        os.unlink(temp_db_path)


if __name__ == "__main__":
    success = test_param_optimizer()
    sys.exit(0 if success else 1)
