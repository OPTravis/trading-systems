"""bug#37 (2026-08-31): validator import must be CWD-independent.

The interceptor used `from scripts.report_validator import validate_all`
inside a function, which only resolves when CWD == repo root. A cron shell
launched from src/ (or anywhere else) hit ModuleNotFoundError — silently
disabling the entire pre-report consistency gate (compounding the 15:12
fail-open escape). Fix: push_notifications._load_validate_all() derives the
repo root from __file__ and retries; these subprocess probes pin the
behaviour under the three CWDs a cron could realistically use.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PROBE = (
    "import sys; sys.path.insert(0, {scripts!r}); "
    "from push_notifications import _load_validate_all as L; "
    "print('OK' if callable(L()) else 'BAD')"
).format(scripts=str(REPO / "scripts"))


def _probe(cwd: str):
    r = subprocess.run([sys.executable, "-c", PROBE],
                       cwd=cwd, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"probe failed in cwd={cwd}:\n{r.stderr}"
    assert "OK" in r.stdout, f"validator not loadable from cwd={cwd}: {r.stdout}"


def test_import_from_repo_root():
    _probe(str(REPO))


def test_import_from_src_cwd():
    # the reported failure: CWD=src/ — no scripts/ package reachable from here
    _probe(str(REPO / "src"))


def test_import_from_unrelated_cwd(tmp_path):
    # arbitrary directory: only __file__-derived root can save the import
    _probe(str(tmp_path))
