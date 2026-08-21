"""Fresh-entry fallback (2026-08-21): best candidate held → pick fresh runner-up.

Scenario: ZEC(80) held, duplicate-entry guard blocks fresh auto-execute on it.
XPL(79)/PYTH(78) pass risk checks. Old behavior: round ends with zero trades.
New behavior: _select_best_candidate prefers the best NON-HELD qualifier.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock
from src.research_phase import _select_best_candidate


def _rr(entries):
    """Build research_results: {sym: (opp, res)}."""
    out = {}
    for sym, score, adj in entries:
        out[sym] = ({"score": score}, {"score_adjustment": adj})
    return out


class FakeClient:
    def __init__(self, held):
        self._held = set(held)

    def get_account(self):
        return {"balances": [
            {"asset": a, "free": "10", "locked": "0"} for a in self._held
        ] + [{"asset": "USDT", "free": "300", "locked": "0"}]}

    def get_24hr_stats(self, symbol):
        # every held asset worth >$5
        return {"last_price": 100}


def test_held_best_loses_to_fresh_runner_up():
    """ZEC 80 held, XPL 79 fresh, threshold 67 → pick XPL with fallback."""
    client = FakeClient(held=["ZEC"])
    rr = _rr([("ZECUSDT", 80, 0), ("XPLUSDT", 79, 0), ("PYTHUSDT", 78, 0)])
    sym, score, fb = _select_best_candidate(rr, client, 67)
    assert sym == "XPLUSDT", f"expected XPLUSDT, got {sym}"
    assert score == 79
    assert fb is True


def test_held_only_qualifies_still_proceeds():
    """Only held candidate above threshold → original behavior preserved."""
    client = FakeClient(held=["ZEC"])
    rr = _rr([("ZECUSDT", 80, 0), ("XPLUSDT", 50, 0)])
    sym, score, fb = _select_best_candidate(rr, client, 67)
    assert sym == "ZECUSDT"
    assert fb is False


def test_fresh_best_wins_normally():
    """No held candidates → same as original selection."""
    client = FakeClient(held=[])
    rr = _rr([("XPLUSDT", 79, 0), ("PYTHUSDT", 78, 0)])
    sym, score, fb = _select_best_candidate(rr, client, 67)
    assert sym == "XPLUSDT" and score == 79 and fb is False


def test_research_adjustment_applies():
    """Adjusted score (score + adjustment) drives the ranking."""
    client = FakeClient(held=["ZEC"])
    rr = _rr([("ZECUSDT", 70, +10), ("XPLUSDT", 72, 0)])
    sym, score, fb = _select_best_candidate(rr, client, 67)
    assert sym == "XPLUSDT" and score == 72 and fb is True


def test_all_below_threshold_returns_none():
    client = FakeClient(held=["ZEC"])
    rr = _rr([("ZECUSDT", 60, 0), ("XPLUSDT", 59, 0)])
    sym, score, fb = _select_best_candidate(rr, client, 67)
    assert sym is None and score < 67


def test_held_check_exception_does_not_crash():
    """If _symbol_already_held raises internally it returns False — selection continues."""
    class BoomClient:
        def get_account(self):
            raise RuntimeError("api down")
    rr = _rr([("XPLUSDT", 79, 0)])
    sym, score, fb = _select_best_candidate(rr, BoomClient(), 67)
    assert sym == "XPLUSDT"
