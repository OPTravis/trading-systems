"""DynamicGate grace-period tests (2026-08-21).

00:26 calendar fire was skipped: elapsed=59m59s < interval=1h — measured
from last gate pass (saved at scan start). Effective cadence collapsed to
every other hour, defeating the hourly bull-window schedule. Grace=10min
lets boundary fires through while still gating genuine over-firing.
"""
import importlib.util
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "scan_gate", Path(__file__).parent.parent / "scripts" / "scan_gate.py"
)
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)


def _run(fng, elapsed_hours):
    """Call gate.main() with mocked F&G and last-scan timestamp."""
    last_ts = time.time() - elapsed_hours * 3600
    with patch.object(gate, "get_fng", return_value=fng), \
         patch.object(gate, "get_last_scan_ts", return_value=last_ts), \
         patch.object(gate, "save_scan_ts") as mock_save:
        with pytest.raises(SystemExit) as exc:
            gate.main()
    return exc.value.code, mock_save.called


class TestGateGrace:
    def test_boundary_fire_59min_passes_with_grace(self):
        """The 00:26 incident: 0.99h elapsed, GREED interval 1h → RUN now."""
        code, saved = _run(fng=62, elapsed_hours=0.99)
        assert code == 0 and saved

    def test_exact_interval_passes(self):
        code, saved = _run(fng=62, elapsed_hours=1.0)
        assert code == 0 and saved

    def test_half_interval_still_skips(self):
        code, saved = _run(fng=62, elapsed_hours=0.5)
        assert code == 1 and not saved

    def test_extreme_greed_30min_interval_boundary(self):
        """interval=0.5h → threshold 0.333h; 0.34h elapsed passes."""
        code, saved = _run(fng=80, elapsed_hours=0.34)
        assert code == 0 and saved

    def test_extreme_greed_under_threshold_skips(self):
        code, saved = _run(fng=80, elapsed_hours=0.3)
        assert code == 1 and not saved

    def test_extreme_fear_4h_interval_with_grace(self):
        """interval=4h → threshold 3.833h."""
        code, saved = _run(fng=10, elapsed_hours=3.9)
        assert code == 0 and saved
        code, saved = _run(fng=10, elapsed_hours=3.5)
        assert code == 1 and not saved

    def test_no_last_scan_runs(self):
        with patch.object(gate, "get_fng", return_value=50), \
             patch.object(gate, "get_last_scan_ts", return_value=0), \
             patch.object(gate, "save_scan_ts"):
            with pytest.raises(SystemExit) as exc:
                gate.main()
        assert exc.value.code == 0
