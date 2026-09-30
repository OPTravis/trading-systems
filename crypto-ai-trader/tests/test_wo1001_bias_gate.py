"""WO-1001-3: bias gate — every strategy under src/strategies must pass
lookahead + repaint detection. New strategy files are picked up
automatically (module scan), so CI blocks unvetted strategies."""
import pytest

from src.bias_analysis import (
    BiasReport, detect_indicator_repaint, detect_signal_lookahead,
    discover_strategies, make_synthetic_klines, run_bias_gate,
)
from src.strategies.base import BaseStrategy, SignalType, StrategySignal


# ── detector self-proof: intentionally broken strategies MUST fail ────

class LookaheadCheat(BaseStrategy):
    """Ignores idx — always decides on the final bar (uses the future)."""
    def __init__(self, params=None):
        super().__init__("LookaheadCheat", params or {})

    def analyze(self, symbol, klines, position=None, idx=None):
        last, first = klines[-1]["close"], klines[0]["close"]
        sig = SignalType.BUY if last > first * 1.10 else SignalType.WAIT
        return StrategySignal(sig, 50, f"pct={(last/first-1)*100:.2f}", {})

    def get_parameters(self):
        return {}


class RepaintCheat(BaseStrategy):
    """Respects idx for bars but normalizes over the WHOLE series."""
    def __init__(self, params=None):
        super().__init__("RepaintCheat", params or {})

    def analyze(self, symbol, klines, position=None, idx=None):
        view = klines[:idx] if idx is not None else klines
        closes = [k["close"] for k in klines]          # full series — repaints
        mx = max(closes)
        rel = view[-1]["close"] / mx if mx else 0.5
        return StrategySignal(
            SignalType.WAIT, rel * 100, f"rel={rel:.6f}", {"rel": rel})

    def get_parameters(self):
        return {}


def test_detector_catches_lookahead_cheat():
    rep = detect_signal_lookahead(LookaheadCheat())
    assert isinstance(rep, BiasReport) and not rep.ok
    assert rep.mismatches >= 1


def test_detector_catches_repaint_cheat():
    rep = detect_indicator_repaint(RepaintCheat())
    assert not rep.ok and rep.mismatches >= 1


def test_clean_strategy_passes_both():
    reps = {r.detector: r for r in run_bias_gate(strategy_cls=LookaheadCheat)}
    # the honest reference: Bollinger — must pass both detectors
    from src.strategies import BollingerStrategy
    inst = BollingerStrategy({})
    assert detect_signal_lookahead(inst).ok
    assert detect_indicator_repaint(inst).ok


def test_synthetic_klines_deterministic():
    a = make_synthetic_klines(seed=7)
    b = make_synthetic_klines(seed=7)
    assert a == b and len(a) == 300
    c = make_synthetic_klines(seed=8)
    assert a[100]["close"] != c[100]["close"]


def test_discover_finds_all_six_builtin_strategies():
    names = {c.__name__ for c in discover_strategies()}
    assert {"BollingerStrategy", "DCAStrategy", "GridStrategy",
            "RSIStrategy", "TrendStrategy", "VWAPStrategy"} <= names


# ── the actual CI gate: parametrized over every discovered class ─────

@pytest.mark.parametrize("cls", discover_strategies(),
                         ids=lambda c: c.__name__)
def test_bias_gate_all_strategies(cls):
    reports = run_bias_gate(strategy_cls=cls)
    assert reports, "gate produced no reports"
    for r in reports:
        assert r.ok, f"{r.summary()} :: {r.examples[:3]}"


def test_gate_rejects_cheat_via_run_bias_gate():
    reports = run_bias_gate(strategy_cls=LookaheadCheat)
    assert any(not r.ok for r in reports)
