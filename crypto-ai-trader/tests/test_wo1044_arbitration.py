"""
WO-1044: strategy deadlock + position concentration — regression tests.

Covers the five fixes:
  A — HMM × bull_regime arbitration (_apply_risk_overlays)
  B — GREED participation: DCA dip -4%, RSI overbought 75 (rsi_params pipe)
  C — trailing stop STEPS tightened 6/5/3/2% → 2.5/2/2/1.5%
  D — GREED threshold calibrated from kv threshold_calibration (P75)
  E — concentration floor ($100-130 ≈ 26% of equity) + yaml caps recompute
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.adaptive_trailing import AdaptiveTrailingStop
from src.strategy_adaptor import StrategyAdaptor

from tests.test_e2e_auto_trade import _make_bc, _make_notifier, _so_filters

HMM_BEAR = {"regime": "BEAR_TREND", "confidence": 0.7}


def _adapt(fng=50, btc_trend="BULLISH", btc_24h=0.0, bull_tier=None,
           hmm=None, kv=None):
    """Run adaptor.adapt() with all external IO frozen.

    hmm: dict patched as HMMRegimeDetector.get_cached_prediction (None = no HMM).
    kv:  dict returned by state_db kv_get("threshold_calibration").
    """
    adaptor = StrategyAdaptor()
    db = MagicMock()
    db.kv_get.return_value = kv
    db.portfolio_get_all.return_value = {}
    db.portfolio_get_cash_balance.return_value = 0.0
    with patch("requests.get", side_effect=OSError("frozen")), \
         patch("src.hmm_regime.HMMRegimeDetector.get_cached_prediction",
               return_value=hmm), \
         patch("src.cvar_risk.CVaRRiskManager.compute_portfolio_risk",
               side_effect=OSError("frozen")), \
         patch("src.contextual_bandit.ContextualBandit.recommend_sltp",
               return_value=(1.0, 1.0)), \
         patch("src.contextual_bandit.ContextualBandit.recommend_size",
               return_value=1.0), \
         patch("src.garch_vol.forecast_volatility",
               side_effect=Exception("frozen")), \
         patch("src.state_db.get_state_db", return_value=db):
        return adaptor.adapt(
            fear_greed=fng, btc_trend=btc_trend,
            btc_price_change_24h=btc_24h, bull_tier=bull_tier,
        )


# ── Fix A: HMM × bull_regime arbitration ───────────────────────────────

class TestWO1044AArbitration:

    def test_conflict_keeps_trend_at_half_size(self):
        """HMM BEAR_TREND avoid[trend,grid] + CONFIRMED_BULL → trend stays
        ON at ×0.5 instead of the unconditional kill (deadlock root cause)."""
        r = _adapt(bull_tier="CONFIRMED_BULL", hmm=HMM_BEAR)
        trend = r["strategies"]["trend"]
        assert trend["enabled"] is True
        assert trend["size_multiplier"] == 0.5
        assert "arbitrated" in trend["reason"]
        assert any("trend: kept ON ×0.5" in c for c in r["changes"])

    def test_conflict_does_not_revive_p0a1_grid(self):
        """Grid already hard-disabled by P0-A1 in CONFIRMED_BULL — the
        arbitration must NOT resurrect it, only spare the living ones.
        (A later global size-floor bumps size 0.0→0.2 cosmetically, but
        enabled=False stays authoritative for selection.)"""
        r = _adapt(bull_tier="CONFIRMED_BULL", hmm=HMM_BEAR)
        grid = r["strategies"]["grid"]
        assert grid["enabled"] is False
        assert "arbitrated" not in grid["reason"]
        assert not any("grid: kept ON" in c for c in r["changes"])
        assert any("grid: force-OFF — CONFIRMED_BULL (P0-A1)" in c
                   for c in r["changes"])

    def test_conflict_waives_threshold_addon(self):
        """HMM BEAR_TREND +10 threshold is waived during the divergence —
        the '85 vs score band 69-88' starvation must not reappear."""
        base = _adapt(hmm=None)["global"]["score_threshold"]
        conflict = _adapt(bull_tier="CONFIRMED_BULL", hmm=HMM_BEAR)["global"]["score_threshold"]
        assert conflict == base  # +10 waived
        assert conflict == pytest.approx(base, abs=0.01)

    def test_consistent_verdict_keeps_original_kill(self):
        """bull tier not CONFIRMED_BULL → two detectors agree → original
        conservative behaviour: trend disabled, threshold +10 applied."""
        r = _adapt(bull_tier="TRANSITION", hmm=HMM_BEAR)
        assert r["strategies"]["trend"]["enabled"] is False
        base = _adapt(hmm=None)["global"]["score_threshold"]
        assert r["global"]["score_threshold"] == base + 10

    def test_no_bull_tier_defaults_to_conservative(self):
        """Live callers that don't pass bull_tier keep the old behaviour."""
        r = _adapt(bull_tier=None, hmm=HMM_BEAR)
        assert r["strategies"]["trend"]["enabled"] is False


# ── Fix B: GREED participation ─────────────────────────────────────────

class TestWO1044BGreedParticipation:

    def test_greed_keeps_dca_with_deeper_dip(self):
        r = _adapt(fng=75)  # GREED
        dca = r["strategies"]["dca"]
        assert dca["enabled"] is True
        assert dca["size_multiplier"] == 0.8
        assert "WO-1044-B" in dca["reason"]
        assert r["dca_params"]["dip_threshold_pct"] == -4.0

    def test_greed_keeps_rsi_with_raised_overbought(self):
        r = _adapt(fng=75)
        rsi = r["strategies"]["rsi_reversion"]
        assert rsi["enabled"] is True
        assert rsi["size_multiplier"] == 0.8
        assert r["rsi_params"] == {"rsi_overbought": 75}

    def test_extreme_greed_still_disables_both(self):
        r = _adapt(fng=85)  # EXTREME_GREED — hard kill unchanged
        assert r["strategies"]["dca"]["enabled"] is False
        assert r["strategies"]["rsi_reversion"]["enabled"] is False
        assert r.get("rsi_params") is None


# ── Fix D: calibrated GREED threshold ─────────────────────────────────

class TestWO1044DCalibration:

    def test_kv_p75_overrides_greed_threshold(self):
        r = _adapt(fng=75, kv={"p75": 78.43})
        assert r["global"]["score_threshold"] == 78

    def test_invalid_kv_falls_back_to_default(self):
        assert _adapt(fng=75, kv={"p75": 120})["global"]["score_threshold"] == 75
        assert _adapt(fng=75, kv=None)["global"]["score_threshold"] == 75
        assert _adapt(fng=75, kv={"p75": "bad"})["global"]["score_threshold"] == 75

    def test_boundary_values_accepted(self):
        assert _adapt(fng=75, kv={"p75": 40})["global"]["score_threshold"] == 40
        assert _adapt(fng=75, kv={"p75": 95})["global"]["score_threshold"] == 95


# ── Fix C: tightened trailing steps ────────────────────────────────────

class TestWO1044CTrailing:

    def test_steps_tightened_but_names_stable(self):
        widths = [s[1] for s in AdaptiveTrailingStop.STEPS]
        names = [s[2] for s in AdaptiveTrailingStop.STEPS]
        assert widths == [0.025, 0.02, 0.02, 0.015]
        assert names == ["step_1_6", "step_3_5", "step_5_10", "step_10_plus"]

    def test_1_3pct_band_now_locks_profit(self):
        """2% profit used to trail 6% (no lock); now trails 2.5%."""
        ats = AdaptiveTrailingStop()
        r = ats.calculate_trailing_sl(
            entry_price=100.0, current_price=102.0,
            highest_price=102.0, initial_sl=94.0,
            volatility_adjustment=1.0,
        )
        assert r["trailing_active"] is True
        assert r["step"] == "step_1_6"
        expected = max(102.0 * (1 - 0.025), 100.0 * 1.005)  # min profit lock
        assert r["trailing_sl"] == pytest.approx(expected, abs=0.01)
        assert r["trailing_sl"] > 100.0  # locks ≥ entry — the 39/92 giveback fix


# ── Fix E: concentration floor + caps ──────────────────────────────────

class TestWO1044ESizing:

    def test_risk_limits_yaml_recalculated(self):
        cfg = yaml.safe_load(open(os.path.join(PROJECT_ROOT, "config/risk_limits.yaml")))
        risk = cfg["risk"]
        assert risk["max_position_pct"] == 30
        assert risk["max_total_exposure_pct"] == 95
        assert risk["cash_reserve_pct"] == 10
        assert risk["max_daily_loss_pct"] == 5
        assert risk["max_open_positions"] == 4

    def test_risk_params_max_active_positions_four(self):
        cfg = yaml.safe_load(open(os.path.join(PROJECT_ROOT, "config/risk_params.yaml")))
        assert cfg["trade_executor"]["max_active_positions"] == 4

    def test_kelly_max_position_pct_raised(self):
        from src.kelly_sizer import MAX_POSITION_PCT
        assert MAX_POSITION_PCT == 0.30

    def _run_exec(self, usdt_bal=1000, kelly_result=None):
        bc = _make_bc(usdt_free=usdt_bal)
        so = _so_filters()
        tps = [
            {"pct": 4.0, "size_pct": 40},
            {"pct": 8.0, "size_pct": 40},
            {"pct": 15.0, "size_pct": 20},
        ]
        with patch("src.trade_executor.get_trading_client", return_value=bc), \
             patch("src.trade_executor.FeishuNotifier", return_value=_make_notifier()), \
             patch("src.trade_executor.count_active_positions", return_value=0), \
             patch("src.smart_order.SmartOrder", return_value=so), \
             patch("src.trade_executor.PortfolioManager"), \
             patch("src.circuit_breaker.CircuitBreaker") as mock_cb, \
             patch("src.daily_loss_breaker.get_daily_loss_breaker") as mock_dlb, \
             patch("src.drawdown_breaker.DrawdownBreaker") as mock_ddb, \
             patch("src.kelly_sizer.KellyPositionSizer") as mock_kelly, \
             patch("src.fee_optimizer.FeeOptimizer") as mock_fee, \
             patch("src.state_db.get_state_db"), \
             patch("src.twap_vwap.time.sleep"):
            mock_cb.return_value.is_tripped.return_value = False
            dl = mock_dlb.return_value
            dl.check_daily_loss.return_value = {"tier": 0}
            dl.should_close_all.return_value = False
            dl.should_block_new_trades.return_value = False
            dl.get_position_size_multiplier.return_value = 1.0
            mock_ddb.return_value.check_drawdown.return_value = {"drawdown_pct": 0}
            mock_fee.return_value.get_effective_fees.return_value = {"taker_fee": 0.001}
            mock_kelly.return_value.get_position_size.return_value = kelly_result
            mock_kelly.return_value.adjust_for_portfolio.side_effect = (
                lambda res, *a, **k: res
            )
            from main import execute_auto_trade
            return execute_auto_trade(
                "SOLUSDT", 100.0, "trend", 2.0, tps, 98.0, 24,
                ["RSI Oversold"], "RSI Oversold", score=75,
            ), bc

    def test_concentration_floor_lifts_dust_order(self):
        """Degenerate Kelly (1% → $10 dust) must be lifted to the $100-130
        concentration floor — $10 probes' TP profit is eaten by fees."""
        result, _ = self._run_exec(kelly_result={
            "position_pct": 0.01, "confidence": "high",
        })
        assert result["success"] is True
        assert result["is_exploration"] is False
        # qty comes from the mocked exchange fill — assert on invest_pct
        invested = result["invest_pct"] / 100.0 * 1000.0
        assert invested >= 100.0, f"dust order not lifted: ${invested:.2f}"
        assert invested <= 130.0

    def test_exploration_exempt_from_floor(self):
        """Exploration probes stay on the epsilon budget — no floor."""
        result, _ = self._run_exec(kelly_result={
            "position_pct": 0.01, "confidence": "high", "is_exploration": True,
        })
        assert result["success"] is True
        assert result["is_exploration"] is True
        invested = result["invest_pct"] / 100.0 * 1000.0
        assert invested < 50.0, f"exploration wrongly floored: ${invested:.2f}"
