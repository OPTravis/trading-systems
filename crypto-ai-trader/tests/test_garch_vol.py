"""
Tests for GARCH(1,1) Volatility Forecaster.

Run: python -m pytest tests/test_garch_vol.py -v
"""

import json
import math
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

import src.garch_vol as garch


# ---------------------------------------------------------------------------
# Tests: get_vol_regime
# ---------------------------------------------------------------------------


class TestGetVolRegime:
    """Test annualized vol → regime bucket mapping."""

    def test_low_vol(self):
        assert garch.get_vol_regime(0.10) == "low"
        assert garch.get_vol_regime(0.29) == "low"

    def test_normal_vol(self):
        assert garch.get_vol_regime(0.30) == "normal"
        assert garch.get_vol_regime(0.59) == "normal"

    def test_high_vol(self):
        assert garch.get_vol_regime(0.60) == "high"
        assert garch.get_vol_regime(0.99) == "high"

    def test_extreme_vol(self):
        assert garch.get_vol_regime(1.00) == "extreme"
        assert garch.get_vol_regime(2.50) == "extreme"


# ---------------------------------------------------------------------------
# Tests: _rolling_std_fallback
# ---------------------------------------------------------------------------


class TestRollingStdFallback:
    """Test rolling std fallback when GARCH unavailable."""

    def test_short_series(self):
        returns = [0.01, -0.02, 0.005, -0.01, 0.003]
        vol = garch._rolling_std_fallback(returns)
        expected = np.std(returns) * math.sqrt(365)
        assert abs(vol - expected) < 1e-10

    def test_long_series_uses_window(self):
        returns = [0.01] * 100
        vol = garch._rolling_std_fallback(returns)
        # Last 20 are all 0.01 → std ≈ 0 (floating point noise)
        assert vol == pytest.approx(0.0, abs=1e-10)

    def test_annualized(self):
        """Output should be annualized (√365 factor)."""
        returns = [0.01, -0.01] * 50
        vol = garch._rolling_std_fallback(returns)
        daily_vol = np.std(returns[-20:])
        assert abs(vol - daily_vol * math.sqrt(365)) < 1e-10


# ---------------------------------------------------------------------------
# Tests: forecast_volatility
# ---------------------------------------------------------------------------


class TestForecastVolatility:
    """Test volatility forecasting with fallback."""

    def test_insufficient_data_uses_fallback(self):
        """With <30 data points, should use rolling std fallback."""
        returns = [0.01, -0.02, 0.005] * 5  # 15 points
        result = garch.forecast_volatility(returns)
        assert "annualized_vol" in result
        assert "vol_regime" in result
        assert result["annualized_vol"] > 0

    def test_garch_import_failure_fallback(self):
        """When arch is not importable, should fall back gracefully."""
        returns = [0.01, -0.02, 0.005] * 20  # 60 points
        # Temporarily remove arch from sys.modules to simulate ImportError
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "arch" or name.startswith("arch."):
                raise ImportError("no arch")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            result = garch.forecast_volatility(returns)
            assert "annualized_vol" in result
            assert "vol_regime" in result

    def test_output_structure(self):
        """Result should have all required keys."""
        returns = [0.01, -0.02, 0.003, 0.005, -0.001] * 10
        result = garch.forecast_volatility(returns)
        required_keys = {"current_vol", "forecast_vol", "annualized_vol", "vol_regime"}
        assert required_keys.issubset(result.keys())


# ---------------------------------------------------------------------------
# Tests: get_dynamic_sl_tp
# ---------------------------------------------------------------------------


class TestGetDynamicSlTp:
    """Test dynamic SL/TP based on volatility regime."""

    def test_low_vol_tight_params(self):
        result = garch.get_dynamic_sl_tp("BTCUSDT", 50000.0, 0.005)
        assert result["sl_pct"] == -0.05
        assert result["tp_pct"] == 0.08

    def test_extreme_vol_wide_params(self):
        result = garch.get_dynamic_sl_tp("BTCUSDT", 50000.0, 0.10)
        assert result["sl_pct"] == -0.10
        assert result["tp_pct"] == 0.15

    def test_has_trailing_fields(self):
        result = garch.get_dynamic_sl_tp("ETHUSDT", 3000.0, 0.01)
        assert "trailing_activation" in result
        assert "trailing_step" in result

    def test_annualization_correctness(self):
        """Input is daily vol, function annualizes it internally."""
        daily_vol = 0.01
        ann_vol = daily_vol * math.sqrt(365)
        assert garch.get_vol_regime(ann_vol) == "low"
        result = garch.get_dynamic_sl_tp("BTCUSDT", 50000.0, daily_vol)
        assert result["sl_pct"] == -0.05


# ---------------------------------------------------------------------------
# Tests: train_from_klines
# ---------------------------------------------------------------------------


class TestTrainFromKlines:
    """Test GARCH model training from kline data."""

    def test_insufficient_data(self):
        klines = [{"close": 100 + i * 0.1} for i in range(10)]
        assert garch.train_from_klines("BTCUSDT", klines) is False

    def test_training_with_mock_arch(self):
        """Mock arch model to test training flow without real arch package."""
        klines = [{"close": 100 + np.random.randn() * 2} for _ in range(100)]
        mock_result = MagicMock()
        mock_result.params = {"mu": 0.01, "omega": 0.001, "alpha": 0.1, "beta": 0.85}
        mock_result.conditional_volatility = np.array([0.5] * 100)

        mock_model = MagicMock()
        mock_model.fit.return_value = mock_result

        mock_arch_module = MagicMock()
        mock_arch_module.arch_model.return_value = mock_model

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(garch, "DATA_DIR", tmpdir):
                with patch.dict("sys.modules", {"arch": mock_arch_module, "arch_model": mock_arch_module}):
                    result = garch.train_from_klines("BTCUSDT", klines)
                    assert result is True
                    path = os.path.join(tmpdir, "garch_BTCUSDT.json")
                    assert os.path.exists(path)
                    with open(path) as f:
                        data = json.load(f)
                    assert "params" in data
                    assert "volatility" in data


# ---------------------------------------------------------------------------
# Tests: load_model
# ---------------------------------------------------------------------------


class TestLoadModel:
    """Test loading saved GARCH model."""

    def test_no_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(garch, "DATA_DIR", tmpdir):
                assert garch.load_model("NONEXISTENT") is None

    def test_loads_and_enriches(self):
        """Should load saved params and add derived fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "garch_BTCUSDT.json")
            with open(path, "w") as f:
                json.dump({"params": {"mu": 0.01}, "volatility": 0.5}, f)
            with patch.object(garch, "DATA_DIR", tmpdir):
                result = garch.load_model("BTCUSDT")
                assert result is not None
                assert "annualized_vol" in result
                assert "vol_regime" in result
                assert "saved_at" in result
                assert "stale" in result

    def test_stale_detection(self):
        """Model saved >24h ago should be marked stale."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "garch_BTCUSDT.json")
            with open(path, "w") as f:
                json.dump({"params": {"mu": 0.01}, "volatility": 0.005}, f)
            old_time = time.time() - 172800
            os.utime(path, (old_time, old_time))
            with patch.object(garch, "DATA_DIR", tmpdir):
                result = garch.load_model("BTCUSDT")
                assert result["stale"] is True

    def test_fresh_model_not_stale(self):
        """Recently saved model should not be stale."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "garch_BTCUSDT.json")
            with open(path, "w") as f:
                json.dump({"params": {"mu": 0.01}, "volatility": 0.005}, f)
            with patch.object(garch, "DATA_DIR", tmpdir):
                result = garch.load_model("BTCUSDT")
                assert result["stale"] is False

    def test_annualized_vol_from_volatility(self):
        """If annualized_vol not in file, should compute from volatility."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "garch_BTCUSDT.json")
            daily_vol = 0.01
            with open(path, "w") as f:
                json.dump({"params": {"mu": 0.01}, "volatility": daily_vol}, f)
            with patch.object(garch, "DATA_DIR", tmpdir):
                result = garch.load_model("BTCUSDT")
                expected_ann = daily_vol * math.sqrt(365)
                assert abs(result["annualized_vol"] - expected_ann) < 0.01


# ---------------------------------------------------------------------------
# Tests: VOL_REGIMES completeness
# ---------------------------------------------------------------------------


class TestVolRegimes:
    """Test that all regime configs are complete."""

    def test_all_regimes_have_required_keys(self):
        required = {"sl_pct", "tp_pct", "trailing_activation", "trailing_step"}
        for regime, params in garch.VOL_REGIMES.items():
            assert required.issubset(params.keys()), f"Regime {regime} missing keys"

    def test_sl_negative_tp_positive(self):
        for regime, params in garch.VOL_REGIMES.items():
            assert params["sl_pct"] < 0, f"{regime} sl_pct should be negative"
            assert params["tp_pct"] > 0, f"{regime} tp_pct should be positive"
