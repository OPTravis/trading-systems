"""Phase 2-A tests (2026-09-18): per-trade rolling stats + Bandit SL/TP arms.

Covers: rolling window semantics (30-trade cap x 7-day band), profit
factor math, kv round-trip, fail-safe paths; bandit SL/TP arm independence
and persistence; adaptor clamp bounds (SL [4,15]%, TP1 [6,25]%) and
static-values fail-safe; record_outcome end-to-end attribution (size arms
+ SL/TP arms + rolling stats refresh on the same close, covering the
reconciler booked-fill path which lands in the same recorder).
"""
import json
import math
import time
from unittest.mock import patch

import pytest

from src.contextual_bandit import (
    ContextualBandit,
    SLTP_MULTIPLIERS,
    SLTP_STORAGE_KEY,
)
from src.strategy_rolling_stats import (
    MIN_SAMPLE_TRADES,
    STORAGE_KEY as ROLLING_KEY,
    WINDOW_TRADES,
    compute_profit_factor,
    get_rolling_stats,
    refresh_rolling_stats,
)


def _db(tmp_path):
    from src.state_db import StateDB
    return StateDB(str(tmp_path / "state.db"))


def _seed_closed(db, strategy, pnls, spacing_sec=60):
    """Insert closed trade_outcomes rows; returns row ids."""
    now = time.time()
    conn = db._get_conn()
    ids = []
    for i, pnl in enumerate(pnls):
        cur = conn.execute(
            """INSERT INTO trade_outcomes
            (symbol, entry_time, entry_price, qty, strategy, status,
             exit_time, exit_price, pnl_pct, net_pnl_pct, is_win)
            VALUES (?, ?, ?, ?, ?, 'closed', ?, ?, ?, ?, ?)""",
            ("TESTUSDT", now - (len(pnls) - i) * spacing_sec, 100.0, 1.0,
             strategy, now - (len(pnls) - i) * spacing_sec + 30, 100.0 + pnl,
             pnl, pnl, 1 if pnl > 0 else 0))
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


class TestRollingStats:
    def test_profit_factor_math(self):
        assert compute_profit_factor([2.0, -1.0, 3.0, -1.0]) == 2.5
        assert compute_profit_factor([1.0, 2.0]) == math.inf
        assert compute_profit_factor([-1.0, -2.0]) == 0.0
        assert compute_profit_factor([]) == 0.0

    def test_window_caps_at_30_trades(self, tmp_path):
        db = _db(tmp_path)
        _seed_closed(db, "rsi", [1.0] * 35)
        stats = refresh_rolling_stats(db=db)
        assert stats["rsi"]["n"] == WINDOW_TRADES == 30

    def test_window_7day_band_filters_old(self, tmp_path):
        db = _db(tmp_path)
        # 3 recent wins + 10 old (12 days ago) losses: band keeps only recent
        now = time.time()
        conn = db._get_conn()
        for i, (pnl, age_days) in enumerate(
                [(1.0, 0.1), (2.0, 0.2), (-1.0, 0.3)] + [(-3.0, 12.0)] * 10):
            ts = now - age_days * 86400
            conn.execute(
                """INSERT INTO trade_outcomes
                (symbol, entry_time, entry_price, qty, strategy, status,
                 exit_time, exit_price, net_pnl_pct, is_win)
                VALUES ('T', ?, 100.0, 1.0, 'grid', 'closed', ?, 100.0, ?, ?)""",
                (ts, ts, pnl, 1 if pnl > 0 else 0))
        conn.commit()
        stats = refresh_rolling_stats(db=db)
        assert stats["grid"]["n"] == 3
        assert stats["grid"]["wr"] == pytest.approx(66.7, abs=0.1)
        assert stats["grid"]["pf"] == 3.0  # (1+2)/1

    def test_kv_round_trip_and_insufficient_flag(self, tmp_path):
        db = _db(tmp_path)
        _seed_closed(db, "dca", [1.0, -1.0, 1.0])  # n=3 < MIN_SAMPLE_TRADES
        refresh_rolling_stats(db=db)
        cached = get_rolling_stats(db=db)
        assert cached["dca"]["n"] == 3
        assert cached["dca"]["insufficient"] is True

    def test_empty_db_returns_empty(self, tmp_path):
        db = _db(tmp_path)
        assert refresh_rolling_stats(db=db) == {}
        assert get_rolling_stats(db=db) == {}

    def test_fail_safe_on_db_error(self, tmp_path):
        db = _db(tmp_path)
        with patch.object(db, "_get_conn", side_effect=RuntimeError("boom")):
            out = refresh_rolling_stats(db=db)  # must not raise
        assert out == {}


class TestBanditSltpArms:
    def _ctx(self):
        return {"hmm_regime": "bull", "fear_greed": 60,
                "btc_trend": "BULLISH", "portfolio_heat": "cold"}

    def test_cold_start_returns_unit(self, tmp_path):
        b = ContextualBandit(db=_db(tmp_path))
        assert b.recommend_sltp(self._ctx()) == (1.0, 1.0)

    def test_update_independent_arms_and_persistence(self, tmp_path):
        db = _db(tmp_path)
        b = ContextualBandit(db=db)
        b.update_sltp(self._ctx(), sl_mult=0.8, tp_mult=1.2, pnl_pct=2.0)
        raw = db.kv_get(SLTP_STORAGE_KEY)
        assert raw, "sltp priors persisted"
        # reload from a fresh instance
        b2 = ContextualBandit(db=db)
        # after a positive outcome: alpha grew on the used arms only
        b2.update_sltp(self._ctx(), sl_mult=0.8, tp_mult=1.2, pnl_pct=-1.0)
        raw2 = db.kv_get(SLTP_STORAGE_KEY)
        assert raw2  # still serializable after mixed updates
        # recommend must stay in the legal action set
        for _ in range(20):
            sl, tp = b2.recommend_sltp(self._ctx())
            assert sl in SLTP_MULTIPLIERS and tp in SLTP_MULTIPLIERS

    def test_win_grows_alpha_loss_grows_beta(self, tmp_path):
        from src.contextual_bandit import _context_to_index
        db = _db(tmp_path)
        b = ContextualBandit(db=db)
        ctx = self._ctx()
        idx = _context_to_index(ctx)
        b.update_sltp(ctx, sl_mult=1.0, tp_mult=1.0, pnl_pct=2.0)
        sl_arms, tp_arms = b._sltp_priors[idx]
        assert sl_arms[2][0] > 1.0 and tp_arms[2][0] > 1.0   # alpha grew
        assert sl_arms[2][1] == 1.0 and tp_arms[2][1] == 1.0  # beta intact
        b.update_sltp(ctx, sl_mult=1.0, tp_mult=1.0, pnl_pct=-2.0)
        assert b._sltp_priors[idx][0][2][1] > 1.0  # beta grew on loss

    def test_update_nearest_arm_snap(self, tmp_path):
        from src.contextual_bandit import _context_to_index
        db = _db(tmp_path)
        b = ContextualBandit(db=db)
        ctx = self._ctx()
        idx = _context_to_index(ctx)
        b.update_sltp(ctx, sl_mult=0.95, tp_mult=1.0, pnl_pct=1.0)  # snaps to 0.9
        sl_arms = b._sltp_priors[idx][0]
        mid = SLTP_MULTIPLIERS.index(0.9)
        assert sl_arms[mid][0] > 1.0
        assert sl_arms[SLTP_MULTIPLIERS.index(1.0)][0] == 1.0

    def test_update_fail_safe(self, tmp_path):
        b = ContextualBandit(db=_db(tmp_path))
        b._db.kv_set = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        b.update_sltp(self._ctx(), 1.0, 1.0, 1.0)  # must not raise


class TestAdaptorBanditSltp:
    def _fake_bandit(self, retval=(1.0, 1.0), exc=None):
        class FB:
            def recommend_sltp(self, ctx):
                if exc:
                    raise exc
                return retval
        return FB()

    def _strategies(self):
        return {
            "rsi": {"sl_pct": 5.0, "tp_levels": [
                {"pct": 8.0}, {"pct": 12.8}, {"pct": 20.0}]},
            "dca": {"sl_pct": 6.0, "tp_levels": [
                {"pct": 9.0}, {"pct": 14.4}, {"pct": 22.5}]},
        }

    def _adaptor(self):
        from src.strategy_adaptor import StrategyAdaptor
        a = StrategyAdaptor()
        a._cache = None
        return a

    def test_multipliers_applied(self):
        a = self._adaptor()
        st = self._strategies()
        changes = []
        with patch("src.contextual_bandit.get_contextual_bandit",
                   return_value=self._fake_bandit((0.8, 1.2))):
            out = a._apply_bandit_sltp(st, 55, "BULLISH", "BULL_TREND", changes)
        assert out == {"sl_mult": 0.8, "tp_mult": 1.2}
        assert st["rsi"]["sl_pct"] == 4.0     # 5.0*0.8, exactly at floor
        assert st["rsi"]["tp_levels"][0]["pct"] == 9.6   # 8.0*1.2
        assert st["rsi"]["tp_levels"][1]["pct"] == 15.36  # 12.8*1.2
        assert any("Bandit SL/TP" in c for c in changes)

    def test_clamp_floors_and_caps(self):
        a = self._adaptor()
        st = {"x": {"sl_pct": 3.0, "tp_levels": [
            {"pct": 24.0}, {"pct": 38.0}, {"pct": 55.0}]}}
        with patch("src.contextual_bandit.get_contextual_bandit",
                   return_value=self._fake_bandit((0.8, 1.2))):
            a._apply_bandit_sltp(st, 55, "BULLISH", None, [])
        assert st["x"]["sl_pct"] == 4.0       # 2.4 clamped to floor
        assert st["x"]["tp_levels"][0]["pct"] == 25.0  # 28.8 capped
        assert st["x"]["tp_levels"][1]["pct"] == 40.0  # 45.6 capped
        assert st["x"]["tp_levels"][2]["pct"] == 60.0  # 66 capped

    def test_fail_safe_keeps_static_values(self):
        a = self._adaptor()
        st = self._strategies()
        before = json.dumps(st, sort_keys=True)
        with patch("src.contextual_bandit.get_contextual_bandit",
                   return_value=self._fake_bandit(exc=RuntimeError("bandit down"))):
            out = a._apply_bandit_sltp(st, 55, "BULLISH", None, [])
        assert out == {"sl_mult": 1.0, "tp_mult": 1.0}
        assert json.dumps(st, sort_keys=True) == before

    def test_adapt_result_carries_bandit_sltp(self):
        a = self._adaptor()
        with patch("src.contextual_bandit.get_contextual_bandit",
                   return_value=self._fake_bandit((0.9, 1.1))):
            res = a.adapt(fear_greed=55, btc_trend="BULLISH",
                          btc_price_change_24h=1.5)
        assert res["bandit_sltp"] == {"sl_mult": 0.9, "tp_mult": 1.1}
        # every strategy's final SL/TP within hard bounds
        for cfg in res["strategies"].values():
            if "sl_pct" in cfg:
                assert 4.0 <= cfg["sl_pct"] <= 15.0
            tps = cfg.get("tp_levels") or []
            if tps:
                assert 6.0 <= tps[0]["pct"] <= 25.0


class TestRecorderEndToEnd:
    def test_close_updates_all_three(self, tmp_path):
        """One record_outcome call: size arms + SL/TP arms + rolling kv."""
        db = _db(tmp_path)
        bandit = ContextualBandit(db=db)
        recorder = None
        from src.trade_outcome_recorder import TradeOutcomeRecorder
        recorder = TradeOutcomeRecorder(db=db)
        recorder.record_entry(
            symbol="TESTUSDT", entry_price=100.0, qty=2.0, score=70,
            strategy="rsi", regime="NEUTRAL", fng_score=55,
            btc_trend="BULLISH")

        fake_pos = {
            "bandit_context": {"hmm_regime": "bull", "fear_greed": 55,
                               "btc_trend": "BULLISH", "portfolio_heat": "cold"},
            "bandit_multiplier": 0.8, "sl_mult": 0.9, "tp_mult": 1.1,
        }

        class FakePM:
            positions = {"TESTUSDT": fake_pos}

        with patch("src.contextual_bandit.get_contextual_bandit",
                   return_value=bandit), \
             patch("src.portfolio.PortfolioManager", return_value=FakePM()):
            out = recorder.record_outcome(
                symbol="TESTUSDT", exit_price=110.0, exit_reason="tp1")
        assert out is not None

        # rolling stats refreshed with the closed trade
        rolled = get_rolling_stats(db=db)
        assert rolled["rsi"]["n"] == 1
        assert rolled["rsi"]["wr"] == 100.0

        # SL/TP priors persisted for that context
        assert db.kv_get(SLTP_STORAGE_KEY)
        # and the size-bandit priors too (pre-existing behaviour intact)
        assert db.kv_get("contextual_bandit:priors")

    def test_reconciler_path_shares_recorder(self):
        """Booked-fill closes go through the same record_outcome, so the
        attribution lands there too — wiring assertion."""
        src = open("scripts/reconcile_fills.py").read()
        assert src.count("TradeOutcomeRecorder(db=db).record_outcome") >= 1
        rec = open("src/trade_outcome_recorder.py").read()
        assert "update_sltp(" in rec
        assert "refresh_rolling_stats(db=self._db)" in rec
