"""Phase 2B tests: dual-window PF auto-switch, dca guardrail, adaptor veto."""
import json
import time

import pytest


# ---------------------------------------------------------------- helpers
@pytest.fixture
def statedb(tmp_path, monkeypatch):
    import src.state_db as sd_mod

    test_db_path = str(tmp_path / "test_p2b.db")
    monkeypatch.setenv("STATE_DB_PATH", test_db_path)
    monkeypatch.setenv("TESTING", "1")
    sd_mod._state_db_instance = None
    yield sd_mod.get_state_db(test_db_path)
    sd_mod._state_db_instance = None


def _seed_closed_trades(db, strategy, pnls, base_ts=None, symbol="TESTUSDT"):
    """Insert closed trade_outcomes rows via SQL (no recorder side effects)."""
    base = base_ts if base_ts is not None else (time.time() - 30 * 86400)
    conn = db._get_conn()
    for i, pnl in enumerate(pnls):
        ts = base + i * 3600
        conn.execute(
            """INSERT INTO trade_outcomes
               (symbol, entry_time, entry_date, entry_price, qty, score,
                strategy, status, exit_time, exit_price, exit_reason,
                pnl_pct, net_pnl_pct, is_win, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, ts, time.strftime("%Y-%m-%d", time.gmtime(ts)), 100.0, 1.0,
             60.0, strategy, ts + 1800, 100.0 * (1 + pnl / 100.0), "tp",
             pnl, pnl, 1 if pnl > 0 else 0, ts, ts),
        )
    conn.commit()


def _set_rolling(db, strategy, stats):
    raw = db.kv_get("strategy_rolling_stats") or {}
    raw[strategy] = stats
    db.kv_set("strategy_rolling_stats", raw)


def _get_audit_actions(db):
    conn = db._get_conn()
    return [r["action"] for r in conn.execute(
        "SELECT action FROM audit_log ORDER BY id").fetchall()]


# ------------------------------------------------- canonical naming
class TestCanonicalStrategy:
    def test_alias_maps_rsi_to_adaptor_name(self):
        from src.strategy_evolver import canonical_strategy
        assert canonical_strategy("rsi") == "rsi_reversion"
        assert canonical_strategy("RSI") == "rsi_reversion"
        assert canonical_strategy("rsi_reversion") == "rsi_reversion"
        assert canonical_strategy("dca") == "dca"

    def test_unknown_passthrough(self):
        from src.strategy_evolver import canonical_strategy
        assert canonical_strategy("switch") == "switch"

    def test_disabled_kv_reads_are_canonicalized(self, statedb):
        from src.strategy_evolver import StrategyEvolver
        statedb.kv_set("evolved_disabled", {"rsi": {"disabled_at": 1.0, "reason": "x"}})
        ev = StrategyEvolver(db=statedb)
        assert "rsi_reversion" in ev.get_disabled_strategies()
        assert "rsi" not in ev.get_disabled_strategies()


# ------------------------------------------------- dual-window disable
class TestDualWindowDisable:
    def test_both_windows_bad_disables(self, statedb):
        from src.strategy_evolver import StrategyEvolver

        _seed_closed_trades(statedb, "trend", [-2.0, -1.5, 1.0, -2.5, -1.0,
                                               -2.0, 1.0, -1.5, -2.0, -1.0,
                                               0.5, -2.0, -1.0, -1.5, -0.5])
        _set_rolling(statedb, "trend", {
            "n": 20, "wins": 5, "wr": 25.0, "pf": 0.45, "avg_pnl": -1.2,
            "insufficient": False,
        })
        ev = StrategyEvolver(db=statedb)
        changes = ev.evaluate_pf_channel()

        acts = [c["action"] for c in changes]
        assert "DISABLED_PF" in acts
        entry = ev.get_disabled_strategies()["trend"]
        assert entry["channel"] == "pf_dual_window"
        assert entry["short_pf"] < 1.0 and entry["long_pf"] < 1.0
        assert "strategy_disabled_pf" in _get_audit_actions(statedb)

    def test_short_ok_long_bad_no_action(self, statedb):
        from src.strategy_evolver import StrategyEvolver

        _seed_closed_trades(statedb, "trend", [2.0] * 12)
        _set_rolling(statedb, "trend", {
            "n": 20, "wr": 30.0, "pf": 0.5, "insufficient": False,
        })
        ev = StrategyEvolver(db=statedb)
        assert ev.evaluate_pf_channel() == []
        assert "trend" not in ev.get_disabled_strategies()

    def test_long_ok_short_bad_no_action(self, statedb):
        from src.strategy_evolver import StrategyEvolver

        _seed_closed_trades(statedb, "trend", [-2.0] * 12)
        _set_rolling(statedb, "trend", {
            "n": 20, "wr": 60.0, "pf": 2.5, "insufficient": False,
        })
        ev = StrategyEvolver(db=statedb)
        assert ev.evaluate_pf_channel() == []
        assert "trend" not in ev.get_disabled_strategies()

    def test_short_insufficient_sample_no_action(self, statedb):
        from src.strategy_evolver import StrategyEvolver

        _seed_closed_trades(statedb, "trend", [-2.0] * 8)  # < 10 trades
        _set_rolling(statedb, "trend", {
            "n": 20, "pf": 0.4, "insufficient": False,
        })
        ev = StrategyEvolver(db=statedb)
        assert ev.evaluate_pf_channel() == []

    def test_long_insufficient_flag_no_action(self, statedb):
        from src.strategy_evolver import StrategyEvolver

        _seed_closed_trades(statedb, "trend", [-2.0] * 12)
        _set_rolling(statedb, "trend", {
            "n": 20, "pf": 0.4, "insufficient": True,
        })
        ev = StrategyEvolver(db=statedb)
        assert ev.evaluate_pf_channel() == []

    def test_short_name_rows_aggregate_with_adaptor_name(self, statedb):
        """'rsi' rows in DB and 'rsi_reversion' rolling kv aggregate together."""
        from src.strategy_evolver import StrategyEvolver

        # 8 losing trades recorded under legacy short name "rsi"
        _seed_closed_trades(statedb, "rsi", [-2.0] * 8)
        # 4 more losing trades under adaptor name "rsi_reversion"
        _seed_closed_trades(statedb, "rsi_reversion", [-1.0] * 4, symbol="T2USDT")
        _set_rolling(statedb, "rsi_reversion", {
            "n": 20, "pf": 0.3, "insufficient": False,
        })
        ev = StrategyEvolver(db=statedb)
        changes = ev.evaluate_pf_channel()
        assert any(
            c["action"] == "DISABLED_PF" and c["strategy"] == "rsi_reversion"
            for c in changes
        )


# ------------------------------------------------- PF recovery
class TestPfRecover:
    def test_long_window_healthy_recovers(self, statedb):
        from src.strategy_evolver import StrategyEvolver

        statedb.kv_set("evolved_disabled", {"trend": {
            "disabled_at": time.time() - 86400,
            "channel": "pf_dual_window", "reason": "x",
            "short_pf": 0.5, "long_pf": 0.4, "n_short": 15, "n_long": 25,
        }})
        _set_rolling(statedb, "trend", {
            "n": 25, "pf": 1.5, "insufficient": False,
        })
        ev = StrategyEvolver(db=statedb)
        changes = ev.evaluate_pf_channel()

        assert any(c["action"] == "RECOVERED_PF" for c in changes)
        assert "trend" not in ev.get_disabled_strategies()
        assert "strategy_recovered_pf" in _get_audit_actions(statedb)

    def test_marginal_pf_below_recover_level_stays_disabled(self, statedb):
        from src.strategy_evolver import StrategyEvolver

        statedb.kv_set("evolved_disabled", {"trend": {
            "disabled_at": time.time() - 86400, "channel": "pf_dual_window",
            "reason": "x",
        }})
        _set_rolling(statedb, "trend", {
            "n": 25, "pf": 1.05, "insufficient": False,  # >=1 but < 1.2
        })
        ev = StrategyEvolver(db=statedb)
        assert ev.evaluate_pf_channel() == []
        assert "trend" in ev.get_disabled_strategies()


# ------------------------------------------------- dca guardrail
class TestDcaGuardrail:
    def test_dca_never_disabled_by_pf_channel(self, statedb):
        from src.strategy_evolver import StrategyEvolver

        _seed_closed_trades(statedb, "dca", [-2.0] * 12)
        _set_rolling(statedb, "dca", {
            "n": 20, "pf": 0.3, "insufficient": False,
        })
        ev = StrategyEvolver(db=statedb)
        changes = ev.evaluate_pf_channel()
        assert all(c["strategy"] != "dca" for c in changes)
        assert "dca" not in ev.get_disabled_strategies()

    def test_dca_force_recovered_after_7_days(self, statedb):
        from src.strategy_evolver import StrategyEvolver

        statedb.kv_set("evolved_disabled", {"dca": {
            "disabled_at": time.time() - 8 * 86400,  # 8 days ago
            "channel": "wr", "reason": "legacy WR disable",
        }})
        ev = StrategyEvolver(db=statedb)
        changes = ev.evaluate_pf_channel()

        assert any(c["action"] == "DCA_GUARDRAIL_RECOVERED" for c in changes)
        assert "dca" not in ev.get_disabled_strategies()
        assert "dca_guardrail_recovered" in _get_audit_actions(statedb)

    def test_dca_within_7_days_stays_disabled(self, statedb):
        from src.strategy_evolver import StrategyEvolver

        statedb.kv_set("evolved_disabled", {"dca": {
            "disabled_at": time.time() - 3 * 86400, "channel": "wr",
            "reason": "x",
        }})
        ev = StrategyEvolver(db=statedb)
        changes = ev.evaluate_pf_channel()
        assert all(c["action"] != "DCA_GUARDRAIL_RECOVERED" for c in changes)
        assert "dca" in ev.get_disabled_strategies()


# ------------------------------------------------- adaptor veto layer
class TestAdaptorVeto:
    @pytest.fixture
    def adaptor(self, tmp_path):
        from src.strategy_adaptor import StrategyAdaptor

        sa = StrategyAdaptor.__new__(StrategyAdaptor)
        sa._filepath = tmp_path / "strategy_state.json"
        sa._state = {"last_regime": None, "last_adjustments": None, "history": []}
        sa._cache = None
        sa._cache_ts = 0
        sa._cache_ttl = 0
        return sa

    def test_vetoed_strategy_forced_off(self, statedb, adaptor):
        statedb.kv_set("evolved_disabled", {"trend": {
            "disabled_at": time.time(), "channel": "pf_dual_window",
            "reason": "dual-window PF<1",
        }})
        result = adaptor.adapt(fear_greed=50, btc_trend="NEUTRAL",
                               btc_price_change_24h=1.5)
        cfg = result["strategies"]["trend"]
        assert cfg["enabled"] is False
        assert "evolver" in cfg["reason"]
        # other strategies untouched by veto layer
        assert result["strategies"]["dca"]["enabled"] is True

    def test_legacy_name_veto_hits_adaptor_key(self, statedb, adaptor):
        statedb.kv_set("evolved_disabled", {"rsi": {
            "disabled_at": time.time(), "channel": "wr", "reason": "x",
        }})
        result = adaptor.adapt(fear_greed=50, btc_trend="NEUTRAL",
                               btc_price_change_24h=1.5)
        assert result["strategies"]["rsi_reversion"]["enabled"] is False

    def test_evolver_error_fail_safe(self, statedb, adaptor, monkeypatch):
        import src.strategy_evolver as se_mod

        def _boom():
            raise RuntimeError("db gone")

        monkeypatch.setattr(se_mod.StrategyEvolver, "__init__", _boom)
        result = adaptor.adapt(fear_greed=50, btc_trend="NEUTRAL",
                               btc_price_change_24h=1.5)
        # static chain fully intact
        assert result["strategies"]["dca"]["enabled"] is True
        assert result["strategies"]["trend"]["enabled"] is True

    def test_recovered_strategy_back_to_default(self, statedb, adaptor):
        # disabled entry cleared → strategy back on default chain
        statedb.kv_set("evolved_disabled", {})
        result = adaptor.adapt(fear_greed=50, btc_trend="NEUTRAL",
                               btc_price_change_24h=1.5)
        assert result["strategies"]["trend"]["enabled"] is True


# ------------------------------------------------- scan orchestration mount
class TestScanMount:
    def test_evolve_step_mounted_after_reconcile(self):
        import inspect

        import src.scan_orchestrator as so

        body = inspect.getsource(so.cmd_cron_scan)
        assert "_step_evolve_strategies(ctx)" in body
        assert body.index("_step_reconcile_portfolio(ctx)") < body.index(
            "_step_evolve_strategies(ctx)"
        )

    def test_evolve_step_fail_safe(self):
        import inspect

        import src.scan_orchestrator as so

        body = inspect.getsource(so._step_evolve_strategies)
        assert "except Exception" in body

    def test_evolve_step_swallows_evolver_crash(self, statedb, monkeypatch):
        import src.scan_orchestrator as so
        import src.strategy_evolver as se_mod

        def _boom(self, now=None):
            raise RuntimeError("evolver exploded")

        monkeypatch.setattr(se_mod.StrategyEvolver, "evaluate_pf_channel", _boom)
        # must not raise
        so._step_evolve_strategies({})
