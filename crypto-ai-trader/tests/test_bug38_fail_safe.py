"""bug#38 (2026-08-31): validator failure must fail SAFE (block), not fail open.

15:12 escape post-mortem: the validator raised inside push_notifications'
try/except; the handler printed a warning and shipped the queue anyway
(fail-open) — notif_20260831151303994385 went out unverified. Fix: a broken
validator now blocks ALL unpushed notifications by default; the only way
through is the explicit env switch ALLOW_UNVERIFIED_REPORT=1.
"""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from scripts import push_notifications as pn


def _mk_queue(tmp_path, n=2):
    notifs = [{"id": f"notif_{i}", "timestamp": "2026-08-31T15:13:03",
               "type": "scan_summary", "title": "",
               "body": f"BODY_{i} 挂单 3 单", "pushed": False} for i in range(n)]
    f = tmp_path / "pending_notifications.json"
    f.write_text(json.dumps(notifs, ensure_ascii=False))
    m = tmp_path / "messages.json"
    m.write_text(json.dumps([], ensure_ascii=False))
    return f, m


@pytest.fixture
def broken_validator(monkeypatch):
    def _boom(unpushed):
        raise RuntimeError("validator exploded")
    monkeypatch.setattr(pn, "_load_validate_all", lambda: _boom)


def test_validator_crash_blocks_by_default(monkeypatch, tmp_path, broken_validator, capsys):
    f, m = _mk_queue(tmp_path)
    monkeypatch.setattr(pn, "NOTIFICATIONS_FILE", f)
    monkeypatch.setattr(pn, "MESSAGES_FILE", m)
    monkeypatch.delenv("ALLOW_UNVERIFIED_REPORT", raising=False)
    pn.main()
    out = capsys.readouterr().out
    assert "FAIL-SAFE" in out
    assert "BODY_0" not in out  # nothing shipped
    # queue untouched: nothing marked pushed, no blocked marks either
    after = json.loads(f.read_text())
    assert all(not n.get("pushed") and not n.get("blocked") for n in after)


def test_validator_crash_allow_unverified_escape(monkeypatch, tmp_path, broken_validator, capsys):
    f, m = _mk_queue(tmp_path)
    monkeypatch.setattr(pn, "NOTIFICATIONS_FILE", f)
    monkeypatch.setattr(pn, "MESSAGES_FILE", m)
    monkeypatch.setenv("ALLOW_UNVERIFIED_REPORT", "1")
    pn.main()
    out = capsys.readouterr().out
    assert "ALLOW_UNVERIFIED_REPORT=1" in out and "BODY_0" in out
    after = json.loads(f.read_text())
    assert all(n["pushed"] for n in after)


def test_unloadable_validator_also_blocks(monkeypatch, tmp_path, capsys):
    """Import failure of the validator module itself (bug#37 scenario on a
    broken install) must hit the same fail-safe, not ship the queue."""
    def _no_loader():
        raise RuntimeError("cannot import report_validator")
    monkeypatch.setattr(pn, "_load_validate_all", _no_loader)
    f, m = _mk_queue(tmp_path)
    monkeypatch.setattr(pn, "NOTIFICATIONS_FILE", f)
    monkeypatch.setattr(pn, "MESSAGES_FILE", m)
    monkeypatch.delenv("ALLOW_UNVERIFIED_REPORT", raising=False)
    pn.main()
    out = capsys.readouterr().out
    assert "FAIL-SAFE" in out and "BODY_0" not in out


def test_healthy_validator_still_passes(monkeypatch, tmp_path, capsys):
    f, m = _mk_queue(tmp_path)
    monkeypatch.setattr(pn, "NOTIFICATIONS_FILE", f)
    monkeypatch.setattr(pn, "MESSAGES_FILE", m)

    def _fake(unpushed):
        return list(unpushed), []

    monkeypatch.setattr(pn, "_load_validate_all", lambda: _fake)
    monkeypatch.delenv("ALLOW_UNVERIFIED_REPORT", raising=False)
    pn.main()
    out = capsys.readouterr().out
    assert "BODY_0" in out and "FAIL-SAFE" not in out
