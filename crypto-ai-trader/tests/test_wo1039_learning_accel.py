"""WO-1039 学习进化加速 tests.

Covers:
1. Bandit single-update invariant — close_position no longer updates the
   bandit directly (double-write removal); TradeOutcomeRecorder remains
   the single source of truth (real entry-time context).
2. daily_learning.py guardrails — min-sample gate, epsilon no-change gate,
   audited writes + one-command rollback.
"""
import inspect
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ---------------------------------------------------------------- bandit ---
def test_close_position_no_direct_bandit_update():
    """WO-1039: the direct bandit block in close_position must stay gone."""
    from src.portfolio import PortfolioManager

    src = inspect.getsource(PortfolioManager.close_position)
    assert "update_from_outcome" not in src, (
        "close_position must not call bandit.update_from_outcome directly — "
        "that was the WO-1039 double-write (doubled effective learning rate)"
    )
    # and the surviving path inside close_position is the recorder
    assert "record_outcome" in src, (
        "close_position must still call TradeOutcomeRecorder.record_outcome"
    )


def test_recorder_is_single_bandit_update_source():
    """record_outcome keeps exactly one update_from_outcome call."""
    from src.trade_outcome_recorder import TradeOutcomeRecorder

    src = inspect.getsource(TradeOutcomeRecorder.record_outcome)
    assert src.count("update_from_outcome") == 1


def test_recorder_updates_bandit_once_per_close(tmp_path, monkeypatch):
    """Behavioural: one closed trade → exactly one bandit update, with the
    context reconstructed from the stored entry context_json."""
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "t.db"))
    from src.state_db import get_state_db

    db = get_state_db()

    # seed one open-trade row (entry context captured at entry time)
    conn = db._get_conn()
    ctx = json.dumps({
        "regime": "BULL", "fng_score": 72, "btc_trend": "UP",
        "portfolio_heat": "hot",
    })
    conn.execute(
        "INSERT INTO trade_outcomes (symbol, entry_time, entry_price, qty, "
        "score, strategy, factors_json, context_json, status, peak_price, "
        "trough_price) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("TESTUSDT", time.time() - 3600, 100.0, 1.0, 50, "test", "{}", ctx,
         "open", 100.0, 100.0),
    )
    conn.commit()

    bandit = MagicMock()
    with patch("src.contextual_bandit.get_contextual_bandit",
               return_value=bandit):
        from src.trade_outcome_recorder import TradeOutcomeRecorder

        rec = TradeOutcomeRecorder(db=db)
        rec.record_outcome(symbol="TESTUSDT", exit_price=110.0,
                           exit_reason="TP1", bandit_multiplier=0.15)

    assert bandit.update_from_outcome.call_count == 1
    ctx_used = bandit.update_from_outcome.call_args.kwargs["context"]
    assert ctx_used["hmm_regime"] == "bull"
    assert ctx_used["fear_greed"] == 72
    # and the row is closed now
    st = conn.execute(
        "SELECT status, exit_price FROM trade_outcomes WHERE symbol='TESTUSDT'"
    ).fetchone()
    assert st[0] == "closed"
    assert abs(st[1] - 110.0) < 1e-9


# ---------------------------------------------------------- daily_learning ---
class FakeDB:
    """Minimal db surface used by daily_learning (kv + trade_outcomes)."""

    def __init__(self, recent_trades=10):
        self.kv = {}
        self.recent_trades = recent_trades

    def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    def kv_set(self, key, value):
        self.kv[key] = value

    def _get_conn(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (self.recent_trades,)
        return conn


@pytest.fixture
def audit_env(tmp_path, monkeypatch):
    import scripts.daily_learning as dl

    monkeypatch.setattr(dl, "LOGS", tmp_path)
    monkeypatch.setattr(dl, "STATUS_FILE", tmp_path / "daily_status.json")
    monkeypatch.setattr(dl, "AUDIT_FILE", tmp_path / "audit.jsonl")
    return dl


def _learner_patch(monkeypatch, current, computed):
    """Patch OnlineLearner methods inside daily_learning."""
    import scripts.daily_learning as dl

    fake = MagicMock()
    fake.get_current_weights.return_value = current
    fake.compute_optimal_weights.return_value = (
        {"weights": computed, "stats": {}, "meta": {}} if computed else None)
    monkeypatch.setattr(
        "src.online_learner.OnlineLearner", MagicMock(return_value=fake))
    return fake


def test_daily_min_sample_gate(audit_env, monkeypatch):
    dl = audit_env
    db = FakeDB(recent_trades=4)  # < MIN_RECENT_TRADES(5)
    _learner_patch(monkeypatch, {}, {"momentum": 60.0})

    r = dl.step_weight_learning(db)
    assert r["status"] == "skipped"
    assert "recent_7d_trades 4 < 5" in r["reason"]
    assert not dl.AUDIT_FILE.exists()          # no audit, no write
    assert "learned_factor_weights" not in db.kv


def test_daily_epsilon_no_change(audit_env, monkeypatch):
    dl = audit_env
    db = FakeDB(recent_trades=10)
    current = {"momentum": 50.0, "volume": 50.0}
    computed = {"momentum": 50.3, "volume": 49.7}  # max delta 0.3 < 0.5
    _learner_patch(monkeypatch, current, computed)

    r = dl.step_weight_learning(db)
    assert r["status"] == "no_change"
    assert db.kv.get("learned_factor_weights") is None
    assert not dl.AUDIT_FILE.exists()


def test_daily_write_is_audited(audit_env, monkeypatch):
    dl = audit_env
    db = FakeDB(recent_trades=10)
    current = {"momentum": 50.0, "volume": 50.0}
    computed = {"momentum": 55.0, "volume": 45.0}  # delta 5.0 >= eps
    _learner_patch(monkeypatch, current, computed)

    r = dl.step_weight_learning(db)
    assert r["status"] == "ok"
    assert db.kv["learned_factor_weights"] == computed
    rows = [json.loads(l) for l in dl.AUDIT_FILE.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["action"] == "weights_written"
    assert rows[0]["old"] == current
    assert rows[0]["new"] == computed


def test_daily_rollback_restores_old(audit_env, monkeypatch):
    dl = audit_env
    db = FakeDB(recent_trades=10)
    current = {"momentum": 50.0, "volume": 50.0}
    computed = {"momentum": 55.0, "volume": 45.0}
    _learner_patch(monkeypatch, current, computed)

    dl.step_weight_learning(db)
    assert db.kv["learned_factor_weights"] == computed

    # weights drift again (a second write) — rollback must restore the LAST
    computed2 = {"momentum": 60.0, "volume": 40.0}
    _learner_patch(monkeypatch, computed, computed2)
    dl.step_weight_learning(db)

    r = dl.rollback_last(db)
    assert r["status"] == "ok"
    assert db.kv["learned_factor_weights"] == computed   # last old restored
    rows = [json.loads(l) for l in dl.AUDIT_FILE.read_text().splitlines()]
    assert rows[-1]["action"] == "rollback_applied"


def test_daily_dry_run_writes_nothing(audit_env, monkeypatch):
    dl = audit_env
    db = FakeDB(recent_trades=10)
    _learner_patch(monkeypatch, {"momentum": 50.0},
                   {"momentum": 55.0})  # delta would pass eps

    r = dl.step_weight_learning(db, dry_run=True)
    assert r["status"] == "would_write"
    assert "learned_factor_weights" not in db.kv
    assert not dl.AUDIT_FILE.exists()
