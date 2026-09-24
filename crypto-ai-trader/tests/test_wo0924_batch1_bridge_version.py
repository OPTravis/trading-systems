"""WO-0924 batch 1A-3: bridge/version fields on all persisted & emitted JSON.

Contract: every JSON artifact that crosses a process boundary (bridge
latest.json, event_trigger state file, main.py stdout protocol) carries an
explicit integer version field so future schema changes are detectable.
Old readers ignore unknown keys; old writers (no version) stay loadable.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestEventTriggerStateVersion:
    def test_save_writes_version(self, tmp_path):
        from src.event_trigger import EventTriggerEngine
        f = str(tmp_path / "evt_state.json")
        eng = EventTriggerEngine(state_file=f)
        eng.save()
        data = json.load(open(f))
        assert data.get("version") == 1
        # existing keys unchanged
        for k in ("price_history", "last_nonregime_trigger_ts",
                  "last_regime", "last_trade_ids"):
            assert k in data

    def test_load_legacy_without_version_still_works(self, tmp_path):
        from src.event_trigger import EventTriggerEngine
        f = str(tmp_path / "evt_state.json")
        with open(f, "w") as fh:
            json.dump({"price_history": {}, "last_nonregime_trigger_ts": 0.0,
                       "last_regime": None, "last_trade_ids": {}}, fh)
        eng = EventTriggerEngine(state_file=f)
        assert eng.last_regime is None  # loaded, not crashed


class TestBridgeJsonSourceInvariants:
    """Static source checks: every bridge/protocol JSON emits a version."""

    def test_reside_scan_verdict_has_version(self):
        src = open(os.path.join(REPO, "scripts", "reside_scan.py")).read()
        assert '"version": 1' in src
        assert src.index('"version": 1') < src.index('"host": "cloud-travis-resident"')

    def test_main_dust_json_has_version(self):
        src = open(os.path.join(REPO, "main.py")).read()
        n = src.count('print(_json.dumps({')
        assert n == 3
        # every dumped payload includes version
        assert src.count('"version": 1') >= 3
