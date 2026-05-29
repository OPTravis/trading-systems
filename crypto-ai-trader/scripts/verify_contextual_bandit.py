#!/usr/bin/env python3
"""
Verification script for Contextual Thompson Sampling Bandit module.

Tests:
  1. Cold start returns 0.8
  2. After positive PnL update, recommendations change
  3. Stats returns expected structure
  4. Persistence works (save and reload)
"""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.contextual_bandit import ContextualBandit, _discretize_fear_greed, _context_to_index, ACTION_MULTIPLIERS, DEFAULT_SIZE


def test_cold_start():
    """Test 1: Cold start returns 0.8."""
    print("TEST 1: Cold start returns 0.8 ... ", end="")
    db = _make_fresh_db()
    bandit = ContextualBandit(db=db)
    context = {
        "hmm_regime": "bull",
        "fear_greed": 50,
        "btc_trend": "BULLISH",
        "portfolio_heat": "cold",
    }
    result = bandit.recommend_size(context)
    assert result == DEFAULT_SIZE, f"Expected {DEFAULT_SIZE}, got {result}"
    print("PASS")
    return True


def test_positive_pnl_changes_recommendation():
    """Test 2: After positive PnL, recommendations should change."""
    print("TEST 2: Positive PnL changes recommendation ... ", end="")
    db = _make_fresh_db()
    bandit = ContextualBandit(db=db)
    context = {
        "hmm_regime": "bull",
        "fear_greed": 50,
        "btc_trend": "BULLISH",
        "portfolio_heat": "cold",
    }
    
    # Get initial recommendation
    initial = bandit.recommend_size(context)
    
    # Update with positive PnL on all actions EXCEPT one specific action
    # Push alpha high on action index 0 (0.3 multiplier)
    for _ in range(50):
        bandit.update_from_outcome(context, 0.3, 2.5)  # positive PnL
        bandit.update_from_outcome(context, 1.2, -1.0)  # negative PnL on aggressive
    
    # The stats should reflect the updates
    stats = bandit.get_stats()
    assert stats["total_contexts"] > 0, "No contexts recorded"
    assert stats["total_updates"] > 0, "No updates recorded"
    
    # Now recommend should favor 0.3 multiplier since it has high alpha
    # Sample many times to verify statistical preference
    recommendations = [bandit.recommend_size(context) for _ in range(100)]
    avg_rec = sum(recommendations) / len(recommendations)
    
    # After 50 positive updates on 0.3 and 50 negative on 1.2,
    # the average should be pulled toward 0.3
    assert avg_rec < 1.0, f"Expected avg < 1.0 after training 0.3, got {avg_rec}"
    print(f"PASS (avg recommendation: {avg_rec:.3f})")
    return True


def test_stats_structure():
    """Test 3: Stats returns expected structure."""
    print("TEST 3: Stats returns expected structure ... ", end="")
    db = _make_fresh_db()
    bandit = ContextualBandit(db=db)
    context = {
        "hmm_regime": "bear",
        "fear_greed": 20,
        "btc_trend": "BEARISH",
        "portfolio_heat": "hot",
    }
    
    # Add some data first
    bandit.update_from_outcome(context, 0.8, 1.5)
    bandit.update_from_outcome(context, 0.8, -0.5)
    
    stats = bandit.get_stats()
    
    # Check top-level keys
    assert "total_contexts" in stats, "Missing 'total_contexts'"
    assert "total_updates" in stats, "Missing 'total_updates'"
    assert "contexts" in stats, "Missing 'contexts'"
    assert "actions" in stats, "Missing 'actions'"
    
    # Check types
    assert isinstance(stats["total_contexts"], int), "total_contexts not int"
    assert isinstance(stats["total_updates"], int), "total_updates not int"
    assert isinstance(stats["contexts"], dict), "contexts not dict"
    assert isinstance(stats["actions"], dict), "actions not dict"
    
    # Check actions have all 5 multipliers
    assert len(stats["actions"]) == 5, f"Expected 5 actions, got {len(stats['actions'])}"
    for mult in ACTION_MULTIPLIERS:
        assert str(mult) in stats["actions"], f"Missing action {mult}"
        action_stat = stats["actions"][str(mult)]
        assert "mean_alpha" in action_stat, f"Missing mean_alpha for {mult}"
        assert "mean_beta" in action_stat, f"Missing mean_beta for {mult}"
        assert "aggregate_win_rate" in action_stat, f"Missing aggregate_win_rate for {mult}"
        assert "contexts_used" in action_stat, f"Missing contexts_used for {mult}"
    
    # Check context entry
    ctx_idx = _context_to_index(context)
    assert str(ctx_idx) in stats["contexts"], f"Missing context {ctx_idx}"
    ctx_stat = stats["contexts"][str(ctx_idx)]
    assert "priors" in ctx_stat, "Missing priors in context stat"
    assert "best_action_index" in ctx_stat, "Missing best_action_index"
    assert "best_multiplier" in ctx_stat, "Missing best_multiplier"
    assert "total_updates" in ctx_stat, "Missing total_updates in context stat"
    
    print("PASS")
    return True


def test_persistence():
    """Test 4: Persistence works (save and reload)."""
    print("TEST 4: Persistence works (save and reload) ... ", end="")
    
    db_path = "/tmp/test_bandit_persistence.db"
    import os
    if os.path.exists(db_path):
        os.remove(db_path)
    
    from src.state_db import StateDB
    
    # Phase 1: Create bandit, update it, save
    db1 = StateDB(db_path=db_path)
    bandit1 = ContextualBandit(db=db1)
    context = {
        "hmm_regime": "volatile",
        "fear_greed": 10,
        "btc_trend": "NEUTRAL",
        "portfolio_heat": "warm",
    }
    bandit1.update_from_outcome(context, 0.5, 3.0)
    bandit1.update_from_outcome(context, 0.5, 1.0)
    bandit1.update_from_outcome(context, 1.0, -2.0)
    stats1 = bandit1.get_stats()
    db1.close()
    
    # Phase 2: Create fresh bandit from same DB, verify loaded
    db2 = StateDB(db_path=db_path)
    bandit2 = ContextualBandit(db=db2)
    stats2 = bandit2.get_stats()
    
    assert stats2["total_contexts"] == stats1["total_contexts"], \
        f"Contexts mismatch: {stats2['total_contexts']} vs {stats1['total_contexts']}"
    assert stats2["total_updates"] == stats1["total_updates"], \
        f"Updates mismatch: {stats2['total_updates']} vs {stats1['total_updates']}"
    
    # Verify priors survived
    ctx_idx = _context_to_index(context)
    assert str(ctx_idx) in stats2["contexts"], "Context not persisted"
    priors = stats2["contexts"][str(ctx_idx)]["priors"]
    assert priors[1][0] == 3.0, f"Alpha for action 0.5 should be 3.0, got {priors[1][0]}"
    assert priors[1][1] == 1.0, f"Beta for action 0.5 should be 1.0, got {priors[1][1]}"
    assert priors[3][1] == 2.0, f"Beta for action 1.0 should be 2.0, got {priors[3][1]}"
    
    db2.close()
    os.remove(db_path)
    print("PASS")
    return True


def test_discretization():
    """Test 5: Discretization functions work correctly."""
    print("TEST 5: Discretization functions ... ", end="")
    
    assert _discretize_fear_greed(0) == 0, "0 -> extreme_fear"
    assert _discretize_fear_greed(15) == 0, "15 -> extreme_fear"
    assert _discretize_fear_greed(25) == 1, "25 -> fear"
    assert _discretize_fear_greed(50) == 2, "50 -> neutral"
    assert _discretize_fear_greed(70) == 3, "70 -> greed"
    assert _discretize_fear_greed(90) == 4, "90 -> extreme_greed"
    assert _discretize_fear_greed(100) == 4, "100 -> extreme_greed"
    assert _discretize_fear_greed(-5) == 0, "clamped low"
    assert _discretize_fear_greed(150) == 4, "clamped high"
    
    # Context indices should be deterministic
    ctx1 = {"hmm_regime": "bull", "fear_greed": 50, "btc_trend": "BULLISH", "portfolio_heat": "cold"}
    ctx2 = {"hmm_regime": "bull", "fear_greed": 50, "btc_trend": "BULLISH", "portfolio_heat": "cold"}
    assert _context_to_index(ctx1) == _context_to_index(ctx2), "Same context -> same index"
    
    # Different contexts should give different indices
    ctx3 = {"hmm_regime": "bear", "fear_greed": 50, "btc_trend": "BULLISH", "portfolio_heat": "cold"}
    assert _context_to_index(ctx1) != _context_to_index(ctx3), "Different context -> different index"
    
    print("PASS")
    return True


def test_multi_context_learning():
    """Test 6: Different contexts learn independently."""
    print("TEST 6: Multi-context independent learning ... ", end="")
    db = _make_fresh_db()
    bandit = ContextualBandit(db=db)
    
    ctx_bull = {"hmm_regime": "bull", "fear_greed": 50, "btc_trend": "BULLISH", "portfolio_heat": "cold"}
    ctx_bear = {"hmm_regime": "bear", "fear_greed": 20, "btc_trend": "BEARISH", "portfolio_heat": "hot"}
    
    # Train bull context to like aggressive sizing
    for _ in range(20):
        bandit.update_from_outcome(ctx_bull, 1.2, 2.0)
        bandit.update_from_outcome(ctx_bull, 0.3, -1.0)
    
    # Train bear context to like conservative sizing
    for _ in range(20):
        bandit.update_from_outcome(ctx_bear, 0.3, 2.0)
        bandit.update_from_outcome(ctx_bear, 1.2, -1.0)
    
    # Sample recommendations
    bull_recs = [bandit.recommend_size(ctx_bull) for _ in range(100)]
    bear_recs = [bandit.recommend_size(ctx_bear) for _ in range(100)]
    
    bull_avg = sum(bull_recs) / len(bull_recs)
    bear_avg = sum(bear_recs) / len(bear_recs)
    
    assert bull_avg > bear_avg, f"Bull avg ({bull_avg:.3f}) should > Bear avg ({bear_avg:.3f})"
    
    stats = bandit.get_stats()
    assert stats["total_contexts"] == 2, f"Expected 2 contexts, got {stats['total_contexts']}"
    print(f"PASS (bull_avg={bull_avg:.3f}, bear_avg={bear_avg:.3f})")
    return True


def _make_fresh_db():
    """Create a fresh in-memory StateDB for testing."""
    import tempfile
    import os
    db_path = tempfile.mktemp(suffix=".db")
    from src.state_db import StateDB
    return StateDB(db_path=db_path)


def main():
    print("=" * 60)
    print("Contextual Thompson Sampling Bandit - Verification")
    print("=" * 60)
    print()
    
    tests = [
        test_cold_start,
        test_positive_pnl_changes_recommendation,
        test_stats_structure,
        test_persistence,
        test_discretization,
        test_multi_context_learning,
    ]
    
    passed = 0
    failed = 0
    
    for test_fn in tests:
        try:
            if test_fn():
                passed += 1
        except Exception as e:
            print(f"FAIL: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    
    print()
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)} tests")
    print("=" * 60)
    
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
