"""WO-0924-x: entry frequency governor — three gates.

Incident shapes (9/24): NEAR/BCH/UNI swept in via FRESH_ENTRY_FALLBACK
under mild drawdown in 50 minutes; UNI re-entered 15h after its SL
exit. Gates: loss-exit cooldown (24h), daily cap (3), fallback under
reduced sizing.
"""
import time

from src import entry_governor as gov


def _db():
    from src.state_db import get_state_db
    return get_state_db()


class TestLossExitCooldown:
    def test_loss_exit_blocks_reentry(self):
        gov.note_loss_exit("UNIUSDT", -0.96)
        r = gov.check_entry("UNIUSDT")
        assert not r["ok"] and r["gate"] == "loss_exit_cooldown"
        assert "0.0h" in r["reason"] or "cooldown" in r["reason"]

    def test_win_exit_no_cooldown(self):
        gov.note_loss_exit("ZAMAWIN", +1.28)   # pnl >= 0 — no stamp
        assert gov.check_entry("ZAMAWIN")["ok"]

    def test_cooldown_expires(self):
        now = time.time()
        _db().kv_set("entry_cooldown:OLDSYM",
                     {"ts": now - 25 * 3600, "pnl": -1.0})
        assert gov.check_entry("OLDSYM", now=now)["ok"]


class TestDailyEntryCap:
    def test_cap_three_blocks_fourth(self):
        now = time.time()
        for i in range(gov.DAILY_ENTRY_CAP):
            gov.note_entry("CAPSYM", now=now)
        r = gov.check_entry("CAPSYM", now=now)
        assert not r["ok"] and r["gate"] == "daily_entry_cap"
        assert str(gov.DAILY_ENTRY_CAP) in r["reason"]

    def test_cap_rolls_over_next_day(self):
        now = time.time()
        for i in range(gov.DAILY_ENTRY_CAP):
            gov.note_entry("CAPSYM2", now=now)
        # next calendar day → fresh counter key
        assert gov.check_entry("CAPSYM2", now=now + 26 * 3600)["ok"]


class TestFallbackUnderDrawdown:
    def test_fallback_blocked_when_reduced(self):
        # research phase stamps the flag; executor gate sees mult < 1
        gov.note_fallback("NEARUSDT")
        r = gov.check_entry("NEARUSDT", size_mult=0.7)
        assert not r["ok"] and r["gate"] == "fallback_under_drawdown"

    def test_normal_signal_unaffected(self):
        gov.note_fallback("BCHUSDT")   # flag exists but mult == 1.0
        assert gov.check_entry("BCHUSDT", size_mult=1.0)["ok"]
        assert gov.check_entry("PLAIN")["ok"]            # no flag at all

    def test_fallback_flag_expires(self):
        now = time.time()
        _db().kv_set("entry_fallback:EXPSYM", {"ts": now - 31 * 60})
        assert gov.check_entry("EXPSYM", size_mult=0.7, now=now)["ok"]


class TestGateOrderAndFailOpen:
    def test_cooldown_wins_over_cap(self):
        now = time.time()
        gov.note_loss_exit("BOTHSYM", -1.0, now=now)
        for i in range(gov.DAILY_ENTRY_CAP):
            gov.note_entry("BOTHSYM", now=now)
        r = gov.check_entry("BOTHSYM", now=now)
        assert r["gate"] == "loss_exit_cooldown"

    def test_db_failure_fails_open(self, monkeypatch):
        def boom():
            raise RuntimeError("db down")
        monkeypatch.setattr(gov, "_db", boom)
        assert gov.check_entry("ANYSYM")["ok"]
