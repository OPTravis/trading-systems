"""WO-1005: dry-run scan chain must own its gate clock.

The dry-run reside_scan wrapper had its own gate file, but the inner
`run_cron.sh cron-scan` -> scan_gate.py read the LIVE last-scan
timestamp, so every dry-run round was collaterally throttled by the
live 1h cadence and never ran a full scan.
"""
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
PY = sys.executable


def test_gate_file_splits_under_dryrun():
    """DRYRUN=1 -> twin file; live path string untouched elsewhere."""
    code = (
        "import os; os.environ['DRYRUN']='1'\n"
        "import sys; sys.path.insert(0, '.')\n"
        "import scripts.scan_gate as sg\n"
        "assert sg.LAST_SCAN_FILE == 'data/last_scan_ts_dryrun.json', "
        "sg.LAST_SCAN_FILE\nprint('OK')\n"
    )
    r = subprocess.run([PY, "-c", code], cwd=REPO, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_live_gate_path_unchanged_without_dryrun():
    """No DRYRUN env -> exact legacy path (live cadence zero-change lock)."""
    code = (
        "import sys; sys.path.insert(0, '.')\n"
        "import scripts.scan_gate as sg\n"
        "assert sg.LAST_SCAN_FILE == 'data/last_scan_ts.json', "
        "sg.LAST_SCAN_FILE\nprint('OK')\n"
    )
    env = {k: v for k, v in __import__('os').environ.items()
           if k != "DRYRUN"}
    r = subprocess.run([PY, "-c", code], cwd=REPO, capture_output=True,
                       text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_save_then_read_roundtrip(tmp_path, monkeypatch):
    """save_scan_ts/get_last_scan_ts honour whatever file the module
    resolved — write/read roundtrip on an isolated twin."""
    import scripts.scan_gate as sg
    twin = tmp_path / "last_scan_ts_dryrun.json"
    monkeypatch.setattr(sg, "LAST_SCAN_FILE", str(twin))
    assert sg.get_last_scan_ts() == 0            # missing -> 0 -> gate opens
    sg.save_scan_ts()
    saved = sg.get_last_scan_ts()
    assert abs(saved - time.time()) < 10


def test_missing_dryrun_twin_opens_gate(tmp_path, monkeypatch):
    """Fresh dry-run twin (no live history) must read elapsed=999,
    i.e. the first dry-run round always runs — never collaterally
    throttled by a file it never wrote."""
    import scripts.scan_gate as sg
    monkeypatch.setattr(sg, "LAST_SCAN_FILE",
                        str(tmp_path / "never_written.json"))
    last = sg.get_last_scan_ts()
    elapsed = (time.time() - last) / 3600 if last else 999
    assert elapsed >= sg.GATE_MIN_INTERVAL_HOURS
