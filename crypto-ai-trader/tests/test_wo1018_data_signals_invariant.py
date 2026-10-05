"""WO-1018: "data present but zero signals" blind spots in DimensionScorer.

Repro base: 10/5 21:50 bridge round — onchain(25%) reported as "[]" in the
Data-health WARN while the fallback fetch had 7 chains of data (avg +0.46).
Root cause class: signal band mappings missing a neutral/observation band,
so a data-bearing round could produce an empty signals list, which
scan_phases interprets as NO_DATA.

Invariant under test (all 6 dimensions): whenever data lands in the
dimension's data dict, at least one signal must be appended — with score
math unchanged for the new bands.
"""
import sys

sys.path.insert(0, ".")

import pytest

from src.dimension_scorer import DimensionScorer


SEVEN = {
    "Ethereum": 0.5, "BSC": 0.42, "Arbitrum": 0.6, "Base": 0.3,
    "Solana": 0.4, "Avalanche": 0.55, "Polygon": 0.46,
}  # avg = +0.46 — exactly the live repro band [-1, +1]


def _neutral_scorer(monkeypatch, fallback_dict, mvrv=None):
    """DimensionScorer with no client; llama primary -> None; fallback feed."""
    scorer = DimensionScorer(binance_client=None)

    import src.data_feed_llama as dfl
    monkeypatch.setattr(dfl.LlamaDataFeed, "get_chain_tvl",
                        lambda self: None)

    import src.data_feed_onchain as dfo
    monkeypatch.setattr(dfo.DeFiLlamaOnChain, "get_chain_tvl_changes",
                        lambda self: fallback_dict)

    monkeypatch.setattr(scorer, "_fetch_mvrv", lambda: mvrv)
    return scorer


class TestOnchainFallbackFlatBand:
    def test_neutral_band_emits_flat_signal(self, monkeypatch):
        """WO-1018 core repro: avg=+0.46 in [-1,+1] must not yield []."""
        scorer = _neutral_scorer(monkeypatch, dict(SEVEN), mvrv=None)
        out = scorer._score_onchain()
        joined = " ".join(out["signals"])
        assert "tvl_fallback_flat_" in joined
        assert "tvl_fallback_inflow" not in joined
        assert "tvl_fallback_outflow" not in joined
        assert out["data"]["chain_tvl_changes_fallback"] == SEVEN
        # score math unchanged: neutral band adds nothing
        assert out["score"] == 0.0

    def test_negative_neutral_band_also_flat(self, monkeypatch):
        data = {k: -v for k, v in SEVEN.items()}  # avg = -0.46
        scorer = _neutral_scorer(monkeypatch, data, mvrv=None)
        out = scorer._score_onchain()
        assert any("tvl_fallback_flat_-0.5pct" in s or "tvl_fallback_flat_" in s
                   for s in out["signals"])
        assert out["score"] == 0.0

    def test_inflow_band_regression_unchanged(self, monkeypatch):
        data = {k: 2.5 for k in SEVEN}
        scorer = _neutral_scorer(monkeypatch, data, mvrv=None)
        out = scorer._score_onchain()
        assert any("tvl_fallback_inflow_+2.5pct" in s for s in out["signals"])
        assert out["score"] == 0.2

    def test_outflow_band_regression_unchanged(self, monkeypatch):
        data = {k: -2.5 for k in SEVEN}
        scorer = _neutral_scorer(monkeypatch, data, mvrv=None)
        out = scorer._score_onchain()
        assert any("tvl_fallback_outflow_-2.5pct" in s for s in out["signals"])
        assert out["score"] == -0.2


class TestMvrvFairValueBand:
    def test_fair_value_emits_observation_signal(self, monkeypatch):
        """MVRV 1.5-3.0 with both TVL paths dead: data exists -> signal."""
        scorer = _neutral_scorer(monkeypatch, {}, mvrv=2.0)
        out = scorer._score_onchain()
        assert any("mvrv_fair_value_2.00" in s for s in out["signals"])
        assert out["score"] == 0.0

    @pytest.mark.parametrize("mvrv,marker,score", [
        (0.9, "mvrv_bottom_0.90", 0.4),
        (3.8, "mvrv_top_3.80", -0.4),
    ])
    def test_extreme_bands_regression_unchanged(self, monkeypatch, mvrv, marker, score):
        scorer = _neutral_scorer(monkeypatch, {}, mvrv=mvrv)
        out = scorer._score_onchain()
        assert any(marker in s for s in out["signals"])
        assert out["score"] == score


class TestBtcVolumeObservationLine:
    def test_stats_data_yields_signal_without_threshold(self, monkeypatch):
        """vol below the 5B threshold still reports data via observation line."""
        stats = {"quote_volume": 3_000_000_000, "price_change_pct": 0.2}
        scorer = DimensionScorer(binance_client=_FakeClient(stats))
        import src.data_feed_llama as dfl
        monkeypatch.setattr(dfl.LlamaDataFeed, "get_chain_tvl",
                            lambda self: None)
        import src.data_feed_onchain as dfo
        monkeypatch.setattr(dfo.DeFiLlamaOnChain, "get_chain_tvl_changes",
                            lambda self: dict(SEVEN))
        monkeypatch.setattr(scorer, "_fetch_mvrv", lambda: None)
        out = scorer._score_onchain()
        assert any("btc_vol_3.0B" in s for s in out["signals"])
        # below-threshold observation adds nothing to score
        assert out["score"] == 0.0

    def test_high_vol_accumulation_regression(self, monkeypatch):
        scorer = DimensionScorer(binance_client=_FakeClient(
            {"quote_volume": 6e9, "price_change_pct": 2.0}))
        import src.data_feed_llama as dfl
        monkeypatch.setattr(dfl.LlamaDataFeed, "get_chain_tvl",
                            lambda self: None)
        import src.data_feed_onchain as dfo
        monkeypatch.setattr(dfo.DeFiLlamaOnChain, "get_chain_tvl_changes",
                            lambda self: dict(SEVEN))
        monkeypatch.setattr(scorer, "_fetch_mvrv", lambda: None)
        out = scorer._score_onchain()
        assert "BTC_high_vol_accumulation" in out["signals"]
        assert out["score"] == 0.15


class TestSentimentNeutralBand:
    def _scorer(self, monkeypatch, payload):
        monkeypatch.setattr(
            "src.sentiment.SentimentAnalyzer.get_market_sentiment",
            lambda self: payload)
        return DimensionScorer(binance_client=None)

    @pytest.mark.parametrize("fng", [46, 50, 55, 59])
    def test_neutral_band_emits_signal(self, monkeypatch, fng):
        out = self._scorer(monkeypatch, {
            "fear_greed": fng, "consecutive_fear_days": 0,
            "consecutive_greed_days": 0, "signal": "NEUTRAL"})._score_sentiment()
        assert out["signals"] == [f"CFGI_neutral_{fng}"]
        assert out["score"] == 0.0

    @pytest.mark.parametrize("fng,marker,score", [
        (45, "CFGI_fear_45", 0.1),
        (60, "CFGI_greed_60", -0.1),
        (25, "CFGI_extreme_fear_25", 0.3),
        (75, "CFGI_extreme_greed_75", -0.3),
    ])
    def test_existing_bands_regression_unchanged(self, monkeypatch, fng, marker, score):
        out = self._scorer(monkeypatch, {
            "fear_greed": fng, "consecutive_fear_days": 0,
            "consecutive_greed_days": 0, "signal": "NEUTRAL"})._score_sentiment()
        assert out["signals"] == [marker]
        assert out["score"] == score


class _FakeClient:
    def __init__(self, stats):
        self._stats = stats

    def get_24hr_stats(self, symbol):
        return self._stats
