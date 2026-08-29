"""bug#29 (2026-08-29): minNotional stranded position.

Timeline anchor — ENSO:
  19:15 switch bought 5.65 ENSO @ 0.887 = $5.01 (no buffer over $5)
  19:30 price slid to 0.837; notional $4.73 < $5
  ensure_tp_sl SL placement rejected twice (notional < 5)
  market sell ALSO rejected (notional < 5) — position stranded while the
  stop was already breached. Manual discipline exit at 0.901 (+$0.08).

Root causes:
  1. Binance validates STOP_LOSS_LIMIT notional at the LIMIT price
     (stop × 0.995), not current price.
  2. Market buys are also subject to the $5 minNotional, so a sub-$5
     position cannot be topped up cheaply either.
  3. The switch buy leg had no floor: $5.01 landed in the [$5, $6) dead zone.

Two fixes tested here:
  F1: position_optimizer._execute_switch bumps the buy leg to a $6 floor
      (mirrors trade_executor's internal floor) so entries never land in
      the dead zone — and fee erosion below $5 no longer rejects the leg.
  F2: ensure_tp_sl._discipline_exit market-exits a breached-but-unprotected
      position while the exit window is open, with verified bookkeeping;
      returns True on critical bookkeeping failure.
"""
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────── F1: switch buy leg floor ───────────────────────────


def _make_decision(from_value):
    return {
        "from_symbol": "AAAUSDT",
        "to_symbol": "BBBUSDT",
        "from_value": from_value,
        "from_price": 0.887,
        "to_price": 1.0,
        "executed": False,
    }


def _run_switch(from_value, usdt_balance):
    from src.position_optimizer import PositionOptimizer

    with patch.object(
        PositionOptimizer, "_load_switch_times", lambda self: None
    ), patch.object(PositionOptimizer, "_save_switch_times", lambda self: None):
        opt = PositionOptimizer(MagicMock(), MagicMock(), MagicMock())

    opt.bc.get_symbol_filters.return_value = {
        "minQty": 0.01,
        "minNotional": 5.0,
        "stepSize": 0.01,
    }
    opt.bc.get_ticker_price.return_value = 0.887
    opt.bc.cancel_all_orders.return_value = []
    opt.bc.get_account.return_value = {
        "balances": [{"asset": "AAA", "free": "5.65"}]
    }
    opt.bc.place_market_sell.return_value = {"orderId": 111, "status": "FILLED"}
    opt.bc.get_free_balance.return_value = usdt_balance
    opt.bc.place_market_buy.return_value = {"orderId": 222, "status": "FILLED"}
    opt.portfolio.get_all_positions.return_value = [
        {"symbol": "AAAUSDT", "quantity": 5.65}
    ]

    with patch("src.state_db.get_state_db", return_value=MagicMock()):
        result = opt._execute_switch(_make_decision(from_value))
    return opt, result


def test_switch_buy_leg_bumped_to_six_dollar_floor():
    """$5.01 entry value with ample USDT → buy leg bumps to $6.00."""
    opt, result = _run_switch(from_value=5.01, usdt_balance=50.0)
    assert result is True
    kwargs = opt.bc.place_market_buy.call_args.kwargs
    assert kwargs["symbol"] == "BBBUSDT"
    assert kwargs["quantity"] == pytest.approx(6.0, abs=1e-9)


def test_switch_buy_leg_clamped_to_available_balance():
    """USDT balance equals proceeds ($5.01 pre-fee → $4.98): bump clamps to
    balance instead of leaving the leg below the exchange minimum.
    (stepSize floor may shave 5.01 → 5.00 via float noise; either value is
    exchange-valid.)"""
    opt, result = _run_switch(from_value=5.01, usdt_balance=5.01)
    assert result is True
    kwargs = opt.bc.place_market_buy.call_args.kwargs
    assert 5.0 <= kwargs["quantity"] <= 5.01


def test_switch_buy_leg_untouched_above_floor():
    """$7.00 entry value → no bump (6.965 post-fee ≥ $6 floor)."""
    opt, result = _run_switch(from_value=7.0, usdt_balance=50.0)
    assert result is True
    kwargs = opt.bc.place_market_buy.call_args.kwargs
    assert kwargs["quantity"] == pytest.approx(6.96, abs=1e-9)  # step-floored


# ─────────────────────────── F2: discipline exit ───────────────────────────


def _run_discipline_exit(place_market_sell_result):
    import scripts.ensure_tp_sl as ets

    client = MagicMock()
    client.place_market_sell.return_value = place_market_sell_result
    pos = {"quantity": 5.65, "entry_price": 0.887, "stop_loss": 0.84265}
    fixes, errors = [], []

    def _run_writes(db, write_fn, verify_fn, label, attempts, backoff_sec):
        write_fn()  # actually build & run the bookkeeping closures
        return True

    with patch.object(
        ets, "get_symbol_filters", return_value={"stepSize": 0.01, "minNotional": 5.0}
    ), patch.object(ets, "get_state_db", return_value=MagicMock()), patch.object(
        ets, "db_write_with_verify", side_effect=_run_writes
    ), patch.object(
        ets, "insert_sell_dedup", return_value=True
    ), patch.object(ets, "TradeOutcomeRecorder") as rec:
        critical = ets._discipline_exit(
            client, "ENSOUSDT", pos, 0.901, "sl_breach_exit", fixes, errors
        )
    return critical, fixes, errors, rec


def test_discipline_exit_sells_all_and_books_outcome():
    """Window open (notional $5.09 ≥ $5): full market sell + clean bookkeeping."""
    critical, fixes, errors, rec = _run_discipline_exit(
        {"orderId": 1, "status": "FILLED"}
    )
    assert critical is False
    assert errors == []
    assert len(fixes) == 1
    assert "紀律市價平倉" in fixes[0]
    assert rec.return_value.record_outcome.call_args.kwargs["exit_reason"] == (
        "sl_breach_exit"
    )


def test_discipline_exit_order_rejected_is_critical():
    """Exchange rejects the market sell (window closed): loud error, critical."""
    critical, fixes, errors, _ = _run_discipline_exit(None)
    assert critical is True
    assert fixes == []
    assert len(errors) == 1


def test_discipline_exit_order_unfilled_is_critical():
    """Market order accepted but not FILLED: loud error, critical."""
    critical, fixes, errors, _ = _run_discipline_exit(
        {"orderId": 2, "status": "NEW"}
    )
    assert critical is True
    assert fixes == []
    assert len(errors) == 1
