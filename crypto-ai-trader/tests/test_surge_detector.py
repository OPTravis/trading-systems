"""Tests for SurgeDetector — pre-pump characteristic checklist."""

import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.surge_detector import SurgeDetector


@pytest.fixture
def detector():
    return SurgeDetector()


@pytest.fixture
def mock_dim_mild_bull():
    """Dimension result with MILD_BULL resonance."""
    return {
        "dimensions": {
            "onchain": {
                "score": 0.3,
                "weight": 0.25,
                "signals": ["mvrv_undervalued_1.20", "tvl_flat_0.1pct"],
                "data": {
                    "mvrv": 1.20,
                    "chain_tvl_changes": {"Ethereum": 0.1, "Arbitrum": -0.2, "Solana": 0.3},
                },
            },
            "liquidity": {
                "score": 0.2,
                "weight": 0.25,
                "signals": ["ssr_low"],
                "data": {"ssr": 2.5, "stablecoin_growth_7d": 1.2},
            },
            "macro": {"score": 0.15, "weight": 0.20, "signals": ["btc_above_ma50"], "data": {}},
            "sentiment": {"score": 0.1, "weight": 0.15, "signals": ["fng_fear"], "data": {}},
            "technical": {"score": 0.05, "weight": 0.10, "signals": ["rsi_recovering"], "data": {}},
            "regulatory": {"score": 0.0, "weight": 0.05, "signals": ["neutral"], "data": {}},
        },
        "bullish_count": 3,
        "bearish_count": 0,
        "resonance": "MILD_BULL",
        "weighted_score": 0.15,
        "surge_probability": "52%",
    }


@pytest.fixture
def mock_dim_strong_bull():
    """Dimension result with STRONG_BULL resonance."""
    return {
        "dimensions": {
            "onchain": {
                "score": 0.5,
                "weight": 0.25,
                "signals": ["mvrv_bottom_0.95", "tvl_strong_inflow_3.5pct", "obv_bullish_divergence"],
                "data": {
                    "mvrv": 0.95,
                    "chain_tvl_changes": {"Ethereum": 3.5, "Arbitrum": 2.1, "Solana": 4.2},
                },
            },
            "liquidity": {
                "score": 0.4,
                "weight": 0.25,
                "signals": ["ssr_low", "stablecoin_growth"],
                "data": {"ssr": 2.0, "stablecoin_growth_7d": 2.5},
            },
            "macro": {"score": 0.3, "weight": 0.20, "signals": ["btc_above_ma50"], "data": {}},
            "sentiment": {"score": 0.3, "weight": 0.15, "signals": ["fng_neutral"], "data": {}},
            "technical": {
                "score": 0.4,
                "weight": 0.10,
                "signals": ["macd_bullish_cross", "rsi_recovering", "consolidation_breaking_out"],
                "data": {},
            },
            "regulatory": {"score": 0.2, "weight": 0.05, "signals": ["positive_news"], "data": {}},
        },
        "bullish_count": 6,
        "bearish_count": 0,
        "resonance": "STRONG_BULL",
        "weighted_score": 0.38,
        "surge_probability": "85-92%",
    }


class TestPhase1Capitulation:
    """Phase 1: Capitulation bottom signals."""

    @patch.object(SurgeDetector, "_fetch_bgeometrics", return_value=None)
    def test_silence_when_no_signals(self, mock_fetch, detector):
        """No signals at all → SILENCE."""
        result = detector.detect(fng=50, btc_rsi=55)
        assert result["alert_level"] == "SILENCE"
        assert not result["should_alert"]

    @patch.object(SurgeDetector, "_fetch_bgeometrics")
    def test_extreme_fear_triggers_watch(self, mock_fetch, detector):
        """F&G <= 20 + RSI oversold → at least WATCH (2+ Phase 1 signals)."""
        mock_fetch.return_value = None
        result = detector.detect(fng=18, btc_rsi=28)
        assert result["alert_level"] in ("WATCH", "IMMINENT")
        assert result["phase1_count"] >= 2
        assert any("F&G" in s for s in result["phase1_signals"])

    @patch.object(SurgeDetector, "_fetch_bgeometrics")
    def test_mvrv_bottom_detected(self, mock_fetch, detector):
        """MVRV < 1.0 triggers bottom signal."""
        mock_fetch.return_value = 0.95
        result = detector.detect(fng=15, btc_rsi=28)
        assert any("MVRV" in s for s in result["phase1_signals"])
        assert result["mvrv"] == 0.95

    @patch.object(SurgeDetector, "_fetch_bgeometrics")
    def test_sopr_below_1_detected(self, mock_fetch, detector):
        """SOPR < 1.0 triggers capitulation signal."""
        mock_fetch.return_value = 0.995
        result = detector.detect(fng=20, btc_rsi=35)
        assert any("SOPR" in s for s in result["phase1_signals"])

    @patch.object(SurgeDetector, "_fetch_bgeometrics")
    def test_rsi_oversold_detected(self, mock_fetch, detector):
        """RSI < 30 triggers oversold signal."""
        mock_fetch.return_value = None
        result = detector.detect(fng=50, btc_rsi=25)
        assert any("RSI" in s for s in result["phase1_signals"])

    @patch.object(SurgeDetector, "_fetch_bgeometrics")
    def test_multiple_phase1_signals(self, mock_fetch, detector):
        """Multiple Phase 1 signals together."""
        def side_effect(endpoint):
            if endpoint == "/mvrv":
                return 0.92
            if endpoint == "/sopr":
                return 0.987
            if endpoint == "/nupl":
                return -0.05
            return None
        mock_fetch.side_effect = side_effect
        result = detector.detect(fng=15, btc_rsi=25)
        assert result["phase1_count"] >= 4
        assert result["alert_level"] in ("WATCH", "ACCUMULATE", "IMMINENT")


class TestPhase2Accumulation:
    """Phase 2: Smart money accumulation signals."""

    @patch.object(SurgeDetector, "_fetch_bgeometrics", return_value=None)
    def test_obv_divergence_detected(self, mock_fetch, detector, mock_dim_mild_bull):
        """OBV divergence in dimension signals triggers Phase 2."""
        dim = mock_dim_mild_bull.copy()
        dim["dimensions"]["onchain"]["signals"] = ["obv_bullish_divergence", "mvrv_low"]
        result = detector.detect(dim_result=dim, fng=23, btc_rsi=42)
        assert any("OBV" in s for s in result["phase2_signals"])

    @patch.object(SurgeDetector, "_fetch_bgeometrics", return_value=None)
    def test_tvl_stabilizing_detected(self, mock_fetch, detector, mock_dim_mild_bull):
        """TVL stable (not crashing) triggers Phase 2."""
        result = detector.detect(dim_result=mock_dim_mild_bull, fng=23, btc_rsi=42)
        assert any("TVL" in s or "tvl" in s.lower() for s in result["phase2_signals"])

    @patch.object(SurgeDetector, "_fetch_bgeometrics", return_value=None)
    def test_ssr_low_detected(self, mock_fetch, detector, mock_dim_mild_bull):
        """SSR < 3.0 triggers Phase 2 stablecoin buying power."""
        result = detector.detect(dim_result=mock_dim_mild_bull, fng=23, btc_rsi=42)
        assert any("SSR" in s for s in result["phase2_signals"])


class TestPhase3Reversal:
    """Phase 3: Reversal trigger signals."""

    @patch.object(SurgeDetector, "_fetch_bgeometrics", return_value=None)
    def test_fng_sharp_jump_detected(self, mock_fetch, detector):
        """F&G jumping +8 from extreme fear → Phase 3."""
        result = detector.detect(fng=30, fng_prev=18, btc_rsi=40)
        assert any("F&G" in s and "暴漲" in s for s in result["phase3_signals"])
        assert result["alert_level"] in ("IMMINENT", "CONFIRMED")

    @patch.object(SurgeDetector, "_fetch_bgeometrics", return_value=None)
    def test_resonance_bull_detected(self, mock_fetch, detector, mock_dim_strong_bull):
        """Strong bull resonance triggers Phase 3."""
        result = detector.detect(dim_result=mock_dim_strong_bull, fng=35, btc_rsi=45)
        assert any("共振" in s for s in result["phase3_signals"])

    @patch.object(SurgeDetector, "_fetch_bgeometrics", return_value=None)
    def test_rsi_recovery_detected(self, mock_fetch, detector):
        """RSI in 30-50 range = recovery from oversold."""
        result = detector.detect(fng=30, btc_rsi=38)
        assert any("RSI" in s for s in result["phase3_signals"])

    @patch.object(SurgeDetector, "_fetch_bgeometrics", return_value=None)
    def test_btc_above_ma50(self, mock_fetch, detector):
        """BTC above 50-day MA → Phase 3."""
        result = detector.detect(fng=30, btc_rsi=42, btc_price=68000, btc_ma50=65000)
        assert any("50日" in s for s in result["phase3_signals"])

    @patch.object(SurgeDetector, "_fetch_bgeometrics", return_value=None)
    def test_market_breadth(self, mock_fetch, detector):
        """Multiple high-score opportunities → Phase 3."""
        opps = [{"score": 85}, {"score": 82}, {"score": 81}, {"score": 60}]
        result = detector.detect(fng=35, btc_rsi=42, opportunities=opps)
        assert any("廣度" in s for s in result["phase3_signals"])


class TestAlertLevels:
    """Alert level escalation logic."""

    @patch.object(SurgeDetector, "_fetch_bgeometrics", return_value=None)
    def test_confirmed_when_strong_bull_and_p3(self, mock_fetch, detector, mock_dim_strong_bull):
        """STRONG_BULL resonance + 3+ Phase 3 signals = CONFIRMED."""
        result = detector.detect(
            dim_result=mock_dim_strong_bull,
            fng=40,
            fng_prev=18,
            btc_rsi=45,
            btc_price=68000,
            btc_ma50=65000,
            opportunities=[{"score": 85}, {"score": 82}, {"score": 81}],
        )
        assert result["alert_level"] == "CONFIRMED"
        assert result["should_alert"] is True

    @patch.object(SurgeDetector, "_fetch_bgeometrics")
    def test_imminent_when_p3_and_p1(self, mock_fetch, detector):
        """Phase 3 signal + Phase 1 signals = IMMINENT."""
        mock_fetch.return_value = 1.05
        result = detector.detect(fng=20, fng_prev=20, btc_rsi=42)
        assert result["alert_level"] == "IMMINENT"
        assert result["should_alert"] is True

    @patch.object(SurgeDetector, "_fetch_bgeometrics")
    def test_watch_with_p1_only(self, mock_fetch, detector):
        """Phase 1 signals only → WATCH, no alert."""
        mock_fetch.return_value = 1.05
        result = detector.detect(fng=18, btc_rsi=55)
        assert result["alert_level"] == "WATCH"
        assert not result["should_alert"]

    @patch.object(SurgeDetector, "_fetch_bgeometrics", return_value=None)
    def test_accumulate_with_p1_and_p2(self, mock_fetch, detector, mock_dim_mild_bull):
        """Phase 1 + Phase 2 → ACCUMULATE."""
        # mvrv will be None (mocked), but fng + TVL stable should trigger
        result = detector.detect(
            dim_result=mock_dim_mild_bull,
            fng=23,
            btc_rsi=48,
        )
        # Should have at least WATCH or higher
        assert result["alert_level"] in ("WATCH", "ACCUMULATE", "IMMINENT")
        assert result["phase1_count"] >= 1


def _age_disk_entry(endpoint: str, seconds: float) -> None:
    """bug#14 helper: age one disk entry (mem + disk are written together)."""
    import json
    import os

    p = os.environ.get("BGE_CACHE_PATH")
    if not p or not os.path.exists(p):
        return
    data = json.loads(open(p, encoding="utf-8").read())
    entry = data.get("entries", {}).get(endpoint)
    if entry:
        entry["fetched_at"] -= seconds
        open(p, "w", encoding="utf-8").write(json.dumps(data))


class TestBGeometricsFetch:
    """BGeometrics API integration tests."""

    @pytest.fixture(autouse=True)
    def _reset_bge_cache(self):
        """Module-level cache leaks across tests in full-suite runs.

        Other suites populate /mvrv with success or fail-TTL entries;
        without a reset the requests.get mocks never get called.
        """
        from src.surge_detector import reset_bge_cache

        reset_bge_cache()
        yield
        reset_bge_cache()

    def test_fetch_returns_none_without_key(self, detector):
        """No API key → returns None gracefully."""
        with patch.dict(os.environ, {}, clear=True):
            # Can't clear all env, so just check key absence
            with patch.object(os, "environ", {"PATH": "/usr/bin"}):
                result = detector._fetch_bgeometrics("/mvrv")
                assert result is None

    def test_fetch_handles_api_error(self, detector):
        """API error → returns None, doesn't crash."""
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_get.return_value = mock_resp
            result = detector._fetch_bgeometrics("/mvrv")
            assert result is None

    def test_fetch_parses_mvrv_correctly(self, detector):
        """Valid MVRV response → returns float."""
        with patch.dict(os.environ, {"BGEOMETRICS_API_KEY": "test_key"}):
            with patch("requests.get") as mock_get:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = [
                    {"d": "2026-07-09", "unixTs": 1783555200, "mvrv": 1.2019}
                ]
                mock_get.return_value = mock_resp
                result = detector._fetch_bgeometrics("/mvrv")
                assert result == 1.2019


class TestSummary:
    """Summary output formatting."""

    @patch.object(SurgeDetector, "_fetch_bgeometrics", return_value=None)
    def test_summary_contains_level(self, mock_fetch, detector):
        """Summary always shows alert level."""
        result = detector.detect(fng=50, btc_rsi=50)
        assert "暴漲預警等級" in result["summary"]

    @patch.object(SurgeDetector, "_fetch_bgeometrics")
    def test_summary_contains_signals(self, mock_fetch, detector):
        """Summary lists active signals."""
        mock_fetch.return_value = 0.92
        result = detector.detect(fng=15, btc_rsi=25)
        assert "MVRV" in result["summary"]
        assert "F&G" in result["summary"]


class TestBGeometricsCache:
    """bug#10: literal startday=today fix + per-endpoint cache + fail TTL."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.setenv("BGEOMETRICS_API_KEY", "test-key")
        from src.surge_detector import reset_bge_cache
        reset_bge_cache()
        yield
        reset_bge_cache()

    def _fetch(self):
        from src.surge_detector import SurgeDetector
        return SurgeDetector()._fetch_bgeometrics("/mvrv")

    @patch("src.surge_detector.requests.get")
    def test_date_params_3day_lookback_no_literal_today(self, mock_get):
        from datetime import date, timedelta
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = [{"d": "2026-08-19", "mvrv": 1.32}]
        val = self._fetch()
        assert val == 1.32
        params = mock_get.call_args.kwargs["params"]
        assert params["startday"] == (date.today() - timedelta(days=3)).isoformat()
        assert params["endday"] == date.today().isoformat()
        assert "today" not in params.values()

    @patch("src.surge_detector.requests.get")
    def test_success_cached_single_http_call(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = [{"d": "2026-08-19", "mvrv": 1.32}]
        assert self._fetch() == 1.32
        assert self._fetch() == 1.32
        assert mock_get.call_count == 1

    @patch("src.surge_detector.requests.get")
    def test_success_ttl_expiry_refetches(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = [{"d": "2026-08-19", "mvrv": 1.32}]
        self._fetch()
        # bug#14: entries now live in mem AND on disk (written together, so
        # they age together); expire both to simulate a real TTL rollover.
        from src.surge_detector import _BGE_CACHE, BGE_CACHE_TTL_SEC
        _BGE_CACHE["/mvrv"]["fetched_at"] -= BGE_CACHE_TTL_SEC + 60
        _age_disk_entry("/mvrv", BGE_CACHE_TTL_SEC + 60)
        self._fetch()
        assert mock_get.call_count == 2

    @patch("src.surge_detector.requests.get")
    def test_failure_cached_short_ttl(self, mock_get):
        mock_get.return_value.status_code = 429
        mock_get.return_value.json.return_value = []
        assert self._fetch() is None
        assert self._fetch() is None  # cached None, no extra HTTP
        assert mock_get.call_count == 1
        from src.surge_detector import _BGE_CACHE, BGE_FAIL_TTL_SEC
        assert _BGE_CACHE["/mvrv"]["value"] is None
        _BGE_CACHE["/mvrv"]["fetched_at"] -= BGE_FAIL_TTL_SEC + 60
        _age_disk_entry("/mvrv", BGE_FAIL_TTL_SEC + 60)
        assert self._fetch() is None  # retried after fail TTL
        assert mock_get.call_count == 2

    @patch("src.surge_detector.requests.get")
    def test_empty_list_cached_as_failure(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = []
        assert self._fetch() is None
        assert self._fetch() is None
        assert mock_get.call_count == 1

    def test_no_api_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("BGEOMETRICS_API_KEY", raising=False)
        assert self._fetch() is None
