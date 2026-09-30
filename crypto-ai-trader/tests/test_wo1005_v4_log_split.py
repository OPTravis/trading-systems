"""WO-1005 addendum-4: cron-scan.log twin split (the 5th shared artefact).

Travis 19:15 followup — DRYRUN=1 wrapper rounds must write
logs/cron-scan_dryrun.log (live cron-scan.log must never carry dry-run
feature lines), and the resident outer loop must tail the matching twin
when building the latest.json report body. Structural locks on
run_cron.sh also pin the three live-artefact bypasses found during the
codebase sweep: failure jsonl, notification-queue drain and live autobak.
"""
import os
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PY = sys.executable


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("DRYRUN", "STATE_DB_PATH", "TESTING")}
    env.update(extra)
    return env


def _read(p):
    with open(os.path.join(REPO, p), errors="replace") as f:
        return f.read()


def test_runcron_logfile_twin_structural_lock():
    """DRYRUN rounds must redirect LOGFILE to <cmd>_dryrun.log."""
    s = _read("run_cron.sh")
    assert 'LOGFILE="$LOGDIR/${CMD}_dryrun.log"' in s
    assert 'if [ "${DRYRUN:-0}" = "1" ]; then' in s


def test_runcron_live_artefact_guards_structural_lock():
    """Failure jsonl, notification drain and live autobak are all
    skipped on DRYRUN rounds (structural lock on the guard clauses)."""
    s = _read("run_cron.sh")
    assert 'if [ $EXIT_CODE -ne 0 ] && [ "${DRYRUN:-0}" != "1" ]; then' in s
    assert ('[ "$CMD" = "cron-scan" ] && [ $EXIT_CODE -eq 0 ] '
            '&& [ "${DRYRUN:-0}" != "1" ]; then') in s
    assert ('if [ "${DRYRUN:-0}" != "1" ] && [ -n "$STATE_DB_PATH" ] '
            '&& [ -f "$STATE_DB_PATH" ]; then') in s


def test_reside_scan_paths_split_by_dryrun():
    """Outer loop: own run log + report-body log source both twin under
    DRYRUN=1, both live without it (import-time constants)."""
    code = ("from scripts.reside_scan import LOG, _scan_log_path; "
            "print(LOG); print(_scan_log_path())")
    r = subprocess.run([PY, "-c", code], cwd=REPO, capture_output=True,
                       text=True, timeout=60, env=_clean_env(DRYRUN="1"))
    assert r.returncode == 0, r.stderr
    twin_log, twin_scan = r.stdout.strip().splitlines()
    assert twin_log.endswith("reside_scan_dryrun.log"), twin_log
    assert twin_scan.endswith("cron-scan_dryrun.log"), twin_scan

    r2 = subprocess.run([PY, "-c", code], cwd=REPO, capture_output=True,
                        text=True, timeout=60, env=_clean_env())
    assert r2.returncode == 0, r2.stderr
    live_log, live_scan = r2.stdout.strip().splitlines()
    assert live_log.endswith("reside_scan.log"), live_log
    assert "dryrun" not in live_log
    assert live_scan.endswith("cron-scan.log"), live_scan
    assert "dryrun" not in live_scan


def test_reside_scan_report_reads_twin_not_live(tmp_path):
    """Behavioural lock: under DRYRUN the report body is tailed from the
    twin log even when the live log holds richer content — a dry-run
    verdict can never be built from live bytes."""
    live = tmp_path / "logs" / "cron-scan.log"
    twin = tmp_path / "logs" / "cron-scan_dryrun.log"
    live.parent.mkdir(parents=True)
    live.write_text("LIVE ROUND — BUY LIVEUSDT @ $1 — LIVE MARKER\n" * 5)
    twin.write_text("DRYRUN ROUND — NO_OPPORTUNITIES (paper sim)\n" * 3)
    code = (
        "import scripts.reside_scan as rs\n"
        "rs.REPO = %r\n"
        "p = rs._scan_log_path()\n"
        "body = open(p, errors='replace').read()[-6000:]\n"
        "print(p.rsplit('/', 1)[-1])\n"
        "print('LIVE MARKER' in body)\n"
        "print('NO_OPPORTUNITIES' in body)\n") % str(tmp_path)
    r = subprocess.run([PY, "-c", code], cwd=REPO, capture_output=True,
                       text=True, timeout=60, env=_clean_env(DRYRUN="1"))
    assert r.returncode == 0, r.stderr
    name, has_live, has_twin = r.stdout.strip().splitlines()
    assert name == "cron-scan_dryrun.log", name
    assert has_live == "False", "live bytes leaked into a dry-run report"
    assert has_twin == "True"
