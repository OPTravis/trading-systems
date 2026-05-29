#!/usr/bin/env python3
"""Verification script for Adaptive Trailing Stop module."""

import sys
sys.path.insert(0, "/home/travis/crypto-ai-trader/src")

from adaptive_trailing import AdaptiveTrailingStop


def main():
    ats = AdaptiveTrailingStop()
    entry = 100.0
    errors = 0

    # --- Test step boundaries ---
    # 1.9% profit: no trailing
    r = ats.calculate_trailing_sl(entry, 101.9, 101.9, 99.0)
    assert r["trailing_active"] is False and r["step"] == "no_trail", f"1.9%: {r}"
    print("PASS: 1.9% profit -> no trail")

    # 2% profit: trail at 3% below peak, but clamped by min profit lock
    r = ats.calculate_trailing_sl(entry, 102.0, 102.0, 99.0)
    raw_sl = 102.0 * (1 - 0.03)  # 98.94
    min_sl = entry * 1.005  # 100.5
    expected_sl = max(raw_sl, min_sl)
    assert r["trailing_active"] is True and r["step"] == "step_2_5", f"2%: {r}"
    assert abs(r["trailing_sl"] - expected_sl) < 0.01, f"2% SL: {r['trailing_sl']} vs {expected_sl}"
    print(f"PASS: 2% profit -> trail at 3% below peak, clamped to min (SL={r['trailing_sl']:.2f})")

    # 4.9% profit: still 3% step
    r = ats.calculate_trailing_sl(entry, 104.9, 104.9, 99.0)
    assert r["step"] == "step_2_5", f"4.9%: {r}"
    print("PASS: 4.9% profit -> step_2_5")

    # 5% profit: trail at 1.5% below peak
    r = ats.calculate_trailing_sl(entry, 105.0, 105.0, 99.0)
    expected_sl = 105.0 * (1 - 0.015)  # 103.425
    assert r["trailing_active"] is True and r["step"] == "step_5_10", f"5%: {r}"
    assert abs(r["trailing_sl"] - expected_sl) < 0.01, f"5% SL: {r['trailing_sl']}"
    print(f"PASS: 5% profit -> trail at 1.5% below peak (SL={r['trailing_sl']:.2f})")

    # 9.9% profit: still 1.5% step
    r = ats.calculate_trailing_sl(entry, 109.9, 109.9, 99.0)
    assert r["step"] == "step_5_10", f"9.9%: {r}"
    print("PASS: 9.9% profit -> step_5_10")

    # 10% profit: trail at 1% below peak
    r = ats.calculate_trailing_sl(entry, 110.0, 110.0, 99.0)
    expected_sl = 110.0 * (1 - 0.01)  # 108.9
    assert r["trailing_active"] is True and r["step"] == "step_10_plus", f"10%: {r}"
    assert abs(r["trailing_sl"] - expected_sl) < 0.01, f"10% SL: {r['trailing_sl']}"
    print(f"PASS: 10% profit -> trail at 1% below peak (SL={r['trailing_sl']:.2f})")

    # 15% profit: trail at 1% below peak
    r = ats.calculate_trailing_sl(entry, 115.0, 115.0, 99.0)
    assert r["step"] == "step_10_plus", f"15%: {r}"
    print("PASS: 15% profit -> step_10_plus")

    # --- Test SL never decreases ---
    assert ats.should_update_sl(100.0, 101.0) is True, "should_update_sl 100->101"
    assert ats.should_update_sl(101.0, 100.0) is False, "should_update_sl 101->100"
    assert ats.should_update_sl(100.0, 100.0) is False, "should_update_sl equal"
    print("PASS: should_update_sl monotonic")

    # --- Test minimum profit lock ---
    # With very wide initial SL below min_sl, it should clamp up
    r = ats.calculate_trailing_sl(entry, 105.0, 105.0, 99.0)
    min_sl = entry * (1 + ats.MIN_PROFIT_LOCK_PCT)  # 100.5
    assert r["trailing_sl"] >= min_sl, f"Min lock failed: {r['trailing_sl']} < {min_sl}"
    print(f"PASS: minimum profit lock SL={r['trailing_sl']:.2f} >= {min_sl:.2f}")

    # --- Test volatility adjustment ---
    r_base = ats.calculate_trailing_sl(entry, 110.0, 110.0, 99.0, 1.0)
    r_wide = ats.calculate_trailing_sl(entry, 110.0, 110.0, 99.0, 1.5)
    r_tight = ats.calculate_trailing_sl(entry, 110.0, 110.0, 99.0, 0.7)
    # High vol -> wider trail -> lower SL
    assert r_wide["trailing_sl"] < r_base["trailing_sl"], f"1.5x should be wider: {r_wide['trailing_sl']} vs {r_base['trailing_sl']}"
    # Low vol -> tighter trail -> higher SL
    assert r_tight["trailing_sl"] > r_base["trailing_sl"], f"0.7x should be tighter: {r_tight['trailing_sl']} vs {r_base['trailing_sl']}"
    print(f"PASS: volatility adjustment (base={r_base['trailing_sl']:.2f}, 1.5x={r_wide['trailing_sl']:.2f}, 0.7x={r_tight['trailing_sl']:.2f})")

    # --- Test get_step_description ---
    assert "No trailing" in ats.get_step_description(0.01)
    assert "3%" in ats.get_step_description(0.03)
    assert "1.5%" in ats.get_step_description(0.07)
    assert "1%" in ats.get_step_description(0.15)
    print("PASS: get_step_description")

    print("\n✅ All tests passed!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
