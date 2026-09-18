"""dca fallback direction gate (research_phase.py fallback chain).

Evidence: 9/16 09:00 UNI (score 67) and 9/17 05:01 UNI (score 69) / 05:51
DASH (score 70) were all bought with strategy=dca in NEUTRAL regime, no
-3% dip, btc_trend=NEUTRAL, dim resonance NEUTRAL, research_adj=0.0 — the
fallback chain handed dca the trade with zero dip/direction checks.

Gate passes on EITHER a real dip (24h change or MA20 deviation <=
dca_params.dip_threshold_pct) OR direction confirmation (six-dimension
resonance weighted_score > 0 AND BTC trend not BEARISH).
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research_phase import _dca_fallback_direction_ok


class FakeStatsClient:
    def __init__(self, change_24h=0.0, exc=None):
        self.change_24h = change_24h
        self.exc = exc
        self.calls = 0

    def get_24hr_stats(self, symbol):
        self.calls += 1
        if self.exc:
            raise self.exc
        return {"price_change_pct": self.change_24h}


def _ctx(regime="NEUTRAL", btc_trend="NEUTRAL", dip=-3.0, weighted=None):
    return {
        "regime": regime,
        "btc_trend": btc_trend,
        "adapted": {"dca_params": {"dip_threshold_pct": dip}},
        "dim_result": ({"weighted_score": weighted} if weighted is not None else None),
    }


def _klines(prices):
    return [{"close": p} for p in prices]


class TestGateRejectsFlatMarket:
    def test_uni_dash_production_case_rejected(self):
        """NEUTRAL regime + NEUTRAL BTC + no dip + NEUTRAL resonance → reject.
        This is the exact 9/16-9/17 UNI/DASH production scenario."""
        c = FakeStatsClient(change_24h=+1.2)   # no dip at all
        ok, why = _dca_fallback_direction_ok(_ctx(weighted=0.0), c, "UNIUSDT")
        assert not ok
        assert "no dip" in why and "no direction confirm" in why

    def test_bearish_btc_blocks_direction_confirm_even_with_score(self):
        c = FakeStatsClient(change_24h=0.5)
        ok, why = _dca_fallback_direction_ok(
            _ctx(btc_trend="BEARISH", weighted=0.6), c, "UNIUSDT")
        assert not ok

    def test_zero_weighted_score_fails_direction_leg(self):
        c = FakeStatsClient(change_24h=-0.4)
        ok, _ = _dca_fallback_direction_ok(_ctx(weighted=0.0), c, "UNIUSDT")
        assert not ok

    def test_missing_dim_result_falls_to_dip_only(self):
        c = FakeStatsClient(change_24h=-0.2)
        ok, _ = _dca_fallback_direction_ok(_ctx(weighted=None), c, "UNIUSDT")
        assert not ok


class TestGatePasses:
    def test_real_dip_24h_passes(self):
        """24h drawdown beyond the -3% threshold passes even in NEUTRAL."""
        c = FakeStatsClient(change_24h=-3.5)
        ok, why = _dca_fallback_direction_ok(_ctx(weighted=0.0), c, "UNIUSDT")
        assert ok and "24h" in why

    def test_ma20_deviation_dip_passes_when_stats_unavailable(self):
        """Stats API down but price sits 4% below the 20-bar mean → pass."""
        c = FakeStatsClient(exc=RuntimeError("stats down"))
        prices = [100.0] * 19 + [96.0]     # last price 4% below MA20
        ok, why = _dca_fallback_direction_ok(
            _ctx(weighted=0.0), c, "UNIUSDT", klines=_klines(prices))
        assert ok and "MA20" in why

    def test_direction_confirm_passes(self):
        c = FakeStatsClient(change_24h=0.0)
        ok, why = _dca_fallback_direction_ok(
            _ctx(btc_trend="BULLISH", weighted=0.45), c, "UNIUSDT")
        assert ok and "direction ok" in why

    def test_dip_passes_even_when_btc_bearish(self):
        """A real dip is a dip — the (a) leg does not require BTC context."""
        c = FakeStatsClient(change_24h=-6.0)
        ok, _ = _dca_fallback_direction_ok(
            _ctx(btc_trend="BEARISH", weighted=-0.3), c, "UNIUSDT")
        assert ok


class TestGateRobustness:
    def test_stats_and_klines_both_unavailable_still_rejects(self):
        c = FakeStatsClient(exc=RuntimeError("net"))
        ok, _ = _dca_fallback_direction_ok(_ctx(weighted=0.0), c, "UNIUSDT")
        assert not ok

    def test_missing_dca_params_uses_default_threshold(self):
        c = FakeStatsClient(change_24h=-5.5)   # below the -5.0 default
        ctx = {"regime": "NEUTRAL", "btc_trend": "NEUTRAL",
               "adapted": {}, "dim_result": None}
        ok, why = _dca_fallback_direction_ok(ctx, c, "XUSDT")
        assert ok

    def test_short_klines_do_not_crash(self):
        c = FakeStatsClient(change_24h=0.0)
        ok, _ = _dca_fallback_direction_ok(
            _ctx(weighted=0.0), c, "XUSDT", klines=_klines([1.0, 2.0]))
        assert not ok


class TestFallbackChainWiring:
    """The chain must keep walking past a gated-out dca, and block the trade
    entirely when dca was the only enabled strategy."""

    def _source(self):
        return (ROOT / "src" / "research_phase.py").read_text(encoding="utf-8")

    def test_gate_called_inside_fallback_chain(self):
        src = self._source()
        assert "for fallback in [\"dca\", \"rsi\", \"bollinger\", \"vwap\", \"trend\", \"grid\"]:" in src
        assert "_dca_fallback_direction_ok(" in src
        # rejection must continue the chain (not break, not fall through)
        assert "DCA_FALLBACK_GATE" in src and "trying next fallback" in src

    def test_only_dca_enabled_blocks_trade(self):
        src = self._source()
        assert "_fb_chosen" in src
        assert "FALLBACK_GATE_BLOCKED" in src

    def test_fear_mode_direct_dca_not_gated(self):
        """research_phase.py:526-area fear-mode direct dca stays ungated."""
        src = self._source()
        fear_assign = src.find('if is_fear_mode:\n        strategy = "dca"')
        assert fear_assign > 0, "fear-mode direct dca assignment must stay"
        fear_skip = src.find("Fear mode: skipping StrategyRegistry")
        assert fear_skip > 0, "fear-mode registry skip must stay"
        # the gate call lives only inside the enabled-fallback chain, which
        # sits after the fear-mode branches — never before/inside them
        gate_call = src.find("_dca_fallback_direction_ok(\n                        ctx")
        fallback_chain = src.find("# Fall back to an enabled strategy")
        assert 0 < fear_skip < fallback_chain
        assert gate_call > fallback_chain
        # fear branches themselves contain no gate invocation
        fear_zone = src[fear_assign:fear_skip + 60]
        assert "_dca_fallback_direction_ok" not in fear_zone
