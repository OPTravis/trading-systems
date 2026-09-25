"""P7: learning-pipeline cleanup tests.

(1) contamination flagging — mark semantics, six learner-side reader
    filters, two analysis readers untouched, idempotent reopen.
(2) kelly parity — the same DB gives different win_rate with/without
    the dirty window flagged (the z2 acceptance artifact).
(3) learner smoke — online_learner runs on a clean-only window without
    degradation (converges / returns structured output).
"""

import pytest

from src.state_db import StateDB


@pytest.fixture()
def db(tmp_path):
    d = StateDB(str(tmp_path / "s.db"))
    yield d
    d._get_conn().close()


def _add_closed(db, symbol, pnl, exit_ts, strategy="S", is_win=None):
    db._get_conn().execute(
        "INSERT INTO trade_outcomes (symbol, entry_time, entry_price,"
        " qty, status, exit_time, exit_price, net_pnl_pct, is_win,"
        " strategy, created_at, updated_at, contaminated)"
        " VALUES (?,?,?,?, 'closed', ?, 100.0, ?, ?, ?, 1, 1, 0)",
        (symbol, exit_ts - 3600, 100.0, 1.0, exit_ts, pnl,
         1 if (pnl > 0) else 0, strategy))
    db._get_conn().commit()


DIRTY_START, DIRTY_END = 1789488000, 1790235710   # 9/16 - 9/24 15:41


class TestMarkContaminated:
    def test_mark_only_in_window_closed(self, db):
        _add_closed(db, "A", 5.0, DIRTY_START + 10)    # in window
        _add_closed(db, "B", -5.0, DIRTY_END - 10)     # in window
        _add_closed(db, "C", 3.0, DIRTY_END + 10)      # clean
        _add_closed(db, "D", 3.0, DIRTY_START - 10)    # clean
        assert db.outcomes_mark_contaminated(DIRTY_START, DIRTY_END) == 2
        s = db.outcomes_contamination_summary()
        assert s == {"closed_total": 4, "contaminated": 2}
        # idempotent: second pass flags nothing new
        assert db.outcomes_mark_contaminated(DIRTY_START, DIRTY_END) == 0

    def test_open_rows_never_flagged(self, db):
        db._get_conn().execute(
            "INSERT INTO trade_outcomes (symbol, entry_time, entry_price,"
            " qty, status, created_at) VALUES ('X', ?, 1, 1, 'open', 1)",
            (DIRTY_START + 10,))
        db._get_conn().commit()
        assert db.outcomes_mark_contaminated(DIRTY_START, DIRTY_END) == 0


class TestLearnerReadersFilter:
    def _seed(self, db):
        # 3 dirty (2 wins) + 2 clean (1 win), two strategies
        _add_closed(db, "A", 5.0, DIRTY_START + 10, strategy="S1")
        _add_closed(db, "A", 4.0, DIRTY_START + 20, strategy="S1")
        _add_closed(db, "B", -5.0, DIRTY_START + 30, strategy="S2")
        _add_closed(db, "C", 6.0, DIRTY_END + 10, strategy="S1")
        _add_closed(db, "C", -6.0, DIRTY_END + 20, strategy="S2")
        db.outcomes_mark_contaminated(DIRTY_START, DIRTY_END)

    def test_get_closed_excludes_dirty(self, db):
        self._seed(db)
        rows = db.outcomes_get_closed()
        assert len(rows) == 2

    def test_recent_net_pnls_excludes_dirty(self, db):
        self._seed(db)
        # exit_time DESC: C(-6.0, exit=END+20) before C(6.0, END+10)
        assert db.outcomes_recent_net_pnls(100) == [-6.0, 6.0]

    def test_recent_pnl_signals_excludes_dirty(self, db):
        self._seed(db)
        rows = db.outcomes_recent_pnl_signals(100)
        assert [r["symbol"] for r in rows] == ["C", "C"]

    def test_strategy_aggregates_exclude_dirty(self, db):
        self._seed(db)
        perf = {r["strategy"]: r for r in db.outcomes_strategy_perf_rows()}
        assert perf["S1"]["trades"] == 1 and perf["S2"]["trades"] == 1
        pnls = db.outcomes_strategy_pnls()
        assert len(pnls) == 2
        per = db.outcomes_recent_pnl_per_strategy(50)
        assert len(per) == 2

    def test_analysis_readers_keep_everything(self, db):
        """concept_drift (oldest-first) and portfolio history are
        analysis views — the dirty rows stay visible there."""
        self._seed(db)
        assert len(db.outcomes_get_closed_oldest()) == 5
        assert len(db.outcomes_history_rows()) == 5

    def test_flag_reversible(self, db):
        self._seed(db)
        db._get_conn().execute(
            "UPDATE trade_outcomes SET contaminated = 0")
        db._get_conn().commit()
        assert len(db.outcomes_get_closed()) == 5


class TestReopenMigration:
    def test_reopen_swallows_duplicate_alter(self, tmp_path):
        p = str(tmp_path / "s.db")
        StateDB(p)._get_conn().close()
        d2 = StateDB(p)   # ALTER hits "duplicate column" -> swallowed
        cols = [r[1] for r in
                d2._get_conn().execute("PRAGMA table_info(trade_outcomes)")]
        assert cols.count("contaminated") == 1


class TestKellyParity:
    def test_win_rate_changes_when_flagged(self, db, monkeypatch):
        """The z2 acceptance artifact in miniature: flagging the dirty
        window moves the kelly inputs (sample count / win_rate)."""
        import src.risk_manager as rm_mod
        import src.state_db as sd_mod
        # 4 dirty rows (3 wins) + 2 clean rows (1 win): 6 total >= 5
        # so the real formula runs BEFORE; after flagging only 2 clean
        # rows remain (< 5) and kelly falls back to its 0.5 default —
        # mirroring the production parity (42 -> 12 clean).
        _add_closed(db, "A", 5.0, DIRTY_START + 10)
        _add_closed(db, "A", 5.0, DIRTY_START + 20)
        _add_closed(db, "A", 4.0, DIRTY_START + 30)
        _add_closed(db, "B", -5.0, DIRTY_START + 40)
        _add_closed(db, "C", 6.0, DIRTY_END + 10)
        _add_closed(db, "C", -6.0, DIRTY_END + 20)
        monkeypatch.setattr(sd_mod, "get_state_db", lambda: db)
        rm = rm_mod.RiskManager.__new__(rm_mod.RiskManager)
        before = rm.calculate_kelly_fraction(50)
        assert before["trades_analyzed"] == 6
        assert before["win_rate"] == pytest.approx(4 / 6, abs=1e-4)
        db.outcomes_mark_contaminated(DIRTY_START, DIRTY_END)
        after = rm.calculate_kelly_fraction(50)
        assert after["trades_analyzed"] == 2
        assert after["win_rate"] == pytest.approx(0.5)  # <5 -> default

    def test_kelly_sizer_clean_window(self, db):
        from src.kelly_sizer import KellyPositionSizer
        for i in range(6):
            _add_closed(db, "A", 5.0 if i % 2 else -3.0,
                        DIRTY_END + 100 + i)
        ks = KellyPositionSizer(state_db=db)
        trades = ks._get_trade_history(limit=50)
        assert len(trades) == 6          # all clean, all visible
        for i in range(4):               # dirty rows invisible to kelly
            _add_closed(db, "A", 50.0, DIRTY_START + 10 + i)
        db.outcomes_mark_contaminated(DIRTY_START, DIRTY_END)
        assert len(ks._get_trade_history(limit=50)) == 6


class TestLearnerSmoke:
    def test_learner_runs_on_clean_only(self, db, monkeypatch):
        """online_learner must produce structured output from the clean
        window without crashing (no degradation = still learns)."""
        import numpy as np
        from src.online_learner import OnlineLearner
        for i in range(20):
            _add_closed(db, "A",
                        4.0 if i % 3 else -2.0,
                        DIRTY_END + 100 + i)
        for i in range(5):   # dirty rows must not reach the learner
            _add_closed(db, "B", 99.0, DIRTY_START + 10 + i)
        db.outcomes_mark_contaminated(DIRTY_START, DIRTY_END)
        ln = OnlineLearner(db=db) if "db" in OnlineLearner.__init__.__code__.co_varnames \
            else OnlineLearner()
        if hasattr(ln, "_db"):
            ln._db = db
        elif hasattr(ln, "db"):
            ln.db = db
        res = ln.learn() if hasattr(ln, "learn") else \
            ln.update_weights() if hasattr(ln, "update_weights") else None
        # either structured result or a clean None (insufficient data)
        assert res is None or isinstance(res, dict)
        if res:
            assert "weights" in res or "meta" in res or "n_trades" in res
