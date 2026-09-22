"""WO-0922-017-iv review hardening: stuck_order_monitor._is_protective.

Production incident (9/22 x3): the OCO-skip checked order.get('listId')
but Binance openOrders legs carry 'orderListId' — LIMIT_MAKER TP legs
fell through as plain entries and got cancelled, and OCO atomicity
stripped the SL leg with them.

This suite pins the hotfix contract AND the review findings:

Review point conclusions (evidence: grep across src/, 2026-09-22):
  P1 orderListId>0 robustness — int(order.get('orderListId') or 0) inside
     try/except (TypeError, ValueError): int OK, None -> 0, '-1' -> -1,
     'abc' -> ValueError caught, '' falsy -> 0. Robust for int/str/None.
  P2 LIMIT_MAKER in PROTECTIVE_TYPES — repo entry orders are MARKET
     (trade_executor.place_market_buy, circuit_tiers, dust_reaper) or
     TWAP plain-LIMIT slices (twap_vwap._place_limit_slice ->
     place_limit_buy/sell). NO code path places LIMIT_MAKER entries —
     the only LIMIT_MAKER producers are OCO aboveType legs
     (_binance_sdk_client new_oco_order) which are protective by
     definition. Adding LIMIT_MAKER therefore has zero entry-side
     false-protection surface.
  P3 cat_ prefix — NOT guardian-only: both exchange wrappers stamp
     clientOrderId f'cat_...' on EVERY order (ccxt_client.py /
     _binance_sdk_client.py place_order). Live false-positive population
     under the unscoped check: TWAP entry slices (plain LIMIT BUY,
     trade_executor uses execute_twap for any buy >= $100) — the exact
     population the monitor exists to police. Minimal fix under test
     here: scope cat_ to non-BUY side (SPOT: resting LIMIT SELLs are
     always protective exits — plain TP sells / guardian heals; LIMIT
     BUYs are entries and stay monitorable; unknown side stays
     protected, fail-safe against catastrophic cancels).

No network, no real orders — pure dict fixtures, SPOT ONLY semantics.
"""

import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src import stuck_order_monitor as som  # noqa: E402


# ── fixtures / doubles ────────────────────────────────────────────────────────

@pytest.fixture()
def _no_live_alerts(monkeypatch):
    emitted = []
    monkeypatch.setattr("src.live_alerts.emit",
                        lambda et, sym=None, d=None: emitted.append(
                            {"event_type": et, "symbol": sym,
                             "details": d}) or True)
    yield emitted


class OrderClient:
    def __init__(self, orders):
        self.orders = orders
        self.cancelled = []

    def get_open_orders(self, symbol=None):
        return self.orders

    def cancel_order(self, symbol, order_id):
        self.cancelled.append((symbol, order_id))
        return {"orderId": order_id, "status": "CANCELED"}


def _old(minutes=20):
    """ms epoch `minutes` in the past (> 15min STUCK_TIMEOUT_S)."""
    return (time.time() - minutes * 60) * 1000


# ── P0: OCO legs (the production incident) ────────────────────────────────────

def test_oco_tp_leg_limit_maker_protective():
    """OCO TP leg: type=LIMIT_MAKER + orderListId>0. This is the exact
    shape stripped in the 9/22 incidents."""
    leg = {"symbol": "TRUMPUSDT", "orderId": 101, "orderListId": 777,
           "status": "NEW", "type": "LIMIT_MAKER", "side": "SELL",
           "price": "12.5", "origQty": "10"}
    assert som._is_protective(leg) is True


def test_oco_sl_leg_stop_loss_limit_protective():
    leg = {"symbol": "NEARUSDT", "orderId": 102, "orderListId": 778,
           "status": "NEW", "type": "STOP_LOSS_LIMIT", "side": "SELL",
           "price": "2.1", "stopPrice": "2.12", "origQty": "50"}
    assert som._is_protective(leg) is True


def test_oco_leg_plain_limit_type_protective_via_list_id():
    """Field path must be type-independent: even if a leg reports a plain
    LIMIT type, orderListId>0 marks it as OCO."""
    leg = {"symbol": "ZECUSDT", "orderId": 103, "orderListId": 779,
           "status": "NEW", "type": "LIMIT", "side": "SELL",
           "price": "50", "origQty": "2"}
    assert som._is_protective(leg) is True


def test_oco_leg_string_list_id_protective():
    """Wrapper variants may deliver orderListId as string."""
    leg = {"symbol": "BTCUSDT", "orderId": 104, "orderListId": "780",
           "status": "NEW", "type": "LIMIT_MAKER", "side": "SELL"}
    assert som._is_protective(leg) is True


def test_oco_leg_float_list_id_protective():
    leg = {"symbol": "BTCUSDT", "orderId": 105, "orderListId": 781.0,
           "status": "NEW", "type": "LIMIT", "side": "SELL"}
    assert som._is_protective(leg) is True


# ── contingencyType / listStatusType paths ────────────────────────────────────

def test_contingency_type_oco_protective():
    order = {"symbol": "FILUSDT", "orderId": 110, "contingencyType": "OCO",
             "status": "NEW", "type": "LIMIT", "side": "SELL"}
    assert som._is_protective(order) is True


def test_list_status_type_protective():
    order = {"symbol": "FILUSDT", "orderId": 111,
             "listStatusType": "EXECUTING",
             "status": "NEW", "type": "LIMIT", "side": "SELL"}
    assert som._is_protective(order) is True


# ── legacy field compatibility (pre-hotfix shapes) ───────────────────────────

def test_legacy_list_id_still_protective():
    """test_p02_defense pins listId=... dicts; legacy path must survive."""
    order = {"symbol": "FILUSDT", "orderId": 120, "listId": 555,
             "status": "NEW", "type": "LIMIT", "side": "SELL"}
    assert som._is_protective(order) is True


def test_orig_client_order_id_protective():
    order = {"symbol": "FILUSDT", "orderId": 121,
             "origClientOrderId": "oco_FIL_1",
             "status": "NEW", "type": "LIMIT", "side": "SELL"}
    assert som._is_protective(order) is True


def test_list_client_order_id_protective():
    order = {"symbol": "FILUSDT", "orderId": 122,
             "listClientOrderId": "oco_FIL_LIST",
             "status": "NEW", "type": "LIMIT", "side": "SELL"}
    assert som._is_protective(order) is True


# ── guardian-healed / plain TP sells: cat_ + SELL ─────────────────────────────

def test_guardian_heal_cat_sell_limit_protective():
    """Guardian heal + trade_executor plain TP sells: LIMIT SELL, universal
    cat_ prefix, orderListId=-1. Protected via scoped cat_ rule."""
    order = {"symbol": "FETUSDT", "orderId": 130, "orderListId": -1,
             "status": "NEW", "type": "LIMIT", "side": "SELL",
             "clientOrderId": "cat_FETUSDT_SELL_17000000_ab12cd",
             "price": "1.5", "origQty": "500"}
    assert som._is_protective(order) is True


def test_cat_prefix_side_missing_stays_protective():
    """Fail-safe: without a side we cannot prove it is an entry — protect
    (avoids re-opening the TP-strip incident class)."""
    order = {"symbol": "FETUSDT", "orderId": 131, "orderListId": -1,
             "status": "NEW", "type": "LIMIT",
             "clientOrderId": "cat_FETUSDT_SELL_17000001_cd34ef"}
    assert som._is_protective(order) is True


def test_cat_prefix_lowercase_sell_protective():
    order = {"symbol": "FETUSDT", "orderId": 132, "orderListId": -1,
             "status": "NEW", "type": "LIMIT", "side": "sell",
             "clientOrderId": "cat_FETUSDT_sell_17000002_ef56ab"}
    assert som._is_protective(order) is True


# ── plain entries MUST stay monitorable (not protective) ─────────────────────

def test_plain_entry_limit_buy_monitorable():
    """Binance standalone orders carry orderListId == -1: monitorable."""
    order = {"symbol": "ZECUSDT", "orderId": 140, "orderListId": -1,
             "status": "NEW", "type": "LIMIT", "side": "BUY",
             "clientOrderId": "manual_x1", "price": "50", "origQty": "2"}
    assert som._is_protective(order) is False


def test_twap_entry_slice_cat_buy_monitorable():
    """REGRESSION (review P3): TWAP slices are plain LIMIT BUYs carrying
    the universal cat_ prefix — the unscoped cat_ check classified them
    protective and neutered the monitor for its documented target."""
    slice_order = {"symbol": "BTCUSDT", "orderId": 141, "orderListId": -1,
                   "status": "NEW", "type": "LIMIT", "side": "BUY",
                   "clientOrderId": "cat_BTCUSDT_BUY_17000003_123456",
                   "price": "50100.5", "origQty": "0.01"}
    assert som._is_protective(slice_order) is False


def test_entry_without_order_list_id_and_client_id_monitorable():
    order = {"symbol": "INJUSDT", "orderId": 142, "status": "NEW",
             "type": "LIMIT", "side": "BUY", "price": "20", "origQty": "5"}
    assert som._is_protective(order) is False


def test_market_order_monitorable():
    order = {"symbol": "INJUSDT", "orderId": 143, "orderListId": -1,
             "status": "NEW", "type": "MARKET", "side": "BUY"}
    assert som._is_protective(order) is False


def test_stop_loss_typed_buy_also_protective():
    """Type substrings win regardless of side — a BUY-typed protective
    variant (if ever produced) stays protected."""
    order = {"symbol": "BTCUSDT", "orderId": 144, "orderListId": -1,
             "status": "NEW", "type": "STOP_LOSS_LIMIT", "side": "BUY"}
    assert som._is_protective(order) is True


# ── orderListId boundary values ──────────────────────────────────────────────

@pytest.mark.parametrize("raw", [None, 0, -1, "0", "-1", "", "abc"])
def test_order_list_id_non_positive_never_protective_by_itself(raw):
    """orderListId None/0/-1/garbage must not mark protection; a plain
    order shape with such a value stays monitorable. 'abc' exercises the
    ValueError swallow (no crash, falls through)."""
    order = {"symbol": "BTCUSDT", "orderId": 150, "status": "NEW",
             "type": "LIMIT", "side": "BUY", "orderListId": raw}
    assert som._is_protective(order) is False


def test_empty_order_not_protective():
    assert som._is_protective({}) is False


# ── run()-level: protective skipped vs entry cancelled ────────────────────────

def test_run_cancels_stuck_twap_slice_but_spares_tp_and_oco(_no_live_alerts):
    """One round, production population: an old unfilled TWAP entry slice
    (cat_ LIMIT BUY) is cancelled; the position's OCO legs and the
    guardian plain-TP sell (cat_ LIMIT SELL) are skipped."""
    orders = [
        {"symbol": "BTCUSDT", "orderId": 201, "orderListId": -1,
         "status": "NEW", "type": "LIMIT", "side": "BUY",
         "clientOrderId": "cat_BTCUSDT_BUY_17000009_abc123",
         "price": "50100", "origQty": "0.01", "time": _old(30)},
        {"symbol": "BTCUSDT", "orderId": 202, "orderListId": 900,
         "status": "NEW", "type": "LIMIT_MAKER", "side": "SELL",
         "price": "52000", "origQty": "0.02", "time": _old(30)},
        {"symbol": "BTCUSDT", "orderId": 203, "orderListId": 900,
         "status": "NEW", "type": "STOP_LOSS_LIMIT", "side": "SELL",
         "price": "48000", "stopPrice": "48100", "origQty": "0.02",
         "time": _old(30)},
        {"symbol": "FETUSDT", "orderId": 204, "orderListId": -1,
         "status": "NEW", "type": "LIMIT", "side": "SELL",
         "clientOrderId": "cat_FETUSDT_SELL_17000010_def456",
         "price": "1.5", "origQty": "500", "time": _old(30)},
    ]
    client = OrderClient(orders)
    res = som.run(client)
    assert res["checked"] == 4
    assert res["protective_skipped"] == 3
    assert res["stuck"] == 1 and res["cancelled"] == 1
    assert client.cancelled == [("BTCUSDT", 201)]
    assert any(e["event_type"] == "STUCK_ORDER" for e in _no_live_alerts)


def test_run_fresh_twap_slice_untouched(_no_live_alerts):
    client = OrderClient([
        {"symbol": "BTCUSDT", "orderId": 210, "orderListId": -1,
         "status": "NEW", "type": "LIMIT", "side": "BUY",
         "clientOrderId": "cat_BTCUSDT_BUY_17000011_456789",
         "time": _old(5)},
    ])
    res = som.run(client)
    assert res["stuck"] == 0 and client.cancelled == []


# ── review P2 pin: independent LIMIT_MAKER blanket ───────────────────────────

def test_independent_limit_maker_sell_protective():
    """Review P2 (fused from the 0922c parallel review): LIMIT_MAKER sits
    in PROTECTIVE_TYPES unconditionally, so a STANDALONE LIMIT_MAKER SELL
    (orderListId == -1, no cat_ marker) is also unfalsifiably protective.
    Acceptable per P2 grep evidence: the only LIMIT_MAKER producers are
    OCO aboveType legs; no repo path places standalone LIMIT_MAKER — zero
    false-protection surface today. This pins the contract so a future
    entry path adopting LIMIT_MAKER trips here first."""
    order = {"symbol": "TRUMPUSDT", "orderId": 160, "orderListId": -1,
             "status": "NEW", "type": "LIMIT_MAKER", "side": "SELL",
             "price": "12.5", "origQty": "10"}
    assert som._is_protective(order) is True
