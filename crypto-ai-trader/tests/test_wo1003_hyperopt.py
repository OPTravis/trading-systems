"""WO-1003-6: Bayesian hyperopt — robust loss functions + staged promotion.

Key invariants:
- loss functions are pure, directional, and penalise under-trading;
- bayesian_optimize NEVER writes the live kv key (staging only);
- promotion is gated by walk-forward validation (unforceable) and the
  dry-run flag (forceable only with explicit human approval).
"""
import pytest

import src.param_optimizer as po_mod
from src.hyperopt_loss import (
    LOSS_FUNCTIONS,
    calmar_loss,
    get_loss,
    max_drawdown_loss,
    robust_sharpe_loss,
    sharpe_daily_loss,
)
from src.param_optimizer import ParamOptimizer


BASE = {"sharpe": 1.0, "total_return_pct": 10.0, "max_drawdown_pct": 5.0,
        "n_trades": 20, "profit_factor": 1.5, "robustness_pct": 60.0}


# ── loss function properties ────────────────────────────────────────

def test_sharpe_loss_directional():
    a = sharpe_daily_loss({**BASE, "sharpe": 1.5})
    b = sharpe_daily_loss({**BASE, "sharpe": 0.5})
    assert a < b  # higher sharpe -> lower loss


def test_max_drawdown_loss_penalises_dd_and_rewards_return():
    deep = max_drawdown_loss({**BASE, "max_drawdown_pct": 30.0})
    shallow = max_drawdown_loss({**BASE, "max_drawdown_pct": 5.0})
    assert deep > shallow
    hi_ret = max_drawdown_loss({**BASE, "total_return_pct": 20.0})
    lo_ret = max_drawdown_loss({**BASE, "total_return_pct": 5.0})
    assert hi_ret < lo_ret


def test_calmar_uses_dd_floor():
    assert calmar_loss({**BASE, "max_drawdown_pct": 30.0}) > \
           calmar_loss({**BASE, "max_drawdown_pct": 10.0})
    # below the 5% floor both behave identically (floor active)
    assert calmar_loss({**BASE, "max_drawdown_pct": 1.0}) == \
           calmar_loss({**BASE, "max_drawdown_pct": 4.0})


def test_robust_sharpe_discounts_by_robustness():
    fragile = robust_sharpe_loss({**BASE, "sharpe": 1.5,
                                  "robustness_pct": 40.0})
    solid = robust_sharpe_loss({**BASE, "sharpe": 1.0,
                                "robustness_pct": 80.0})
    assert solid < fragile  # 1.0*0.8 beats 1.5*0.4


def test_undertrading_penalised_and_zero_trades_rejected():
    full = sharpe_daily_loss({**BASE, "n_trades": 50})
    thin = sharpe_daily_loss({**BASE, "n_trades": 2})
    none = sharpe_daily_loss({**BASE, "n_trades": 0})
    assert full < thin < none
    assert none > 9e5  # rejection-scale loss (=-sharpe+1e6)
    # penalty is linear below the minimum
    p3 = sharpe_daily_loss({**BASE, "n_trades": 3})
    p4 = sharpe_daily_loss({**BASE, "n_trades": 4})
    assert abs((thin - p3) - (p3 - p4)) < 1e-9


def test_registry_complete_and_unknown_rejected():
    assert set(LOSS_FUNCTIONS) == {"sharpe_daily", "max_drawdown",
                                   "calmar", "robust_sharpe"}
    with pytest.raises(ValueError, match="Unknown loss"):
        get_loss("nope")


# ── Bayesian staging (never live) ────────────────────────────────────

def _mk_opt(monkeypatch, metric_fn=None, validated=True):
    d = {}
    import time as _t
    from src.state_db import get_state_db
    db = get_state_db()
    db.kv_set("ledger:shadow:bootstrap_ts", _t.time() - 86400)
    d["db"] = db

    def _fake_run(self, params, symbols, days=90):
        if metric_fn is None:
            return dict(BASE)
        return metric_fn(params)

    def _fake_validate(self, params, symbols=None):
        return {"validated": validated, "reason":
                "OK" if validated else "OOS Sharpe -1.00 < 0.5",
                "avg_oos_sharpe": 0.9 if validated else -1.0,
                "avg_robustness": 60.0, "total_trades": 30}

    monkeypatch.setattr(ParamOptimizer, "_run_backtest_with_params",
                        _fake_run)
    monkeypatch.setattr(ParamOptimizer, "validate_best", _fake_validate)
    return ParamOptimizer(db=db), d


def test_bayesian_stages_but_never_writes_live(monkeypatch):
    opt, d = _mk_opt(monkeypatch, metric_fn=lambda p: {
        # optimum: rsi_oversold as low as possible -> sharpe improves
        **BASE, "sharpe": 3.0 - (p["rsi_oversold"] - 20) * 0.1})
    before = d["db"].kv_get("optimized_params")
    res = opt.bayesian_optimize(n_trials=12)
    assert res["status"] == "staged"
    staged = d["db"].kv_get("optimized_params_staged")
    assert isinstance(staged, dict) and staged["params"]
    # LIVE key untouched by optimisation
    assert d["db"].kv_get("optimized_params") == before
    # history recorded
    hist = d["db"].kv_get("hyperopt:history")
    assert isinstance(hist, list) and hist[-1]["event"] == "staged"


def test_bayesian_converges_toward_known_optimum(monkeypatch):
    opt, d = _mk_opt(monkeypatch, metric_fn=lambda p: {
        **BASE, "sharpe": 3.0 - (p["rsi_oversold"] - 20) * 0.1})
    res = opt.bayesian_optimize(n_trials=15)
    assert res["params"]["rsi_oversold"] <= 23  # near the low edge


def test_bayesian_all_fail_returns_no_result(monkeypatch):
    opt, d = _mk_opt(monkeypatch)

    def _boom(self, params, symbols, days=90):
        raise RuntimeError("klines unavailable")

    monkeypatch.setattr(ParamOptimizer, "_run_backtest_with_params", _boom)
    res = opt.bayesian_optimize(n_trials=5)
    assert res["status"] == "no_result"
    assert d["db"].kv_get("optimized_params_staged") is None


# ── promotion gates ──────────────────────────────────────────────────

def _stage(db, validated=True, dry_run=False):
    db.kv_set("optimized_params_staged", {
        "params": {**po_mod.DEFAULT_PARAMS, "rsi_oversold": 24},
        "metrics": dict(BASE), "loss_name": "sharpe_daily",
        "loss_value": -1.2, "n_trials": 10,
        "validation": {"validated": validated, "reason": "OK"},
        "dry_run_verified": dry_run, "staged_at": 1.0,
    })


def test_promote_rejected_without_dry_run(monkeypatch):
    opt, d = _mk_opt(monkeypatch)
    _stage(d["db"], validated=True, dry_run=False)
    res = opt.promote_staged_params()
    assert res["status"] == "rejected" and "dry-run" in res["reason"]
    assert d["db"].kv_get("optimized_params_staged") is not None


def test_promote_after_dry_run_verification(monkeypatch):
    opt, d = _mk_opt(monkeypatch)
    _stage(d["db"], validated=True, dry_run=False)
    assert opt.mark_dry_run_verified("drift 3.2% < 10% gate")["status"] == "ok"
    res = opt.promote_staged_params()
    assert res["status"] == "promoted"
    # live key now carries staged params; staging cleared; history logged
    assert d["db"].kv_get("optimized_params")["rsi_oversold"] == 24
    assert d["db"].kv_get("optimized_params_staged") is None
    hist = d["db"].kv_get("hyperopt:history")
    assert hist[-1]["event"] == "promoted"


def test_promote_force_bypasses_dry_run_but_not_wf(monkeypatch):
    opt, d = _mk_opt(monkeypatch)
    _stage(d["db"], validated=True, dry_run=False)
    res = opt.promote_staged_params(force=True)
    assert res["status"] == "promoted" and res["forced"] is True

    _stage(d["db"], validated=False, dry_run=True)
    res = opt.promote_staged_params(force=True)
    assert res["status"] == "rejected" and "walk-forward" in res["reason"]


def test_promote_nothing_staged(monkeypatch):
    opt, d = _mk_opt(monkeypatch)
    assert opt.promote_staged_params()["status"] == "no_staged"


def test_history_bounded(monkeypatch):
    import time as _t
    opt, d = _mk_opt(monkeypatch)
    for i in range(25):
        opt._append_history({"event": "staged", "at": _t.time(), "i": i})
    hist = d["db"].kv_get("hyperopt:history")
    assert len(hist) == po_mod.HISTORY_KEEP
    assert hist[-1]["i"] == 24
