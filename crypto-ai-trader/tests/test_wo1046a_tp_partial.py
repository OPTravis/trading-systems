"""WO-1046a P1 (2026-10-12): two-tranche take-profit + trailing phase 2.

S2 — decision core tp_partial: evaluate_one banks the first tranche
(sell_pct=50) once pnl >= r_multiple * |sl_pct| (R anchored to the ENTRY
stop distance, not the trailed stop); once banked (tp1_banked) the full
take_profit exit is waived so the remainder rides trailing/phase2.

S2 — resting orders: tiered ladder is a single TP1 at 50% (was 33+33),
TP1 price floored at max(3×ATR, 3%, 1.5R); _check_tp_fills writes the
banked kv + moves SL to the profit-lock floor (entry+lock_pct).

S1 — trailing phase 2: at profit >= phase2_r_pct (default 7.5% ≈ 1.5R
for the stock 5% SL) the trailing band switches ONCE from the tight
STEPS ladder to a wide ATR band (clamp(2.5×ATR, 2%, 12%)); the switch
round may move the SL DOWN, afterwards it only ratchets up (sticky).

C2 anchor (Travis 19:51): momentum_reversal partial exits keep their
PENGU #91 semantics — sell_pct=50 with the remainder re-listed at the
breakeven floor entry×0.995, NOT the tp_partial profit-lock floor.
"""
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.exit_check import evaluate_one, evaluate_exits, execute_exit  # noqa: E402
from src.adaptive_trailing import AdaptiveTrailingStop  # noqa: E402

FILTERS = {"stepSize": 1.0, "tickSize": 1e-06, "minQty": 1, "minNotional": 1.0}


def _pos(entry=100.0, qty=100, stop_loss=None):
    row = {"symbol": "TESTUSDT", "quantity": qty, "entry_price": entry,
           "strategy": "technical_v2",
           "opened_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())}
    if stop_loss is not None:
        row["stop_loss"] = stop_loss
    return row


def _klines(price, atr_pct, n=20):
    """Synthetic 1h bars whose ATR14 == atr_pct% of price."""
    tr = price * atr_pct / 100.0
    out = []
    for i in range(n):
        c = price
        out.append({"high": c + tr / 2, "low": c - tr / 2, "close": c,
                    "open": c, "volume": 100})
    return out


# ────────────────────────────────────────────────────────────
# A. evaluate_one — decision-core boundaries (pure function)
# ────────────────────────────────────────────────────────────

class TestEvaluateOneTpPartial:
    def test_triggers_at_1_5r(self):
        # sl 5% → 1.5R = 7.5%: price 107.6 banks the first tranche
        d = evaluate_one(_pos(entry=100.0), 107.6, 1.0,
                         tp_pct=12.0, sl_pct=5.0, hold_hours=48.0,
                         tp_partial_r=1.5, tp_partial_pct=50)
        assert d and d["kind"] == "tp_partial"
        assert d["sell_pct"] == 50

    def test_below_threshold_no_partial(self):
        # 1.49R = 7.45% → price 107.4 falls to take_profit only if >= tp
        d = evaluate_one(_pos(entry=100.0), 107.4, 1.0,
                         tp_pct=12.0, sl_pct=5.0, hold_hours=48.0,
                         tp_partial_r=1.5, tp_partial_pct=50)
        assert d is None  # below 1.5R and below tp 12%

    def test_partial_priority_over_full_tp(self):
        # pnl 12.5% is >= tp AND >= 1.5R → tp_partial wins (bank first)
        d = evaluate_one(_pos(entry=100.0), 112.5, 1.0,
                         tp_pct=12.0, sl_pct=5.0, hold_hours=48.0,
                         tp_partial_r=1.5, tp_partial_pct=50)
        assert d["kind"] == "tp_partial"
        assert d["sell_pct"] == 50

    def test_banked_waives_full_tp(self):
        # banked + pnl 12.5% (>= tp): take_profit waived, remainder rides
        d = evaluate_one(_pos(entry=100.0), 112.5, 1.0,
                         tp_pct=12.0, sl_pct=5.0, hold_hours=48.0,
                         tp_partial_r=1.5, tp1_banked=True)
        assert d is None

    def test_banked_is_idempotent(self):
        # banked + pnl 20%: no second tp_partial either
        d = evaluate_one(_pos(entry=100.0), 120.0, 1.0,
                         tp_pct=12.0, sl_pct=5.0, hold_hours=48.0,
                         tp_partial_r=1.5, tp1_banked=True)
        assert d is None

    def test_default_args_legacy_bit_identical(self):
        # tp_partial_r=0 (default) → the branch is OFF, legacy full tp
        d = evaluate_one(_pos(entry=100.0), 112.5, 1.0,
                         tp_pct=12.0, sl_pct=5.0, hold_hours=48.0)
        assert d["kind"] == "take_profit"
        assert d.get("sell_pct", 100) == 100

    def test_r_anchored_to_sl_pct_not_default(self):
        # sl 4% → 1.5R = 6%: 5.9% must NOT trigger, 6.1% must
        d = evaluate_one(_pos(entry=100.0), 105.9, 1.0,
                         tp_pct=12.0, sl_pct=4.0, hold_hours=48.0,
                         tp_partial_r=1.5)
        assert d is None
        d = evaluate_one(_pos(entry=100.0), 106.1, 1.0,
                         tp_pct=12.0, sl_pct=4.0, hold_hours=48.0,
                         tp_partial_r=1.5)
        assert d and d["kind"] == "tp_partial"

    def test_hold_expiry_beats_tp_partial(self):
        # 48h+ AND pnl >= 1.5R → hold_expiry full close still first
        d = evaluate_one(_pos(entry=100.0), 110.0, 49.0,
                         tp_pct=12.0, sl_pct=5.0, hold_hours=48.0,
                         tp_partial_r=1.5)
        assert d["kind"] == "hold_expiry"

    def test_stop_loss_beats_tp_partial(self):
        # pnl negative → sl (no partial on losses)
        d = evaluate_one(_pos(entry=100.0), 94.0, 1.0,
                         tp_pct=12.0, sl_pct=5.0, hold_hours=48.0,
                         tp_partial_r=1.5)
        assert d["kind"] == "stop_loss"


# ────────────────────────────────────────────────────────────
# B. execute_exit — market-partial path
# ────────────────────────────────────────────────────────────

class FakeExitClient:
    """Minimal exchange for the exit chain: resting legs, market sells,
    SL re-lists, TP1 order lookup, klines for the phase2 band."""

    def __init__(self, *, price, tp1_order=None, klines=None):
        self.price = price
        self.orders = [
            {"symbol": "TESTUSDT", "side": "SELL", "status": "NEW",
             "orderId": 111, "type": "LIMIT_MAKER"},
            {"symbol": "TESTUSDT", "side": "SELL", "status": "NEW",
             "orderId": 222, "type": "STOP_LOSS_LIMIT"},
        ]
        self.tp1_order = tp1_order
        self._klines = klines
        self.sold = None
        self.sl_relist = None

    def get_symbol_filters(self, symbol):
        return dict(FILTERS)

    def get_free_balance(self, asset="USDT"):
        return 10_000.0

    def get_open_orders(self, symbol=None):
        return self.orders

    def cancel_order(self, symbol, order_id):
        self.orders = [o for o in self.orders if o["orderId"] != order_id]
        return {"orderId": order_id, "status": "CANCELED"}

    def cancel_all_orders(self, symbol):
        self.orders = []
        return True

    def get_ticker_price(self, symbol):
        return self.price

    def get_24hr_stats(self, symbol):
        return {"last_price": str(self.price)}

    def place_market_sell(self, symbol, quantity):
        self.sold = quantity
        return {"orderId": 990001, "executedQty": str(quantity),
                "status": "FILLED"}

    def place_stop_loss_market(self, symbol, quantity, stop_price):
        self.sl_relist = (quantity, stop_price)
        return {"orderId": 990002}

    def get_order(self, symbol, order_id):
        return self.tp1_order

    def get_klines(self, symbol, interval="1h", limit=40):
        return self._klines

    # reconciler surface — failures are swallowed by the exit chain
    def get_my_trades(self, symbol, limit=100, from_id=None):
        return []

    def get_account(self):
        return {"balances": [{"asset": "TEST", "free": "100", "locked": "0"},
                             {"asset": "USDT", "free": "100", "locked": "0"}]}


@pytest.fixture
def db():
    d = tempfile.mkdtemp()
    os.environ["STATE_DB_PATH"] = os.path.join(d, "wo1046a.db")
    os.environ["TESTING"] = "1"
    from src.state_db import get_state_db

    _db = get_state_db()
    yield _db
    os.environ["STATE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "n.db")


def _dec(kind, sell_pct, price=107.6, entry=100.0, qty=100):
    return {"symbol": "TESTUSDT", "kind": kind, "qty": qty,
            "entry_price": entry, "price": price, "pnl_pct": 7.6,
            "held_hours": 1.0, "reason": f"{kind} test",
            "sell_pct": sell_pct, "auto": True, "ts": time.time()}


class TestExecuteTpPartial:
    def test_half_sell_and_banked_kv(self, db):
        cli = FakeExitClient(price=107.6, klines=None)
        out = execute_exit(cli, db, _dec("tp_partial", 50))
        assert out["status"] == "ok"
        assert cli.sold == 50  # 50% of 100, stepSize 1
        banked = db.kv_get("tp1_banked:TESTUSDT")
        assert banked and banked.get("kind") == "market_partial"
        assert banked.get("qty") == 50

    def test_remainder_lock_floor(self, db):
        # no klines → phase2 band unavailable → lock floor entry+0.25%
        cli = FakeExitClient(price=107.6, klines=None)
        execute_exit(cli, db, _dec("tp_partial", 50))
        qty, sl = cli.sl_relist
        assert qty == 50
        assert sl == pytest.approx(100.0 * 1.0025, abs=1e-3)

    def test_remainder_phase2_wide_band_wins_when_wider(self, db):
        # ATR 1.5% → band 3.75% → 107.6×0.9625 = 103.565 > lock 100.25
        kl = _klines(107.6, 1.5)
        cli = FakeExitClient(price=107.6, klines=kl)
        execute_exit(cli, db, _dec("tp_partial", 50))
        qty, sl = cli.sl_relist
        assert sl == pytest.approx(107.6 * 0.9625, abs=0.01)

    def test_remainder_lock_floor_wins_when_band_tighter(self, db):
        # ATR 3% → band 7.5% → 107.6×0.925 = 99.53 < lock 100.25
        kl = _klines(107.6, 3.0)
        cli = FakeExitClient(price=107.6, klines=kl)
        execute_exit(cli, db, _dec("tp_partial", 50))
        _, sl = cli.sl_relist
        assert sl == pytest.approx(100.0 * 1.0025, abs=1e-3)

    def test_exchange_tp1_filled_skips_market_sell(self, db):
        # exit step runs BEFORE trailing check: TP1 already FILLED
        # server-side → sync banked, no double sell. The dedup reads the
        # resting TP1 order id from the tp_sl_tracker state.
        db.kv_set("tp_sl_tracker:TESTUSDT", {
            "entry_price": 100.0,
            "tp_orders": [{"order_id": 111, "price": 107.5, "qty": 50,
                           "tier": 1, "pct": 7.5}],
            "tp_filled": [False], "sl_moved_after_tp": 0,
            "sl_order": {"order_id": 222, "price": 93.0,
                         "stop_price": 95.0, "qty": 50},
        })
        cli = FakeExitClient(
            price=107.6,
            tp1_order={"orderId": 111, "status": "FILLED",
                       "executedQty": "50"},
        )
        out = execute_exit(cli, db, _dec("tp_partial", 50))
        assert out["status"] == "ok"
        assert cli.sold is None  # no market sell — banked on exchange
        banked = db.kv_get("tp1_banked:TESTUSDT")
        assert banked and banked.get("kind") == "limit_tp1"

    def test_full_exit_clears_banked_kv(self, db):
        db.kv_set("tp1_banked:TESTUSDT", {"ts": time.time(), "qty": 50})
        cli = FakeExitClient(price=120.0)
        out = execute_exit(cli, db, _dec("take_profit", 100, price=120.0))
        assert out["status"] == "ok"
        assert cli.sold == 100
        assert db.kv_get("tp1_banked:TESTUSDT") is None

    def test_momentum_breakeven_floor_unchanged_C2(self, db):
        # C2 (PENGU #91 anchor): momentum partial keeps entry×0.995
        cli = FakeExitClient(price=107.6, klines=None)
        execute_exit(cli, db, _dec("momentum_reversal", 50))
        qty, sl = cli.sl_relist
        assert qty == 50
        assert sl == pytest.approx(100.0 * 0.995, abs=1e-3)


# ────────────────────────────────────────────────────────────
# C. adaptive_trailing — phase 2 band (pure function)
# ────────────────────────────────────────────────────────────

class TestAdaptivePhase2:
    def _calc(self, **kw):
        a = AdaptiveTrailingStop()
        return a.calculate_trailing_sl(
            entry_price=kw.get("entry", 100.0),
            current_price=kw.get("price", 108.0),
            highest_price=kw.get("highest", 108.0),
            initial_sl=kw.get("initial_sl", 95.0),
            volatility_adjustment=kw.get("vol_adj", 1.0),
            phase2_profit_pct=kw.get("p2_r", None),
            phase2_atr_pct=kw.get("p2_atr", None),
            phase2_force=kw.get("p2_force", False),
        )

    def test_phase2_band_when_profit_ge_threshold(self):
        # profit 8% >= 7.5%, ATR 3% → width 7.5% → raw 108×0.925 = 99.9,
        # floored by the min-profit lock entry×1.005 = 100.5
        r = self._calc(price=108.0, highest=108.0, p2_r=7.5, p2_atr=3.0)
        assert r["phase2"] is True
        assert r["step"] == "phase2_atr"
        assert r["phase2_width_pct"] == pytest.approx(7.5)
        assert r["trailing_sl"] == pytest.approx(100.0 * 1.005)

    def test_phase2_force_sticky_below_threshold(self):
        # activated, profit decayed to 3% → band stays (one-way);
        # raw 108×0.925 = 99.9 floored to the entry×1.005 lock
        r = self._calc(price=103.0, highest=108.0, p2_r=7.5, p2_atr=3.0,
                       p2_force=True)
        assert r["phase2"] is True
        assert r["trailing_sl"] == pytest.approx(100.0 * 1.005)

    def test_default_args_legacy_steps(self):
        # no phase2 args → tight ladder, bit-identical legacy
        r = self._calc(price=106.0, highest=106.0)  # profit 6%
        assert r["phase2"] is False
        assert r["step"] == "step_5_10"
        assert r["trailing_sl"] == pytest.approx(106.0 * 0.98)

    def test_width_clamped(self):
        # ATR 6% → 2.5×6=15 → clamp 12
        r = self._calc(price=108.0, highest=108.0, p2_r=7.5, p2_atr=6.0)
        assert r["phase2_width_pct"] == pytest.approx(12.0)
        # ATR 0.4% → 1.0 → floor 2.0
        r = self._calc(price=108.0, highest=108.0, p2_r=7.5, p2_atr=0.4)
        assert r["phase2_width_pct"] == pytest.approx(2.0)

    def test_band_respects_min_profit_lock(self):
        # extreme ATR: band would price SL below entry×1.005 → floored
        r = self._calc(price=108.0, highest=108.0, p2_r=7.5, p2_atr=6.0)
        assert r["trailing_sl"] >= 100.0 * 1.005 - 1e-9


# ────────────────────────────────────────────────────────────
# D. risk_manager.TrailingStop — phase2 sticky switch
# ────────────────────────────────────────────────────────────

class TestTrailingPhase2Switch:
    def _ts(self, db, *, state=None):
        from src.risk_manager import TrailingStop

        ts = TrailingStop()
        if state:
            ts._state["TESTUSDT"] = state
        return ts

    def _activated_state(self, sl_price, entry=100.0, highest=108.0, atr=3.24):
        return {"entry_price": entry, "highest_price": highest,
                "sl_price": sl_price, "activated": True, "atr": atr}

    def test_switch_round_moves_sl_down_once(self, db):
        # tight STEPS had SL at 106.1 (108×0.982); phase2 band 7.5% → 99.9
        ts = self._ts(db, state=self._activated_state(sl_price=106.10))
        r = ts.update("TESTUSDT", current_price=108.0, atr=3.24)
        assert r.get("phase2_switched") is True
        # raw band 108×0.925=99.9 floored to entry×1.005=100.5 — still a
        # ONE-TIME downgrade from the tight 106.10
        assert r["sl_price"] == pytest.approx(100.5, abs=1e-3)

    def test_after_switch_only_ratchets_up(self, db):
        ts = self._ts(db, state=self._activated_state(sl_price=99.9))
        st = ts._state["TESTUSDT"]
        st["phase2_activated"] = True
        # price climbs: highest 112 → band SL = 112×0.925 = 103.6
        ts.update("TESTUSDT", current_price=112.0, atr=3.36)
        assert ts._state["TESTUSDT"]["sl_price"] == pytest.approx(
            103.6, abs=0.1)
        # price decays but stays above the band: SL never moves down
        before = ts._state["TESTUSDT"]["sl_price"]
        ts.update("TESTUSDT", current_price=104.0, atr=3.12)
        assert ts._state["TESTUSDT"]["sl_price"] >= before - 1e-9

    def test_kv_disabled_keeps_legacy_ladder(self, db):
        db.kv_set("trailing_phase2_config", {"enabled": False})
        ts = self._ts(db, state=self._activated_state(sl_price=105.0))
        r = ts.update("TESTUSDT", current_price=108.0, atr=3.24)
        assert r.get("phase2_switched") is not True
        # legacy step_5_10 at profit 8%: 108×0.98 = 105.84 > 105
        assert ts._state["TESTUSDT"]["sl_price"] == pytest.approx(
            105.84, abs=1e-2)


# ────────────────────────────────────────────────────────────
# E. trade_executor — single 50% TP1 with the R floor
# ────────────────────────────────────────────────────────────

class TestTieredSingleTp1:
    def _run(self, stop_loss_pct=5.0, tp_pcts=(6.0, 9.0, 12.0)):
        from src.trade_executor import _place_sl_tp_orders

        client = MagicMock()
        notifier = MagicMock()
        client.place_order.return_value = {"orderId": 1, "status": "NEW"}
        client.get_open_orders.return_value = []
        client.get_price_precision.return_value = 2
        client.get_klines.return_value = None  # ATR fetch fails → R floor only
        tp_levels = [{"pct": p, "size_pct": s} for p, s in
                     zip(tp_pcts, (50, 30, 20))]
        result = _place_sl_tp_orders(
            client, notifier, "TESTUSDT",
            executed_qty=100.0, price=100.0, p_prec=2,
            stop_loss_pct=stop_loss_pct, tp_levels=tp_levels,
            _step_size=1.0, _qty_decimals=0, _min_notional=5.0,
            strategy_size_multiplier=1.0,
        )
        return client, result

    def _tp_legs(self, client):
        legs = [c for c in client.place_order.call_args_list
                if c[0][2] == "LIMIT"]
        return legs

    def test_single_tp1_leg_at_50pct(self):
        client, result = self._run()
        legs = self._tp_legs(client)
        # ONE TP leg (no TP2), qty = 50% of 100
        assert len(legs) == 1
        assert legs[0][0][3] == 50.0
        assert result["tp_placed_qty"] == 50.0

    def test_sl_covers_remaining_50(self):
        client, result = self._run()
        sl_legs = [c for c in client.place_order.call_args_list
                   if c[0][2] in ("STOP_LOSS_LIMIT", "STOP_LOSS")]
        assert len(sl_legs) == 1
        assert sl_legs[0][0][3] == 50.0

    def test_tp1_price_floored_at_1_5r(self):
        # sl 5% → 1.5R = 7.5% floor beats the configured 6% TP1
        client, _ = self._run(stop_loss_pct=5.0, tp_pcts=(6.0, 9.0, 12.0))
        legs = self._tp_legs(client)
        assert legs[0][1]["price"] == pytest.approx(107.5, abs=0.01)

    def test_tp1_configured_above_floor_untouched(self):
        # sl 5% → floor 7.5%; configured TP1 12% stays 12%
        client, _ = self._run(stop_loss_pct=5.0, tp_pcts=(12.0, 15.0, 20.0))
        legs = self._tp_legs(client)
        assert legs[0][1]["price"] == pytest.approx(112.0, abs=0.01)


# ────────────────────────────────────────────────────────────
# F. _check_tp_fills — TP1 fill banks kv + lock-floor SL move
# ────────────────────────────────────────────────────────────

class TestTpFillBanks:
    def test_tp1_fill_writes_banked_and_moves_sl_to_lock(self, db):
        from src.cmd_trailing_check import _check_tp_fills
        from src.tp_sl_tracker import save_state

        save_state(
            "TESTUSDT", entry_price=100.0, total_qty=100.0,
            tp_orders=[{"order_id": 111, "price": 107.5, "qty": 50,
                        "tier": 1, "pct": 7.5}],
            sl_order={"order_id": 222, "price": 93.0,
                      "stop_price": 95.0, "qty": 50},
        )

        class Cli(FakeExitClient):
            def __init__(self):
                super().__init__(price=107.0)
                self.orders = []  # TP1 gone (filled)
                self.new_sl = None

            def get_price_precision(self, symbol):
                return 6

            def get_my_trades(self, symbol, limit=100, from_id=None):
                return [{"id": 1, "price": "107.5", "qty": "50",
                         "quoteQty": "5375", "commission": "0",
                         "commissionAsset": "USDT", "time": 1,
                         "isBuyer": False, "isMaker": False,
                         "orderId": 111, "symbol": symbol}]

            def place_order(self, symbol, side, otype, qty,
                            price=None, stop_price=None, **kw):
                self.new_sl = (otype, qty, price, stop_price)
                return {"orderId": 333, "status": "NEW"}

        cli = Cli()
        notifier = MagicMock()
        positions = [{"asset": "TEST", "symbol": "TESTUSDT",
                      "free": 50.0, "locked": 0.0, "total": 50.0}]
        results = _check_tp_fills(cli, notifier, positions)

        # banked kv written by the limit-TP1 fill detector
        banked = db.kv_get("tp1_banked:TESTUSDT")
        assert banked and banked.get("kind") == "limit_tp1"
        assert banked.get("qty") == 50
        # SL moved to the profit-lock floor entry×1.0025
        moved = [r for r in results if r.get("action") == "tp_fill_sl_moved"]
        assert moved
        assert cli.new_sl is not None
        otype, qty, _limit, stop = cli.new_sl
        assert otype == "STOP_LOSS_LIMIT"
        assert stop == pytest.approx(100.0 * 1.0025, abs=1e-2)

    def test_banked_state_blocks_second_partial_via_evaluate(self, db):
        # integration: kv banked → evaluate_exits waives full tp at +12%
        db.kv_set("tp_partial_config", {"enabled": True})
        db.kv_set("tp1_banked:TESTUSDT",
                  {"ts": time.time(), "kind": "limit_tp1", "qty": 50})
        db.portfolio_set("TESTUSDT", {
            "quantity": 50, "entry_price": 100.0,
            "strategy": "technical_v2",
            "opened_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        cli = FakeExitClient(price=112.5)
        decs = evaluate_exits(cli, db, now=time.time(),
                              include_momentum=False)
        kinds = [d["kind"] for d in decs if d["symbol"] == "TESTUSDT"]
        assert "take_profit" not in kinds
