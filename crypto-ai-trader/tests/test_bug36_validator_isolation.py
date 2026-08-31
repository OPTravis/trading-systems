"""bug#36 (2026-08-31): test-suite isolation regression guard.

Earlier runs of test_bug34 leaked 54 validator tickets into the production
logs/report_validator_failures.jsonl and 6 lines into logs/cron_failures.jsonl
(15:09-15:19 window, all notif_id=='n1'): tests that built ReportValidator
without injecting shadow paths fell back to the module-level defaults.

Fix: tests/conftest.py gained an autouse fixture that repoints
report_validator.NOTIFICATIONS_FILE / FAILURES_FILE / CRON_FAILURES at a
per-test tmp shadow dir. These tests pin that guarantee in place.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.report_validator import ReportValidator


def test_default_constructed_validator_uses_shadow_paths():
    """Worst case — ZERO injected paths — must still land in the conftest
    shadow dir, never the production signals/logs files."""
    v = ReportValidator()
    assert "validator_shadow" in str(v._history_file)
    assert "validator_shadow" in str(v._failures_file)
    assert "validator_shadow" in str(v._cron_failures_file)


def test_blocked_validate_writes_shadow_only(tmp_path):
    """A blocking validate() must produce tickets under the shadow dir and
    must NOT grow any production-side file (conftest autouse guarantees the
    module-level defaults are repointed; here we assert the writes land)."""
    v = ReportValidator()
    verdict, reasons, _ = v.validate(
        {"id": "nX", "timestamp": "2026-08-31T10:00:00", "type": "scan_summary",
         "title": "", "body": "📈 BTCUSDT 持仓 0.001 开多", "pushed": False})
    assert verdict == "block"
    assert v._failures_file.exists() and "validator_shadow" in str(v._failures_file)
    assert v._cron_failures_file.exists() and "validator_shadow" in str(v._cron_failures_file)
