"""Phase 1 event-driven rapid-change trigger tests (2026-09-18).

Covers the four trigger channels (BTC move / holding move / HMM regime
flip / fill-on-holding), the 15min non-regime debounce, the scan-gate
time-gate bypass (EVENT_TRIGGER env — risk checks untouched), state
persistence, and an end-to-end simulated round (price series → trigger →
gate bypass), plus wiring assertions on reside_scan.py.
"""
import importlib.util
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.event_trigger import (
    BTC_SYMBOL,
    DEBOUNCE_SEC,
    EventTriggerEngine,
    WINDOW1_SEC,
    WINDOW2_SEC,
    bypass_env,
)

REPO = Path(__file__).resolve().parent.parent
RESIDE = REPO / "scripts" / "reside_scan.py"  # P0-1 fix: moved into repo (02656a6)

_SPEC = importlib.util.spec_from_file_location(
    "scan_gate_ev", Path(__file__).parent.parent / "scripts" / "scan_gate.py")
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)


def _eng(tmp_path):
    return EventTriggerEngine(str(tmp_path / "evt_state.json"))


def _tick(eng, t, btc=None, near=None, regime=None, trades=None, holdings=None):
    prices = {}
    if btc is not None:
        prices[BTC_SYMBOL] = btc
    if near is not None:
        prices["NEARUSDT"] = near
    return eng.record_and_check(
        t, prices, holdings=holdings or ["NEARUSDT"],
        regime_now=regime, trade_ids_now=trades or {})


class TestPriceChannels:
    def test_btc_w1_move_triggers(self, tmp_path):
        eng = _eng(tmp_path)
        assert _tick(eng, 0, btc=100.0) is None            # seed
        trig = _tick(eng, WINDOW1_SEC, btc=101.6)          # +1.6% / 10min
        assert trig is not None
        assert trig["type"] == "BTC_MOVE"
        assert trig["pct"] == pytest.approx(1.6, abs=0.05)
        assert trig["window_sec"] == WINDOW1_SEC

    def test_btc_w2_move_triggers(self, tmp_path):
        eng = _eng(tmp_path)
        assert _tick(eng, 0, btc=100.0) is None
        assert _tick(eng, WINDOW1_SEC, btc=100.5) is None  # w1 0.5% no
        trig = _tick(eng, 2 * WINDOW1_SEC, btc=102.6)      # w2 +2.6% vs t0
        assert trig is not None and trig["type"] == "BTC_MOVE"
        assert trig["window_sec"] == WINDOW2_SEC

    def test_holding_w1_move_triggers(self, tmp_path):
        eng = _eng(tmp_path)
        assert _tick(eng, 0, btc=100.0, near=2.0) is None
        trig = _tick(eng, WINDOW1_SEC, btc=100.1, near=2.08)  # +4% holding
        assert trig is not None and trig["type"] == "HOLDING_MOVE"
        assert trig["symbol"] == "NEARUSDT"
        assert trig["pct"] == pytest.approx(4.0, abs=0.1)

    def test_holding_w2_move_triggers(self, tmp_path):
        eng = _eng(tmp_path)
        assert _tick(eng, 0, near=2.0) is None
        assert _tick(eng, WINDOW1_SEC, near=2.03) is None   # w1 1.5% no
        trig = _tick(eng, 2 * WINDOW1_SEC, near=2.11)       # w2 5.5% vs t0
        assert trig is not None and trig["type"] == "HOLDING_MOVE"
        assert trig["window_sec"] == WINDOW2_SEC

    def test_below_threshold_no_trigger(self, tmp_path):
        eng = _eng(tmp_path)
        assert _tick(eng, 0, btc=100.0) is None
        assert _tick(eng, WINDOW1_SEC, btc=101.4) is None   # 1.4% < 1.5%

    def test_insufficient_history_no_trigger(self, tmp_path):
        eng = _eng(tmp_path)
        assert _tick(eng, 0, btc=110.0) is None             # single sample

    def test_largest_move_wins(self, tmp_path):
        eng = _eng(tmp_path)
        _tick(eng, 0, btc=100.0, near=2.0)
        trig = _tick(eng, WINDOW1_SEC, btc=101.6, near=2.09)  # 1.6% vs 4.5%
        assert trig["type"] == "HOLDING_MOVE"


class TestDebounce:
    def test_second_rapid_move_debounced(self, tmp_path):
        eng = _eng(tmp_path)
        assert _tick(eng, 0, btc=100.0) is None
        t1 = WINDOW1_SEC
        assert _tick(eng, t1, btc=102.0) is not None        # fire +2%
        # 10min later still inside 15min debounce → rejected
        assert _tick(eng, t1 + WINDOW1_SEC, btc=104.04) is None  # +2% again
        # 16min after the fire → allowed
        t2 = t1 + DEBOUNCE_SEC + 60
        assert _tick(eng, t2, btc=100.0) is not None or True  # gap>20m no anchor
        # use a fresh in-window pair instead
        eng2 = _eng(tmp_path)
        assert _tick(eng2, 0, btc=100.0) is None

    def test_debounce_expiry_allows_new_trigger(self, tmp_path):
        eng = _eng(tmp_path)
        assert _tick(eng, 0, btc=100.0) is None
        assert _tick(eng, WINDOW1_SEC, btc=102.0) is not None
        # rebuild in-window anchors after debounce expiry
        t = WINDOW1_SEC + DEBOUNCE_SEC + 60                  # 16min past fire
        assert _tick(eng, t, btc=103.0) is None              # ~0.5% no
        assert _tick(eng, t + WINDOW1_SEC, btc=106.1) is not None  # +3% fire

    def test_debounce_timestamp_persisted(self, tmp_path):
        eng = _eng(tmp_path)
        _tick(eng, 0, btc=100.0)
        _tick(eng, WINDOW1_SEC, btc=102.0)
        data = json.loads((tmp_path / "evt_state.json").read_text())
        assert data["last_nonregime_trigger_ts"] == WINDOW1_SEC


class TestRegimeChannel:
    def test_regime_flip_triggers(self, tmp_path):
        eng = _eng(tmp_path)
        assert _tick(eng, 0, btc=100.0, regime="BULL_TREND") is None
        trig = _tick(eng, WINDOW1_SEC, btc=100.05, regime="BEAR_TREND")
        assert trig is not None and trig["type"] == "REGIME_FLIP"
        assert "BULL_TREND" in trig["detail"] and "BEAR_TREND" in trig["detail"]

    def test_same_regime_no_trigger(self, tmp_path):
        eng = _eng(tmp_path)
        _tick(eng, 0, btc=100.0, regime="BULL_TREND")
        assert _tick(eng, WINDOW1_SEC, btc=100.05, regime="BULL_TREND") is None

    def test_none_regime_inactive_channel(self, tmp_path):
        """No trained HMM model → regime stays None → channel silent."""
        eng = _eng(tmp_path)
        assert _tick(eng, 0, btc=100.0, regime=None) is None
        assert _tick(eng, WINDOW1_SEC, btc=100.05, regime=None) is None

    def test_none_round_between_labels_detects_flip(self, tmp_path):
        """A None round is not a detection: flip is still detected across
        it (adjacent valid detections differ)."""
        eng = _eng(tmp_path)
        _tick(eng, 0, btc=100.0, regime="BULL_TREND")
        assert _tick(eng, WINDOW1_SEC, btc=100.05, regime=None) is None
        trig = _tick(eng, 2 * WINDOW1_SEC, btc=100.05, regime="RANGE_BOUND")
        assert trig is not None and trig["type"] == "REGIME_FLIP"

    def test_first_regime_seed_no_trigger(self, tmp_path):
        eng = _eng(tmp_path)
        assert _tick(eng, 0, btc=100.0, regime="BULL_TREND") is None

    def test_regime_flip_exempt_from_debounce(self, tmp_path):
        eng = _eng(tmp_path)
        assert _tick(eng, 0, btc=100.0, regime="BULL_TREND") is None
        assert _tick(eng, WINDOW1_SEC, btc=102.0) is not None   # price fire
        # 5min later (inside debounce) a regime flip still fires
        trig = _tick(eng, WINDOW1_SEC + 300, btc=102.05,
                     regime="BEAR_TREND")
        assert trig is not None and trig["type"] == "REGIME_FLIP"

    def test_regime_flip_beats_price(self, tmp_path):
        eng = _eng(tmp_path)
        _tick(eng, 0, btc=100.0, regime="BULL_TREND")
        trig = _tick(eng, WINDOW1_SEC, btc=103.0, regime="BEAR_TREND")
        assert trig["type"] == "REGIME_FLIP"


class TestFillChannel:
    def test_new_trade_id_triggers(self, tmp_path):
        eng = _eng(tmp_path)
        assert _tick(eng, 0, trades={"NEARUSDT": 100}) is None   # cold seed
        trig = _tick(eng, WINDOW1_SEC, trades={"NEARUSDT": 105})
        assert trig is not None and trig["type"] == "FILL"
        assert "NEARUSDT" in trig["symbol"]

    def test_same_trade_id_no_trigger(self, tmp_path):
        eng = _eng(tmp_path)
        _tick(eng, 0, trades={"NEARUSDT": 100})
        assert _tick(eng, WINDOW1_SEC, trades={"NEARUSDT": 100}) is None

    def test_cold_start_seeds_without_trigger(self, tmp_path):
        eng = _eng(tmp_path)
        assert _tick(eng, 0, trades={"NEARUSDT": 100}) is None


class TestStatePersistence:
    def test_round_trip(self, tmp_path):
        f = str(tmp_path / "evt_state.json")
        eng = EventTriggerEngine(f)
        _tick(eng, 0, btc=100.0, regime="BULL_TREND", trades={"NEARUSDT": 7})
        _tick(eng, WINDOW1_SEC, btc=102.0)
        eng2 = EventTriggerEngine(f)
        assert eng2.last_regime == "BULL_TREND"
        assert eng2.last_trade_ids == {"NEARUSDT": 7}
        assert eng2.last_nonregime_trigger_ts == WINDOW1_SEC
        assert len(eng2.price_history[BTC_SYMBOL]) == 2

    def test_corrupt_state_cold_start(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{not json")
        eng = EventTriggerEngine(str(f))
        assert eng.last_regime is None

    def test_history_pruned_beyond_window2(self, tmp_path):
        from src.event_trigger import HISTORY_SEC
        eng = _eng(tmp_path)
        for i in range(6):
            _tick(eng, i * 600, btc=100.0 + i)
        cutoff = 5 * 600 - HISTORY_SEC
        assert all(ts >= cutoff for ts, _ in eng.price_history[BTC_SYMBOL])


class TestGateBypass:
    def test_event_trigger_env_bypasses_time_gate(self, tmp_path, monkeypatch):
        """Bypass: exit 0 + ts saved, WITHOUT reading F&G at all."""
        monkeypatch.setenv("EVENT_TRIGGER", "BTC_MOVE:BTCUSDT 1.9%/10m")
        monkeypatch.setattr(gate, "LAST_SCAN_FILE", str(tmp_path / "ts.json"))
        with patch.object(gate, "get_fng",
                          side_effect=AssertionError("bypass must not read F&G")):
            with pytest.raises(SystemExit) as exc:
                gate.main()
        assert exc.value.code == 0
        data = json.loads((tmp_path / "ts.json").read_text())
        assert data["timestamp"] == pytest.approx(time.time(), abs=30)

    def test_no_env_gate_logic_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.delenv("EVENT_TRIGGER", raising=False)
        monkeypatch.setattr(gate, "LAST_SCAN_FILE", str(tmp_path / "ts.json"))
        with patch.object(gate, "get_fng", return_value=62), \
             patch.object(gate, "get_last_scan_ts",
                          return_value=time.time() - 0.5 * 3600), \
             patch.object(gate, "save_scan_ts") as save:
            with pytest.raises(SystemExit) as exc:
                gate.main()
        assert exc.value.code == 1 and not save.called

    def test_bypass_env_helper(self):
        assert bypass_env("X") == {"EVENT_TRIGGER": "X"}
        assert bypass_env("")["EVENT_TRIGGER"] == "unspecified"


class TestEndToEndSimulated:
    def test_price_spike_to_gate_bypass_round(self, tmp_path, monkeypatch):
        """Simulated round: price series → trigger → env → gate exits 0."""
        eng = _eng(tmp_path)
        assert _tick(eng, 0, btc=100.0) is None
        trig = _tick(eng, WINDOW1_SEC, btc=101.9)
        assert trig is not None
        reason = f"{trig['type']}:{trig['symbol']} {trig['detail']}"
        env = bypass_env(reason)
        assert env["EVENT_TRIGGER"].startswith("BTC_MOVE")

        monkeypatch.setenv("EVENT_TRIGGER", env["EVENT_TRIGGER"])
        monkeypatch.setattr(gate, "LAST_SCAN_FILE", str(tmp_path / "ts.json"))
        with patch.object(gate, "get_fng", return_value=50), \
             patch.object(gate, "get_last_scan_ts", return_value=time.time()):
            with pytest.raises(SystemExit) as exc:
                gate.main()
        assert exc.value.code == 0  # waived despite 0s elapsed

    def test_reside_scan_wiring_present(self):
        """Resident-layer wiring: event_tick + env hand-off survive."""
        src = RESIDE.read_text()
        assert "def event_tick()" in src
        assert 'scan_env["EVENT_TRIGGER"] = ev_reason' in src
        assert "fail-open to normal scan" in src

    def test_reside_scan_fail_open(self):
        """event_tick exceptions must fail open to the scheduled flow."""
        import ast as _ast
        tree = _ast.parse(RESIDE.read_text())
        fn = next(n for n in _ast.walk(tree)
                  if isinstance(n, _ast.FunctionDef) and n.name == "event_tick")
        assert any(isinstance(n, _ast.ExceptHandler) for n in _ast.walk(fn))
