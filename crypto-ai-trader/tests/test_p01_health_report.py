"""P0-1: health self-report tiering (設計 v1.1 §五簡版).

L0 clean / L1 self-healed silent / L2 DEGRADED / L3 ESCALATED."""

import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src import health_report as hr  # noqa: E402


class FakeDB:
    def __init__(self, kv=None):
        self.kv = kv or {}

    def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    def kv_set(self, key, value):
        self.kv[key] = value


class FakePortfolio:
    def __init__(self, db=None):
        self._db = db


class FakeClientOK:
    def get_account(self):
        return {"balances": [{"asset": "USDT", "free": "1"}]}


class FakeClientDown:
    def get_account(self):
        raise RuntimeError("api down")


@pytest.fixture(autouse=True)
def _neutral(monkeypatch, tmp_path):
    from src import _binance_sdk_client as _sdk
    monkeypatch.setattr(
        _sdk, "RATE_STATS", {"429": 0, "418": 0, "5xx": 0, "network": 0},
        raising=False,
    )
    emitted = []
    fake_alerts = types.SimpleNamespace(
        emit=lambda t, s, d=None: emitted.append((t, s, d)))
    monkeypatch.setitem(sys.modules, "src.live_alerts", fake_alerts)
    # neutralize SDK rate stats import
    monkeypatch.setattr(hr, "_THROTTLE", True, raising=False)
    yield {"emitted": emitted}


def test_l0_clean_round(_neutral):
    db = FakeDB()
    out = hr.run(FakeClientOK(), FakePortfolio(db), dust_summary={})
    assert out["level"] == "L0"
    assert not _neutral["emitted"]


def test_l1_rate_events_silent(_neutral, monkeypatch):
    import src._binance_sdk_client as sdk
    monkeypatch.setattr(sdk, "RATE_STATS", {"429": 1}, raising=False)
    out = hr.run(FakeClientOK(), FakePortfolio(FakeDB()), dust_summary={})
    assert out["level"] == "L1"
    assert not _neutral["emitted"]   # self-healed → silent


def test_l2_filter_failures_degraded(_neutral):
    db = FakeDB()
    out = hr.run(
        FakeClientOK(), FakePortfolio(db),
        dust_summary={"filter_failures": 2},
    )
    assert out["level"] == "L2"
    assert _neutral["emitted"] and _neutral["emitted"][0][0] == "SYSTEM_DEGRADED"


def test_l2_high_rate_pressure_degraded(_neutral, monkeypatch):
    import src._binance_sdk_client as sdk
    monkeypatch.setattr(sdk, "RATE_STATS", {"429": 12}, raising=False)
    out = hr.run(FakeClientOK(), FakePortfolio(FakeDB()), dust_summary={})
    assert out["level"] == "L2"


def test_l3_api_down_escalated(_neutral):
    db = FakeDB()
    out = hr.run(FakeClientDown(), FakePortfolio(db), dust_summary={})
    assert out["level"] == "L3"
    assert any(e[0] == "SYSTEM_ESCALATED" for e in _neutral["emitted"])


def test_l3_liquidation_failures_escalated(_neutral):
    db = FakeDB()
    out = hr.run(
        FakeClientOK(), FakePortfolio(db),
        dust_summary={"liquidation_failures": 2},
    )
    assert out["level"] == "L3"


def test_l2_throttled_no_repeat_alert(_neutral):
    db = FakeDB()
    import time as _t
    db.kv[hr.KV_LAST_DEG_TS] = _t.time()  # alerted moments ago
    out = hr.run(
        FakeClientOK(), FakePortfolio(db),
        dust_summary={"filter_failures": 3},
    )
    assert out["level"] == "L2"
    assert not _neutral["emitted"]   # throttled within 4h


def test_watch_note_emitted_without_l3(_neutral):
    db = FakeDB()
    out = hr.run(
        FakeClientOK(), FakePortfolio(db),
        dust_summary={"watch": 3, "filter_failures": 0},
    )
    assert out["level"] == "L0"       # watch alone does not degrade the tier
    assert any(
        e[0] == "SYSTEM_ESCALATED" and e[2].get("reason") == "unprotectable_unsellable_watch"
        for e in _neutral["emitted"]
    )
