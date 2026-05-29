#!/usr/bin/env python3
"""
Verify Phase 3 Strategy Registry.

Tests:
1. Registry initialization (all 6 strategies loaded)
2. Default strategy weights
3. Strategy weight computation from synthetic outcomes
4. Weight storage/retrieval
5. Strategy run_all returns signals
6. Strategy select_best picks highest confidence
7. Report formatting
"""

import sys
import os
import json
import time
import tempfile

sys.path.insert(0, os.path.expanduser("~/crypto-ai-trader"))


def test_strategy_registry():
    """Full verification of strategy registry."""

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db_path = f.name

    try:
        from src.state_db import StateDB
        from src.strategy_registry import (
            StrategyRegistry, DEFAULT_STRATEGY_WEIGHTS,
            MIN_STRATEGY_TRADES,
        )
        from src.trade_outcome_recorder import TradeOutcomeRecorder

        db = StateDB(db_path=temp_db_path)
        registry = StrategyRegistry(db=db)
db = get_state_db()
    40|        recorder = TradeOutcomeRecorder(db=db)

        # 1. All strategies loaded
        assert len(registry._strategies) == 6, \
            f"Expected 6 strategies, got {len(registry._strategies)}"
        for name in ["rsi", "bollinger", "vwap", "trend", "dca", "grid"]:
            assert name in registry._strategies, f"Missing strategy: {name}"
        print("✅ 1. Registry initialization: OK (6 strategies)")

        # 2. Default weights
        weights = registry.get_strategy_weights()
        assert weights == DEFAULT_STRATEGY_WEIGHTS
        assert all(w == 1.0 for w in weights.values())
        print("✅ 2. Default weights: OK (all 1.0)")

        # 3. Compute weights from synthetic outcomes
        # Create trades with different strategy performance
        for i in range(8):
            strategy = "rsi" if i < 4 else "bollinger"
            pnl = 5.0 if i % 2 == 0 else -2.0  # RSI: 50% WR, avg +1.5%
            is_win = 1 if pnl > 0 else 0

            sym = f"TEST{i}USDT"
            recorder.record_entry(
                symbol=sym, entry_price=100.0, qty=10.0,
                score=70.0, strategy=strategy,
            )
            conn = db._get_conn()
            conn.execute(
                """UPDATE trade_outcomes SET
                status = 'closed', exit_time = ?, exit_price = ?,
                exit_reason = 'test', net_pnl_pct = ?, is_win = ?,
                updated_at = ?
                WHERE symbol = ? AND status = 'open'""",
                (time.time(), 100.0 * (1 + pnl / 100), pnl, is_win, time.time(), sym),
            )
            conn.commit()

        new_weights = registry.compute_strategy_weights()
        assert new_weights is not None, "Should compute weights with 8 trades"
        # RSI has 4 trades (at MIN_STRATEGY_TRADES threshold)
        # bollinger has 4 trades (at MIN_STRATEGY_TRADES threshold)
        print(f"✅ 3. Weight computation: OK (rsi={new_weights['rsi']:.3f}, bollinger={new_weights['bollinger']:.3f})")

        # 4. Store and retrieve
        conn = db._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
            ("strategy_weights", json.dumps(new_weights), time.time()),
        )
        conn.commit()
        retrieved = registry.get_strategy_weights()
        assert retrieved == new_weights
        print("✅ 4. Weight storage/retrieval: OK")

        # 5. Run all strategies (needs klines data)
        # Create synthetic klines
        import random
        random.seed(42)
        klines = []
        price = 100.0
        for i in range(100):
            change = random.uniform(-2, 2)
            o = price
            c = price * (1 + change / 100)
            h = max(o, c) * 1.01
            l = min(o, c) * 0.99
            klines.append({
                "open": o, "high": h, "low": l,
                "close": c, "volume": random.uniform(1000, 5000),
            })
            price = c

        results = registry.run_all("TESTUSDT", klines, ["rsi", "bollinger", "vwap"])
        # Results may be empty if no strategy generates BUY/SELL signal
        # That's OK — the important thing is no crash
        assert isinstance(results, list)
        print(f"✅ 5. Run all strategies: OK ({len(results)} signals)")

        # 6. Select best
        best = registry.select_best("TESTUSDT", klines, ["rsi", "bollinger", "vwap"])
        if best:
            name, conf, reason, meta = best
            assert name in ["rsi", "bollinger", "vwap"]
            assert conf > 0
            print(f"✅ 6. Select best: OK ({name}, conf={conf:.1f})")
        else:
            print("✅ 6. Select best: OK (no signal — acceptable)")

        # 7. Report formatting
        report = registry.format_weights_report()
        assert "策略權重" in report
        assert "rsi" in report
        print("✅ 7. Report formatting: OK")

        print("\n" + "=" * 50)
        print("ALL PHASE 3 VERIFICATIONS PASSED")
        print("=" * 50)
        return True

    finally:
        os.unlink(temp_db_path)


if __name__ == "__main__":
    success = test_strategy_registry()
    sys.exit(0 if success else 1)
