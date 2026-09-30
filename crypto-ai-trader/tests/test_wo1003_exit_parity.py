"""WO-1003-7: backtest / live exit parity — one decision core.

exit_check.evaluate_one() is THE single place exit semantics live; the
backtest replay feeds it bar closes + in-memory hold hours, the live shell
feeds it kv params + trades anchors + ticker prices. These tests lock the
parity invariants and the replay bookkeeping (full close / momentum 50%
trim + P2 breakeven relist).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.backtest as bt_mod
from src.backtest import BacktestEngine, ClosedTrade, Position
from src.exit_check import (EXIT_DEFAULTS, MOMENTUM_BAND_POS_MAX,
                            evaluate_one)

H1 = 3_600_000


def _pos_dict(sym="TSTUSDT", qty=10.0, entry=100.0):
    return {"symbol": sym, "quantity": qty, "entry_price": entry}


def _pos(sym="TSTUSDT", qty=10.0, entry=100.0, entry_bar=0, entry_time=0):
    return Position(symbol=sym, entry_price=entry, entry_bar=entry_bar,
                    entry_time=entry_time, quantity=qty, usdt_cost=qty*entry,
                    atr=1.0, sl_price=entry*0.9, tp1_price=entry*1.5,
                    tp1_size=qty*0.4, tp2_price=entry*2.0, tp2_size=qty*0.4,
                    tp3_price=entry*3.0, tp3_size=qty*0.2)


KW = dict(tp_pct=8.0, sl_pct=6.0, hold_hours=48.0)


# ── pure core ───────────────────────────────────────────────────────

def test_core_hold_wins_over_tp():
    d = evaluate_one(_pos_dict(), 109.0, 50.0, **KW)  # tp hit AND hold hit
    assert d["kind"] == "hold_expiry" and d["sell_pct"] == 100


def test_core_tp_then_sl_priority():
    assert evaluate_one(_pos_dict(), 108.5, 1.0, **KW)["kind"] == "take_profit"
    assert evaluate_one(_pos_dict(), 93.9, 1.0, **KW)["kind"] == "stop_loss"


def test_core_momentum_tiers():
    d100 = evaluate_one(_pos_dict(), 101.0, 1.0, band_position=0.10, **KW)
    assert d100["kind"] == "momentum_reversal" and d100["sell_pct"] == 100
    d50 = evaluate_one(_pos_dict(), 101.0, 1.0, band_position=0.20, **KW)
    assert d50["sell_pct"] == 50
    # losing position never momentum-exits; above band max no exit
    assert evaluate_one(_pos_dict(), 99.0, 1.0, band_position=0.05, **KW) is None
    assert evaluate_one(_pos_dict(), 101.0, 1.0, band_position=0.90, **KW) is None


def test_core_quiet_zone_returns_none():
    assert evaluate_one(_pos_dict(), 100.5, 10.0, band_position=0.5, **KW) is None


# ── backtest parity helpers ─────────────────────────────────────────

def test_band_position_matches_live_math():
    # same windowing/formula as live exit_check._band_position
    closes = [10, 11, 12, 11, 10, 9, 8, 9, 10, 11,
              12, 13, 12, 11, 10, 9, 8, 7, 8, 9, 9.5][-20:]
    kl = [{"close": float(c)} for c in closes]
    mid = sum(closes) / len(closes)
    sd = (sum((c - mid) ** 2 for c in closes) / len(closes)) ** 0.5
    want = (closes[-1] - (mid - 2*sd)) / ((mid + 2*sd) - (mid - 2*sd))
    got = bt_mod._band_position_from_klines(kl, len(kl) - 1)
    assert abs(got - want) < 1e-12
    # live parity: identical window fed to live helper gives same value
    # (live _band_position queries exchange klines; its math core is the
    #  same 20-close bollinger position — locked by the formula above)
    assert bt_mod._band_position_from_klines(kl[:10], 9) is None


def test_engine_parity_eval_hold_tp_sl():
    eng = BacktestEngine(None)
    kl = [{"open_time": i * H1, "close": 100.5} for i in range(60)]
    # hold: entry 48h ago
    p = _pos(entry_time=(60 - 49) * H1)
    d = eng._eval_exit_check_parity(p, kl[59], kl, 59)
    assert d and d["kind"] == "hold_expiry"
    # tp: +8% close (entry 100 → close 108.5), held < 48h
    p = _pos(entry_time=(59 - 5) * H1)
    kl[59]["close"] = 108.5
    assert eng._eval_exit_check_parity(p, kl[59], kl, 59)["kind"] == \
        "take_profit"
    # sl: -6%
    kl[59]["close"] = 93.5
    assert eng._eval_exit_check_parity(p, kl[59], kl, 59)["kind"] == \
        "stop_loss"


def test_engine_parity_eval_momentum_and_trim_guard():
    eng = BacktestEngine(None)
    # closes drifting down then a small positive close: low-band position
    base = [102.0] * 19 + [101.4]
    kl = [{"open_time": i * H1, "close": c} for i, c in enumerate(base)]
    p = _pos(entry=100.0, entry_time=0)
    idx = len(kl) - 1
    bp = bt_mod._band_position_from_klines(kl, idx)
    assert bp is not None and bp < MOMENTUM_BAND_POS_MAX
    d = eng._eval_exit_check_parity(p, kl[idx], kl, idx)
    assert d and d["kind"] == "momentum_reversal"
    assert d["sell_pct"] == (100 if bp < 0.15 else 50)
    # trimmed flag disables momentum on later evaluations
    p.momentum_trimmed = True
    d2 = eng._eval_exit_check_parity(p, kl[idx], kl, idx)
    assert d2 is None or d2["kind"] != "momentum_reversal"


def test_apply_full_close_books_trade():
    eng = BacktestEngine(None)
    p = _pos(qty=10.0)
    closed = []
    dec = {"symbol": "TSTUSDT", "qty": 10.0, "entry_price": 100.0,
           "price": 90.0, "pnl_pct": -10.0, "held_hours": 2.0,
           "kind": "stop_loss", "sell_pct": 100}
    eng._apply_exit_check_exit(p, dec, 99 * H1, 5, closed)
    assert p.quantity == 0
    assert len(closed) == 1 and closed[0].reason == "stop_loss"
    assert closed[0].exit_price == 90.0


def test_apply_momentum_partial_trims_and_relists_breakeven():
    eng = BacktestEngine(None)
    p = _pos(qty=10.0, entry=100.0)
    closed = []
    dec = {"symbol": "TSTUSDT", "qty": 10.0, "entry_price": 100.0,
           "price": 102.0, "pnl_pct": 2.0, "held_hours": 1.0,
           "kind": "momentum_reversal", "sell_pct": 50}
    eng._apply_exit_check_exit(p, dec, 9 * H1, 9, closed)
    assert abs(p.quantity - 5.0) < 1e-9          # half trimmed
    assert p.usdt_cost == pytest_approx(500.0)   # cost basis halved
    assert p.momentum_trimmed is True
    # breakeven: entry*0.995=99.5, close*0.98=99.96 → cand(99.5) below
    # 99.96 so stays 99.5 (P2: only falls back to close*0.95 when too close)
    assert abs(p.sl_price - 99.5) < 1e-9
    assert len(closed) == 1 and closed[0].reason == "momentum_reversal"


def pytest_approx(x, tol=1e-6):
    class _A:
        def __eq__(self, other):
            return abs(other - x) <= tol
    return _A()


def test_apply_momentum_partial_breakeven_fallback_when_too_close():
    eng = BacktestEngine(None)
    # price crashed to just above breakeven → cand >= price*0.98 → price*0.95
    p = _pos(qty=10.0, entry=100.0)
    dec = {"symbol": "TSTUSDT", "qty": 10.0, "entry_price": 100.0,
           "price": 100.05, "pnl_pct": 0.05, "held_hours": 1.0,
           "kind": "momentum_reversal", "sell_pct": 50}
    eng._apply_exit_check_exit(p, dec, 9 * H1, 9, [])
    assert abs(p.sl_price - 100.05 * 0.95) < 1e-9


# ── full _simulate integration: hold-expiry fires end-to-end ────────

def test_simulate_hold_expiry_end_to_end(monkeypatch):
    n = 220
    kl = []
    px = 100.0
    for i in range(n):
        wiggle = 0.15 if i % 2 == 0 else -0.15
        o = px
        px = px + 0.002            # gentle drift, stays inside ±1% of entry
        kl.append({"open_time": i * H1, "open": round(o, 4),
                   "high": round(max(o, px) + 0.05, 4),
                   "low": round(min(o, px) - 0.05, 4),
                   "close": round(px, 4), "volume": 1000.0})
    # entries at WARMUP=100: bar 100 scores, bar 101 opens the position;
    # hold fires 48 bars later (pnl tiny, band neutral → only hold exits)
    monkeypatch.setattr(bt_mod, "calculate_score",
                        lambda *a, **kw: 60.0)
    monkeypatch.setattr(bt_mod.Indicators, "analyze_symbol",
                        staticmethod(lambda kl_: {"rsi": 50, "atr": 5.0,
                                                  "macd": 0, "boll": {}}))
    eng = BacktestEngine(None)
    res = eng._simulate(
        symbol="TSTUSDT", interval="1h", start_date="2026-06-01",
        end_date="2026-09-01", klines=kl, klines_4h=None, klines_1d=None,
        btc_daily=None, btc_sma_200_cache={}, enable_trend_filter=False,
        enable_trailing_stop=False,
    )
    trades = res["trades"]
    reasons = {t["reason"] for t in trades}
    assert "hold_expiry" in reasons, f"parity hold never fired: {reasons}"
    # every hold-expiry trade exited around its entry (no crash path)
    for t in trades:
        if t["reason"] == "hold_expiry":
            assert -7.0 < t["pnl_pct"] < 2.0
