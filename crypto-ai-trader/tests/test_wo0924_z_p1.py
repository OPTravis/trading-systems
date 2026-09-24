"""WO-0924-z P1 acceptance: unified failure contract + governor
cold-start backstop + outcomes closure + isolation."""
import time

from src import entry_governor as gov


class TestUnifiedFailureContract:
    def test_fail_helper_shape(self):
        from src.trade_executor import _fail
        r = _fail("some reason", gate="x")
        assert r["success"] is False
        assert r["error"] == "some reason" == r["reason"]
        assert r["gate"] == "x"

    def test_governor_reason_via_fail(self):
        from src.trade_executor import _fail
        gov.note_loss_exit("CONTRACTX", -1.0)
        r = _fail(gov.check_entry("CONTRACTX")["reason"],
                  governor="loss_exit_cooldown")
        assert r["error"] == r["reason"] and "cooldown" in r["reason"]

    def test_no_bare_failures_in_executor(self):
        src = open("src/trade_executor.py").read()
        body = src[src.index("def execute_auto_trade"):]
        assert '"success": False' not in body


class TestGovernorColdStartBackstop:
    def test_trades_count_backstops_empty_kv(self):
        # kv counter empty, but trades table already has same-day BUYs
        from src.state_db import get_state_db
        db = get_state_db()
        for i in range(gov.DAILY_ENTRY_CAP):
            db.trade_add("BACKSTOP%dUSDT" % i, "BUY", 1.0, 1.0)
        r = gov.check_entry("FRESHSYM")   # no kv counter at all
        assert not r["ok"] and r["gate"] == "daily_entry_cap"


class TestExecuteIsolation:
    def test_raise_inside_execute_isolated(self, monkeypatch):
        """P1-4: execute raising must not propagate out of
        _step_execute_trades (cron round continues)."""
        from src import execute_phases as ep

        def boom(**kw):
            raise RuntimeError("exchange exploded")
        monkeypatch.setattr(ep, "execute_auto_trade", boom)
        ep._step_execute_trades(_mk_ctx())  # must not raise


def _mk_ctx():
    return {"symbol": "UNIUSDT", "price": 9.4, "stop_loss_pct": 6.0,
            "stop_price": 8.8, "reason": "t", "strategy": "trend",
            "signals": [], "adjusted_score": 70, "top": {},
            "adapted": {"global": {}}, "research": {}, "bear_result": None,
            "score": 70, "tier_label": "T1", "tp_levels": [],
            "max_hold": 48, "max_position_pct": 15, "cash_reserve_pct": 30,
            "size_multiplier": 1.0, "regime": "GREED", "fng": 71,
            "fng_label": "Greed", "btc_trend": "BULLISH", "portfolio": {},
            "active_pos": 4, "research_adj": 0, "research_confidence": "H",
            "research_summary": "ok"}
