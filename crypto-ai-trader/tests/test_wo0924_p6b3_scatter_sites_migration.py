"""P6-B3 scatter-site migration tests (WO-0924-z2).

13 modules moved off raw SQL onto StateDB methods:
  kv writes/reads: tp_sl_tracker / self_healer / trade_executor (bandit) /
                   param_optimizer / concept_drift (result store)
  audit: protection_guardian legacy fallback
  trade_outcomes reads: strategy_rolling_stats / cvar_risk / strategy_adaptor /
                   concept_drift / risk_manager (kelly) / portfolio history /
                   cmd_trailing_check (bug#32 ghost guard)
  portfolio table: kv_preflight read / cmd_trailing_check stop-loss write

Out of scope (recorded, untouched): trades-table sites (P4 Ledger),
cache.db family, paper_trader store, event_bus private db.
"""

import json
import re
import time
from pathlib import Path
from unittest import mock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.state_db import StateDB


@pytest.fixture()
def db(tmp_path):
    d = StateDB(str(tmp_path / "state.db"))
    yield d
    d._get_conn().close()


def _add(db, symbol="BTCUSDT", entry_time=None, strategy="rsi",
         context="{}", factors="{}", status_open=True, close_kwargs=None,
         net_pnl_pct=None, exit_time=None):
    """Insert an outcome row through the public writer path."""
    entry_time = entry_time if entry_time is not None else time.time()
    row_id = db.outcome_add_entry(
        symbol=symbol, entry_time=entry_time,
        entry_date="2026-09-24", entry_price=100.0, qty=1.0,
        score=5.0, strategy=strategy,
        factors_json=factors, context_json=context)
    if not status_open:
        kw = dict(exit_time=exit_time if exit_time is not None else entry_time + 3600,
                  exit_price=110.0, exit_reason="tp1", pnl_pct=10.0,
                  pnl_absolute=10.0,
                  net_pnl_pct=net_pnl_pct if net_pnl_pct is not None else 9.0,
                  net_pnl_absolute=9.0, time_held_hours=1.0,
                  max_profit_pct=10.0, max_drawdown_pct=0.0,
                  peak_price=110.0, trough_price=99.0, is_win=True,
                  updated_at=entry_time + 3600)
        kw.update(close_kwargs or {})
        db.outcome_close(row_id, **kw)
    return row_id


# ============ kv_get_prefix / kv_age_seconds (StateDB) ============

class TestKvPrefixAndAge:
    def test_prefix_scan_parses_mixed_values(self, db):
        db.kv_set("tp_sl_tracker:AAA", {"qty": 1})
        db.kv_set("tp_sl_tracker:BBB", [1, 2, 3])
        db.kv_set("tp_sl_tracker:CCC", 0.5)
        db.kv_set("other:XXX", {"unrelated": True})
        out = db.kv_get_prefix("tp_sl_tracker:")
        assert out == {"tp_sl_tracker:AAA": {"qty": 1},
                       "tp_sl_tracker:BBB": [1, 2, 3],
                       "tp_sl_tracker:CCC": 0.5}

    def test_prefix_raw_string_on_parse_failure(self, db):
        # raw write bypassing kv_set: a non-JSON value must survive as str
        db._get_conn().execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
            ("tp_sl_tracker:RAW", "not-json", time.time()))
        db._get_conn().commit()
        assert db.kv_get_prefix("tp_sl_tracker:")["tp_sl_tracker:RAW"] == "not-json"

    def test_prefix_empty_when_no_match(self, db):
        db.kv_set("zzz", 1)
        assert db.kv_get_prefix("tp_sl_tracker:") == {}

    def test_age_none_when_absent(self, db):
        assert db.kv_age_seconds("missing") is None

    def test_age_fresh_after_write(self, db):
        db.kv_set("k", 1)
        age = db.kv_age_seconds("k")
        assert age is not None and 0.0 <= age < 5.0

    def test_age_clamped_non_negative(self, db):
        db.kv_set("k", 1)
        db._get_conn().execute("UPDATE kv SET updated_at=? WHERE key='k'",
                               (time.time() + 3600,))
        db._get_conn().commit()
        assert db.kv_age_seconds("k") == 0.0


# ============ tp_sl_tracker.get_all_tracked (prefix + legacy decode) ============

class TestTpSlTrackerIntegration:
    def test_get_all_tracked_mixed_states(self, db, monkeypatch):
        from src import tp_sl_tracker as tst
        monkeypatch.setattr("src.state_db.get_state_db", lambda *a, **k: db)
        db.kv_set("tp_sl_tracker:BTC", {"entry": 100.0, "qty": 2})
        db.kv_set("tp_sl_tracker:ETH", {"entry": 50.0, "qty": 1})
        out = tst.get_all_tracked()
        assert out == {"BTC": {"entry": 100.0, "qty": 2},
                       "ETH": {"entry": 50.0, "qty": 1}}

    def test_double_encoded_legacy_value(self, db, monkeypatch):
        from src import tp_sl_tracker as tst
        monkeypatch.setattr("src.state_db.get_state_db", lambda *a, **k: db)
        # legacy double-encoding: kv_set of a pre-dumped JSON string
        db.kv_set("tp_sl_tracker:OLD", json.dumps({"entry": 9.0}))
        out = tst.get_all_tracked()
        assert out["OLD"] == {"entry": 9.0}

    def test_garbage_value_skipped(self, db, monkeypatch):
        from src import tp_sl_tracker as tst
        monkeypatch.setattr("src.state_db.get_state_db", lambda *a, **k: db)
        db._get_conn().execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
            ("tp_sl_tracker:BAD", "not-json-at-all", time.time()))
        db._get_conn().commit()
        db.kv_set("tp_sl_tracker:GOOD", {"ok": True})
        out = tst.get_all_tracked()
        assert out == {"GOOD": {"ok": True}}


# ============ new outcome readers (StateDB) ============

class TestOutcomeNewMethods:
    def test_recent_net_pnls_null_excluded_desc_limit(self, db):
        base = 1_000_000.0
        _add(db, net_pnl_pct=1.0, exit_time=base + 100, status_open=False)
        _add(db, net_pnl_pct=2.0, exit_time=base + 300, status_open=False)
        _add(db, net_pnl_pct=3.0, exit_time=base + 200, status_open=False)
        # NULL net_pnl_pct must ride close_kwargs (positional default 9.0
        # would otherwise swallow it — lesson pinned in the B2 suite)
        _add(db, close_kwargs={"net_pnl_pct": None},
             exit_time=base + 400, status_open=False)
        _add(db, net_pnl_pct=9.0)  # still open -> excluded
        assert db.outcomes_recent_net_pnls(10) == [2.0, 3.0, 1.0]
        assert db.outcomes_recent_net_pnls(2) == [2.0, 3.0]

    def test_recent_net_pnls_empty(self, db):
        assert db.outcomes_recent_net_pnls(100) == []

    def test_closed_oldest_true_ascending(self, db):
        base = 2_000_000.0
        _add(db, net_pnl_pct=1.0, exit_time=base + 100, status_open=False)
        _add(db, net_pnl_pct=2.0, exit_time=base + 300, status_open=False)
        _add(db, net_pnl_pct=3.0, exit_time=base + 200, status_open=False)
        rows = db.outcomes_get_closed_oldest()
        assert [r["net_pnl_pct"] for r in rows] == [1.0, 3.0, 2.0]

    def test_closed_oldest_not_natural_order(self, db):
        # pin that ASC is a real ORDER BY, not the accidental insert order
        # (insert order mid, newest, oldest must NOT survive)
        base = 3_000_000.0
        _add(db, net_pnl_pct=10.0, exit_time=base + 200, status_open=False)
        _add(db, net_pnl_pct=20.0, exit_time=base + 300, status_open=False)
        _add(db, net_pnl_pct=30.0, exit_time=base + 100, status_open=False)
        rows = db.outcomes_get_closed_oldest()
        assert [r["net_pnl_pct"] for r in rows] == [30.0, 10.0, 20.0]

    def test_history_rows_symbol_filter_and_projection(self, db):
        _add(db, symbol="BTCUSDT", net_pnl_pct=1.0, status_open=False,
             entry_time=100.0, exit_time=200.0)
        _add(db, symbol="ETHUSDT", net_pnl_pct=2.0, status_open=False,
             entry_time=300.0, exit_time=400.0)
        rows = db.outcomes_history_rows(symbol="ETHUSDT", limit=10)
        assert len(rows) == 1
        assert set(rows[0].keys()) == {"symbol", "entry_price", "exit_price",
                                       "net_pnl_pct", "strategy",
                                       "exit_reason", "status"}
        assert rows[0]["symbol"] == "ETHUSDT"

    def test_history_rows_all_desc_limit(self, db):
        _add(db, net_pnl_pct=1.0, status_open=False, entry_time=100.0)
        _add(db, net_pnl_pct=2.0, status_open=False, entry_time=300.0)
        _add(db, net_pnl_pct=3.0, status_open=False, entry_time=200.0)
        rows = db.outcomes_history_rows(limit=2)
        # newest entry first: pnl 2.0 (entry_time 300) then 3.0 (200)
        assert [r["net_pnl_pct"] for r in rows] == [2.0, 3.0]

    def test_symbol_counts(self, db):
        _add(db, symbol="BTCUSDT", status_open=True)
        _add(db, symbol="BTCUSDT", net_pnl_pct=1.0, status_open=False)
        assert db.outcomes_symbol_counts("BTCUSDT") == (2, 1)
        assert db.outcomes_symbol_counts("NOPE") == (0, 0)


# ============ portfolio_set_stop_loss (StateDB) ============

class TestPortfolioSetStopLoss:
    def _seed(self, db):
        db._get_conn().execute(
            """INSERT INTO portfolio
               (symbol, quantity, entry_price, strategy, opened_at,
                updated_at, stop_loss, take_profit, invest_pct)
               VALUES ('BTC', 1.0, 100.0, 'rsi', 1.0, 1.0, 90.0, 120.0, 0.1)""")
        db._get_conn().commit()

    def test_updates_stop_loss_and_updated_at(self, db):
        self._seed(db)
        before = db.portfolio_get("BTC")["updated_at"]
        time.sleep(0.01)
        db.portfolio_set_stop_loss("BTC", 95.5)
        row = db.portfolio_get("BTC")
        assert row["stop_loss"] == 95.5
        assert row["updated_at"] > before

    def test_missing_symbol_noop(self, db):
        db.portfolio_set_stop_loss("GHOST", 1.0)  # must not raise
        assert db.portfolio_get("GHOST") is None


# ============ migrated call sites (behaviour level) ============

class TestMigratedCallSites:
    def test_rolling_stats_groups_by_strategy(self, db):
        from src.strategy_rolling_stats import refresh_rolling_stats
        now = time.time()
        _add(db, strategy="A", net_pnl_pct=2.0, status_open=False,
             exit_time=now - 60)
        _add(db, strategy="A", net_pnl_pct=1.0, status_open=False,
             exit_time=now - 50)
        _add(db, strategy="A", net_pnl_pct=-1.0, status_open=False,
             exit_time=now - 40, close_kwargs={"is_win": False})
        _add(db, strategy="B", net_pnl_pct=-2.0, status_open=False,
             exit_time=now - 30)
        stats = refresh_rolling_stats(db=db)
        a = stats["A"]
        assert a["n"] == 3 and a["wins"] == 2
        assert a["wr"] == round(2 / 3 * 100, 1)
        assert a["pf"] == round(3.0 / 1.0, 2)  # (2+1) gross win / 1 loss
        assert a["insufficient"] is True  # n < MIN_SAMPLE_TRADES(5)
        assert stats["B"]["n"] == 1

    def test_cvar_uses_outcomes_path(self, db):
        from src.cvar_risk import CVaRRiskManager
        for i in range(12):
            _add(db, net_pnl_pct=1.0, status_open=False,
                 exit_time=1_000.0 + i)
        for i in range(3):
            _add(db, net_pnl_pct=-2.0, status_open=False,
                 exit_time=2_000.0 + i)
        mgr = CVaRRiskManager(db=db)
        risk = mgr.compute_portfolio_risk([
            {"symbol": "BTC", "entry_price": 100.0,
             "current_price": 99.0, "quantity": 1.0}])
        # the outcomes path (>10 rows) ran: worst-tail loss computed
        # (empty positions short-circuits to the zero-default dict)
        assert risk["portfolio_cvar_95"] == -2.0
        assert "risk_level" in risk

    def test_concept_drift_insufficient_and_store(self, db, monkeypatch):
        from src.concept_drift import ConceptDriftDetector
        monkeypatch.setattr("src.state_db.get_state_db", lambda *a, **k: db)
        det = ConceptDriftDetector(db=db)
        for i in range(5):
            _add(db, net_pnl_pct=1.0, status_open=False,
                 exit_time=1_000.0 + i)
        res = det.detect_drift()
        assert res["drift_detected"] is False
        assert "數據不足" in res["recommendation"]
        # full 30-row path: no crash, result stored via kv_set (P6-B3)
        for i in range(25):
            _add(db, net_pnl_pct=1.0, status_open=False,
                 exit_time=2_000.0 + i)
        res = det.detect_drift()
        assert res["severity"] == "none"
        stored = db.kv_get("drift_detection")
        assert isinstance(stored, dict) and stored["severity"] == "none"

    def test_kelly_fraction_values(self, db, monkeypatch):
        from src.risk_manager import RiskManager
        monkeypatch.setattr("src.state_db.get_state_db", lambda *a, **k: db)
        for _ in range(6):
            _add(db, net_pnl_pct=2.0, status_open=False, exit_time=1.0)
        for _ in range(4):
            _add(db, net_pnl_pct=-1.0, status_open=False, exit_time=2.0)
        rm = object.__new__(RiskManager)  # method takes no self state
        out = rm.calculate_kelly_fraction(lookback_trades=50)
        # p=.6, b=avg_win/avg_loss=2.0, kelly=(2*.6-.4)/2=.4 -> capped .25
        assert out["kelly_fraction"] == 0.25
        assert out["win_rate"] == 0.6
        assert out["profit_ratio"] == 2.0
        assert out["trades_analyzed"] == 10

    def test_param_optimizer_roundtrip(self, db):
        from src.param_optimizer import ParamOptimizer, DEFAULT_PARAMS
        po = ParamOptimizer(db=db)
        good = dict(DEFAULT_PARAMS)
        good["extra_k"] = 1
        db.kv_set("optimized_params", good)
        assert po.get_current_params() == good
        db.kv_set("optimized_params", {"partial": 1})  # missing keys
        assert po.get_current_params() == dict(DEFAULT_PARAMS)
        db._get_conn().execute(
            "INSERT OR REPLACE INTO kv (key, value, updated_at)"
            " VALUES ('optimized_params', 'garbage', ?)", (time.time(),))
        db._get_conn().commit()
        assert po.get_current_params() == dict(DEFAULT_PARAMS)

    def test_portfolio_trade_history_projection(self, db, monkeypatch):
        from src.portfolio import PortfolioManager
        monkeypatch.setattr("src.state_db.get_state_db", lambda *a, **k: db)
        pm = PortfolioManager()
        _add(db, symbol="BTCUSDT", net_pnl_pct=1.5, status_open=False,
             entry_time=100.0)
        _add(db, symbol="BTCUSDT", net_pnl_pct=2.5, status_open=False,
             entry_time=300.0)
        _add(db, symbol="ETHUSDT", net_pnl_pct=3.5, status_open=False,
             entry_time=200.0)
        hist = pm.get_trade_history("BTCUSDT", limit=10)
        assert len(hist) == 2
        assert [h["pnl"] for h in hist] == [2.5, 1.5]  # newest entry first
        assert hist[0]["entry_price"] == 100.0
        assert hist[0]["status"] == "closed"
        allh = pm.get_trade_history(None, limit=2)
        assert len(allh) == 2
        assert allh[0]["pnl"] == 2.5  # entry_time 300 is newest

    def test_self_healer_covars_fix(self, db, monkeypatch):
        from src.self_healer import _fix_hmm_covars_shape
        monkeypatch.setattr("src.state_db.get_state_db", lambda *a, **k: db)
        empty = _fix_hmm_covars_shape()
        assert empty == {"fixed": False, "msg": "No HMM model state in DB"}
        # 3-D covars (n_components x 2 x 2) must be flattened to diag form
        db.kv_set("hmm_model_state",
                  {"covars": [[[1.0, 2.0], [3.0, 4.0]]]})
        res = _fix_hmm_covars_shape()
        assert res["fixed"] is True
        state = db.kv_get("hmm_model_state")
        assert state["covars"] == [[1.0, 4.0]]  # np.diag of first layer


# ============ protection_guardian legacy audit fallback ============

class TestProtectionGuardianAuditFallback:
    def test_fallback_row_bytes_and_source(self, db, monkeypatch):
        from src.protection_guardian import _audit
        monkeypatch.setattr("src.state_db.get_state_db", lambda *a, **k: db)
        db.kv_set("ledger:repairs", 0)  # funnel OFF -> fallback path
        _audit("PG_TEST", {"中文": "值", "n": 1})
        rows = db.audit_get_recent(limit=5, action="PG_TEST")
        assert len(rows) == 1
        # details bytes match the legacy ensure_ascii=False dump
        assert rows[0]["details"] == '{"中文": "值", "n": 1}'
        assert rows[0]["source"] == "system"  # documented deviation

    def test_age_helper_uses_state_db_method(self, db):
        from src.kv_preflight import _kv_age_s
        assert _kv_age_s(db, "nope") is None
        db.kv_set("k", 1)
        assert _kv_age_s(db, "k") is not None


# ============ migration guard: no raw SQL against migrated tables ============

_MODULES = ["tp_sl_tracker", "strategy_rolling_stats", "self_healer",
            "protection_guardian", "cvar_risk", "strategy_adaptor",
            "concept_drift", "portfolio", "trade_executor", "risk_manager",
            "param_optimizer", "kv_preflight", "cmd_trailing_check",
            "paper_trader"]

_RAW_SQL = re.compile(
    r"(FROM|INTO|UPDATE|TABLE|JOIN)\s+"
    r"(IF\s+NOT\s+EXISTS\s+)?"
    r"(trade_outcomes|kv|audit_log|bull_regime_log|portfolio|paper_trades|paper_portfolio|paper_pending_orders)\b")


@pytest.mark.parametrize("mod", _MODULES)
def test_no_raw_sql_against_migrated_tables(mod):
    src = (Path(__file__).resolve().parent.parent / "src" / f"{mod}.py").read_text()
    for i, line in enumerate(src.splitlines(), 1):
        assert not _RAW_SQL.search(line), \
            f"{mod}.py:{i} still contains raw SQL: {line.strip()!r}"


@pytest.mark.parametrize("method", [
    "kv_get_prefix", "kv_age_seconds", "outcomes_recent_net_pnls",
    "outcomes_get_closed_oldest", "outcomes_history_rows",
    "outcomes_symbol_counts", "portfolio_set_stop_loss",
    "commit", "rollback", "paper_sim_get", "paper_sim_set",
    "paper_trades_recent", "paper_last_buy_price", "paper_trade_add",
    "paper_pending_add", "paper_pending_open", "paper_pending_get",
])
def test_state_db_methods_exist(method):
    assert hasattr(StateDB, method)


# ============ paper store (StateDB layer) ============

class TestPaperStoreStateDB:
    def test_sim_roundtrip_and_default(self, db):
        assert db.paper_sim_get("missing", "0") == "0"
        db.paper_sim_set("k", "1.5")
        assert db.paper_sim_get("k") == "1.5"
        db.paper_sim_set("k", "2.5")  # upsert
        assert db.paper_sim_get("k") == "2.5"

    def test_sim_deferred_commit_rollback_semantics(self, db):
        db.paper_sim_set("k", "1", commit=True)
        db.paper_sim_set("k", "2", commit=False)  # deferred (P3-1)
        db.rollback()
        assert db.paper_sim_get("k") == "1"        # deferred write undone
        db.paper_sim_set("k", "3", commit=False)
        db.commit()
        assert db.paper_sim_get("k") == "3"        # committed atomically

    def test_trade_add_and_recent(self, db):
        db.paper_trade_add("t1", "BTCUSDT", "BUY", 1.0, 100.0, 0.05,
                           0.1, 100.0, "{}")
        db.paper_trade_add("t2", "ETHUSDT", "SELL", 2.0, 50.0, -0.05,
                           0.1, 100.0, "{}")
        db.paper_trade_add("t3", "BTCUSDT", "SELL", 1.0, 110.0, -0.05,
                           0.11, 110.0, "{}")
        rows = db.paper_trades_recent(limit=10)
        assert len(rows) == 3
        assert rows[0]["id"] == "t3"  # newest first
        btc = db.paper_trades_recent(symbol="BTCUSDT", limit=10)
        assert {r["id"] for r in btc} == {"t1", "t3"}
        assert btc[0]["side"] == "SELL"

    def test_last_buy_price(self, db):
        assert db.paper_last_buy_price("BTCUSDT") is None
        db.paper_trade_add("t1", "BTCUSDT", "BUY", 1.0, 100.0, 0.05,
                           0.1, 100.0, "{}")
        db.paper_trade_add("t2", "BTCUSDT", "SELL", 1.0, 110.0, -0.05,
                           0.11, 110.0, "{}")
        db.paper_trade_add("t3", "BTCUSDT", "BUY", 1.0, 120.0, 0.05,
                           0.12, 120.0, "{}")
        assert db.paper_last_buy_price("BTCUSDT") == 120.0
        assert db.paper_last_buy_price("ETHUSDT") is None

    def test_pending_roundtrip(self, db):
        db.paper_pending_add("1", "BTCUSDT", "BUY", "LIMIT", 1.0,
                             95.0, None, "{}")
        db.paper_pending_add("2", "ETHUSDT", "SELL", "STOP_LOSS_LIMIT",
                             2.0, 55.0, 54.0, "{}")
        got = db.paper_pending_get("1")
        assert got is not None and got["symbol"] == "BTCUSDT"
        assert got["order_type"] == "LIMIT" and got["stop_price"] is None
        assert db.paper_pending_get("nope") is None
        assert len(db.paper_pending_open()) == 2
        only_btc = db.paper_pending_open(symbol="BTCUSDT")
        assert len(only_btc) == 1 and only_btc[0]["id"] == "1"


# ============ paper store (PaperTrader behaviour, fix semantics) ============

def _make_trader(db):
    """PaperTrader without the ccxt __init__ (no network)."""
    from src.paper_trader import PaperTrader
    pt = object.__new__(PaperTrader)
    pt._db = db
    pt._in_transaction = False
    return pt


class TestPaperTraderFillPipeline:
    def test_buy_fill_updates_state_atomically(self, db):
        pt = _make_trader(db)
        res = pt._fill_market("BTCUSDT", "BUY", 1.0, 100.0)
        assert res is not None and res["status"] == "FILLED"
        # balance: 10000 - (100.05 notional + 0.10005 fee)
        assert pt._get_sim_balance() == pytest.approx(10000 - 100.15005)
        pos = pt._get_sim_positions()["BTC"]
        assert pos["qty"] == pytest.approx(1.0)
        assert pos["entry_price"] == pytest.approx(100.05)
        rows = db.paper_trades_recent("BTCUSDT")
        assert len(rows) == 1 and rows[0]["side"] == "BUY"

    def test_sell_realized_pnl_fix_semantics(self, db):
        """Travis-approved fix: SELL realized PnL + sim_pnl must actually
        update. The legacy query read a nonexistent entry_price column,
        always threw, and silently kept pnl=0."""
        pt = _make_trader(db)
        assert pt._fill_market("BTCUSDT", "BUY", 1.0, 100.0)
        res = pt._fill_market("BTCUSDT", "SELL", 1.0, 100.0)
        assert res is not None
        # BUY fill 100.05, SELL fill 99.95, fee on sell 0.09995
        expected = (99.95 - 100.05) * 1.0 - 0.09995
        assert res["_paper"]["pnl"] == pytest.approx(expected)  # legacy: always 0.0
        assert pt._get_sim_pnl() == pytest.approx(expected)  # legacy: never updated
        # position closed
        assert "BTC" not in pt._get_sim_positions()

    def test_sell_without_buy_leg_pnl_zero(self, db):
        pt = _make_trader(db)
        pt.paper_force_position("BTCUSDT", 1.0, 100.0) if hasattr(
            pt, "paper_force_position") else None
        # no BUY leg on record -> paper_last_buy_price None -> pnl 0
        pt._set_sim_positions({"BTC": {"qty": 1.0, "entry_price": 100.0,
                                       "symbol": "BTCUSDT"}})
        res = pt._fill_market("BTCUSDT", "SELL", 1.0, 100.0)
        assert res is not None
        assert res["_paper"]["pnl"] == 0.0
        assert pt._get_sim_pnl() == 0.0

    def test_insufficient_buy_rolls_back_atomically(self, db):
        pt = _make_trader(db)
        pt._set_sim_balance(50.0)  # below 100.15 total cost
        res = pt._fill_market("BTCUSDT", "BUY", 1.0, 100.0)
        assert res is None
        assert pt._get_sim_balance() == pytest.approx(50.0)
        assert pt._get_sim_positions() == {}
        assert db.paper_trades_recent("BTCUSDT") == []

    def test_trade_history_via_new_reader(self, db):
        pt = _make_trader(db)
        pt._fill_market("BTCUSDT", "BUY", 1.0, 100.0)
        pt._fill_market("ETHUSDT", "BUY", 2.0, 50.0)
        hist = pt.get_trade_history("BTCUSDT", limit=10)
        assert len(hist) == 1 and hist[0]["symbol"] == "BTCUSDT"
        allh = pt.get_trade_history(None, limit=10)
        assert len(allh) == 2

    def test_pending_order_roundtrip(self, db):
        pt = _make_trader(db)
        # seed counter so _increment_order_counter has a base
        pt._set_sim_value("order_counter", "0")
        res = pt._place_limit("BTCUSDT", "BUY", 1.0, 95.0)
        # current price unavailable (no ccxt) -> order stays pending
        assert res is not None and res["status"] == "NEW"
        assert res["_paper"]["pending"] is True
        rows = db.paper_pending_open("BTCUSDT")
        assert len(rows) == 1
        assert rows[0]["price"] == 95.0
        got = db.paper_pending_get(rows[0]["id"])
        assert got is not None and got["symbol"] == "BTCUSDT"
