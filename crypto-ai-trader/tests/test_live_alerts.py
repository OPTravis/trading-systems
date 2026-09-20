"""Order-4 tests (2026-09-20): near-realtime event alerts to live_alerts/.

Covers the alert module contract (atomic write, never-raises, bounded
retention) and the four wired event sources: SWITCH_EXECUTED /
PROTECTION_FAILED (position_optimizer), OCO_FILL (reconciler),
REGIME_CHANGE (event_trigger).
"""

import json
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    import src.binance_client  # noqa: F401
except Exception:
    sys.modules["src.binance_client"] = types.SimpleNamespace(
        BinanceClient=object)

import src.live_alerts as la  # noqa: E402
from src.event_trigger import EventTriggerEngine  # noqa: E402


@pytest.fixture
def alert_dir(tmp_path, monkeypatch):
    d = tmp_path / "live_alerts"
    monkeypatch.setattr(la, "ALERT_DIR", d)
    return d


class TestAlertModule:
    def test_emit_writes_structured_json(self, alert_dir):
        assert la.emit("SWITCH_EXECUTED", "SUIUSDT",
                       {"from_symbol": "BCHUSDT"}) is True
        files = list(alert_dir.glob("*.json"))
        assert len(files) == 1
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        assert payload["event_type"] == "SWITCH_EXECUTED"
        assert payload["symbol"] == "SUIUSDT"
        assert "timestamp" in payload and "timestamp_iso" in payload
        assert payload["details"]["from_symbol"] == "BCHUSDT"
        assert "SWITCH_EXECUTED_SUIUSDT" in files[0].name

    def test_emit_never_raises(self, tmp_path, monkeypatch):
        # make mkdir fail for real: a plain FILE occupies the parent path
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir")
        monkeypatch.setattr(la, "ALERT_DIR", blocker / "live" / "alerts")
        assert la.emit("ANY", None, None) is False  # swallowed, not raised

    def test_prune_keeps_newest_max_files(self, alert_dir, monkeypatch):
        monkeypatch.setattr(la, "MAX_FILES", 3)
        for i in range(6):
            (alert_dir).mkdir(exist_ok=True)
            (alert_dir / f"{1000 + i:05d}_E_NA.json").write_text("{}")
        la._prune()
        remaining = sorted(p.name for p in alert_dir.glob("*.json"))
        assert len(remaining) == 3
        assert remaining[0].startswith("01003"), "oldest must be pruned"


class TestRegimeChangeAlert:
    def test_regime_flip_emits_alert(self, alert_dir, tmp_path):
        eng = EventTriggerEngine(state_file=str(tmp_path / "et.json"))
        eng.last_regime = "BEAR_TREND"
        eng.record_and_check(now=1_000_000.0, prices={}, regime_now="RANGE")
        files = list(alert_dir.glob("*REGIME_CHANGE*.json"))
        assert len(files) == 1
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        assert payload["details"] == {"from": "BEAR_TREND", "to": "RANGE"}

    def test_same_regime_no_alert(self, alert_dir, tmp_path):
        eng = EventTriggerEngine(state_file=str(tmp_path / "et.json"))
        eng.last_regime = "BEAR_TREND"
        eng.record_and_check(now=1_000_000.0, prices={}, regime_now="BEAR_TREND")
        assert not list(alert_dir.glob("*.json"))


class TestOcoFillAlert:
    def test_booked_fill_emits_alert(self, alert_dir, tmp_path):
        import time as _t
        from src.state_db import StateDB
        from src.portfolio_reconciler import reconcile_portfolio_drift

        class FakeClient:
            def __init__(self, bals, trades):
                self._b, self._t = bals, trades
            def get_account(self):
                return {"balances": self._b}
            def get_my_trades(self, symbol, limit=100, from_id=None):
                return self._t.get(symbol, [])

        db = StateDB(db_path=str(tmp_path / "s.db"))
        db.trade_add("NEARUSDT", "BUY", 15.0, 3.0)
        db.portfolio_set("NEARUSDT", {"quantity": 15.0, "entry_price": 3.0})
        now = _t.time()
        client = FakeClient(
            [{"asset": "NEAR", "free": "0", "locked": "0"}],
            {"NEARUSDT": [{
                "id": 1, "price": "3.1", "qty": "15.0",
                "quoteQty": "46.5", "commission": "0", "commissionAsset": "USDT",
                "time": int(now * 1000), "isBuyer": False, "isMaker": False,
                "orderId": 4242, "symbol": "NEARUSDT"}]})
        booked = reconcile_portfolio_drift(client, db)
        assert len(booked) == 1
        files = list(alert_dir.glob("*OCO_FILL*.json"))
        assert len(files) == 1
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        assert payload["symbol"] == "NEARUSDT"
        assert payload["details"]["order_id"] == "4242"
        assert payload["details"]["pnl"] > 0
        db.close()


class TestProtectionFailedAlert:
    def test_falsy_tp_emits_protection_alert(self, alert_dir, caplog):
        from src.position_optimizer import PositionOptimizer
        opt = object.__new__(PositionOptimizer)
        opt.bc = SimpleNamespace(
            get_symbol_filters=lambda s: {
                "stepSize": "0.1", "tickSize": "0.0001", "minNotional": "10"},
            place_stop_loss_limit=lambda *a, **k: {"orderId": 1},
            place_limit_sell=lambda *a, **k: None,
        )
        buy_order = {"cummulativeQuoteQty": "40.4", "executedQty": "53.2"}
        with caplog.at_level(logging.ERROR):
            opt._place_switch_protections("ETHFIUSDT", 53.2, buy_order, 0.7588)
        files = list(alert_dir.glob("*PROTECTION_FAILED*.json"))
        assert len(files) == 1
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        assert payload["details"]["which"] == "TP"
        assert "error" in payload["details"]
