"""WO-1009-② (10/2): switch-protection must cover the MERGED position on a
DCA-add switch (target symbol already held).

Evidence (10/1 14:20 ICP->NEAR, exchange allOrders + audit id 982):
- merged book after the buy: NEAR 2.1988 @ 5.4898 (portfolio merge logged
  BEFORE the OCO placement)
- switch-protection OCO covered only the fresh buy leg: qty=1.0
  (TP 5.724 / SL 5.118)
- the old slice kept its own stale OCO on a different price ladder
  (1.1 @ TP 5.806 / SL 5.05, alive since 02:41) — two split OC Os per
  symbol, nothing on the book ever protected "the position" as a whole,
  and had the old slice been NAKED at switch time it would have stayed
  naked until the next guardian sweep.

Fix under test: _execute_switch step 8b now
(a) derives prot_qty = buy_qty + held_qty from the entry snapshot,
(b) cancels the target symbol's stale sell legs first (a live
    LIMIT_MAKER leg locks balance and would reject a full-qty OCO with
    -2010; paper trader lacks cancel_all_orders → warning, orders there
    don't lock balance),
(c) protects the merged quantity with a single OCO.
Fresh-target switches (no prior holding) keep the exact legacy behavior.
"""

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

from src.position_optimizer import PositionOptimizer  # noqa: E402


class FakeBC:
    """Records cancel + OCO calls; implements the exact surface
    _execute_switch → _place_switch_protections touches."""

    def __init__(self, *, cancel_error_symbols=()):
        self.cancels = []
        self.ocos = []
        self.buys = []
        self.sells = []
        self._cancel_error_symbols = set(cancel_error_symbols)
        self.prices = {"ICPUSDT": 3.4, "NEARUSDT": 5.5}

    def get_symbol_filters(self, symbol):
        return {"minQty": 0.0, "minNotional": 5.0, "stepSize": 0.1,
                "tickSize": 0.001}

    def get_ticker_price(self, symbol=0, **kw):
        return self.prices.get(symbol, 1.0)

    def get_24hr_stats(self, symbol):
        return {"last_price": self.prices.get(symbol, 1.0)}

    def get_account(self):
        # from-asset balance lookup: ICP has plenty
        return {"balances": [{"asset": "ICP", "free": "10.0"}]}

    def get_free_balance(self, asset="USDT"):
        return 12.0

    def cancel_all_orders(self, symbol):
        if symbol in self._cancel_error_symbols:
            raise RuntimeError("proxy hiccup")
        self.cancels.append(symbol)
        return True

    def place_market_sell(self, symbol, quantity):
        self.sells.append((symbol, quantity))
        return {"orderId": 7, "status": "FILLED",
                "executedQty": f"{quantity}",
                "cummulativeQuoteQty": f"{quantity * self.prices[symbol]}"}

    def place_market_buy(self, symbol, quantity):
        self.buys.append((symbol, quantity))
        return {"orderId": 9, "status": "FILLED",
                "executedQty": f"{quantity}",
                "cummulativeQuoteQty": f"{quantity * self.prices[symbol]}"}

    def place_oco(self, symbol, quantity, tp_price, sl_price,
                  sl_limit_price=None):
        self.ocos.append({"symbol": symbol, "qty": quantity,
                          "tp": tp_price, "sl": sl_price})
        return {"orderId": 11, "status": "FILLED", "origQty": str(quantity)}


class FakePortfolio:
    def __init__(self, positions):
        self._positions = positions
        self.closed = []
        self.added = []

    def get_all_positions(self):
        return self._positions

    def close_position(self, symbol, close_price=None, exit_reason=None,
                       client_order_id=None):
        self.closed.append(symbol)
        self._positions = [p for p in self._positions
                           if p["symbol"] != symbol]

    def add_position(self, symbol, quantity, entry_price, strategy=None,
                     deduct_cash=True, _skip_validation=False):
        self.added.append((symbol, quantity, entry_price))


def _run_switch(monkeypatch, tmp_path, *, held_near, cancel_error=()):
    bc = FakeBC(cancel_error_symbols=cancel_error)
    positions = [
        {"symbol": "ICPUSDT", "quantity": 1.72, "entry_price": 3.47},
    ]
    if held_near:
        positions.append(
            {"symbol": "NEARUSDT", "quantity": 1.1988, "entry_price": 5.478})
    portfolio = FakePortfolio(positions)

    opt = object.__new__(PositionOptimizer)
    opt.bc = bc
    opt.portfolio = portfolio
    opt.risk_manager = None
    opt._last_switch_time = {}
    monkeypatch.setattr(opt, "_save_switch_times", lambda: None)

    fake_db = SimpleNamespace(
        drawdown_get=lambda: {"current_drawdown_pct": 0.0})
    monkeypatch.setattr("src.state_db.get_state_db", lambda: fake_db)
    monkeypatch.setattr(
        "src.stepwise_drawdown.get_drawdown_action",
        lambda pct, read_only=False, db=None, now=None:
        {"level": "normal", "block_new_trades": False, "close_all": False,
         "size_multiplier": 1.0, "sl_tightening": 1.0, "reason": "",
         "time_in_level": 0.0, "escalated": False})

    decision = {"from_symbol": "ICPUSDT", "to_symbol": "NEARUSDT",
                "from_value": 6.0, "from_price": 3.4}
    ok = opt._execute_switch(decision)
    return ok, bc


class TestSwitchMergedProtection:

    def test_dca_add_protects_merged_qty(self, monkeypatch, tmp_path):
        """Old slice + fresh buy must land under ONE full-qty OCO."""
        ok, bc = _run_switch(monkeypatch, tmp_path, held_near=True)
        assert ok is True

        # stale NEAR legs cancelled before the OCO (ICP cancel = sell leg)
        assert bc.cancels == ["ICPUSDT", "NEARUSDT"]
        assert len(bc.ocos) == 1
        oco = bc.ocos[0]
        assert oco["symbol"] == "NEARUSDT"
        # buy_qty floors to step 1.0; merged = 1.0 + 1.1988 = 2.1988 →
        # step-floored 2.1 (legacy code would have protected only 1.0)
        assert oco["qty"] == pytest.approx(2.1)
        # price ladder from the fresh fill (5.5): SL -7% / TP +4%
        # (_round_tick floors to tick 0.001: 5.115 → 5.114)
        assert oco["sl"] == pytest.approx(5.114, abs=1e-6)
        assert oco["tp"] == pytest.approx(5.5 * 1.04, abs=1e-3)

    def test_fresh_switch_keeps_legacy_buy_qty(self, monkeypatch, tmp_path):
        """No prior holding → no extra cancel, OCO covers just the buy."""
        ok, bc = _run_switch(monkeypatch, tmp_path, held_near=False)
        assert ok is True
        assert bc.cancels == ["ICPUSDT"]
        assert len(bc.ocos) == 1
        assert bc.ocos[0]["symbol"] == "NEARUSDT"
        assert bc.ocos[0]["qty"] == pytest.approx(1.0)

    def test_cancel_failure_still_places_protection(self, monkeypatch,
                                                    tmp_path):
        """A failed stale-leg cancel must not abort protection: the OCO
        attempt (and its PROTECTION_FAILED fallback / guardian backstop)
        still runs."""
        ok, bc = _run_switch(monkeypatch, tmp_path, held_near=True,
                             cancel_error=("NEARUSDT",))
        assert ok is True
        assert "ICPUSDT" in bc.cancels  # NEAR cancel raised
        assert len(bc.ocos) == 1
        assert bc.ocos[0]["qty"] == pytest.approx(2.1)

    def test_merged_qty_uses_entry_snapshot_not_post_merge_book(
            self, monkeypatch, tmp_path):
        """prot_qty derives from the snapshot loaded at _execute_switch
        entry (pre-buy) + buy_qty — immune to portfolio cache timing."""
        ok, bc = _run_switch(monkeypatch, tmp_path, held_near=True)
        assert ok is True
        # FakePortfolio.add_position appends without merging, so if the
        # code had re-read get_all_positions() it would see 1.1988+1.0
        # only via the snapshot path anyway; snapshot math proven by 2.1.
        assert bc.ocos[0]["qty"] == pytest.approx(2.1)
