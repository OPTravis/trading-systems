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

    def test_extreme_greed_locked_to_1h_under_old_30min_boundary_skips(self):
        """2026-09-17 lock (weekly #6 / Leo 8/28 ruling): EXTREME_GREED no
        longer speeds up to 0.5h — 0.34h elapsed used to RUN, now SKIPs."""
        code, saved = _run(fng=80, elapsed_hours=0.34)
        assert code == 1 and not saved

    def test_extreme_greed_1h_boundary_passes(self):
        """Locked to 1h → same threshold math as GREED/NEUTRAL."""
        code, saved = _run(fng=80, elapsed_hours=0.99)
        assert code == 0 and saved
        code, saved = _run(fng=80, elapsed_hours=0.5)
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


class TestIntervalLock:
    """2026-09-17: DynamicGate EXTREME_GREED cadence locked to 1h.

    Weekly review #6 flagged the adaptive 0.5h speedup in F&G>=75 as a
    conflict with Leo's 8/28 ruling ("DynamicGate stays hourly; crypto
    cadence changes need Leo's say-so"). The lock: FREQ_MAP entry set to
    1.0 AND a GATE_MIN_INTERVAL_HOURS floor clamping whatever the table
    resolves to. Slower waste-prevention regimes are untouched.
    """

    def test_extreme_greed_map_entry_is_1h(self):
        assert gate.FREQ_MAP[(75, 101)] == (1.0, "EXTREME_GREED")

    def test_floor_constant_is_1h(self):
        assert gate.GATE_MIN_INTERVAL_HOURS == 1.0

    def test_every_map_interval_respects_the_1h_floor(self):
        for (lo, hi), (hrs, _lbl) in gate.FREQ_MAP.items():
            assert hrs >= gate.GATE_MIN_INTERVAL_HOURS, (lo, hi, hrs)

    def test_boundary_fng_75_locked(self):
        """F&G=75 falls in the (75, 101) bucket — locked, not 0.5h."""
        code, saved = _run(fng=75, elapsed_hours=0.34)
        assert code == 1 and not saved
        code, saved = _run(fng=75, elapsed_hours=0.99)
        assert code == 0 and saved

    def test_extreme_greed_behaves_like_greed_cadence(self):
        """Same elapsed, same verdict across GREED vs EXTREME_GREED."""
        assert _run(fng=62, elapsed_hours=0.6) == _run(fng=80, elapsed_hours=0.6)

    def test_slower_regimes_unaffected(self):
        """Waste-prevention downshifts survive the lock: FEAR 2h, EXTREME_FEAR 4h."""
        code, _ = _run(fng=35, elapsed_hours=1.5)   # FEAR 2h → skip at 1.5h
        assert code == 1
        code, _ = _run(fng=10, elapsed_hours=3.5)   # EXTREME_FEAR 4h → skip at 3.5h
        assert code == 1
