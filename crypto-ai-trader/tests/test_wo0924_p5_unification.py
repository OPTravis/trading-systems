"""WO-0924 batch 1B / P5: strategy-engine unification A/B parity.

Engine A (reference): the four hand-written adapt+override sequences
that lived in scan_phases special phases before the P5 refactor,
reproduced verbatim below.

Engine B (live): the shared scan_phases._adapt_special_phase helper.

Freeze contract (environment-independent parity):
- requests.get fails -> GARCH deterministic 24h-estimate fallback
- HMM cached prediction -> None (no overlay)
- CVaR overlay -> unavailable (scale stays 1.0)
- bandit sltp/size recommendations pinned to cold-start neutrals
  ((1.0, 1.0) / 0.8 — the no-priors values, no learning overlay)

Contract under test: for every F&G grid point across all four special
phases, A == B == archived snapshot (tests/fixtures/p5_ab_snapshot.json).
The unification is structural, NOT behavioral. Strategy selection stays
in StrategyRegistry (research_phase); StrategyAdaptor is parameter
provider only.
"""
import json
import os
import sys
from contextlib import ExitStack
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "p5_ab_snapshot.json")
SNAP = json.load(open(FIXTURE))
FNG_GRID = [int(k) for k in SNAP["deep_value_btc"]]


def _frozen(fn, *args, **kwargs):
    with ExitStack() as st:
        st.enter_context(patch("requests.get",
                               side_effect=OSError("frozen for A/B parity")))
        st.enter_context(patch(
            "src.hmm_regime.HMMRegimeDetector.get_cached_prediction",
            return_value=None))
        st.enter_context(patch(
            "src.cvar_risk.CVaRRiskManager.compute_portfolio_risk",
            side_effect=OSError("frozen for A/B parity")))
        st.enter_context(patch(
            "src.contextual_bandit.ContextualBandit.recommend_sltp",
            return_value=(1.0, 1.0)))
        st.enter_context(patch(
            "src.contextual_bandit.ContextualBandit.recommend_size",
            return_value=0.8))
        return fn(*args, **kwargs)


# ── Engine A: verbatim pre-refactor hand-written sequences ────────────

def _a_deep_value(fng):
    from src.strategy_adaptor import StrategyAdaptor
    adaptor = StrategyAdaptor()
    adapted = adaptor.adapt(fear_greed=fng, btc_trend="BEARISH",
                            btc_price_change_24h=0)
    adapted["global"]["score_threshold"] = 50
    adapted["global"]["cash_reserve_pct"] = 50
    adapted["global"]["max_position_pct"] = 5
    return adapted


def _a_fear(fng):
    from src.strategy_adaptor import StrategyAdaptor
    adaptor = StrategyAdaptor()
    adapted = adaptor.adapt(fear_greed=fng, btc_trend="BEARISH",
                            btc_price_change_24h=0)
    adapted["global"]["score_threshold"] = 50
    adapted["global"]["cash_reserve_pct"] = 50
    adapted["global"]["max_position_pct"] = 5
    if "dca" in adapted.get("strategies", {}):
        adapted["strategies"]["dca"]["size_multiplier"] = 0.5
    return adapted


def _a_qfl(fng):
    from src.strategy_adaptor import StrategyAdaptor
    adaptor = StrategyAdaptor()
    adapted = adaptor.adapt(fear_greed=fng, btc_trend="BEARISH",
                            btc_price_change_24h=0)
    adapted["global"]["score_threshold"] = 50
    adapted["global"]["cash_reserve_pct"] = 60
    adapted["global"]["max_position_pct"] = 3
    return adapted


def _a_hash():
    from src.strategy_adaptor import StrategyAdaptor
    adaptor = StrategyAdaptor()
    adapted = adaptor.adapt(fear_greed=50, btc_trend="NEUTRAL",
                            btc_price_change_24h=0)
    adapted["global"]["score_threshold"] = 50
    adapted["global"]["cash_reserve_pct"] = 30
    return adapted


# ── Engine B: the shared helper ───────────────────────────────────────

def _b_deep_value(fng):
    from src.scan_phases import _adapt_special_phase
    return _frozen(_adapt_special_phase, fng, score_threshold=50,
                   cash_reserve_pct=50, max_position_pct=5)


def _b_fear(fng):
    from src.scan_phases import _adapt_special_phase
    return _frozen(_adapt_special_phase, fng, score_threshold=50,
                   cash_reserve_pct=50, max_position_pct=5,
                   dca_size_mult=0.5)


def _b_qfl(fng):
    from src.scan_phases import _adapt_special_phase
    return _frozen(_adapt_special_phase, fng, score_threshold=50,
                   cash_reserve_pct=60, max_position_pct=3)


def _b_hash():
    from src.scan_phases import _adapt_special_phase
    return _frozen(_adapt_special_phase, 50, btc_trend="NEUTRAL",
                   score_threshold=50, cash_reserve_pct=30)


class TestABParity:
    """A == B == archived snapshot, for all four special phases."""

    @pytest.mark.parametrize("fng", FNG_GRID)
    def test_deep_value(self, fng):
        a, b = _frozen(_a_deep_value, fng), _b_deep_value(fng)
        assert a == b == SNAP["deep_value_btc"][str(fng)]

    @pytest.mark.parametrize("fng", FNG_GRID)
    def test_fear_accum(self, fng):
        a, b = _frozen(_a_fear, fng), _b_fear(fng)
        assert a == b == SNAP["fear_accum"][str(fng)]

    @pytest.mark.parametrize("fng", FNG_GRID)
    def test_qfl_panic(self, fng):
        a, b = _frozen(_a_qfl, fng), _b_qfl(fng)
        assert a == b == SNAP["qfl_panic"][str(fng)]

    def test_hash_ribbon(self):
        a, b = _frozen(_a_hash), _b_hash()
        assert a == b == SNAP["hash_ribbon"]["50"]


class TestUnificationInvariants:
    def test_no_handwritten_override_sequences_left(self):
        """The four duplicated adapt+override blocks are gone from scan_phases."""
        src = open("src/scan_phases.py").read()
        assert src.count('adapted["global"]["score_threshold"]') == 1  # helper only
        assert src.count("_adapt_special_phase(") == 5  # 1 def + 4 call sites

    def test_adaptor_is_parameter_provider_only(self):
        """Adaptor docstring declares its P5 role: parameters, not selection."""
        from src.strategy_adaptor import StrategyAdaptor
        assert "parameter provider" in StrategyAdaptor.__doc__.lower()

    def test_registry_owns_strategy_selection(self):
        """research_phase routes strategy selection via StrategyRegistry."""
        src = open("src/research_phase.py").read()
        assert "StrategyRegistry" in src
        assert "select_best" in src

    def test_main_scan_adapts_via_provider_face(self):
        """Main scan keeps its multi-input adapt call (market-adaptive),
        consuming regime/threshold/params — the provider contract."""
        src = open("src/scan_phases.py").read()
        assert "dynamic_threshold = global_cfg[\"score_threshold\"]" in src
