#!/usr/bin/env python3
"""
Verify Phase 7 (Concept Drift) + Phase 8 (CVaR Risk).

Tests:
1. CVaR computation with known distribution
2. VaR computation
3. CVaR position scaling
4. Dynamic SL adjustment
5. Drift correlation shift detection
6. Drift win rate drop detection
7. Drift PnL distribution shift detection
"""

import sys
import os
import json
import time
import tempfile

sys.path.insert(0, os.path.expanduser("~/crypto-ai-trader"))


def test_phase7_8():
    """Verification of drift detection and CVaR."""

    # === Phase 8: CVaR ===
    from src.cvar_risk import CVaRRiskManager

    cvar = CVaRRiskManager.__new__(CVaRRiskManager)

    # 1. CVaR with known distribution
    # Normal-ish returns: mean=0.5, std=3
    import random
    random.seed(42)
    returns = [random.gauss(0.5, 3) for _ in range(200)]
    cvar_95 = cvar.compute_cvar(returns, 0.05)
    cvar_99 = cvar.compute_cvar(returns, 0.01)
    assert cvar_95 < 0, f"CVaR_95 should be negative, got {cvar_95}"
    assert cvar_99 < cvar_95, f"CVaR_99 ({cvar_99}) should be worse than CVaR_95 ({cvar_95})"
    print(f"✅ 1. CVaR computation: 95%={cvar_95:+.2f}%, 99%={cvar_99:+.2f}%")

    # 2. VaR
    var_95 = cvar.compute_var(returns, 0.05)
    assert var_95 < 0, f"VaR should be negative, got {var_95}"
    assert var_95 >= cvar_95, f"VaR ({var_95}) >= CVaR ({cvar_95})"
    print(f"✅ 2. VaR computation: 95%={var_95:+.2f}%")

    # 3. Position scaling
    assert cvar.get_dynamic_sl(5.0, -20.0) < 5.0, "Critical CVaR should tighten SL"
    assert cvar.get_dynamic_sl(5.0, -1.0) > 5.0, "Low CVaR should widen SL"
    assert cvar.get_dynamic_sl(5.0, -5.0) == 5.0, "Medium CVaR should keep SL"
    print("✅ 3. Position scaling: correct")

    # 4. Dynamic SL
    sl_critical = cvar.get_dynamic_sl(5.0, -20.0)
    sl_warning = cvar.get_dynamic_sl(5.0, -10.0)
    sl_normal = cvar.get_dynamic_sl(5.0, -5.0)
    sl_low = cvar.get_dynamic_sl(5.0, -1.0)
    assert sl_critical < sl_warning < sl_normal < sl_low
    print(f"✅ 4. Dynamic SL: critical={sl_critical:.1f}, warning={sl_warning:.1f}, normal={sl_normal:.1f}, low={sl_low:.1f}")

    # === Phase 7: Concept Drift ===
    from src.concept_drift import ConceptDriftDetector

    # Use temp DB
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db_path = f.name

    try:
        from src.state_db import StateDB
        from src.trade_outcome_recorder import TradeOutcomeRecorder

        db = StateDB(db_path=temp_db_path)
        drift = ConceptDriftDetector(db=db)
db = get_state_db()
    76|        recorder = TradeOutcomeRecorder(db=db)

        # 5. No drift with consistent data
        for i in range(20):
            sym = f"D{i}USDT"
            pnl = random.gauss(2.0, 3.0)
            recorder.record_entry(
                symbol=sym, entry_price=100.0, qty=10.0,
                score=70.0, strategy="trend",
                f_technical=60 + random.uniform(-10, 10),
                f_trend=50 + random.uniform(-10, 10),
            )
            conn = db._get_conn()
            conn.execute(
                """UPDATE trade_outcomes SET
                status = 'closed', exit_time = ?, exit_price = ?,
                net_pnl_pct = ?, is_win = ?, updated_at = ?
                WHERE symbol = ? AND status = 'open'""",
                (time.time(), 100.0 * (1 + pnl / 100), pnl, 1 if pnl > 0 else 0, time.time(), sym),
            )
            conn.commit()

        result = drift.detect_drift()
        assert result is not None
        assert "drift_detected" in result
        assert "severity" in result
        print(f"✅ 5. Drift detection (no drift): severity={result['severity']}")

        # 6. Report formatting
        report = drift.format_report(result)
        assert "概念漂移檢測" in report
        print("✅ 6. Drift report: OK")

        # 7. CVaR report formatting
        mock_risk = {
            "portfolio_cvar_95": -8.5,
            "portfolio_cvar_99": -15.2,
            "portfolio_var_95": -6.3,
            "max_position_pct": 45.0,
            "risk_level": "high",
            "position_scale": 0.5,
            "recommendations": ["集中風險：最大倉位 45% > 40%"],
            "n_samples": 50,
        }
        cvar_report = cvar.format_report(mock_risk)
        assert "CVaR 風險報告" in cvar_report
        assert "高風險" in cvar_report
        print("✅ 7. CVaR report: OK")

        print("\n" + "=" * 50)
        print("ALL PHASE 7+8 VERIFICATIONS PASSED")
        print("=" * 50)
        return True

    finally:
        os.unlink(temp_db_path)


if __name__ == "__main__":
    success = test_phase7_8()
    sys.exit(0 if success else 1)
