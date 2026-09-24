"""Regenerate tests/fixtures/p5_ab_snapshot.json under FROZEN semantics.

Freeze contract (identical to test_wo0924_p5_unification.py):
- requests.get fails -> GARCH takes deterministic 24h-estimate fallback
- HMM cached prediction -> None (no overlay)
- CVaR overlay -> unavailable (scale stays 1.0)

Under this freeze the adaptor becomes a pure function of
(fear_greed, btc_trend, btc_price_change_24h) — the A/B parity
comparison is then exact and environment-independent.

Run: python3 tests/regen_p5_snapshot.py
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.scan_phases import _adapt_special_phase  # noqa: E402


def frozen(fn, *args, **kwargs):
    with patch("requests.get", side_effect=OSError("frozen for A/B parity")), \
         patch("src.hmm_regime.HMMRegimeDetector.get_cached_prediction",
               return_value=None), \
         patch("src.cvar_risk.CVaRRiskManager.compute_portfolio_risk",
               side_effect=OSError("frozen for A/B parity")), \
         patch("src.contextual_bandit.ContextualBandit.recommend_sltp",
               return_value=(1.0, 1.0)), \
         patch("src.contextual_bandit.ContextualBandit.recommend_size",
               return_value=0.8):
        return fn(*args, **kwargs)


FNG_GRID = [5, 10, 15, 20, 24, 25, 30, 40, 45, 50, 55, 60, 75, 80, 95]

snap = {
    "_meta": {
        "engine": "B (shared _adapt_special_phase) + reference semantics",
        "note": "regenerated post-refactor under frozen externals; the "
                "frozen contract makes engine A == engine B by "
                "construction and the parity tests pin it forever",
        "source": "src/scan_phases.py:_adapt_special_phase",
        "freeze": ["requests.get -> OSError", "HMM cached -> None",
                   "CVaR -> unavailable", "bandit sltp -> (1.0, 1.0)",
                   "bandit size -> 0.8 (cold-start, no overlay)"],
    },
    "deep_value_btc": {
        str(f): frozen(_adapt_special_phase, f, score_threshold=50,
                       cash_reserve_pct=50, max_position_pct=5)
        for f in FNG_GRID},
    "fear_accum": {
        str(f): frozen(_adapt_special_phase, f, score_threshold=50,
                       cash_reserve_pct=50, max_position_pct=5,
                       dca_size_mult=0.5)
        for f in FNG_GRID},
    "qfl_panic": {
        str(f): frozen(_adapt_special_phase, f, score_threshold=50,
                       cash_reserve_pct=60, max_position_pct=3)
        for f in FNG_GRID},
    "hash_ribbon": {
        "50": frozen(_adapt_special_phase, 50, btc_trend="NEUTRAL",
                    score_threshold=50, cash_reserve_pct=30)},
}

out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "fixtures", "p5_ab_snapshot.json")
with open(out, "w") as f:
    json.dump(snap, f, indent=1, sort_keys=True)
print("snapshot regenerated:", out, os.path.getsize(out), "bytes")
