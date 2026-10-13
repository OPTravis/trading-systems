"""WO-1046h (P4/S6): HMM rolling score — hit-rate downweight + divergence
retrain + log-only-first semantics.

Covers:
- rolling_hit_rate direction-consistency semantics (bull-ish×win /
  bear-ish×loss = hit), window cutoff, context-tag vs daily-ledger join
- divergence_days streak semantics (CONFIRMED_BULL × BEAR_TREND, break on
  non-divergent day, gap tolerance)
- apply_downweight: log-only no-op vs active shrink (threshold_adj ×factor,
  position_scale toward 1.0, avoid downgrade flag)
- compute_hmm_score: min_sample noise gate, would/active separation,
  kv persistence (weekly-review source)
- should_retrain third trigger: log-only reports would_reason without
  firing; enabled fires with divergence reason
- strategy_adaptor integration: downweight active → threshold shrinks and
  avoid downgraded to half-size; log-only → would line only
"""
import json
import time

import pytest

from src.hmm_scoring import (
    DEFAULTS, KV_DAILY_VERDICTS, KV_ROLLING_SCORE, KV_SCORING_CONFIG,
    apply_downweight, compute_hmm_score, divergence_days, get_config,
    record_daily_verdict, rolling_hit_rate,
)
from src.state_db import StateDB


@pytest.fixture()
def db(tmp_path):
    return StateDB(str(tmp_path / "wo1046h.db"))


def _mk_closed(db, *, symbol="T1", entry_date, days_ago, is_win,
               hmm_ctx=None, exit_days_ago=None, strategy="trend"):
    """Insert one closed outcome row at (now - days_ago)."""
    now = time.time()
    entry_ts = now - days_ago * 86400
    exit_ts = now - (exit_days_ago if exit_days_ago is not None else
                     max(days_ago - 0.1, 0)) * 86400
    ctx = {}
    if hmm_ctx:
        ctx["hmm_regime"] = hmm_ctx
    pnl = 5.0 if is_win else -5.0
    db.outcome_add_entry(
        symbol=symbol, entry_time=entry_ts, entry_date=entry_date,
        entry_price=100.0, qty=1.0, score=80, strategy=strategy,
        factors_json="{}", context_json=json.dumps(ctx))
    row_id = db._get_conn().execute(
        "SELECT id FROM trade_outcomes ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]
    db._get_conn().execute(
        """UPDATE trade_outcomes
           SET status='closed', exit_time=?, exit_price=?, exit_reason='tp1',
               is_win=?, pnl_pct=?, net_pnl_pct=?, updated_at=?
           WHERE id=?""",
        (exit_ts, 105.0 if is_win else 95.0, 1 if is_win else 0, pnl, pnl,
         exit_ts, row_id))
    db._get_conn().commit()
    return row_id


# ── rolling_hit_rate ─────────────────────────────────────────────────────

class TestRollingHitRate:
    def test_bull_win_and_bear_loss_are_hits(self, db):
        _mk_closed(db, entry_date="2026-10-01", days_ago=10, is_win=True,
                   hmm_ctx="BULL_TREND")
        _mk_closed(db, entry_date="2026-10-02", days_ago=9, is_win=False,
                   hmm_ctx="BEAR_TREND", symbol="T2")
        r = rolling_hit_rate(db)
        assert r["sample_size"] == 2
        assert r["hits"] == 2
        assert r["hit_rate"] == 1.0

    def test_bull_loss_and_bear_win_are_misses(self, db):
        _mk_closed(db, entry_date="2026-10-01", days_ago=10, is_win=False,
                   hmm_ctx="BULL_TREND")
        _mk_closed(db, entry_date="2026-10-02", days_ago=9, is_win=True,
                   hmm_ctx="BEAR_TREND", symbol="T2")
        r = rolling_hit_rate(db)
        assert r["hit_rate"] == 0.0

    def test_daily_ledger_join_fallback(self, db):
        # no context tag; verdict comes from the daily ledger via entry_date
        _mk_closed(db, entry_date="2026-10-05", days_ago=8, is_win=False)
        db.kv_set(KV_DAILY_VERDICTS,
                  {"2026-10-05": {"hmm": "BEAR_TREND"}})
        r = rolling_hit_rate(db)
        assert r["sample_size"] == 1 and r["hits"] == 1

    def test_no_verdict_coverage_skips_not_misses(self, db):
        _mk_closed(db, entry_date="2026-10-01", days_ago=10, is_win=True)
        r = rolling_hit_rate(db)
        assert r["sample_size"] == 0 and r["hit_rate"] is None

    def test_window_cutoff_excludes_old_trades(self, db):
        _mk_closed(db, entry_date="2026-09-01", days_ago=40, is_win=True,
                   hmm_ctx="BULL_TREND")
        _mk_closed(db, entry_date="2026-10-10", days_ago=5, is_win=False,
                   hmm_ctx="BULL_TREND", symbol="T2")
        r = rolling_hit_rate(db, window_days=30)
        assert r["sample_size"] == 1  # only the recent one

    def test_unknown_regime_label_skipped(self, db):
        _mk_closed(db, entry_date="2026-10-01", days_ago=5, is_win=True,
                   hmm_ctx="SIDEWAYS_UNKNOWN")
        r = rolling_hit_rate(db)
        assert r["sample_size"] == 0

    def test_context_tag_preferred_over_ledger(self, db):
        _mk_closed(db, entry_date="2026-10-05", days_ago=5, is_win=True,
                   hmm_ctx="BULL_TREND")
        db.kv_set(KV_DAILY_VERDICTS,
                  {"2026-10-05": {"hmm": "BEAR_TREND"}})
        r = rolling_hit_rate(db)
        # context says BULL×win = hit; ledger would say BEAR×win = miss
        assert r["hits"] == 1


# ── divergence_days ──────────────────────────────────────────────────────

class TestDivergenceDays:
    def test_streak_counts_trailing_run(self, db):
        db.kv_set(KV_DAILY_VERDICTS, {
            "2026-10-08": {"hmm": "RANGE_BOUND", "bull_tier": "CONFIRMED_BULL"},
            "2026-10-11": {"hmm": "BEAR_TREND", "bull_tier": "CONFIRMED_BULL"},
            "2026-10-12": {"hmm": "BEAR_TREND", "bull_tier": "CONFIRMED_BULL"},
            "2026-10-13": {"hmm": "BEAR_TREND", "bull_tier": "CONFIRMED_BULL"},
        })
        d = divergence_days(db)
        assert d["divergence_days"] == 3
        assert d["first_date"] == "2026-10-11"

    def test_non_divergent_day_breaks_streak(self, db):
        db.kv_set(KV_DAILY_VERDICTS, {
            "2026-10-11": {"hmm": "BEAR_TREND", "bull_tier": "CONFIRMED_BULL"},
            "2026-10-12": {"hmm": "BULL_TREND", "bull_tier": "CONFIRMED_BULL"},
            "2026-10-13": {"hmm": "BEAR_TREND", "bull_tier": "CONFIRMED_BULL"},
        })
        assert divergence_days(db)["divergence_days"] == 1

    def test_bear_bull_mismatch_required(self, db):
        # BEAR_TREND but bull tier not CONFIRMED_BULL -> no divergence
        db.kv_set(KV_DAILY_VERDICTS, {
            "2026-10-11": {"hmm": "BEAR_TREND", "bull_tier": "NEUTRAL"},
            "2026-10-12": {"hmm": "BEAR_TREND", "bull_tier": "MILD_BULL"},
        })
        assert divergence_days(db)["divergence_days"] == 0

    def test_empty_ledger(self, db):
        assert divergence_days(db)["divergence_days"] == 0

    def test_record_daily_verdict_idempotent_freshest(self, db):
        record_daily_verdict(db, "CONFIRMED_BULL", "RANGE_BOUND",
                             date_str="2026-10-13")
        record_daily_verdict(db, "CONFIRMED_BULL", "BEAR_TREND",
                             date_str="2026-10-13")
        ledger = db.kv_get(KV_DAILY_VERDICTS)
        assert ledger["2026-10-13"]["hmm"] == "BEAR_TREND"  # freshest wins


# ── apply_downweight ─────────────────────────────────────────────────────

class TestApplyDownweight:
    def _adj(self):
        return {"preferred_strategies": ["dca", "rsi"],
                "avoid_strategies": ["trend", "grid"],
                "position_scale": 1.2,
                "score_threshold_adj": 10}

    def test_log_only_by_default_no_mutation(self, db):
        out = apply_downweight(db, self._adj(), hit_rate=0.3)
        assert out["audit"]["applied"] is False
        assert out["audit"].get("would") is True
        assert out["adjustments"]["score_threshold_adj"] == 10
        assert "avoid_downgraded_to_half_size" not in out["adjustments"]

    def test_enabled_shrinks_threshold_and_scale(self, db):
        db.kv_set(KV_SCORING_CONFIG, {"enabled": True,
                                      "downweight_factor": 0.5})
        out = apply_downweight(db, self._adj(), hit_rate=0.3)
        a = out["adjustments"]
        assert out["audit"]["applied"] is True
        assert a["score_threshold_adj"] == 5          # 10 × 0.5
        assert a["position_scale"] == pytest.approx(1.1)  # 1 + 0.2×0.5
        assert a["avoid_downgraded_to_half_size"] is True

    def test_enabled_but_no_hit_rate_no_mutation(self, db):
        db.kv_set(KV_SCORING_CONFIG, {"enabled": True})
        out = apply_downweight(db, self._adj(), hit_rate=None)
        assert out["audit"]["applied"] is False
        assert out["adjustments"]["score_threshold_adj"] == 10

    def test_kv_factor_hot_tunable(self, db):
        db.kv_set(KV_SCORING_CONFIG, {"enabled": True,
                                      "downweight_factor": 0.25})
        out = apply_downweight(db, self._adj(), hit_rate=0.3)
        # 10 × 0.25 = 2.5 -> int(round(2.5)) == 2 (banker's rounding)
        assert out["adjustments"]["score_threshold_adj"] == 2
        assert out["adjustments"]["position_scale"] == \
            pytest.approx(1.05)


# ── compute_hmm_score ────────────────────────────────────────────────────

class TestComputeHmmScore:
    def test_min_sample_noise_gate(self, db):
        # 3 samples below min_sample=10 -> would/active both False
        for i, win in enumerate([True, True, True]):
            _mk_closed(db, entry_date=f"2026-10-0{i+1}", days_ago=5 - i,
                       is_win=win, hmm_ctx="BULL_TREND", symbol=f"S{i}")
        s = compute_hmm_score(db, persist=True)
        assert s["sample_size"] == 3
        assert s["downweight_would"] is False   # not enough samples
        assert db.kv_get(KV_ROLLING_SCORE)["sample_size"] == 3

    def test_low_hit_rate_would_downweight(self, db):
        # 10 samples, 3 hits -> 0.3 < 0.45
        for i in range(10):
            _mk_closed(db, entry_date=f"2026-10-0{i+1:02d}"[:10],
                       days_ago=5, is_win=(i < 3), hmm_ctx="BULL_TREND",
                       symbol=f"S{i}")
        s = compute_hmm_score(db)
        assert s["sample_size"] == 10
        assert s["hit_rate"] == pytest.approx(0.3)
        assert s["downweight_would"] is True
        assert s["downweight_active"] is False  # enabled=False default

    def test_enabled_and_low_hit_rate_activates(self, db):
        for i in range(10):
            _mk_closed(db, entry_date=f"2026-10-0{i+1:02d}"[:10],
                       days_ago=5, is_win=(i < 3), hmm_ctx="BULL_TREND",
                       symbol=f"S{i}")
        db.kv_set(KV_SCORING_CONFIG, {"enabled": True})
        s = compute_hmm_score(db)
        assert s["downweight_active"] is True

    def test_divergence_retrain_would_vs_active(self, db):
        ledger = {f"2026-10-{d:02d}":
                  {"hmm": "BEAR_TREND", "bull_tier": "CONFIRMED_BULL"}
                  for d in range(1, 12)}   # 11 consecutive days
        db.kv_set(KV_DAILY_VERDICTS, ledger)
        s = compute_hmm_score(db)
        assert s["divergence_days"] == 11
        assert s["retrain_would"] is True
        assert s["retrain_active"] is False   # log-only default

        db.kv_set(KV_SCORING_CONFIG, {"divergence_retrain_enabled": True})
        s = compute_hmm_score(db)
        assert s["retrain_active"] is True

    def test_score_persisted_to_kv_weekly_review_source(self, db):
        s = compute_hmm_score(db)
        stored = db.kv_get(KV_ROLLING_SCORE)
        assert stored["ts"] == s["ts"]
        assert stored["hit_rate"] == s["hit_rate"]
        assert "downweight_would" in stored and "divergence_days" in stored

    def test_get_config_defaults_and_kv_merge(self, db):
        cfg = get_config(db)
        assert cfg["enabled"] is False
        assert cfg["hit_rate_threshold"] == 0.45
        db.kv_set(KV_SCORING_CONFIG, {"hit_rate_threshold": 0.55})
        cfg = get_config(db)
        assert cfg["hit_rate_threshold"] == 0.55
        assert cfg["enabled"] is False  # untouched keys keep defaults


# ── should_retrain third trigger ─────────────────────────────────────────

class TestShouldRetrainDivergence:
    def _mk_divergent(self, db, days):
        ledger = {f"2026-10-{d:02d}":
                  {"hmm": "BEAR_TREND", "bull_tier": "CONFIRMED_BULL"}
                  for d in range(1, days + 1)}
        db.kv_set(KV_DAILY_VERDICTS, ledger)

    def test_log_only_reports_would_reason_without_firing(self, db):
        from src.hmm_regime import HMMRegimeDetector
        self._mk_divergent(db, 11)
        det = HMMRegimeDetector(db=db)
        det._db.kv_set("hmm_model_state",
                       {"trained_at": time.time(), "n_samples": 100})
        r = det.should_retrain()
        assert r["should_retrain"] is False or "divergence" not in (r["reason"] or "")
        assert r.get("would_reason") and "divergence" in r["would_reason"]

    def test_enabled_divergence_fires_retrain(self, db):
        from src.hmm_regime import HMMRegimeDetector
        self._mk_divergent(db, 11)
        db.kv_set(KV_SCORING_CONFIG, {"divergence_retrain_enabled": True})
        det = HMMRegimeDetector(db=db)
        det._db.kv_set("hmm_model_state",
                       {"trained_at": time.time(), "n_samples": 100})
        r = det.should_retrain()
        assert r["should_retrain"] is True
        assert "divergence" in r["reason"]

    def test_short_divergence_does_not_fire(self, db):
        from src.hmm_regime import HMMRegimeDetector
        self._mk_divergent(db, 9)   # < 10
        db.kv_set(KV_SCORING_CONFIG, {"divergence_retrain_enabled": True})
        det = HMMRegimeDetector(db=db)
        det._db.kv_set("hmm_model_state",
                       {"trained_at": time.time(), "n_samples": 100})
        r = det.should_retrain()
        assert r.get("would_reason") is None
        assert r["should_retrain"] is False


# ── strategy_adaptor integration ─────────────────────────────────────────

class TestAdaptorDownweight:
    def _adapt(self, db, monkeypatch, score, config=None):
        """Drive StrategyAdaptor._apply_risk_overlays with a stubbed DB."""
        from src.strategy_adaptor import StrategyAdaptor

        if config is not None:
            db.kv_set(KV_SCORING_CONFIG, config)

        result = {
            "global": {"score_threshold": 70},
            "strategies": {
                "trend": {"enabled": True, "size_multiplier": 1.0,
                          "reason": ""},
                "dca": {"enabled": True, "size_multiplier": 1.0,
                        "reason": ""},
            },
        }
        changes = []
        adaptor = StrategyAdaptor()
        hmm_adj = {"preferred_strategies": ["dca"],
                   "avoid_strategies": ["trend"],
                   "position_scale": 1.2, "score_threshold_adj": 10}
        monkeypatch.setattr(
            "src.state_db.get_state_db", lambda: db, raising=False)
        adaptor._apply_risk_overlays(
            result, "BEAR_TREND", hmm_adj, 1.0, None,
            50, "NEUTRAL", changes, bull_tier=None, hmm_score=score)
        return result, changes

    def test_active_downweight_shrinks_threshold_and_avoid_half_size(self,
                                                                     db,
                                                                     monkeypatch):
        score = {"downweight_active": True, "downweight_would": False,
                 "hit_rate": 0.30, "hit_rate_threshold": 0.45,
                 "sample_size": 15}
        result, changes = self._adapt(
            db, monkeypatch, score,
            config={"enabled": True, "downweight_factor": 0.5})
        assert result["global"]["score_threshold"] == 75  # 70 + 10×0.5
        # avoid downgraded to half-size instead of disable
        assert result["strategies"]["trend"]["enabled"] is True
        assert result["strategies"]["trend"]["size_multiplier"] == 0.5
        assert any("downgraded" in c for c in changes)

    def test_log_only_keeps_original_behaviour(self, db, monkeypatch):
        score = {"downweight_active": False, "downweight_would": True,
                 "hit_rate": 0.30, "hit_rate_threshold": 0.45,
                 "sample_size": 15}
        result, changes = self._adapt(db, monkeypatch, score, config=None)
        assert result["global"]["score_threshold"] == 80  # 70 + 10 full
        assert result["strategies"]["trend"]["enabled"] is False  # killed
        assert any("WOULD" in c for c in changes)

    def test_healthy_hmm_no_downweight_line(self, db, monkeypatch):
        score = {"downweight_active": False, "downweight_would": False,
                 "hit_rate": 0.60, "hit_rate_threshold": 0.45,
                 "sample_size": 15}
        result, changes = self._adapt(db, monkeypatch, score, config=None)
        assert result["global"]["score_threshold"] == 80
        assert not any("downweight" in c.lower() for c in changes)


# ── record_entry hmm_regime passthrough ─────────────────────────────────

class TestRecorderField:
    def test_record_entry_persists_hmm_regime(self, db, tmp_path):
        from src.trade_outcome_recorder import TradeOutcomeRecorder

        rec = TradeOutcomeRecorder(db=db)
        row_id = rec.record_entry(
            symbol="BTCUSDT", entry_price=100.0, qty=0.5, score=85,
            strategy="trend", regime="GREED", fng_score=60, fng_label="Greed",
            btc_trend="NEUTRAL", hmm_regime="BEAR_TREND")
        row = db._get_conn().execute(
            "SELECT context_json FROM trade_outcomes WHERE id=?",
            (row_id,)).fetchone()
        ctx = json.loads(row["context_json"])
        assert ctx["hmm_regime"] == "BEAR_TREND"

    def test_record_entry_backward_compatible_no_hmm(self, db):
        from src.trade_outcome_recorder import TradeOutcomeRecorder

        rec = TradeOutcomeRecorder(db=db)
        row_id = rec.record_entry(
            symbol="BTCUSDT", entry_price=100.0, qty=0.5, score=85,
            strategy="trend", regime="GREED", fng_score=60, fng_label="Greed",
            btc_trend="NEUTRAL")
        row = db._get_conn().execute(
            "SELECT context_json FROM trade_outcomes WHERE id=?",
            (row_id,)).fetchone()
        ctx = json.loads(row["context_json"])
        assert ctx["hmm_regime"] == ""   # default, old callers unaffected
