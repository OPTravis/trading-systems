"""WO-1023 (10/9): single-source SL — DB stop_loss must equal the
exchange order chain's sl_price at entry.

Incident (10/9 12:22 PYTHUSDT, cron-scan.log L3477):
  sl_reconcile reported DB SL 0.079990 (fills avg ×0.95 — the portfolio
  config default 5%) vs exchange resting stop 0.077890 (GARCH normal
  band), dev 2.63%, dry-run. Root cause: _record_trade_portfolio called
  add_position WITHOUT stop_loss, so the new-position branch fell back
  to config["stop_loss"]["default_pct"]=5 while the order chain placed
  the ctx['stop_loss_pct'] band. Two write sources, one drift.

Fix under test:
  1. _place_sl_tp_orders exposes "sl_price" in its result dict;
  2. execute_auto_trade passes it into _record_trade_portfolio →
     add_position(stop_loss=...) — DB row lands on the SAME price the
     exchange legs carry (dev=0 for sl_reconcile);
  3. merge floors are monotonic: a fresh leg's stop can never lower a
     trailing-raised floor (max(old, new));
  4. sl_reconcile itself stays dry-run (docstring documents activation
     conditions; no behaviour change).

Regression matrix: GARCH low tier (-4.0%) and normal tier (-6.0%) buys
must leave DB SL == exchange stop == round(price × (1-pct/100), prec).
"""
import os
import sys
import tempfile
from unittest.mock import MagicMock

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from src.state_db import get_state_db
from src import trade_executor as te  # noqa: E402
from src.portfolio import PortfolioManager  # noqa: E402


class FakeExchange:
    """Surface for execute_auto_trade through the SL/TP chain."""

    def __init__(self, *, usdt=400.0, fill_px="1.0"):
        self.usdt = usdt
        self.fill_px = fill_px
        self.place_calls = []      # dicts of every SELL-side placement
        self.ocos = []
        self.bought = False

    def get_free_balance(self, asset="USDT"):
        return self.usdt

    def get_account(self):
        return {"balances": [
            {"asset": "USDT", "free": str(self.usdt), "locked": "0"},
            # fee-adjusted free lookup AFTER the buy = fills qty; before
            # the buy the base balance is clean (duplicate-entry guard)
            {"asset": "TST", "free": "100" if self.bought else "0",
             "locked": "0"},
        ]}

    def get_24hr_stats(self, symbol):
        return {"last_price": self.fill_px, "price_change_pct": "1.0"}

    def get_open_orders(self, symbol=None):
        return []

    def get_klines(self, symbol, interval="1h", limit=40):
        # flat candles at the fill price: no 3σ anomaly, no ATR tightening
        return [{"open": "1.0", "high": "1.0", "low": "1.0", "close": "1.0",
                 "volume": "100"}] * max(limit, 20)

    def get_symbol_filters(self, symbol):
        return {"minQty": 0.01, "minNotional": 5.0, "stepSize": 0.01,
                "tickSize": 0.0001, "quantityPrecision": 2}

    def get_price_precision(self, symbol):
        return 4

    def place_market_buy(self, symbol, quantity):
        self.bought = True
        return {"symbol": symbol, "orderId": 501, "status": "FILLED",
                "fills": [{"price": self.fill_px, "qty": "100",
                           "commission": "0"}]}

    def place_order(self, symbol, side, order_type, quantity,
                    price=None, stop_price=None, **kw):
        self.place_calls.append({"symbol": symbol, "side": side,
                                 "type": order_type, "qty": quantity,
                                 "price": price, "stop_price": stop_price})
        return {"symbol": symbol, "orderId": 601, "status": "NEW"}

    def place_oco(self, symbol, quantity, tp_price, sl_price, **kw):
        self.ocos.append({"symbol": symbol, "qty": quantity,
                          "tp": tp_price, "sl": sl_price})
        return {"orderId": 602}


def _stop_legs(cli):
    return [c for c in cli.place_calls if (c.get("stop_price") or 0) > 0]


@pytest.fixture
def db():
    d = tempfile.mkdtemp()
    os.environ["STATE_DB_PATH"] = os.path.join(d, "wo1023.db")
    os.environ["TESTING"] = "1"
    os.environ["DCA_CHECK_DISABLED"] = "1"
    yield get_state_db()
    os.environ["STATE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "n.db")


def _run_buy(monkeypatch, db, *, symbol, stop_loss_pct):
    cli = FakeExchange()
    monkeypatch.setattr(te, "get_trading_client", lambda: cli)
    monkeypatch.setattr(te, "FeishuNotifier", MagicMock)

    # risk gates: pass-through (unit scope = SL single-source, not risk)
    monkeypatch.setattr(
        te, "_pretrade_risk_checks",
        lambda client, bal: {"blocked": False, "reason": "",
                             "total_invested": 0.0,
                             "total_portfolio": bal,
                             "dl_multiplier": 1.0, "sd_multiplier": 1.0})

    kelly_mock = MagicMock()
    kelly_mock.get_position_size.return_value = {
        "position_pct": 0.25, "confidence": "high",
        "is_exploration": False, "win_rate": 0.55,
        "reward_risk": 1.5, "reason": "test"}
    kelly_mock.adjust_for_portfolio.side_effect = lambda r, **kw: r
    monkeypatch.setattr("src.kelly_sizer.KellyPositionSizer",
                        MagicMock(return_value=kelly_mock))
    fee_mock = MagicMock()
    fee_mock.get_effective_fees.return_value = {"taker_fee": 0.001}
    monkeypatch.setattr("src.fee_optimizer.FeeOptimizer",
                        MagicMock(return_value=fee_mock))

    res = te.execute_auto_trade(
        symbol=symbol, price=1.0, strategy="technical_v2",
        stop_loss_pct=stop_loss_pct,
        tp_levels=[{"pct": 8.0, "size_pct": 40},
                   {"pct": 12.0, "size_pct": 40},
                   {"pct": 20.0, "size_pct": 20}],
        stop_price=None, max_hold=48,
        signals={}, reason="wo1023-test", score=75,
    )
    return res, cli


class TestTwoTierSingleSource:
    """GARCH low (-4.0%) / normal (-6.0%) buys: DB SL == exchange leg."""

    def test_low_tier_4pct(self, monkeypatch, db):
        res, cli = _run_buy(monkeypatch, db, symbol="TSTUSDT",
                            stop_loss_pct=4.0)
        assert res.get("success") is True, res
        legs = _stop_legs(cli)
        assert legs, "no stop leg placed"
        ex_sl = max(c["stop_price"] for c in legs)
        assert ex_sl == pytest.approx(round(1.0 * 0.96, 4))   # -4.0%
        row = db.portfolio_get("TSTUSDT")
        assert row is not None
        assert row["stop_loss"] == pytest.approx(ex_sl)       # DB == exchange
        assert row["stop_loss"] != pytest.approx(1.0 * 0.95)  # not the 5% default

    def test_normal_tier_6pct(self, monkeypatch, db):
        res, cli = _run_buy(monkeypatch, db, symbol="TSTUSDT",
                            stop_loss_pct=6.0)
        assert res.get("success") is True, res
        legs = _stop_legs(cli)
        ex_sl = max(c["stop_price"] for c in legs)
        assert ex_sl == pytest.approx(round(1.0 * 0.94, 4))   # -6.0%
        row = db.portfolio_get("TSTUSDT")
        assert row["stop_loss"] == pytest.approx(ex_sl)
        # the incident shape: 0.95×entry must NOT appear anymore
        assert row["stop_loss"] != pytest.approx(round(1.0 * 0.95, 4))

    def test_sltp_result_exposes_sl_price(self, monkeypatch, db):
        """Source-level: the helper result carries the same-source stop
        (consumed by the record path even when legs fail downstream)."""
        cli = FakeExchange()
        r = te._place_sl_tp_orders(
            cli, MagicMock(), "TSTUSDT", 100.0, 1.0, 4,
            6.0, [{"pct": 8.0, "size_pct": 100}], 0.01, 2, 5.0, 1.0)
        assert r["sl_price"] == pytest.approx(0.94)


class TestMergeFloorMonotonic:
    """add_position merge: an explicit stop never LOWERS a raised floor."""

    def test_raised_floor_survives_lower_new_stop(self, monkeypatch, db):
        os.environ.setdefault("TESTING", "1")
        pm = PortfolioManager()
        pm.add_position("TSTUSDT", 10, 5.0, strategy="t",
                        stop_loss=4.6, _skip_validation=True,
                        deduct_cash=False)
        # fresh leg's GARCH stop sits BELOW the trailing-raised floor
        pm.add_position("TSTUSDT", 10, 5.2, strategy="t",
                        stop_loss=4.2, _skip_validation=True,
                        deduct_cash=False, on_conflict="merge")
        assert pm.positions["TSTUSDT"]["stop_loss"] == pytest.approx(4.6)

    def test_new_stop_applies_when_old_missing(self, monkeypatch, db):
        pm = PortfolioManager()
        pm.add_position("TSTUSDT", 10, 5.0, strategy="t",
                        _skip_validation=True, deduct_cash=False)
        pm.positions["TSTUSDT"].pop("stop_loss", None)
        pm.add_position("TSTUSDT", 10, 5.2, strategy="t",
                        stop_loss=4.2, _skip_validation=True,
                        deduct_cash=False, on_conflict="merge")
        assert pm.positions["TSTUSDT"]["stop_loss"] == pytest.approx(4.2)

    def test_no_explicit_stop_keeps_legacy_merge(self, monkeypatch, db):
        """Callers that pass nothing keep the exact legacy semantics
        (old floor, else config default) — switch/sync unaffected."""
        pm = PortfolioManager()
        pm.add_position("TSTUSDT", 10, 5.0, strategy="t",
                        stop_loss=4.6, _skip_validation=True,
                        deduct_cash=False)
        pm.add_position("TSTUSDT", 10, 5.2, strategy="t",
                        _skip_validation=True, deduct_cash=False,
                        on_conflict="merge")
        assert pm.positions["TSTUSDT"]["stop_loss"] == pytest.approx(4.6)


class TestSourcePins:
    def test_record_signature_and_call_site(self):
        """Pins: record helper takes stop_loss; call site passes the
        order-chain sl_price with a same-formula fallback."""
        from pathlib import Path
        text = Path(os.path.join(REPO, "src", "trade_executor.py")
                    ).read_text(encoding="utf-8")
        assert "stop_loss=stop_loss,  # WO-1023" in text
        assert '(sltp_result or {}).get("sl_price")' in text
        assert "stop_loss=_sl_price," in text

    def test_sl_reconcile_stays_dry_run(self):
        """sl_reconcile default unchanged + activation conditions
        documented (WO-1023 requirement ③)."""
        from pathlib import Path
        text = Path(os.path.join(REPO, "src", "cmd_trailing_check.py")
                    ).read_text(encoding="utf-8")
        assert "cfg_get('SL_RECONCILE_DRYRUN', '1')" in text
        assert "WO-1023 (10/9): the PRIMARY fix is upstream" in text
        assert "do NOT flip earlier" in text
