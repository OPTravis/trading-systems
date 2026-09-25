"""CVaR overlay stage-1 shadow log (P7 tail batch, Travis 9/25 go-ahead).

Pure observation, zero mainline impact:
- CVaRRiskManager.log_shadow_observation computes what position_scale
  WOULD be given a real position snapshot and appends
  {ts, scale_if_active, risk_level, cvar_95, n_positions, n_samples}
  to kv cvar:shadow_log (rolling window). All failures swallowed.
- StrategyAdaptor.adapt keeps the PINNED empty-list mainline
  (cvar_scale stays 1.0 — no "CVaR" entries in changes) while the
  shadow snapshot is recorded alongside.
"""

import json
import os
import sys
from contextlib import ExitStack
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import cvar_risk as cr_mod
from src import state_db as sd_mod
from src.cvar_risk import CVaRRiskManager, SHADOW_LOG_KEY
from src.state_db import StateDB


class _StubDB:
    """Minimal kv + outcomes stub (no sqlite needed for unit tests)."""

    def __init__(self, net_pnls=None):
        self.kv = {}
        self._net_pnls = list(net_pnls or [])

    def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    def kv_set(self, key, value):
        self.kv[key] = value

    def outcomes_recent_net_pnls(self, limit=100):
        return list(self._net_pnls)


class TestLogShadowObservation:
    def test_empty_positions_records_default(self):
        db = _StubDB()
        mgr = CVaRRiskManager(db=db)
        entry = mgr.log_shadow_observation([])
        assert entry["scale_if_active"] == 1.0
        assert entry["risk_level"] == "low"
        assert entry["n_positions"] == 0
        log = db.kv[SHADOW_LOG_KEY]
        assert isinstance(log, list) and len(log) == 1 and log[0] is entry

    def test_critical_returns_drive_scale_03(self):
        # 12 samples >= 10 -> db-returns path; all -20% -> CVaR_95 = -20
        db = _StubDB(net_pnls=[-20.0] * 12)
        mgr = CVaRRiskManager(db=db)
        pos = [{"symbol": "BTCUSDT", "quantity": 1.0,
                "entry_price": 100.0, "current_price": 100.0}]
        entry = mgr.log_shadow_observation(pos)
        assert entry["scale_if_active"] == 0.3
        assert entry["risk_level"] == "critical"
        assert entry["cvar_95"] == -20.0
        assert entry["n_positions"] == 1
        assert entry["n_samples"] == 12

    def test_medium_band_scale_08(self):
        db = _StubDB(net_pnls=[-5.0] * 12)  # -8 < -5 < -3 -> medium
        mgr = CVaRRiskManager(db=db)
        entry = mgr.log_shadow_observation(
            [{"symbol": "A", "quantity": 1, "entry_price": 10,
              "current_price": 10}])
        assert entry["scale_if_active"] == 0.8
        assert entry["risk_level"] == "medium"

    def test_low_risk_scale_up_1_2_recorded_as_is(self):
        # Activation is not only downside: low risk would scale UP to 1.2.
        # The shadow log must record that fact as-is, not clamp it.
        db = _StubDB(net_pnls=[-1.0] * 12)  # -1 > -3 -> low -> 1.2
        mgr = CVaRRiskManager(db=db)
        entry = mgr.log_shadow_observation(
            [{"symbol": "A", "quantity": 1, "entry_price": 10,
              "current_price": 10}])
        assert entry["scale_if_active"] == 1.2
        assert entry["risk_level"] == "low"

    def test_rolling_window_keeps_latest(self, monkeypatch):
        monkeypatch.setattr(cr_mod, "SHADOW_LOG_MAX_ENTRIES", 3)
        db = _StubDB()
        mgr = CVaRRiskManager(db=db)
        entries = [mgr.log_shadow_observation([]) for _ in range(5)]
        log = db.kv[SHADOW_LOG_KEY]
        assert len(log) == 3
        assert log[-1] is entries[-1]
        assert all(e not in log for e in entries[:2])

    def test_non_list_corrupted_log_resets(self):
        db = _StubDB()
        db.kv[SHADOW_LOG_KEY] = "garbage-not-a-list"
        mgr = CVaRRiskManager(db=db)
        entry = mgr.log_shadow_observation([])
        assert db.kv[SHADOW_LOG_KEY] == [entry]

    def test_failure_swallowed_no_write(self):
        db = _StubDB()
        mgr = CVaRRiskManager(db=db)
        with patch.object(CVaRRiskManager, "compute_portfolio_risk",
                          side_effect=OSError("boom")):
            assert mgr.log_shadow_observation([]) is None
        assert SHADOW_LOG_KEY not in db.kv


def _frozen_adapt(**kwargs):
    """adapt() under the P5 freeze contract (no network / no HMM / cold
    bandit neutrals) — the shadow snapshot must still be recorded."""
    from src.strategy_adaptor import StrategyAdaptor
    adaptor = StrategyAdaptor()
    with ExitStack() as st:
        st.enter_context(patch("requests.get",
                               side_effect=OSError("frozen for shadow")))
        st.enter_context(patch(
            "src.hmm_regime.HMMRegimeDetector.get_cached_prediction",
            return_value=None))
        st.enter_context(patch(
            "src.contextual_bandit.ContextualBandit.recommend_sltp",
            return_value=(1.0, 1.0)))
        st.enter_context(patch(
            "src.contextual_bandit.ContextualBandit.recommend_size",
            return_value=0.8))
        return adaptor.adapt(
            fear_greed=kwargs.get("fear_greed", 50),
            btc_trend=kwargs.get("btc_trend", "NEUTRAL"),
            btc_price_change_24h=kwargs.get("btc_price_change_24h", 0.0),
        )


class TestAdaptorShadowIntegration:
    def _seed(self, tmp_path, monkeypatch, name="shadow.db"):
        monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / name))
        db = sd_mod.get_state_db()
        db.portfolio_set("BTCUSDT", {"quantity": 2.0, "entry_price": 100.0,
                                     "strategy": "DCA"})
        for i in range(12):
            db._get_conn().execute(
                "INSERT INTO trade_outcomes (symbol, entry_time, entry_price,"
                " qty, status, exit_time, exit_price, net_pnl_pct, is_win,"
                " strategy, created_at, updated_at, contaminated)"
                " VALUES (?,?,?,?, 'closed', ?, 80.0, -20.0, 0, 'DCA', 1, 1, 0)",
                ("BTCUSDT", 1 + i, 100.0, 1.0, 2 + i))
            db._get_conn().commit()
        return db

    def test_shadow_logged_and_mainline_unchanged(self, tmp_path, monkeypatch):
        db = self._seed(tmp_path, monkeypatch)
        result = _frozen_adapt()
        log = db.kv_get(SHADOW_LOG_KEY)
        assert isinstance(log, list) and len(log) >= 1
        e = log[-1]
        assert e["n_positions"] == 1
        assert e["scale_if_active"] == 0.3      # would have been critical
        assert e["risk_level"] == "critical"
        assert e["n_samples"] == 12
        # mainline red line: the PINNED path keeps scale at 1.0 — no CVaR
        # overlay entries in changes, even though shadow says 0.3.
        assert "CVaR" not in json.dumps(result.get("changes", []))

    def test_shadow_failure_does_not_break_adapt(self, tmp_path, monkeypatch):
        db = self._seed(tmp_path, monkeypatch, name="shadow2.db")
        with patch.object(CVaRRiskManager, "log_shadow_observation",
                          side_effect=OSError("shadow boom")):
            result = _frozen_adapt()
        assert result["regime"] == "NEUTRAL"
        assert "CVaR" not in json.dumps(result.get("changes", []))
        assert isinstance(result.get("strategies"), dict)


class TestReportScript:
    def test_report_on_seeded_db(self, tmp_path):
        import subprocess
        db = StateDB(str(tmp_path / "rep.db"))
        mgr = CVaRRiskManager(db=db)
        mgr.log_shadow_observation([])
        mgr.log_shadow_observation(
            [{"symbol": "A", "quantity": 1, "entry_price": 10,
              "current_price": 10}])
        db._get_conn().close()
        out = subprocess.run(
            [sys.executable, "scripts/cvar_shadow_report.py",
             "--db", str(tmp_path / "rep.db")],
            capture_output=True, text=True, check=True).stdout
        assert "shadow report" in out
        assert "scale" in out
        js = subprocess.run(
            [sys.executable, "scripts/cvar_shadow_report.py",
             "--db", str(tmp_path / "rep.db"), "--json"],
            capture_output=True, text=True, check=True).stdout
        parsed = json.loads(js)
        assert parsed["n_observations"] == 2

    def test_report_empty_db(self, tmp_path):
        import subprocess
        db = StateDB(str(tmp_path / "empty.db"))
        db._get_conn().close()
        out = subprocess.run(
            [sys.executable, "scripts/cvar_shadow_report.py",
             "--db", str(tmp_path / "empty.db")],
            capture_output=True, text=True, check=True).stdout
        assert "no shadow observations" in out
