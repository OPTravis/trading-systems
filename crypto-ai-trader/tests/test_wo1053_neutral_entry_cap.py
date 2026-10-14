"""WO-1053: NEUTRAL-regime fresh-entry rate cap (rolling 24h x 2).

Evidence base (10/13-14 stop-loss wave): five failed entries all sat in
the NEUTRAL market-regime bucket (F&G 57, 83% failure) while bull_regime
was MILD_BULL/CONFIRMED_BULL — so the gate binds on the F&G-bucket
market regime (executor-threaded, scan-chain kv snapshot fallback), NOT
bull_regime, which has not visited NEUTRAL since 9/21 and would have
blocked none of the wave.

Acceptance (WO-1053 ①): simulate the 3rd fresh entry of a NEUTRAL
period being blocked — TestNeutralCapCore.test_third_neutral_entry_blocked.
"""
import json
import time

from src import entry_governor as gov


def _db():
    from src.state_db import get_state_db
    return get_state_db()


def _outcome(symbol, entry_time, regime):
    db = _db()
    db.outcome_add_entry(
        symbol=symbol,
        entry_time=entry_time,
        entry_date=time.strftime("%Y-%m-%d", time.localtime(entry_time)),
        entry_price=1.0,
        qty=1.0,
        score=75.0,
        strategy="dca",
        factors_json="{}",
        context_json=json.dumps({"regime": regime}),
    )


class TestNeutralCapCore:
    def test_third_neutral_entry_blocked(self):
        """WO-1053 acceptance ①: NEUTRAL period, 3rd fresh entry blocked."""
        now = time.time()
        gov.note_entry("NEUTRALCAP1", now=now, market_regime="NEUTRAL")
        gov.note_entry("NEUTRALCAP2", now=now, market_regime="NEUTRAL")
        r = gov.check_entry("NEUTRALCAP3", now=now, market_regime="NEUTRAL")
        assert not r["ok"] and r["gate"] == "neutral_entry_cap"
        assert "neutral fresh-entry cap 2" in r["reason"]
        assert "2 NEUTRAL entries" in r["reason"]

    def test_first_two_neutral_entries_pass(self):
        now = time.time()
        gov.note_entry("NCPASS1", now=now, market_regime="NEUTRAL")
        r = gov.check_entry("NCPASS2", now=now, market_regime="NEUTRAL")
        assert r["ok"], r

    def test_greed_bucket_unaffected(self):
        """WO-1053 ③: BULL-side bucket stays on the global cap only."""
        now = time.time()
        gov.note_entry("GREED1", now=now, market_regime="NEUTRAL")
        gov.note_entry("GREED2", now=now, market_regime="NEUTRAL")
        r = gov.check_entry("GREED3", now=now, market_regime="GREED")
        assert r["ok"], r

    def test_rolling_window_expiry(self):
        """24h rolling window: entries older than the window free the cap."""
        now = time.time()
        _db().kv_set("gov:neutral_entries",
                     [now - 25 * 3600, now - 24.5 * 3600])
        r = gov.check_entry("EXPIRE1", now=now, market_regime="NEUTRAL")
        assert r["ok"], r

    def test_snapshot_fallback_case_insensitive(self):
        """No executor hint → scan-chain kv snapshot decides (case-agnostic)."""
        now = time.time()
        _db().kv_set("gov:market_regime",
                     {"regime": "neutral", "fng": 57, "ts": now})
        _db().kv_set("gov:neutral_entries", [now - 60, now - 30])
        r = gov.check_entry("SNAP1", now=now)
        assert not r["ok"] and r["gate"] == "neutral_entry_cap"

    def test_regime_unknown_skips_gate(self):
        """No hint + no snapshot → gate skipped (governor fail-open)."""
        now = time.time()
        _db().kv_set("gov:neutral_entries", [now - 60, now - 30])
        r = gov.check_entry("UNKNOWN1", now=now)
        assert r["ok"], r


class TestBackstop:
    """Cold-start floor: rebuild the window from trade_outcomes' recorded
    entry regimes whenever the kv tag list is empty (redeploy scenario)."""

    def test_outcomes_backstop_blocks(self):
        now = time.time()
        _outcome("BS1", now - 400, "NEUTRAL")
        _outcome("BS2", now - 300, "NEUTRAL")
        r = gov.check_entry("BS3", now=now, market_regime="NEUTRAL")
        assert not r["ok"] and r["gate"] == "neutral_entry_cap"

    def test_outcomes_backstop_ignores_other_regimes(self):
        now = time.time()
        _outcome("BSG1", now - 400, "GREED")
        _outcome("BSG2", now - 300, "FEAR")
        r = gov.check_entry("BSG3", now=now, market_regime="NEUTRAL")
        assert r["ok"], r

    def test_outcomes_backstop_window(self):
        now = time.time()
        _outcome("BSW1", now - 25 * 3600, "NEUTRAL")
        _outcome("BSW2", now - 26 * 3600, "NEUTRAL")
        r = gov.check_entry("BSW3", now=now, market_regime="NEUTRAL")
        assert r["ok"], r

    def test_kv_and_backstop_take_max(self):
        """kv tag says 1, outcomes rebuild says 2 — the stricter wins."""
        now = time.time()
        gov.note_entry("MAXTAG", now=now, market_regime="NEUTRAL")
        _outcome("MAXBS1", now - 200, "NEUTRAL")
        _outcome("MAXBS2", now - 100, "NEUTRAL")
        r = gov.check_entry("MAXNEW", now=now, market_regime="NEUTRAL")
        assert not r["ok"] and r["gate"] == "neutral_entry_cap"


class TestNoteEntryTagging:
    def test_note_tags_neutral(self):
        now = time.time()
        gov.note_entry("TAG1", now=now, market_regime="NEUTRAL")
        tags = _db().kv_get("gov:neutral_entries")
        assert tags and abs(float(tags[-1]) - now) < 1.0

    def test_note_skips_greed(self):
        now = time.time()
        gov.note_entry("TAG2", now=now, market_regime="GREED")
        assert not _db().kv_get("gov:neutral_entries")

    def test_note_uses_snapshot_when_no_hint(self):
        now = time.time()
        _db().kv_set("gov:market_regime",
                     {"regime": "NEUTRAL", "fng": 57, "ts": now})
        gov.note_entry("TAG3", now=now)
        assert _db().kv_get("gov:neutral_entries")

    def test_note_window_prune(self):
        now = time.time()
        _db().kv_set("gov:neutral_entries", [now - 30 * 3600, now - 29 * 3600])
        gov.note_entry("TAG4", now=now, market_regime="NEUTRAL")
        tags = [float(x) for x in _db().kv_get("gov:neutral_entries")]
        assert len(tags) == 1  # stale tags pruned, fresh one appended


class TestGateOrderAndFailOpen:
    def test_cooldown_wins_over_neutral_cap(self):
        """Existing gate order preserved: cooldown fires before the cap."""
        now = time.time()
        gov.note_loss_exit("ORDSYM", -1.0, now=now)
        _db().kv_set("gov:neutral_entries", [now - 60, now - 30])
        r = gov.check_entry("ORDSYM", now=now, market_regime="NEUTRAL")
        assert r["gate"] == "loss_exit_cooldown"

    def test_db_failure_fails_open(self, monkeypatch):
        def _boom():
            raise RuntimeError("db down")
        monkeypatch.setattr(gov, "_db", _boom)
        r = gov.check_entry("FAILOPEN", market_regime="NEUTRAL")
        assert r["ok"]  # governor must never break trading

    def test_global_cap_still_binds_in_greed(self):
        """Global 3/day cap unchanged outside NEUTRAL (stacking: stricter
        binds — in NEUTRAL the 2/24h cap is the stricter one)."""
        now = time.time()
        for i in range(gov.DAILY_ENTRY_CAP):
            gov.note_entry("GLOBALC", now=now, market_regime="GREED")
        r = gov.check_entry("GLOBALC4", now=now, market_regime="GREED")
        assert not r["ok"] and r["gate"] == "daily_entry_cap"
