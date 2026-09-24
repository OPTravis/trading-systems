"""WO-0924-z2 P6-B2: learning-chain SQL migration to StateDB methods.

Characterizes the StateDB trade_outcomes / bull_regime_log API surface
(previously raw SQL in trade_outcome_recorder + kelly_sizer /
strategy_evolver / online_learner / strategy_registry / hmm_regime /
bull_regime) and pins migrated call-site behavior. Mirrors the P6-B1
bull_paper characterization suite.
"""

import re
import time
from pathlib import Path

import pytest

from src.state_db import StateDB


@pytest.fixture()
def db():
    return StateDB()


def _add(db, symbol="BTCUSDT", entry_time=None, strategy="rsi",
         context="{}", factors="{}", status_open=True, close_kwargs=None,
         net_pnl_pct=None):
    """Insert an outcome row through the public writer path."""
    entry_time = entry_time if entry_time is not None else time.time()
    row_id = db.outcome_add_entry(
        symbol=symbol, entry_time=entry_time,
        entry_date="2026-09-24", entry_price=100.0, qty=1.0,
        score=5.0, strategy=strategy,
        factors_json=factors, context_json=context)
    if not status_open:
        kw = dict(exit_time=entry_time + 3600, exit_price=110.0,
                  exit_reason="tp1", pnl_pct=10.0, pnl_absolute=10.0,
                  net_pnl_pct=net_pnl_pct if net_pnl_pct is not None else 9.0,
                  net_pnl_absolute=9.0, time_held_hours=1.0,
                  max_profit_pct=12.0, max_drawdown_pct=-2.0,
                  peak_price=112.0, trough_price=98.0, is_win=1,
                  updated_at=entry_time + 3600)
        kw.update(close_kwargs or {})
        db.outcome_close(row_id, **kw)
    return row_id


class TestOutcomeWriterReaderRoundTrip:

    def test_add_and_get_by_id(self, db):
        rid = _add(db, symbol="ETHUSDT", strategy="bollinger",
                   factors='{"technical": 3}', context='{"regime": "bull"}')
        row = db.outcome_get_by_id(rid)
        assert row["symbol"] == "ETHUSDT"
        assert row["strategy"] == "bollinger"
        assert row["status"] == "open"
        assert row["peak_price"] == 100.0  # seeded at entry price
        assert row["trough_price"] == 100.0
        assert db.outcome_get_by_id(99999) is None

    def test_latest_open_picks_most_recent(self, db):
        _add(db, symbol="BTCUSDT", entry_time=1000.0)
        rid2 = _add(db, symbol="BTCUSDT", entry_time=2000.0)
        row = db.outcome_latest_open("BTCUSDT")
        assert row["id"] == rid2
        _add(db, symbol="BTCUSDT", entry_time=3000.0, status_open=False)
        # latest OPEN row is still rid2 (the 3000 one is closed now)
        assert db.outcome_latest_open("BTCUSDT")["id"] == rid2

    def test_update_extremes(self, db):
        rid = _add(db)
        db.outcome_update_extremes(rid, peak=130.0, trough=95.0,
                                   updated_at=time.time())
        row = db.outcome_get_by_id(rid)
        assert row["peak_price"] == 130.0
        assert row["trough_price"] == 95.0

    def test_close_full_field_writeback(self, db):
        rid = _add(db, entry_time=100.0)
        db.outcome_close(
            rid, exit_time=200.0, exit_price=90.0, exit_reason="sl",
            pnl_pct=-10.0, pnl_absolute=-10.0, net_pnl_pct=-11.0,
            net_pnl_absolute=-11.0, time_held_hours=2.5,
            max_profit_pct=3.0, max_drawdown_pct=-10.0,
            peak_price=103.0, trough_price=90.0, is_win=0,
            updated_at=200.0)
        row = db.outcome_get_by_id(rid)
        assert row["status"] == "closed"
        assert row["exit_reason"] == "sl"
        assert row["net_pnl_pct"] == -11.0
        assert row["is_win"] == 0
        assert row["time_held_hours"] == 2.5

    def test_get_open_and_closed(self, db):
        _add(db, symbol="A")
        _add(db, symbol="B", status_open=False)
        open_rows = db.outcomes_get_open()
        closed_rows = db.outcomes_get_closed()
        assert [r["symbol"] for r in open_rows] == ["A"]
        assert [r["symbol"] for r in closed_rows] == ["B"]

    def test_get_closed_filters_and_order(self, db):
        _add(db, entry_time=100.0, strategy="rsi", status_open=False,
             close_kwargs={"exit_time": 100.0})
        _add(db, entry_time=200.0, strategy="vwap", status_open=False,
             close_kwargs={"exit_time": 300.0})
        _add(db, entry_time=300.0, strategy="rsi", status_open=False,
             close_kwargs={"exit_time": 200.0})
        only_rsi = db.outcomes_get_closed(strategy="rsi")
        assert len(only_rsi) == 2
        newest = db.outcomes_get_closed(newest_first=True)
        assert [r["exit_time"] for r in newest] == [300.0, 200.0, 100.0]
        # newest_first=False drops the ORDER BY — rows come back in
        # natural insertion order (characterized; factor-stats/summary
        # callers are order-insensitive)
        natural = db.outcomes_get_closed(newest_first=False)
        assert [r["exit_time"] for r in natural] == [100.0, 300.0, 200.0]
        assert len(db.outcomes_get_closed(limit=2)) == 2


class TestOutcomeDerivedQueries:

    def test_count_closed(self, db):
        _add(db, status_open=False)
        _add(db, status_open=False)
        _add(db)  # open
        assert db.outcomes_count_closed() == 2

    def test_count_context_like_has_no_status_filter(self, db):
        # Characterization: kelly's exploration/refresh caps count open AND
        # closed rows — the original SQL had no status clause.
        now = time.time()
        _add(db, entry_time=now - 100, context='{"note": "regime warming entry"}')
        _add(db, entry_time=now - 200,
             context='{"note": "bull regime refresh"}', status_open=False)
        old = db.outcomes_count_context_like("regime warming", now - 86400)
        assert old == 1
        recent = db.outcomes_count_context_like("bull regime refresh",
                                                now - 7 * 86400)
        assert recent == 1
        # window excludes old entries
        _add(db, entry_time=now - 40 * 86400,
             context='{"note": "regime warming ancient"}')
        assert db.outcomes_count_context_like("regime warming",
                                              now - 30 * 86400) == 1
        # no match
        assert db.outcomes_count_context_like("nonexistent", 0) == 0

    def test_recent_pnl_signals_shape(self, db):
        _add(db, symbol="A", strategy="rsi", status_open=False,
             net_pnl_pct=5.0)
        _add(db, symbol="B", strategy="vwap", status_open=False,
             net_pnl_pct=-3.0)
        # NULL pnl rows are excluded (pass NULL explicitly via close_kwargs)
        rid = _add(db, symbol="C", strategy="grid", status_open=False,
                   close_kwargs={"net_pnl_pct": None})
        rows = db.outcomes_recent_pnl_signals(10)
        assert {r["symbol"] for r in rows} == {"A", "B"}
        assert all(isinstance(r, dict) for r in rows)
        for r in rows:
            assert set(r.keys()) == {"symbol", "net_pnl_pct", "is_win",
                                     "strategy"}
        assert db.outcome_get_by_id(rid)["net_pnl_pct"] is None

    def test_strategy_perf_rows(self, db):
        _add(db, strategy="rsi", status_open=False, net_pnl_pct=4.0,
             close_kwargs={"is_win": 1})
        _add(db, strategy="rsi", status_open=False, net_pnl_pct=-2.0,
             close_kwargs={"is_win": 0})
        _add(db, strategy="vwap", status_open=False, net_pnl_pct=6.0,
             close_kwargs={"is_win": 1})
        _add(db, strategy=None, status_open=False)  # excluded (NULL strategy)
        rows = {r["strategy"]: r for r in db.outcomes_strategy_perf_rows()}
        assert rows["rsi"]["trades"] == 2
        assert rows["rsi"]["wins"] == 1
        assert rows["rsi"]["avg_pnl"] == pytest.approx(1.0)
        assert rows["vwap"]["trades"] == 1

    def test_strategy_pnls_newest_first(self, db):
        _add(db, strategy="rsi", status_open=False,
             close_kwargs={"exit_time": 100.0})
        _add(db, strategy="rsi", status_open=False,
             close_kwargs={"exit_time": 300.0})
        _add(db, strategy="rsi", status_open=False,
             close_kwargs={"exit_time": 200.0})
        rows = db.outcomes_strategy_pnls()
        assert len(rows) == 3
        assert all(set(r.keys()) == {"strategy", "net_pnl_pct"} for r in rows)

    def test_recent_pnl_per_strategy_window(self, db):
        for i in range(5):
            _add(db, strategy="rsi", status_open=False,
                 close_kwargs={"exit_time": float(i),
                               "net_pnl_pct": float(i)})
        for i in range(2):
            _add(db, strategy="vwap", status_open=False,
                 close_kwargs={"exit_time": float(i),
                               "net_pnl_pct": 100.0 + i})
        rows = db.outcomes_recent_pnl_per_strategy(3)
        rsi = [r["net_pnl_pct"] for r in rows if r["strategy"] == "rsi"]
        vwap = [r["net_pnl_pct"] for r in rows if r["strategy"] == "vwap"]
        # window keeps the 3 newest rsi (exit 4,3,2) and both vwap
        assert rsi == [4.0, 3.0, 2.0]
        assert vwap == [101.0, 100.0]

    def test_strategy_rows_win(self, db):
        _add(db, strategy="rsi", status_open=False,
             close_kwargs={"is_win": 1})
        _add(db, strategy="rsi", status_open=False,
             close_kwargs={"is_win": 0})
        rows = db.outcomes_strategy_rows_win()
        by_s = {r["strategy"]: r for r in rows}
        assert set(by_s["rsi"].keys()) == {"strategy", "net_pnl_pct", "is_win"}
        assert len(rows) == 2


class TestAuditAndBullRegimeLog:

    def test_audit_get_recent_action_filter(self, db):
        db.audit_log("learned_weights_update", details="w1",
                     source="online_learner")
        db.audit_log("strategy_disabled", details="x",
                     source="strategy_evolver")
        db.audit_log("learned_weights_update", details="w2",
                     source="online_learner")
        hist = db.audit_get_recent(limit=20, action="learned_weights_update")
        assert len(hist) == 2
        assert all(r["action"] == "learned_weights_update" for r in hist)
        assert hist[0]["details"] == "w2"  # newest first
        all_recent = db.audit_get_recent(limit=50)
        assert len(all_recent) == 3

    def test_bull_regime_log_roundtrip(self, db):
        t = {"ts": 1789900000000, "bar_ts": 1789899999000,
             "from": "NEUTRAL", "to": "CONFIRMED_BULL",
             "reason": "sma200+fng",
             "btc_close": 65000.0, "btc_sma200": 60000.0,
             "fng_avg": 62.0, "fng_today": 65, "adx": 27.5,
             "conditions": {"sma200_ok": True, "fng_ok": True}}
        db.bull_regime_log_add(t)
        rows = db.bull_regime_log_recent(limit=5)
        assert len(rows) == 1
        r = rows[0]
        # reader keeps the original 7-column projection (no fng_today /
        # btc_sma200 — pinned; add columns here only with a caller)
        assert set(r.keys()) == {"ts", "from_state", "to_state", "reason",
                                 "btc_close", "fng_avg", "adx"}
        assert r["to_state"] == "CONFIRMED_BULL"

    def test_bull_regime_log_table_in_schema(self, db):
        # fresh DB (test isolation) must have the table via StateDB schema
        # init, not via bull_regime._ensure_table (removed in P6-B2)
        import sqlite3
        conn = sqlite3.connect(str(db.db_path))
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        assert "bull_regime_log" in names


class TestMigratedCallSites:
    """Behavior through the migrated module APIs."""

    def test_registry_weights_kv_roundtrip(self, db):
        from src.strategy_registry import (
            StrategyRegistry, DEFAULT_STRATEGY_WEIGHTS)
        reg = StrategyRegistry(db=db)
        assert reg.get_strategy_weights() == DEFAULT_STRATEGY_WEIGHTS
        custom = dict(DEFAULT_STRATEGY_WEIGHTS)
        custom["rsi"] = 2.5
        db.kv_set("strategy_weights", custom)
        assert reg.get_strategy_weights()["rsi"] == 2.5
        # invalid shape falls back to defaults
        db.kv_set("strategy_weights", "not-a-dict")
        assert reg.get_strategy_weights() == DEFAULT_STRATEGY_WEIGHTS

    def test_registry_optimized_params_invalid_falls_back(self, db):
        from src.strategy_registry import StrategyRegistry
        reg = StrategyRegistry(db=db)
        db.kv_set("optimized_params", {"rsi": {"period": 14}})
        assert reg._get_optimized_params() == {"rsi": {"period": 14}}
        db.kv_set("optimized_params", "garbage")
        assert reg._get_optimized_params() == {}

    def test_evolver_disabled_roundtrip_and_audit_mapping(self, db):
        from src.strategy_evolver import StrategyEvolver
        ev = StrategyEvolver(db=db)
        assert ev.get_disabled_strategies() == {}
        from src.strategy_evolver import canonical_strategy
        ev._set_disabled({"rsi": {"disabled_at": 123.0, "reason": "test"}})
        # canonical_strategy normalizes via STRATEGY_ALIAS
        out = ev.get_disabled_strategies()
        assert list(out.keys()) == [canonical_strategy("rsi")]
        ev._log_audit("evolver_test", "details-text")
        row = db.audit_get_recent(limit=5)[0]
        # legacy column mapping: text lands in new_value (not details)
        assert row["new_value"] == "details-text"
        assert row["old_value"] is None
        assert row["source"] == "strategy_evolver"

    def test_learner_weights_default_and_roundtrip(self, db):
        from src.online_learner import OnlineLearner, DEFAULT_WEIGHTS
        ln = OnlineLearner(db=db)
        assert ln.get_current_weights() == DEFAULT_WEIGHTS
        learned = dict(DEFAULT_WEIGHTS)
        learned["technical"] = 7.0
        db.kv_set("learned_factor_weights", learned)
        assert ln.get_current_weights()["technical"] == 7.0
        # missing factor -> defaults
        partial = {k: v for k, v in learned.items() if k != "trend"}
        db.kv_set("learned_factor_weights", partial)
        assert ln.get_current_weights() == DEFAULT_WEIGHTS

    def test_learner_weight_history_action_filter(self, db):
        from src.online_learner import OnlineLearner
        ln = OnlineLearner(db=db)
        db.audit_log("learned_weights_update", details="w1",
                     source="online_learner")
        db.audit_log("other_action", details="x", source="system")
        hist = ln.get_weight_history()
        assert len(hist) == 1
        assert hist[0]["action"] == "learned_weights_update"

    def test_kelly_exploration_and_refresh_counts(self, db):
        from src.kelly_sizer import KellyPositionSizer
        ks = KellyPositionSizer(state_db=db)
        now = time.time()
        _add(db, entry_time=now - 100,
             context='{"confidence": "regime warming"}')
        _add(db, entry_time=now - 200,
             context='{"confidence": "regime warming"}', status_open=False)
        _add(db, entry_time=now - 50,
             context='{"confidence": "bull regime refresh"}')
        _add(db, entry_time=now - 50 - 7 * 86400,
             context='{"confidence": "bull regime refresh"}')  # outside 7d
        assert ks._exploration_entries_last_30d() == 2
        assert ks._bull_refresh_entries_last_7d() == 1

    def test_kelly_trade_history_dict_access(self, db):
        from src.kelly_sizer import KellyPositionSizer
        ks = KellyPositionSizer(state_db=db)
        _add(db, symbol="BTCUSDT", strategy="rsi", status_open=False,
             net_pnl_pct=4.0)
        hist = ks._get_trade_history()
        assert len(hist) == 1
        assert hist[0]["symbol"] == "BTCUSDT"
        assert hist[0]["pnl"] == 4.0  # original dict shape preserved


_MIGRATED_MODULES = [
    "trade_outcome_recorder.py",
    "kelly_sizer.py",
    "strategy_evolver.py",
    "online_learner.py",
    "strategy_registry.py",
    "hmm_regime.py",
    "bull_regime.py",
]

# state.db tables owned by StateDB after P6-B2. Uppercase SQL keyword
# prefix avoids docstring false-positives (P6-B1 lesson).
_RAW_SQL_RE = re.compile(
    r"(FROM|INTO|UPDATE|TABLE|JOIN)\s+"
    r"(IF\s+NOT\s+EXISTS\s+)?"
    r"(trade_outcomes|kv|audit_log|bull_regime_log)\b",
)


class TestMigrationGuard:

    @pytest.mark.parametrize("mod", _MIGRATED_MODULES)
    def test_no_raw_state_db_sql_left(self, mod):
        src = (Path("src") / mod).read_text()
        hits = _RAW_SQL_RE.findall(src)
        assert not hits, f"{mod} still holds raw state.db SQL: {hits}"

    def test_state_db_owns_the_queries(self):
        src = Path("src/state_db.py").read_text()
        for needle in ["outcome_add_entry", "outcome_close",
                       "outcomes_count_context_like",
                       "outcomes_recent_pnl_per_strategy",
                       "bull_regime_log_add"]:
            assert needle in src
