#!/usr/bin/env python3
"""
Verify Phase 1 Online Learner integration.

Tests:
1. Default weights (no learning data)
2. Weight computation with synthetic trades
3. Weight constraints (MIN/MAX, normalization)
4. Weight storage and retrieval
5. Market scanner integration (learned weights used)
6. Report formatting
"""

import sys
import os
import json
import time
import tempfile

sys.path.insert(0, os.path.expanduser("~/trading-systems/crypto-ai-trader"))


def test_online_learner():
    """Full verification of online learner."""

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db_path = f.name

    try:
        from src.state_db import StateDB
        from src.online_learner import (
            OnlineLearner, FACTOR_NAMES, DEFAULT_WEIGHTS,
            MIN_WEIGHT, MAX_WEIGHT, MIN_TRADES,
        )
        from src.trade_outcome_recorder import TradeOutcomeRecorder

        db = StateDB(db_path=temp_db_path)
        learner = OnlineLearner(db=db)
db = get_state_db()
    39|        recorder = TradeOutcomeRecorder(db=db)

        # 1. Default weights (no data)
        weights = learner.get_current_weights()
        assert weights == DEFAULT_WEIGHTS, f"Expected defaults, got {weights}"
        assert abs(sum(weights.values()) - 100.0) < 0.1, "Weights should sum to 100"
        print("✅ 1. Default weights: OK")

        # 2. Insufficient data → returns None
        result = learner.compute_optimal_weights()
        assert result is None, "Should return None with < 10 trades"
        print("✅ 2. Insufficient data guard: OK")

        # 3. Create synthetic trades with known correlations
        # Strategy: technical score correlates with PnL, trend score anti-correlates
        import random
        random.seed(42)

        for i in range(20):
            # Technical score: high → win, low → loss
            tech = random.uniform(60, 90) if i % 2 == 0 else random.uniform(20, 50)
            trend = random.uniform(20, 50) if i % 2 == 0 else random.uniform(60, 90)
            pnl = random.uniform(1, 8) if i % 2 == 0 else random.uniform(-8, -1)

            sym = f"TEST{i}USDT"
            recorder.record_entry(
                symbol=sym,
                entry_price=100.0,
                qty=10.0,
                score=70.0,
                strategy="trend",
                f_technical=tech,
                f_trend=trend,
                f_volume=50.0,
                f_sentiment=50.0,
                f_price_action=50.0,
                f_obv_divergence=50.0,
                f_consolidation=50.0,
                f_bb_squeeze=50.0,
                f_rsi_divergence=50.0,
                f_onchain=50.0,
                f_market_sentiment=50.0,
            )
            # Manually set PnL by updating the outcome
            conn = db._get_conn()
            entry_price = 100.0
            exit_price = entry_price * (1 + pnl / 100)
            conn.execute(
                """UPDATE trade_outcomes SET
                    status = 'closed', exit_time = ?, exit_price = ?,
                    exit_reason = 'test', net_pnl_pct = ?, is_win = ?,
                    updated_at = ?
                WHERE symbol = ? AND status = 'open'""",
                (time.time(), exit_price, pnl, 1 if pnl > 0 else 0, time.time(), sym),
            )
            conn.commit()

        print("✅ 3. Synthetic trades created: OK (20 trades)")

        # 4. Compute optimal weights
        result = learner.compute_optimal_weights()
        assert result is not None, "Should return result with 20 trades"
        assert "weights" in result
        assert "stats" in result
        assert "meta" in result
        assert result["meta"]["n_trades"] == 20

        weights = result["weights"]
        assert abs(sum(weights.values()) - 100.0) < 0.5, f"Weights sum = {sum(weights.values())}"
        for f in FACTOR_NAMES:
            assert MIN_WEIGHT <= weights[f] <= MAX_WEIGHT + 0.1, \
                f"{f} weight {weights[f]} out of range [{MIN_WEIGHT}, {MAX_WEIGHT}]"

        # Technical should have higher weight (positive correlation with PnL)
        # Trend should have lower weight (negative correlation with PnL)
        tech_corr = result["stats"]["technical"]["correlation"]
        trend_corr = result["stats"]["trend"]["correlation"]
        assert tech_corr > 0, f"Expected positive tech correlation, got {tech_corr}"
        assert trend_corr < 0, f"Expected negative trend correlation, got {trend_corr}"
        print(f"✅ 4. Weight computation: OK (tech r={tech_corr:+.3f}, trend r={trend_corr:+.3f})")

        # 5. Store and retrieve
        stored = learner.learn_and_store()
        assert stored is not None
        assert len(stored.get("changes", [])) > 0, "Expected weight changes"

        # Retrieve from DB
        retrieved = learner.get_current_weights()
        assert retrieved == weights, "Stored weights should match computed"
        print("✅ 5. Weight storage/retrieval: OK")

        # 6. Report formatting
        report = learner.format_report(result)
        assert "因子權重學習報告" in report
        assert "交易數" in report
        assert "相關性" in report
        print("✅ 6. Report formatting: OK")

        # 7. Verify weight changes are reasonable
        for f in FACTOR_NAMES:
            old = DEFAULT_WEIGHTS[f]
            new = weights[f]
            # With learning_rate=0.3, max change per cycle is ~30% of default
            max_change = old * 0.5  # generous bound
            assert abs(new - old) <= max_change + 1, \
                f"{f}: change {old:.1f}→{new:.1f} exceeds bound ±{max_change:.1f}"
        print("✅ 7. Weight bounds reasonable: OK")

        print("\n" + "=" * 50)
        print("ALL PHASE 1 VERIFICATIONS PASSED")
        print("=" * 50)
        return True

    finally:
        os.unlink(temp_db_path)


if __name__ == "__main__":
    success = test_online_learner()
    sys.exit(0 if success else 1)
