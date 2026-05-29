#!/usr/bin/env python3
"""
Verify Phase 0 trade outcome recorder integration.

Tests:
1. Table creation in state.db
2. Entry recording
3. Price extreme tracking
4. Outcome recording (close)
5. Factor stats computation
6. Summary generation
7. Sync script import

Assertions verify actual behavior, not just "no crash".
"""

import sys
import os
import json
import time
import tempfile

sys.path.insert(0, os.path.expanduser("~/crypto-ai-trader"))


def test_outcome_recorder():
    """Full verification of trade outcome recorder."""

    # Use temp DB to avoid polluting real state.db
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db_path = f.name

    try:
        from src.state_db import StateDB
        from src.trade_outcome_recorder import TradeOutcomeRecorder

        # 1. Table creation
        db = StateDB(db_path=temp_db_path)
        conn = db._get_conn()
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trade_outcomes'"
        ).fetchall()]
        assert "trade_outcomes" in tables, "trade_outcomes table not created"
        print("✅ 1. Table creation: OK")

        # 2. Entry recording
db = get_state_db()
    47|        recorder = TradeOutcomeRecorder(db=db)
        row_id = recorder.record_entry(
            symbol="TESTUSDT",
            entry_price=100.0,
            qty=10.0,
            score=75.0,
            strategy="trend",
            f_technical=80.0,
            f_trend=70.0,
            f_volume=65.0,
            regime="FEAR",
            fng_score=25,
            fng_label="Extreme Fear",
            btc_trend="BEARISH",
            kelly_pct=15.0,
            kelly_win_rate=60.0,
            kelly_confidence="Half-Kelly (6 trades)",
            stop_loss_pct=3.0,
            tp1_pct=5.0,
            tp2_pct=8.0,
            tp3_pct=12.0,
            max_hold_hours=72,
            research_adj=-2.0,
            bear_score=45.0,
            bear_veto=False,
        )
        assert row_id > 0, f"Expected positive row_id, got {row_id}"

        # Verify entry data
        open_entries = recorder.get_open_entries()
        assert len(open_entries) == 1, f"Expected 1 open entry, got {len(open_entries)}"
        entry = open_entries[0]
        assert entry["symbol"] == "TESTUSDT"
        assert entry["entry_price"] == 100.0
        assert entry["qty"] == 10.0
        assert entry["score"] == 75.0
        assert entry["strategy"] == "trend"
        assert entry["status"] == "open"
        factors = json.loads(entry["factors_json"])
        assert factors["technical"] == 80.0
        assert factors["trend"] == 70.0
        context = json.loads(entry["context_json"])
        assert context["regime"] == "FEAR"
        assert context["fng_score"] == 25
        assert context["bear_veto"] == False
        print("✅ 2. Entry recording: OK")

        # 3. Price extreme tracking
        recorder.update_price_extremes("TESTUSDT", 105.0)  # peak
        recorder.update_price_extremes("TESTUSDT", 97.0)   # trough
        recorder.update_price_extremes("TESTUSDT", 103.0)  # neither

        open_entries = recorder.get_open_entries()
        entry = open_entries[0]
        assert entry["peak_price"] == 105.0, f"Expected peak 105.0, got {entry['peak_price']}"
        assert entry["trough_price"] == 97.0, f"Expected trough 97.0, got {entry['trough_price']}"
        print("✅ 3. Price extreme tracking: OK")

        # 4. Outcome recording (close)
        outcome = recorder.record_outcome(
            symbol="TESTUSDT",
            exit_price=108.0,
            exit_reason="tp1",
        )
        assert outcome is not None, "Expected outcome dict, got None"
        assert outcome["symbol"] == "TESTUSDT"
        assert outcome["exit_reason"] == "tp1"
        assert outcome["is_win"] == True
        assert outcome["pnl_pct"] > 0, f"Expected positive PnL, got {outcome['pnl_pct']}"
        assert outcome["max_profit_pct"] >= 5.0, f"Expected max_profit >= 5%, got {outcome['max_profit_pct']}"
        assert outcome["max_drawdown_pct"] <= -3.0, f"Expected max_drawdown <= -3%, got {outcome['max_drawdown_pct']}"
        assert outcome["time_held_hours"] >= 0
        print(f"✅ 4. Outcome recording: OK (pnl={outcome['net_pnl_pct']:+.2f}%, "
              f"max_profit={outcome['max_profit_pct']:+.2f}%, "
              f"max_dd={outcome['max_drawdown_pct']:+.2f}%)")

        # Verify no more open entries
        open_entries = recorder.get_open_entries()
        assert len(open_entries) == 0, f"Expected 0 open entries, got {len(open_entries)}"

        # Verify closed outcome in DB
        closed = recorder.get_closed_outcomes()
        assert len(closed) == 1
        assert closed[0]["status"] == "closed"
        assert closed[0]["is_win"] == 1
        print("✅ 5. Outcome persistence: OK")

        # 5. Record a losing trade for stats
        recorder.record_entry(
            symbol="LOSSUSDT",
            entry_price=50.0,
            qty=20.0,
            score=65.0,
            strategy="dca",
            f_technical=40.0,
            f_trend=30.0,
            f_volume=55.0,
        )
        recorder.update_price_extremes("LOSSUSDT", 48.0)
        recorder.record_outcome(
            symbol="LOSSUSDT",
            exit_price=47.0,
            exit_reason="sl",
        )

        # 6. Factor stats
        stats = recorder.get_factor_stats(min_trades=2)
        assert stats is not None, "Expected stats dict"
        assert stats["total_trades"] == 2
        assert stats["winners"] == 1
        assert stats["losers"] == 1
        assert stats["win_rate"] == 50.0
        assert "technical" in stats["factors"]
        assert "trend" in stats["factors"]
        print(f"✅ 6. Factor stats: OK ({stats['total_trades']} trades, "
              f"win_rate={stats['win_rate']}%)")

        # 7. Summary
        summary = recorder.get_summary()
        assert summary["open_trades"] == 0
        assert summary["closed_trades"] == 2
        assert summary["win_rate"] == 50.0
        assert summary["best_trade"]["symbol"] == "TESTUSDT"
        assert summary["worst_trade"]["symbol"] == "LOSSUSDT"
        print(f"✅ 7. Summary: OK (closed={summary['closed_trades']}, "
              f"win_rate={summary['win_rate']}%)")

        # 8. Sync script import
        from scripts.sync_trade_outcomes import sync_outcomes
        assert callable(sync_outcomes)
        print("✅ 8. Sync script import: OK")

        print("\n" + "=" * 50)
        print("ALL PHASE 0 VERIFICATIONS PASSED")
        print("=" * 50)
        return True

    finally:
        os.unlink(temp_db_path)


if __name__ == "__main__":
    success = test_outcome_recorder()
    sys.exit(0 if success else 1)
