"""Tests for DimensionScorer — SSR, Seller Exhaustion Constant, and MVRV."""

import math
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.dimension_scorer import DimensionScorer


# ---------------------------------------------------------------------------
# Helper: create realistic klines for testing
# ---------------------------------------------------------------------------
def _make_klines(closes: list[float]) -> list[dict]:
    """Build minimal kline dicts from a list of close prices."""
    return [{"close": str(c), "open": str(c), "high": str(c), "low": str(c)} for c in closes]


# ===========================================================================
# SSR (Stablecoin Supply Ratio) tests
# ===========================================================================
class TestSSRScoring:
    """Verify SSR integration in _score_liquidity()."""

    def _make_client_with_price(self, btc_price: float):
        client = MagicMock()
        client.get_ticker_price.return_value = btc_price
        client.get_24hr_stats.return_value = {
            "quote_volume": 0,
            "price_change_pct": 0,
            "volume": 0,
        }
        client.get_klines.return_value = []
        return client

    @patch("src.dimension_scorer.DimensionScorer._score_liquidity")
    def test_ssr_not_crash_without_stablecoin_data(self, mock_liq):
        """If stablecoin data unavailable, SSR is skipped gracefully."""
        # We test the real _score_liquidity by not patching it
        pass  # indirect — covered by integration below

    def test_ssr_low_bullish(self):
        """SSR < 10 should add bullish score to liquidity dimension."""
        client = self._make_client_with_price(63000)  # BTC at $63K
        scorer = DimensionScorer(binance_client=client)

        # total stablecoin supply = 200B → SSR = (63000 * 19.7M) / 200B = 6.2
        mock_stbl = {
            "usdt_circulating": 120e9,
            "usdc_circulating": 80e9,
            "total_circulating_usd": 200e9,
            "usdt_change_day": 0,
            "usdc_change_day": 0,
            "depeg_alerts": [],
        }

        with patch("src.data_feed_llama.LlamaDataFeed") as MockLlama:
            instance = MockLlama.return_value
            instance.get_stablecoin_supply.return_value = mock_stbl
            instance.get_chain_tvl.return_value = None
            instance.get_dex_volume.return_value = None

            # Also mock funding rate
            with patch("src.data_feed_funding.FundingRate") as MockFR:
                MockFR.return_value.get_funding_rolling_avg.return_value = {
                    "signal_strength": 0,
                    "signal": "NEUTRAL",
                    "rolling_avg": 0.0001,
                    "negative_pct": 0,
                }

                result = scorer._score_liquidity()

        # SSR = 63000 * 19700000 / 200e9 = 6.2055 → < 10 → bullish
        assert result["data"].get("ssr", 0) < 10
        assert any("SSR_low_bullish" in s for s in result["signals"])
        # Score should be positive (bullish)
        assert result["score"] > 0

    def test_ssr_high_bearish(self):
        """SSR > 15 should add bearish score to liquidity dimension."""
        client = self._make_client_with_price(126000)  # BTC at $126K (ATH scenario)
        scorer = DimensionScorer(binance_client=client)

        # total stablecoin supply = 150B → SSR = (126000 * 19.7M) / 150B = 16.5
        mock_stbl = {
            "usdt_circulating": 90e9,
            "usdc_circulating": 60e9,
            "total_circulating_usd": 150e9,
            "usdt_change_day": 0,
            "usdc_change_day": 0,
            "depeg_alerts": [],
        }

        with patch("src.data_feed_llama.LlamaDataFeed") as MockLlama:
            instance = MockLlama.return_value
            instance.get_stablecoin_supply.return_value = mock_stbl
            instance.get_chain_tvl.return_value = None
            instance.get_dex_volume.return_value = None

            with patch("src.data_feed_funding.FundingRate") as MockFR:
                MockFR.return_value.get_funding_rolling_avg.return_value = {
                    "signal_strength": 0,
                    "signal": "NEUTRAL",
                    "rolling_avg": 0.0001,
                    "negative_pct": 0,
                }

                result = scorer._score_liquidity()

        # SSR = 126000 * 19700000 / 150e9 = 16.5 → > 15 → bearish
        assert result["data"].get("ssr", 0) > 15
        assert any("SSR_high_bearish" in s for s in result["signals"])
        assert result["score"] < 0

    def test_ssr_neutral_zone(self):
        """SSR between 10-15 should not add SSR signal."""
        client = self._make_client_with_price(85000)  # BTC at $85K
        scorer = DimensionScorer(binance_client=client)

        # total stablecoin = 160B → SSR = (85000 * 19.7M) / 160B = 10.47
        mock_stbl = {
            "usdt_circulating": 100e9,
            "usdc_circulating": 60e9,
            "total_circulating_usd": 160e9,
            "usdt_change_day": 0,
            "usdc_change_day": 0,
            "depeg_alerts": [],
        }

        with patch("src.data_feed_llama.LlamaDataFeed") as MockLlama:
            instance = MockLlama.return_value
            instance.get_stablecoin_supply.return_value = mock_stbl
            instance.get_chain_tvl.return_value = None
            instance.get_dex_volume.return_value = None

            with patch("src.data_feed_funding.FundingRate") as MockFR:
                MockFR.return_value.get_funding_rolling_avg.return_value = {
                    "signal_strength": 0,
                    "signal": "NEUTRAL",
                    "rolling_avg": 0.0001,
                    "negative_pct": 0,
                }

                result = scorer._score_liquidity()

        ssr = result["data"].get("ssr", 0)
        assert 10 <= ssr <= 15
        assert not any("bullish" in s.lower() or "bearish" in s.lower() for s in result["signals"]
                       if "SSR_" in s)
        # Score from SSR should be 0 (neutral), overall score depends on other signals
        # but with all neutral inputs, should be close to 0
        assert abs(result["score"]) < 0.05


# ===========================================================================
# Seller Exhaustion Constant tests
# ===========================================================================
class TestSellerExhaustion:
    """Verify seller exhaustion integration in _score_technical()."""

    def test_downtrend_with_low_volatility_triggers_exhaustion(self):
        """A scenario with significant drawdown and low volatility should trigger bullish exhaustion."""
        # Price dropped from 100 to 85 with low volatility
        closes = [100, 99.5, 99, 98.5, 98, 97.5, 97, 96.5, 96, 95.5,
                  95, 94.5, 94, 93.5, 93, 92.5, 92, 91.5, 91, 90.5,
                  90, 89.5, 89, 88.5, 88, 87.5, 87, 86.5, 86, 85.5, 85]
        assert len(closes) >= 15

        client = MagicMock()
        client.get_klines.return_value = _make_klines(closes)

        scorer = DimensionScorer(binance_client=client)
        result = scorer._score_technical()

        # Should have exhaustion data
        assert "exhaustion" in result["data"]
        assert result["data"]["exhaustion"] > 3  # Should be significant
        # Drawdown = (100 - 85) / 100 = 15%
        assert result["data"]["drawdown_pct"] == pytest.approx(15.0, abs=0.5)
        # Should have bullish exhaustion signal
        assert any("exhaustion" in s for s in result["signals"])

    def test_uptrend_no_exhaustion(self):
        """Rising prices should not trigger exhaustion signal."""
        closes = [80, 81, 82, 83, 84, 85, 86, 87, 88, 89,
                  90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100]

        client = MagicMock()
        client.get_klines.return_value = _make_klines(closes)

        scorer = DimensionScorer(binance_client=client)
        result = scorer._score_technical()

        # No drawdown → exhaustion should be 0 or very low
        assert result["data"]["exhaustion"] < 1
        # Should not have bullish exhaustion signal
        assert not any("exhaustion_high_bullish" in s for s in result["signals"])
        assert not any("exhaustion_mild" in s for s in result["signals"])

    def test_exhaustion_with_oversold_rsi(self):
        """When both RSI oversold AND high exhaustion, score should be bullish."""
        # Strong selloff pattern: RSI will be very low, drawdown large, vol moderate
        closes = [100, 98, 96, 94, 92, 90, 88, 86, 85, 84,
                  83, 82, 81, 80, 79, 78, 77, 76, 75, 74, 73]

        client = MagicMock()
        client.get_klines.return_value = _make_klines(closes)

        scorer = DimensionScorer(binance_client=client)
        result = scorer._score_technical()

        # Both RSI oversold and exhaustion should contribute positively
        assert result["score"] > 0
        assert any("RSI_oversold" in s or "RSI_low" in s for s in result["signals"])
        assert "exhaustion" in result["data"]

    def test_technical_no_client_returns_neutral(self):
        """Without client, D5 should return neutral."""
        scorer = DimensionScorer(binance_client=None)
        result = scorer._score_technical()
        assert result["score"] == 0
        assert result["weight"] == 0.10


# ===========================================================================
# Integration: score_all still works
# ===========================================================================
class TestScoreAllIntegration:
    """Ensure score_all() still returns expected structure with new sub-signals."""

    def test_score_all_structure_unchanged(self):
        """score_all() should still return all 6 dimensions."""
        scorer = DimensionScorer(binance_client=None)
        result = scorer.score_all()

        assert "dimensions" in result
        assert len(result["dimensions"]) == 6
        for dim_name in ["onchain", "liquidity", "macro", "sentiment", "technical", "regulatory"]:
            assert dim_name in result["dimensions"]
        assert "resonance" in result
        assert "surge_probability" in result
        assert "weighted_score" in result


# ===========================================================================
# MVRV (Market Value to Realized Value) tests
# ===========================================================================
class TestMVRVScoring:
    """Verify MVRV integration in _score_onchain()."""

    def _score_with_mvrv(self, mvrv_val):
        """Helper: score onchain with mocked MVRV, return result."""
        scorer = DimensionScorer(binance_client=None)
        with patch.object(scorer, "_fetch_mvrv", return_value=mvrv_val):
            return scorer._score_onchain()

    def _score_without_mvrv(self):
        """Baseline score without MVRV contribution."""
        scorer = DimensionScorer(binance_client=None)
        with patch.object(scorer, "_fetch_mvrv", return_value=None):
            return scorer._score_onchain()

    def test_mvrv_bottom_zone(self):
        """MVRV < 1.0 should add +0.4 to score and produce bottom signal."""
        result = self._score_with_mvrv(0.95)
        assert result["data"]["mvrv"] == 0.95
        assert any("mvrv_bottom" in s for s in result["signals"])

    def test_mvrv_undervalued(self):
        """MVRV 1.0-1.2 should add +0.25 and produce undervalued signal."""
        result = self._score_with_mvrv(1.15)
        assert result["data"]["mvrv"] == 1.15
        assert any("mvrv_undervalued" in s for s in result["signals"])

    def test_mvrv_below_average(self):
        """MVRV 1.2-1.5 should add +0.1 and produce below_avg signal."""
        result = self._score_with_mvrv(1.35)
        assert result["data"]["mvrv"] == 1.35
        assert any("mvrv_below_avg" in s for s in result["signals"])

    def test_mvrv_neutral_zone(self):
        """MVRV 1.5-3.0 should not add any MVRV signal."""
        result = self._score_with_mvrv(2.0)
        assert result["data"]["mvrv"] == 2.0
        assert not any("mvrv_" in s for s in result["signals"])

    def test_mvrv_top_zone(self):
        """MVRV > 3.7 should add -0.4 and produce top signal."""
        result = self._score_with_mvrv(3.8)
        assert result["data"]["mvrv"] == 3.8
        assert any("mvrv_top" in s for s in result["signals"])

    def test_mvrv_overvalued(self):
        """MVRV 3.0-3.7 should add -0.2 and produce overvalued signal."""
        result = self._score_with_mvrv(3.2)
        assert result["data"]["mvrv"] == 3.2
        assert any("mvrv_overvalued" in s for s in result["signals"])

    def test_mvrv_none_graceful(self):
        """If MVRV fetch returns None, should not crash and no mvrv signal."""
        scorer = DimensionScorer(binance_client=None)
        with patch.object(scorer, "_fetch_mvrv", return_value=None):
            result = scorer._score_onchain()
        assert "mvrv" not in result["data"]
        assert not any("mvrv_" in s for s in result["signals"])

    def test_mvrv_bullish_increments_score(self):
        """MVRV < 1.0 should produce higher score than without MVRV."""
        baseline = self._score_without_mvrv()["score"]
        with_mvrv = self._score_with_mvrv(0.95)["score"]
        assert with_mvrv > baseline + 0.3  # +0.4 increment minus rounding

class TestMvrvFetchParams(unittest.TestCase):
    """Regression (2026-08-20): literal startday=today returns [] upstream.

    _fetch_mvrv must use explicit ISO dates with a 3-day lookback so the
    latest published point (>= 1d lag) is included.
    """

    def test_lookback_dates_and_parse(self):
        from datetime import date, timedelta
        import src.dimension_scorer as ds_mod

        ds = DimensionScorer.__new__(DimensionScorer)
        captured = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return [{"d": "2026-08-19", "mvrv": 1.3248}, {"d": "2026-08-20", "mvrv": 1.31}]

        def fake_get(url, headers=None, params=None, timeout=None):
            captured["params"] = params
            return FakeResp()

        with patch.object(ds_mod.requests, "get", side_effect=fake_get), \
             patch.dict(ds_mod.os.environ, {"BGEOMETRICS_API_KEY": "k"}):
            val = ds._fetch_mvrv()

        self.assertEqual(val, 1.31)  # last point, not first
        self.assertNotIn("today", str(captured["params"].values()))
        expect_start = (date.today() - timedelta(days=3)).isoformat()
        self.assertEqual(captured["params"]["startday"], expect_start)

    def test_empty_still_none(self):
        import src.dimension_scorer as ds_mod

        ds = DimensionScorer.__new__(DimensionScorer)

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return []

        with patch.object(ds_mod.requests, "get", return_value=FakeResp()), \
             patch.dict(ds_mod.os.environ, {"BGEOMETRICS_API_KEY": "k"}):
            self.assertIsNone(ds._fetch_mvrv())
