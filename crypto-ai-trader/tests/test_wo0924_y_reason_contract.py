"""WO-0924-y: BLOCKED returns without "error" key must not crash
_step_execute_trades' self-healer branch (14:11 cron_scan crash:
governor return carried "reason", reader demanded result["error"]).
"""
from src import execute_phases as ep


def _ctx():
    return {"symbol": "UNIUSDT", "price": 9.4, "stop_loss_pct": 6.0,
            "stop_price": 8.8, "reason": "test", "strategy": "trend",
            "signals": [], "adjusted_score": 70, "top": {},
            "adapted": {"global": {}}, "research": {}, "bear_result": None,
            "score": 70, "tier_label": "T1", "tp_levels": [],
            "max_hold": 48, "max_position_pct": 15, "cash_reserve_pct": 30,
            "size_multiplier": 1.0,
            "regime": "GREED", "fng": 71, "fng_label": "Greed",
            "btc_trend": "BULLISH", "portfolio": {}, "active_pos": 4,
            "research_adj": 0, "research_confidence": "HIGH",
            "research_summary": "ok"}


def test_reason_style_blocked_no_crash(monkeypatch):
    """governor/blacklist/shutdown style: success=False + reason only."""
    monkeypatch.setattr(
        ep, "execute_auto_trade",
        lambda **kw: {"success": False, "reason": "ENTRY_BLOCKED: cooldown",
                      "governor": "loss_exit_cooldown"})
    # must not raise — prints the failure line with heal info (or none)
    ep._step_execute_trades(_ctx())


def test_error_style_still_reported(monkeypatch):
    monkeypatch.setattr(
        ep, "execute_auto_trade",
        lambda **kw: {"success": False, "error": "Balance too small"})
    ep._step_execute_trades(_ctx())


def test_bare_success_false_no_keys(monkeypatch):
    monkeypatch.setattr(
        ep, "execute_auto_trade",
        lambda **kw: {"success": False})
    ep._step_execute_trades(_ctx())
