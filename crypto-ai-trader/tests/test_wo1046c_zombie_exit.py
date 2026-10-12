"""WO-1046c S5: zombie exit — no-progress positions release capital early.

双确认: time (held >= zombie_hours, default 24h) + no-progress
(|pnl| < zombie_pnl_pct AND band_position < zombie_band_pos_max;
unknown band -> pnl-only confirmation). Fires BEFORE hold_expiry so
stagnant positions don't idle to the 48h hard expiry. zombie_hours=0
disables the branch (rollback: kv exit:zombie_hours=0).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import src.backtest as bt_mod
from src.backtest import BacktestEngine, Position
from src.exit_check import AUTO_KINDS, EXIT_DEFAULTS, evaluate_one, _zombie_param

H1 = 3_600_000
KW = dict(tp_pct=8.0, sl_pct=6.0, hold_hours=48.0)
ZK = dict(zombie_hours=24.0)


def _pos(sym="TSTUSDT", qty=10.0, entry=100.0):
    return {"symbol": sym, "quantity": qty, "entry_price": entry}


# ── pure core: fires ─────────────────────────────────────────────────────

def test_fires_at_25h_flat_low_band():
    d = evaluate_one(_pos(), 100.3, 25.0, band_position=0.3, **KW, **ZK)
    assert d and d["kind"] == "zombie_exit" and d["auto"] is True
    assert d["sell_pct"] == 100
    assert "no progress" in d["reason"]


def test_fires_on_small_negative_pnl():
    d = evaluate_one(_pos(), 99.7, 25.0, band_position=0.3, **KW, **ZK)
    assert d and d["kind"] == "zombie_exit"


def test_fires_with_unknown_band_pnl_only_confirmation():
    """band_position=None (no klines / momentum disabled) -> the pnl
    flatness alone confirms no-progress — S5 must not silently die for
    strategies without band data."""
    d = evaluate_one(_pos(), 100.2, 25.0, band_position=None, **KW, **ZK)
    assert d and d["kind"] == "zombie_exit"


def test_priority_before_hold_expiry():
    """Flat position at 50h: zombie fires, hold_expiry never labels it."""
    d = evaluate_one(_pos(), 100.2, 50.0, band_position=0.3, **KW, **ZK)
    assert d and d["kind"] == "zombie_exit"


def test_banked_remainder_released():
    """tp1_banked remainder sitting at entry±0.5% is stagnant capital —
    zombie releases it (realized first-tranche profit stays banked)."""
    d = evaluate_one(_pos(), 100.2, 25.0, band_position=0.3,
                     tp1_banked=True, tp_partial_r=1.5, **KW, **ZK)
    assert d and d["kind"] == "zombie_exit"


# ── pure core: does NOT fire ─────────────────────────────────────────────

def test_not_before_24h():
    # band 0.4 sits between the momentum threshold (0.35) and the zombie
    # threshold (0.5) so neither branch interferes — isolating the clock.
    d = evaluate_one(_pos(), 100.3, 23.9, band_position=0.4, **KW, **ZK)
    assert d is None  # quiet zone: no hold, no tp/sl, no momentum/zombie


def test_not_with_realized_progress_pnl():
    d = evaluate_one(_pos(), 101.2, 25.0, band_position=0.8, **KW, **ZK)
    assert d is None  # +1.2% pnl: not stagnant


def test_not_when_band_upper_half():
    """|pnl| flat but price in upper band half -> upside momentum
    present, give it room."""
    d = evaluate_one(_pos(), 100.3, 25.0, band_position=0.8, **KW, **ZK)
    assert d is None


def test_zero_hours_disables_branch():
    d = evaluate_one(_pos(), 100.2, 50.0, band_position=0.3, **KW,
                     zombie_hours=0)
    assert d and d["kind"] == "hold_expiry"  # legacy default path intact


def test_default_signature_off_for_legacy_callers():
    """No zombie args -> branch disabled (tp_partial_r=0 pattern)."""
    d = evaluate_one(_pos(), 100.2, 50.0, band_position=0.3, **KW)
    assert d and d["kind"] == "hold_expiry"


def test_zombie_exit_in_auto_kinds():
    assert "zombie_exit" in AUTO_KINDS


def test_exit_defaults_carry_spec_values():
    assert EXIT_DEFAULTS["zombie_hours"] == 24.0
    assert EXIT_DEFAULTS["zombie_pnl_pct"] == 0.5
    assert EXIT_DEFAULTS["zombie_band_pos_max"] == 0.5


# ── knob resolution: kv (per-symbol > global) > yaml > defaults ──────────

@pytest.fixture
def db():
    from src.state_db import get_state_db
    d = get_state_db()
    yield d
    for k in ("exit:TSTUSDT:zombie_hours", "exit:zombie_hours"):
        d.kv_set(k, None)


def test_param_kv_global_beats_default(db):
    db.kv_set("exit:zombie_hours", 0)
    assert _zombie_param(db, "zombie_hours", "OTHER") == 0.0


def test_param_kv_per_symbol_beats_global(db):
    db.kv_set("exit:zombie_hours", 24)
    db.kv_set("exit:TSTUSDT:zombie_hours", 0)
    assert _zombie_param(db, "zombie_hours", "TSTUSDT") == 0.0
    assert _zombie_param(db, "zombie_hours", "OTHER") == 24.0


def test_param_yaml_beats_default(db, monkeypatch):
    import src.risk_config as rc
    monkeypatch.setattr(
        rc, "get_section",
        lambda name: {"zombie_hours": 12.0} if name == "exit_zombie" else None,
    )
    assert _zombie_param(db, "zombie_hours", "TSTUSDT") == 12.0


def test_param_default_when_no_overrides(db):
    assert _zombie_param(db, "zombie_hours", "TSTUSDT") == 24.0


# ── parity engine feed (live/backtest no-drift) ──────────────────────────

def _eng_pos(sym="TSTUSDT", qty=10.0, entry=100.0, entry_time=0):
    return Position(symbol=sym, entry_price=entry, entry_bar=0,
                    entry_time=entry_time, quantity=qty, usdt_cost=qty*entry,
                    atr=1.0, sl_price=entry*0.9, tp1_price=entry*1.5,
                    tp1_size=qty*0.4, tp2_price=entry*2.0, tp2_size=qty*0.4,
                    tp3_price=entry*3.0, tp3_size=qty*0.2)


def test_engine_parity_feeds_zombie():
    """BacktestEngine passes the EXIT_DEFAULTS zombie knobs — the replay
    exit chain stays live-parity for S5 (band already fed)."""
    eng = BacktestEngine(None)
    kl = [{"open_time": i * H1, "close": 100.3} for i in range(60)]
    p = _eng_pos(entry_time=(60 - 25) * H1)  # held 25h, +0.3% close
    d = eng._eval_exit_check_parity(p, kl[59], kl, 59)
    assert d and d["kind"] == "zombie_exit"


def test_engine_zombie_rollback_via_class_attr():
    eng = BacktestEngine(None)
    assert eng.EXIT_ZOMBIE_HOURS == EXIT_DEFAULTS["zombie_hours"]
    eng.EXIT_ZOMBIE_HOURS = 0
    kl = [{"open_time": i * H1, "close": 100.3} for i in range(60)]
    p = _eng_pos(entry_time=(60 - 25) * H1)
    d = eng._eval_exit_check_parity(p, kl[59], kl, 59)
    assert d is None  # 25h < 48h hold, zombie off -> quiet
