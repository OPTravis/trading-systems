"""
P2-fix: Unit tests for volatility_adjustment in adaptive trailing stop.

Tests:
1. volatility_adjustment is properly applied to trail_width
2. High vol → wider trailing stop (looser stop)
3. Low vol → tighter trailing stop (closer to price)
4. Default volatility_adjustment=1.0 preserves original behavior
5. StrategyAdaptor.compute_volatility_adjustment produces correct values
6. Integration: adapt() output includes volatility_adjustment
"""

import math
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.adaptive_trailing import AdaptiveTrailingStop, calculate_trailing_sl


class TestAdaptiveTrailingVolatility:
    """Test that volatility_adjustment is correctly applied to trailing width."""

    @pytest.fixture
    def ats(self):
        return AdaptiveTrailingStop()

    def test_default_volatility_preserves_base_width(self, ats):
        """volatility_adjustment=1.0 should produce exact same result as base width."""
        result = ats.calculate_trailing_sl(
            entry_price=100.0,
            current_price=105.0,  # 5% profit → step_5_10, base_width=0.03
            highest_price=106.0,
            initial_sl=95.0,
            volatility_adjustment=1.0,
        )
        assert result["trailing_active"] is True
        # Trail width = 0.03 * 1.0 = 0.03
        expected_sl = 106.0 * (1 - 0.03)
        assert result["trailing_sl"] == pytest.approx(expected_sl, abs=0.01)

    def test_high_volatility_wider_trailing_stop(self, ats):
        """High vol (adjustment > 1.0) → wider trail → lower trailing SL."""
        result_normal = ats.calculate_trailing_sl(
            entry_price=100.0,
            current_price=105.0,
            highest_price=106.0,
            initial_sl=95.0,
            volatility_adjustment=1.0,
        )
        result_high_vol = ats.calculate_trailing_sl(
            entry_price=100.0,
            current_price=105.0,
            highest_price=106.0,
            initial_sl=95.0,
            volatility_adjustment=1.5,
        )
        # High vol → wider trail → lower SL
        assert result_high_vol["trailing_sl"] < result_normal["trailing_sl"]

    def test_low_volatility_tighter_trailing_stop(self, ats):
        """Low vol (adjustment < 1.0) → tighter trail → higher trailing SL."""
        result_normal = ats.calculate_trailing_sl(
            entry_price=100.0,
            current_price=105.0,
            highest_price=106.0,
            initial_sl=95.0,
            volatility_adjustment=1.0,
        )
        result_low_vol = ats.calculate_trailing_sl(
            entry_price=100.0,
            current_price=105.0,
            highest_price=106.0,
            initial_sl=95.0,
            volatility_adjustment=0.7,
        )
        # Low vol → tighter trail → higher SL
        assert result_low_vol["trailing_sl"] > result_normal["trailing_sl"]

    def test_volatility_applied_to_step_1_6(self, ats):
        """Test volatility adjustment on step_1_6 (profit 1-3%, base=6%)."""
        # Use high enough price so min_sl doesn't override
        result = ats.calculate_trailing_sl(
            entry_price=100.0,
            current_price=102.0,  # 2% profit → step_1_6
            highest_price=110.0,  # High enough that trail SL > min_sl
            initial_sl=95.0,
            volatility_adjustment=1.3,
        )
        assert result["trailing_active"] is True
        assert result["step"] == "step_1_6"
        # Trail width = 0.06 * 1.3 = 0.078
        expected_sl = 110.0 * (1 - 0.06 * 1.3)
        min_sl = 100.0 * (1 + 0.005)  # 0.5% profit lock
        # SL should be max(trail_sl, min_sl)
        expected_sl = max(expected_sl, min_sl)
        assert result["trailing_sl"] == pytest.approx(expected_sl, abs=0.01)

    def test_volatility_applied_to_step_10_plus(self, ats):
        """Test volatility adjustment on step_10_plus (profit >10%, base=2%)."""
        result = ats.calculate_trailing_sl(
            entry_price=100.0,
            current_price=115.0,  # 15% profit → step_10_plus
            highest_price=116.0,
            initial_sl=95.0,
            volatility_adjustment=1.5,
        )
        assert result["trailing_active"] is True
        assert result["step"] == "step_10_plus"
        # Trail width = 0.02 * 1.5 = 0.03
        expected_sl = 116.0 * (1 - 0.02 * 1.5)
        assert result["trailing_sl"] == pytest.approx(expected_sl, abs=0.01)

    def test_volatility_with_min_sl_floor(self, ats):
        """Even with high volatility, SL should never go below min_profit_lock."""
        result = ats.calculate_trailing_sl(
            entry_price=100.0,
            current_price=101.5,  # 1.5% profit
            highest_price=101.5,
            initial_sl=95.0,
            volatility_adjustment=2.0,  # Very wide trail
        )
        min_sl = 100.0 * (1 + 0.005)  # 0.5% min profit lock
        assert result["trailing_sl"] >= min_sl

    def test_convenience_function_passes_volatility(self):
        """Module-level calculate_trailing_sl should pass volatility_adjustment."""
        result = calculate_trailing_sl(
            entry_price=100.0,
            current_price=105.0,
            highest_price=106.0,
            initial_sl=95.0,
            volatility_adjustment=1.3,
        )
        # Trail width = 0.03 * 1.3 = 0.039
        expected_sl = 106.0 * (1 - 0.03 * 1.3)
        assert result["trailing_sl"] == pytest.approx(expected_sl, abs=0.01)

    def test_volatility_adjustment_zero_or_negative(self, ats):
        """Edge case: zero or negative adjustment should still work (tight trail)."""
        result = ats.calculate_trailing_sl(
            entry_price=100.0,
            current_price=105.0,
            highest_price=106.0,
            initial_sl=95.0,
            volatility_adjustment=0.0,  # Extreme tight
        )
        # Trail width = 0.03 * 0.0 = 0.0 → SL = highest_price
        assert result["trailing_sl"] == pytest.approx(106.0, abs=0.01)

    def test_all_steps_volatility_scaling(self, ats):
        """Verify all 4 steps properly scale with volatility adjustment."""
        vol_adj = 1.3
        test_cases = [
            # (current_price, highest_price, expected_step, expected_base_width)
            (102.0, 110.0, "step_1_6", 0.06),    # 2% profit, high peak for min_sl
            (104.0, 110.0, "step_3_5", 0.05),    # 4% profit
            (106.0, 115.0, "step_5_10", 0.03),   # 6% profit
            (112.0, 120.0, "step_10_plus", 0.02), # 12% profit
        ]
        entry = 100.0
        min_sl = entry * (1 + 0.005)  # 0.5% profit lock
        for price, peak, expected_step, base_width in test_cases:
            result = ats.calculate_trailing_sl(
                entry_price=entry,
                current_price=price,
                highest_price=peak,
                initial_sl=95.0,
                volatility_adjustment=vol_adj,
            )
            assert result["step"] == expected_step, f"Wrong step at price={price}"
            expected_trail = peak * (1 - base_width * vol_adj)
            expected_sl = max(expected_trail, min_sl)
            assert result["trailing_sl"] == pytest.approx(expected_sl, abs=0.01), (
                f"Wrong SL at price={price}, step={expected_step}"
            )


class TestStrategyAdaptorVolatility:
    """Test StrategyAdaptor.compute_volatility_adjustment method."""

    def test_compute_low_vol_adjustment(self):
        """Low 24h BTC change → adjustment 0.7."""
        from src.strategy_adaptor import StrategyAdaptor

        adaptor = StrategyAdaptor()
        # Mock GARCH to fail so we hit the 24h fallback
        with patch("src.garch_vol.forecast_volatility", side_effect=Exception("no garch")):
            adj = adaptor.compute_volatility_adjustment(btc_price_change_24h=1.0)
        assert adj == 0.7

    def test_compute_normal_vol_adjustment(self):
        """Normal 24h BTC change (3%) → adjustment 1.0."""
        from src.strategy_adaptor import StrategyAdaptor

        adaptor = StrategyAdaptor()
        with patch("src.garch_vol.forecast_volatility", side_effect=Exception("no garch")):
            adj = adaptor.compute_volatility_adjustment(btc_price_change_24h=3.0)
        assert adj == 1.0

    def test_compute_high_vol_adjustment(self):
        """High 24h BTC change (7%) → adjustment 1.3."""
        from src.strategy_adaptor import StrategyAdaptor

        adaptor = StrategyAdaptor()
        with patch("src.garch_vol.forecast_volatility", side_effect=Exception("no garch")):
            adj = adaptor.compute_volatility_adjustment(btc_price_change_24h=7.0)
        assert adj == 1.3

    def test_compute_extreme_vol_adjustment(self):
        """Extreme 24h BTC change (12%) → adjustment 1.5."""
        from src.strategy_adaptor import StrategyAdaptor

        adaptor = StrategyAdaptor()
        with patch("src.garch_vol.forecast_volatility", side_effect=Exception("no garch")):
            adj = adaptor.compute_volatility_adjustment(btc_price_change_24h=12.0)
        assert adj == 1.5

    def test_garch_takes_priority_over_fallback(self):
        """When GARCH is available, use its vol_regime to determine adjustment."""
        from src.strategy_adaptor import StrategyAdaptor

        adaptor = StrategyAdaptor()
        # Mock GARCH to return 'high' vol regime
        mock_vol_result = {
            "current_vol": 0.05,
            "forecast_vol": 0.05,
            "annualized_vol": 0.80,
            "vol_regime": "high",
        }
        with patch(
            "src.garch_vol.forecast_volatility",
            return_value=mock_vol_result,
        ), patch(
            "src.garch_vol.get_vol_regime",
            return_value="high",
        ):
            adj = adaptor.compute_volatility_adjustment(
                btc_price_change_24h=2.0,  # Would be 1.0 via fallback
                daily_returns=[0.01, -0.02, 0.015] * 10,  # Enough data
            )
        # GARCH says 'high' → 1.3, not fallback 1.0
        assert adj == 1.3

    def test_garch_extreme_regime(self):
        """GARCH extreme vol → adjustment 1.5."""
        from src.strategy_adaptor import StrategyAdaptor

        adaptor = StrategyAdaptor()
        mock_vol_result = {
            "current_vol": 0.10,
            "forecast_vol": 0.10,
            "annualized_vol": 1.50,
            "vol_regime": "extreme",
        }
        with patch(
            "src.garch_vol.forecast_volatility",
            return_value=mock_vol_result,
        ), patch(
            "src.garch_vol.get_vol_regime",
            return_value="extreme",
        ):
            adj = adaptor.compute_volatility_adjustment(
                btc_price_change_24h=3.0,
                daily_returns=[0.02, -0.03, 0.025] * 10,
            )
        assert adj == 1.5

    def test_garch_low_regime(self):
        """GARCH low vol → adjustment 0.7."""
        from src.strategy_adaptor import StrategyAdaptor

        adaptor = StrategyAdaptor()
        mock_vol_result = {
            "current_vol": 0.01,
            "forecast_vol": 0.01,
            "annualized_vol": 0.20,
            "vol_regime": "low",
        }
        with patch(
            "src.garch_vol.forecast_volatility",
            return_value=mock_vol_result,
        ), patch(
            "src.garch_vol.get_vol_regime",
            return_value="low",
        ):
            adj = adaptor.compute_volatility_adjustment(
                btc_price_change_24h=3.0,
                daily_returns=[0.001, -0.002, 0.001] * 10,
            )
        assert adj == 0.7

    def test_adapt_output_includes_volatility_adjustment(self):
        """adapt() should include volatility_adjustment in its output dict."""
        from src.strategy_adaptor import StrategyAdaptor

        adaptor = StrategyAdaptor()
        # Mock everything that adapt() needs to not crash
        with patch("src.hmm_regime.HMMRegimeDetector") as mock_hmm, \
             patch("src.cvar_risk.CVaRRiskManager") as mock_cvar, \
             patch("src.param_optimizer.ParamOptimizer") as mock_param, \
             patch("src.garch_vol.forecast_volatility") as mock_fc, \
             patch("src.garch_vol.get_dynamic_sl_tp") as mock_dsl, \
             patch("src.garch_vol.get_vol_regime", return_value="normal"), \
             patch("requests.get") as mock_req:
            mock_hmm.return_value.get_cached_prediction.return_value = None
            mock_cvar.return_value._db._get_conn.return_value.execute.return_value.fetchall.return_value = []
            mock_param.return_value.get_current_params.return_value = {"score_threshold": 60}
            mock_fc.return_value = None
            mock_dsl.return_value = None
            # Mock klines fetch
            mock_resp = MagicMock()
            mock_resp.json.return_value = [[1, "100", "105", "99", "102", "1000", 2, "100000", 50, "500", "250", "0"]] * 31
            mock_req.return_value = mock_resp

            result = adaptor.adapt(fear_greed=50, btc_trend="neutral", btc_price_change_24h=3.0)

        assert "volatility_adjustment" in result
        assert isinstance(result["volatility_adjustment"], float)
        assert result["volatility_adjustment"] > 0
