"""bug#39 (2026-08-31): self-heal pipeline skeleton — 异常分类器 / 工单生成 /
fail-safe safe_mode 开关 / 显式解除。All file knobs are env-injectable; every
test runs against tmp paths only (bug#36 lesson: never touch production logs).
"""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from scripts import self_heal as sh


@pytest.fixture
def env(tmp_path, monkeypatch):
    d = tmp_path / "sh"
    d.mkdir()
    monkeypatch.setenv("CRON_FAILURES_FILE", str(d / "cron_failures.jsonl"))
    monkeypatch.setenv("SELF_HEAL_TICKETS_FILE", str(d / "tickets.jsonl"))
    monkeypatch.setenv("SAFE_MODE_FILE", str(d / "safe_mode.json"))
    monkeypatch.setenv("REPORT_VALIDATOR_FAILURES_FILE",
                       str(d / "validator_failures.jsonl"))
    return d


def _cron(env, rows):
    with open(env / "cron_failures.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# ── 分类器 ──
def test_classifier_categories():
    assert sh.classify({"type": "db_write_failure", "detail": "x"}) == \
        ("db_write_failure", "high")
    assert sh.classify({"job": "report_validator", "detail": "blocked notif"}) == \
        ("report_block", "high")
    assert sh.classify({"detail": "cash balance mismatch: 1 vs 2"}) == \
        ("balance_mismatch", "critical")
    assert sh.classify({"detail": "ConnectionError: proxy unreachable"}) == \
        ("network_proxy", "medium")
    assert sh.classify({"exit_code": 1, "detail": "step died"}) == \
        ("cron_exit", "medium")
    assert sh.classify({"detail": "something odd"}) == ("unknown", "low")


# ── 扫描 → 工单 → safe_mode ──
def test_threshold_trips_ticket_and_safe_mode(env, capsys):
    rows = [{"timestamp": "2026-08-31T15:09:00", "job": "report_validator",
             "exit_code": 1, "detail": "blocked notif n1: position-claim-mismatch"}
            for _ in range(3)]
    _cron(env, rows)
    assert sh.scan(window_s=86400, threshold=3) == 0
    assert sh.is_safe_mode() is not None
    tickets = [json.loads(l) for l in
               open(env / "tickets.jsonl", encoding="utf-8")]
    opened = [t for t in tickets if t.get("kind") == "ticket"]
    assert len(opened) == 1 and opened[0]["category"] == "report_block"
    audits = [t for t in tickets if t.get("event") == "safe_mode_on"]
    assert len(audits) == 1


def test_rescan_no_duplicate_tickets(env):
    _cron(env, [{"timestamp": "2026-08-31T15:09:00", "job": "report_validator",
                 "exit_code": 1, "detail": "blocked notif"}] * 5)
    sh.scan(window_s=86400, threshold=3)
    sh.scan(window_s=86400, threshold=3)
    opened = [json.loads(l) for l in open(env / "tickets.jsonl", encoding="utf-8")
              if json.loads(l).get("kind") == "ticket"]
    assert len(opened) == 1  # same category ⇒ one open ticket only


def test_critical_single_hit_trips(env):
    _cron(env, [{"timestamp": "2026-08-31T15:09:00",
                 "detail": "balance mismatch: DB 398.0 vs exchange 399.26"}])
    sh.scan(window_s=86400, threshold=3)
    assert sh.is_safe_mode() is not None


def test_out_of_window_records_ignored(env):
    _cron(env, [{"timestamp": "2026-08-01T15:09:00", "exit_code": 1,
                 "detail": "old cron exit"}] * 10)
    sh.scan(window_s=86400, threshold=3)
    assert sh.is_safe_mode() is None


def test_unparsable_lines_do_not_crash(env):
    (env / "cron_failures.jsonl").write_text("not-json\n{\"broken\": \n")
    assert sh.scan(window_s=86400, threshold=3) == 0  # unparsable→unknown, 2<thr
    assert sh.is_safe_mode() is None


# ── 显式解除 ──
def test_lift_requires_reason(env):
    _cron(env, [{"timestamp": "2026-08-31T15:09:00", "exit_code": 1,
                 "detail": "x"}] * 3)
    sh.scan(window_s=86400, threshold=3)
    ok, _ = sh.lift_safe_mode("")
    assert ok is False and sh.is_safe_mode() is not None  # stays engaged


def test_lift_with_reason_audited(env):
    _cron(env, [{"timestamp": "2026-08-31T15:09:00", "exit_code": 1,
                 "detail": "x"}] * 3)
    sh.scan(window_s=86400, threshold=3)
    ok, _ = sh.lift_safe_mode("人工核对：balance 实际一致，误报")
    assert ok is True and sh.is_safe_mode() is None
    audits = [json.loads(l) for l in open(env / "tickets.jsonl", encoding="utf-8")]
    lift = [a for a in audits if a.get("event") == "safe_mode_lifted"]
    assert len(lift) == 1 and "人工核对" in lift[0]["reason"]
    assert lift[0]["previous"]["enabled"] is True


def test_lift_when_not_engaged(env):
    ok, msg = sh.lift_safe_mode("no-op")
    assert ok is True and "未启用" in msg


# ── fail-safe ──
def test_corrupt_safe_mode_file_reads_as_engaged(env):
    (env / "safe_mode.json").write_text("{corrupt json")
    d = sh.is_safe_mode()
    assert d is not None and d.get("enabled") is True


def test_scan_internal_error_never_touches_switch(env, monkeypatch, capsys):
    _cron(env, [{"timestamp": "2026-08-31T15:09:00", "exit_code": 1,
                 "detail": "x"}] * 3)
    sh.scan(window_s=86400, threshold=3)  # engage
    before = (env / "safe_mode.json").read_text()
    monkeypatch.setattr(sh, "_read_jsonl",
                        lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    rc = sh.main(["--scan-nothing"])  # falls through to scan() raising
    # main() catches: exit code 2, switch untouched
    assert rc == 2
    assert (env / "safe_mode.json").read_text() == before
