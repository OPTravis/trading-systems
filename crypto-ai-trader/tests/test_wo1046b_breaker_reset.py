"""WO-1046b: DrawdownBreaker tiered auto-reset state machine tests.

Spec (Leo 2026-10-12 20:48 ruling, Travis 20:49 work order):
 (1) earliest reset 48h: HWM dd narrowed < 6%
 (2) else 72h recheck: dd < 8% -> reset
 (3) else every 24h: reset once dd < 8%; no hard cap, deferral escalates
 (4) anti-churn: re-trip within 72h of ANY reset -> cooldown x2
 (5) manual fast-lane: reset(actor, reason) audited
 (6) trip instant high-priority outbox alert
Rollback: kv drawdown_breaker_cooldown {"enabled": false} -> legacy
instant re-arm (dd back under 10% unblocks immediately).
"""

import time

import pytest


@pytest.fixture
def db():
    from src.state_db import get_state_db
    d = get_state_db()
    yield d
    d.kv_set("drawdown_breaker_cooldown", None)
    d.kv_set("drawdown_breaker_cooldown_state", None)


@pytest.fixture(autouse=True)
def no_feishu(monkeypatch):
    """Capture Feishu sends instead of hitting the real notifier."""
    calls = []

    class FakeNotifier:
        def __init__(self, *a, **k):
            pass

        def send_text(self, msg):
            calls.append(msg)

    import src.notifier as nm
    monkeypatch.setattr(nm, "FeishuNotifier", FakeNotifier)
    return calls


@pytest.fixture
def breaker(db, tmp_path):
    from src.drawdown_breaker import DrawdownBreaker
    b = DrawdownBreaker.__new__(DrawdownBreaker)
    b.client = None
    b._state = {
        "high_watermark": 0.0,
        "current_drawdown_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "tripped_at": None,
        "tripped_count": 0,
        "reset_at": None,
        "history": [],
    }
    return b


def _tripped(b, hwm=100.0, hours_ago=0.0):
    """Put the breaker into a tripped state N hours ago."""
    b._state["high_watermark"] = hwm
    b._state["tripped_at"] = time.time() - hours_ago * 3600
    b._state["tripped_count"] = 1
    return b


H = 3600.0  # one hour in seconds


class TestTier1EarlyReset:
    """(1) earliest reset at 48h requires dd < 6%."""

    def test_tier1_reset_at_49h_dd5(self, breaker, db):
        _tripped(breaker, hours_ago=49)
        r = breaker.check_drawdown(95.0)  # dd = 5.0%
        assert r["tripped"] is False
        assert r["action"] == "RESET"
        assert "tier1_early" in r["reason"]
        cst = db.kv_get("drawdown_breaker_cooldown_state")
        assert cst["last_chain_reset_at"] is not None
        assert any(e["type"] == "auto_reset_tier1_early"
                   for e in cst["events"])

    def test_no_reset_before_48h_even_at_dd5(self, breaker, db):
        _tripped(breaker, hours_ago=47)
        r = breaker.check_drawdown(95.0)  # dd = 5.0%, but only 47h
        assert r["tripped"] is True
        assert "Tiered cooldown active" in r["reason"]

    def test_no_tier1_when_dd_between_6_and_8(self, breaker, db):
        _tripped(breaker, hours_ago=49)
        r = breaker.check_drawdown(93.0)  # dd = 7.0% — tier1 needs <6
        assert r["tripped"] is True


class TestTier2Recheck:
    """(2) 72h recheck: dd < 8% -> reset."""

    def test_tier2_reset_at_73h_dd7(self, breaker, db):
        _tripped(breaker, hours_ago=73)
        r = breaker.check_drawdown(93.0)  # dd = 7.0%
        assert r["tripped"] is False
        assert "tier2_recheck" in r["reason"]

    def test_no_reset_at_71h_even_dd7(self, breaker, db):
        _tripped(breaker, hours_ago=71)
        r = breaker.check_drawdown(93.0)
        assert r["tripped"] is True


class TestDeferralEscalation:
    """(3) dd still >= 8%: every 24h recheck defers + escalates."""

    def _escalated_rounds(self, db):
        cst = db.kv_get("drawdown_breaker_cooldown_state") or {}
        return int(cst.get("escalation_rounds", 0) or 0)

    def test_first_escalation_at_recheck_point(self, breaker, db, no_feishu):
        _tripped(breaker, hours_ago=73)
        r = breaker.check_drawdown(91.0)  # dd = 9.0% >= 8
        assert r["tripped"] is True
        assert self._escalated_rounds(db) == 1
        assert any("DEFERRAL" in m for m in no_feishu)

    def test_escalation_idempotent_within_interval(self, breaker, db):
        _tripped(breaker, hours_ago=73)
        breaker.check_drawdown(91.0)
        breaker.check_drawdown(91.0)  # same instant — no double fire
        assert self._escalated_rounds(db) == 1

    def test_next_escalation_after_24h(self, breaker, db):
        _tripped(breaker, hours_ago=97)  # 73h + 24h
        r = breaker.check_drawdown(91.0)
        assert r["tripped"] is True
        assert self._escalated_rounds(db) == 1  # first round fires now

    def test_no_escalation_before_recheck_point(self, breaker, db):
        _tripped(breaker, hours_ago=50)
        breaker.check_drawdown(91.0)  # dd 9% but only 50h elapsed
        assert self._escalated_rounds(db) == 0


class TestChainCrashMultiplier:
    """(4) re-trip within 72h of ANY reset -> multiplier x2."""

    def _trip_now(self, breaker, hwm=100.0):
        breaker._state["high_watermark"] = hwm
        breaker._state["tripped_at"] = None
        return breaker.check_drawdown(hwm * 0.89)  # dd = 11%

    def test_chain_crash_doubles(self, breaker, db):
        # tier1 auto-reset 10h ago (inside 72h chain window)
        _tripped(breaker, hours_ago=49)
        breaker.check_drawdown(95.0)  # auto reset -> last_chain_reset_at
        cst = db.kv_get("drawdown_breaker_cooldown_state")
        cst["last_chain_reset_at"] = time.time() - 10 * H
        db.kv_set("drawdown_breaker_cooldown_state", cst)

        r = self._trip_now(breaker)  # re-trip 10h after reset
        assert r["action"] == "TRIP"
        cst = db.kv_get("drawdown_breaker_cooldown_state")
        assert cst["cooldown_multiplier"] == 2
        assert any(e["type"] == "chain_crash" for e in cst["events"])

    def test_doubled_cooldown_extends_tier1_window(self, breaker, db):
        # multiplier=2 -> tier1 needs 96h and dd<6
        _tripped(breaker, hours_ago=49)
        cst = db.kv_get("drawdown_breaker_cooldown_state") or {}
        cst["cooldown_multiplier"] = 2
        db.kv_set("drawdown_breaker_cooldown_state", cst)
        r = breaker.check_drawdown(95.0)  # 49h < 96h
        assert r["tripped"] is True

    def test_doubled_cooldown_resets_at_97h(self, breaker, db):
        _tripped(breaker, hours_ago=97)
        cst = db.kv_get("drawdown_breaker_cooldown_state") or {}
        cst["cooldown_multiplier"] = 2
        db.kv_set("drawdown_breaker_cooldown_state", cst)
        r = breaker.check_drawdown(95.0)  # 97h >= 96h, dd 5% < 6
        assert r["tripped"] is False
        assert "tier1_early" in r["reason"]

    def test_retrip_outside_chain_window_restarts_multiplier(
            self, breaker, db):
        _tripped(breaker, hours_ago=49)
        breaker.check_drawdown(95.0)  # auto reset
        cst = db.kv_get("drawdown_breaker_cooldown_state")
        cst["last_chain_reset_at"] = time.time() - 100 * H  # > 72h ago
        db.kv_set("drawdown_breaker_cooldown_state", cst)

        r = self._trip_now(breaker)
        assert r["action"] == "TRIP"
        cst = db.kv_get("drawdown_breaker_cooldown_state")
        assert cst["cooldown_multiplier"] == 1


class TestManualFastLane:
    """(5) manual reset with actor/reason, audited, arms chain window."""

    def test_manual_reset_audited(self, breaker, db):
        _tripped(breaker, hours_ago=5)
        breaker.reset(actor="Travis", reason="normal-vol, market stabilised")
        assert breaker._state["tripped_at"] is None
        cst = db.kv_get("drawdown_breaker_cooldown_state")
        assert cst["last_chain_reset_at"] is not None
        assert cst["cooldown_multiplier"] == 1
        evt = [e for e in cst["events"] if e["type"] == "manual_reset"]
        assert evt and evt[0]["actor"] == "Travis"
        assert "normal-vol" in evt[0]["reason"]
        rows = db._get_conn().execute(
            "SELECT action FROM audit_log "
            "WHERE action='DRAWDOWN_BREAKER_MANUAL_RESET'").fetchall()
        assert rows, "manual reset must hit audit_log"

    def test_manual_reset_then_retrip_is_chain_crash(self, breaker, db):
        _tripped(breaker, hours_ago=49)
        breaker.check_drawdown(95.0)  # auto reset happens
        breaker.reset(actor="Travis", reason="fast-lane")  # manual again
        breaker._state["high_watermark"] = 100.0
        breaker._state["tripped_at"] = None
        r = breaker.check_drawdown(89.0)  # dd 11% re-trip immediately
        assert r["action"] == "TRIP"
        cst = db.kv_get("drawdown_breaker_cooldown_state")
        assert cst["cooldown_multiplier"] == 2


class TestTripAlert:
    """(6) trip instant high-priority outbox alert."""

    def test_trip_writes_outbox_and_kv_state(self, breaker, db, no_feishu):
        breaker._state["high_watermark"] = 100.0
        r = breaker.check_drawdown(89.0)  # dd 11% -> TRIP
        assert r["action"] == "TRIP"
        assert r["tripped"] is True
        rows = db._get_conn().execute(
            "SELECT msg_type, title FROM notification_outbox "
            "WHERE msg_type='drawdown_breaker_trip'").fetchall()
        assert rows, "trip must land in notification_outbox"
        assert any("high priority" in t for _, t in rows)
        assert no_feishu, "Feishu immediate text expected on trip"
        cst = db.kv_get("drawdown_breaker_cooldown_state")
        assert cst["cooldown_multiplier"] == 1

    def test_auto_reset_writes_outbox(self, breaker, db):
        _tripped(breaker, hours_ago=49)
        breaker.check_drawdown(95.0)  # tier1 auto reset
        rows = db._get_conn().execute(
            "SELECT msg_type FROM notification_outbox "
            "WHERE msg_type='drawdown_breaker_auto_reset'").fetchall()
        assert rows, "auto-reset must land in notification_outbox"


class TestNewHwmDuringCooldown:
    """A new high watermark while tripped still passes the cooldown
    (spec: 48h earliest point; manual fast-lane exists for genuine
    v-shaped recoveries)."""

    def test_new_hwm_before_48h_stays_tripped(self, breaker, db):
        _tripped(breaker, hours_ago=10)
        r = breaker.check_drawdown(120.0)  # new HWM (dd = 0)
        assert r["tripped"] is True
        assert "new high watermark recorded" in r["reason"].lower()
        assert breaker._state["high_watermark"] == 120.0

    def test_new_hwm_after_48h_auto_resets(self, breaker, db):
        _tripped(breaker, hours_ago=49)
        r = breaker.check_drawdown(120.0)  # dd = 0 < 6, elapsed >= 48h
        assert r["tripped"] is False
        assert "tier1_early" in r["reason"]


class TestKvRollbackToLegacy:
    """kv drawdown_breaker_cooldown {"enabled": false} -> legacy
    instant re-arm (pre-1046b behaviour: dd back under 10% unblocks)."""

    def test_disabled_dd_fallback_rearms_instantly(self, breaker, db):
        db.kv_set("drawdown_breaker_cooldown", {"enabled": False})
        _tripped(breaker, hours_ago=1)
        r = breaker.check_drawdown(95.0)  # dd 5% < 10% — legacy re-arm
        assert r["tripped"] is False
        assert breaker._state["tripped_at"] is not None  # DB flag stays

    def test_disabled_trip_still_blocks(self, breaker, db):
        db.kv_set("drawdown_breaker_cooldown", {"enabled": False})
        breaker._state["high_watermark"] = 100.0
        r = breaker.check_drawdown(89.0)  # dd 11% -> TRIP
        assert r["tripped"] is True

    def test_disabled_multiplier_stays_1(self, breaker, db):
        db.kv_set("drawdown_breaker_cooldown", {"enabled": False})
        breaker._state["high_watermark"] = 100.0
        breaker.check_drawdown(89.0)  # trip
        breaker.check_drawdown(95.0)  # legacy: dd < 10% -> re-arm
        breaker._state["tripped_at"] = None
        breaker.check_drawdown(89.0)  # re-trip right after
        cst = db.kv_get("drawdown_breaker_cooldown_state") or {}
        # no chain bookkeeping when disabled
        assert cst.get("cooldown_multiplier", 1) == 1


class TestKvOverrides:
    """kv params take precedence over yaml/defaults."""

    def test_kv_shorter_early_hours(self, breaker, db):
        db.kv_set("drawdown_breaker_cooldown",
                  {"early_reset_hours": 24, "early_reset_dd_pct": 6.0})
        _tripped(breaker, hours_ago=25)
        r = breaker.check_drawdown(95.0)  # dd 5%, elapsed 25h >= 24h
        assert r["tripped"] is False
        assert "tier1_early" in r["reason"]

    def test_kv_tighter_dd_threshold_blocks(self, breaker, db):
        db.kv_set("drawdown_breaker_cooldown",
                  {"early_reset_dd_pct": 3.0})
        _tripped(breaker, hours_ago=49)
        r = breaker.check_drawdown(95.0)  # dd 5% >= 3% -> no tier1
        assert r["tripped"] is True


class TestStatusView:
    def test_status_cooldown_summary(self, breaker, db):
        _tripped(breaker, hours_ago=3)
        s = breaker.get_status()
        assert s["tripped"] is True
        assert s["cooldown"]["enabled"] is True
        assert s["cooldown"]["multiplier"] == 1
        assert 2.9 <= s["cooldown"]["elapsed_h"] <= 3.1
        assert s["cooldown"]["early_reset_at_h"] == 48.0

    def test_status_multiplier_reflected(self, breaker, db):
        _tripped(breaker, hours_ago=49)
        cst = db.kv_get("drawdown_breaker_cooldown_state") or {}
        cst["cooldown_multiplier"] = 4
        db.kv_set("drawdown_breaker_cooldown_state", cst)
        s = breaker.get_status()
        assert s["cooldown"]["multiplier"] == 4
        assert s["cooldown"]["early_reset_at_h"] == 192.0  # 48h * 4
