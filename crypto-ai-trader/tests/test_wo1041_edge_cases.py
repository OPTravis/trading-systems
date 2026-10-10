"""WO-1041 T2: boundary & fault-injection tests for the paper channel.

Covers the approved matrix:
  - extreme large orders (over balance)
  - extreme small orders (below exchange minimums)
  - balance exactly equal to cost / fee boundary
  - rapid repeat cancels + cancel of nonexistent orders (idempotency)
  - network interruption & recovery (mocked feed outage)
  - malformed input injection (NaN / None / negative / zero / bad symbol)
  - cross-module error containment: a failed commit leaves ZERO dirty
    state (atomic rollback); a failed ledger write never blocks the fill

Every rejection case asserts BOTH the refusal AND snapshot equality of
paper state (balance / positions / pnl / counter) plus zero new ledger
rows — a rejected order must not leave any trace.
"""
import json
import math
from unittest import mock

import pytest


# ── helpers ────────────────────────────────────────────────────────────

def _snap(pt):
    """Full paper-state snapshot for dirty-write detection."""
    db = pt._get_db()
    return {
        "balance": pt._get_sim_balance(),
        "positions": pt._get_sim_positions(),
        "pnl": pt._get_sim_pnl(),
        "counter": pt._get_sim_order_counter(),
        "trades": db._get_conn().execute(
            "SELECT COUNT(*) FROM trades").fetchone()[0],
        "paper_trades": db._get_conn().execute(
            "SELECT COUNT(*) FROM paper_trades").fetchone()[0],
    }


@pytest.fixture
def pt(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    from src.paper_trader import PaperTrader, PAPER_MIN_ORDER_USDT
    monkeypatch.setattr(PaperTrader, "get_current_price",
                        lambda self, s: 100.0)
    monkeypatch.setattr(PaperTrader, "get_klines",
                        lambda self, s, i="1h", limit=500, **kw: [])
    trader = PaperTrader()
    trader.min_order = PAPER_MIN_ORDER_USDT
    return trader


# ── A/B: extreme sizes ────────────────────────────────────────────────

def test_t2_large_order_over_balance(pt):
    """BUY larger than balance+fee is refused with zero side effects."""
    before = _snap(pt)
    # balance 10000 @100 → 101 units costs 10100+fee > balance
    r = pt.place_market_buy("BTCUSDT", 101.0)
    assert r is None
    assert _snap(pt) == before


def test_t2_small_order_below_exchange_min(pt):
    """Notional below the paper minimum is refused with zero side effects."""
    before = _snap(pt)
    r = pt.place_market_buy("BTCUSDT", 0.05)  # $5 < $10 min
    assert r is None
    assert _snap(pt) == before


def test_t2_sell_more_than_held(pt):
    """SELL exceeding held qty is refused (small float tolerance kept)."""
    assert pt.place_market_buy("BTCUSDT", 0.5) is not None  # $50
    before = _snap(pt)
    r = pt.place_market_sell("BTCUSDT", 0.6)  # holds 0.5
    assert r is None
    assert _snap(pt) == before


# ── C: balance == cost boundary ───────────────────────────────────────

def test_t2_balance_exactly_covers_cost(pt):
    """balance == notional + fee (to the cent) succeeds and lands at ~0."""
    price = 100.0
    # market BUY fills at price*(1+slip 0.05%); fee = notional*0.075%
    # solve qty so total_cost == balance exactly
    from src.paper_trader import PAPER_SLIPPAGE_PCT, PAPER_FEE_RATE
    fill = price * (1 + PAPER_SLIPPAGE_PCT / 100)
    fee_rate = PAPER_FEE_RATE
    # total = q*fill*(1+fee_rate) = 10000 → q = 10000/(fill*(1+fee_rate))
    q = 10000.0 / (fill * (1 + fee_rate))
    pt._set_sim_balance(10000.0)
    r = pt.place_market_buy("BTCUSDT", q)
    assert r is not None, "exact-balance order should fill"
    assert pt._get_sim_balance() == pytest.approx(0.0, abs=1e-6)


def test_t2_balance_one_dust_short(pt):
    """balance a hair below cost is refused — no partial/dirty fill."""
    from src.paper_trader import PAPER_SLIPPAGE_PCT, PAPER_FEE_RATE
    fill = 100.0 * (1 + PAPER_SLIPPAGE_PCT / 100)
    q = 100.0 / (fill * (1 + PAPER_FEE_RATE))  # needs exactly 100 USDT
    pt._set_sim_balance(100.0 - 1e-6)
    before = _snap(pt)
    r = pt.place_market_buy("BTCUSDT", q)
    assert r is None
    assert _snap(pt) == before


# ── D: rapid cancels ──────────────────────────────────────────────────

def test_t2_cancel_repeat_idempotent(pt):
    """Cancelling the same order 3x: first wins, rest are clean no-ops."""
    o = pt.place_limit_sell("BTCUSDT", 0.5, 150.0)
    assert o is not None
    oid = o["orderId"]
    assert pt.cancel_order("BTCUSDT", oid) is not None
    for _ in range(2):
        assert pt.cancel_order("BTCUSDT", oid) is None  # idempotent no-op
    assert pt.get_open_orders("BTCUSDT") == []


def test_t2_cancel_nonexistent(pt):
    """Cancelling an unknown order id never raises."""
    assert pt.cancel_order("BTCUSDT", "999999") is None
    assert pt.cancel_order("BTCUSDT", 999999) is None


def test_t2_rapid_place_cancel_cycle(pt):
    """50 place/cancel cycles: state stays consistent throughout."""
    assert pt.place_market_buy("BTCUSDT", 0.5) is not None
    for i in range(50):
        o = pt.place_limit_sell("BTCUSDT", 0.1, 150.0)
        assert o is not None
        assert pt.cancel_order("BTCUSDT", o["orderId"]) is not None
    # all limit residue cancelled; only the market BUY remains in ledger
    assert pt.get_open_orders("BTCUSDT") == []
    n = pt._get_db()._get_conn().execute(
        "SELECT COUNT(*) FROM paper_trades WHERE side='SELL'").fetchone()[0]
    assert n == 0, "cancelled orders must never fill"


# ── E: network interruption ───────────────────────────────────────────

def test_t2_feed_outage_then_recovery(pt, monkeypatch):
    """Price feed outage: orders fail cleanly, state intact; after
    recovery the same order fills normally."""
    assert pt.place_market_buy("BTCUSDT", 0.5) is not None
    before = _snap(pt)

    # outage: feed raises (network down)
    from src.paper_trader import PaperTrader
    def _down(self, s):
        raise ConnectionError("feed down")
    monkeypatch.setattr(PaperTrader, "get_current_price", _down)
    assert pt.place_market_sell("BTCUSDT", 0.5) is None
    assert _snap(pt) == before, "outage must not mutate any state"

    # recovery
    monkeypatch.setattr(PaperTrader, "get_current_price",
                        lambda self, s: 105.0)
    r = pt.place_market_sell("BTCUSDT", 0.5)
    assert r is not None
    assert pt._get_sim_balance() > before["balance"]


def test_t2_price_feed_returns_garbage(pt, monkeypatch):
    """Feed returning 0 / NaN is treated as unavailable, never traded."""
    from src.paper_trader import PaperTrader
    for bad in (0.0, float("nan"), float("inf"), -1.0):
        monkeypatch.setattr(PaperTrader, "get_current_price",
                            lambda self, s, b=bad: b)
        before = _snap(pt)
        assert pt.place_market_buy("BTCUSDT", 0.5) is None, f"traded on {bad}"
        assert _snap(pt) == before


# ── F: malformed injection ────────────────────────────────────────────

@pytest.mark.parametrize("qty", [float("nan"), None, -1.0, 0.0, -0.0])
def test_t2_malformed_quantity(pt, qty):
    """NaN/None/negative/zero quantities are refused without a trace."""
    before = _snap(pt)
    assert pt.place_market_buy("BTCUSDT", qty) is None
    assert pt.place_market_sell("BTCUSDT", qty) is None
    assert _snap(pt) == before


@pytest.mark.parametrize("symbol", ["", "NOTASYMBOL", "BTC", "btcusdt!"])
def test_t2_malformed_symbol(pt, symbol, monkeypatch):
    """Garbage symbols never reach the fill engine (allowlist gate)."""
    from src.paper_trader import PaperTrader
    monkeypatch.setattr(PaperTrader, "validate_symbol",
                        lambda self, s: s.endswith("USDT")
                        and s[:-4].isalnum() and s.isupper())
    before = _snap(pt)
    r = pt.place_market_buy(symbol, 0.5)
    assert r is None
    assert _snap(pt) == before


def test_t2_malformed_limit_price(pt):
    """LIMIT with NaN/None/negative price is refused."""
    before = _snap(pt)
    assert pt.place_limit_buy("BTCUSDT", 0.5, float("nan")) is None
    assert pt.place_limit_buy("BTCUSDT", 0.5, None) is None
    assert pt.place_limit_buy("BTCUSDT", 0.5, -100.0) is None
    assert _snap(pt) == before


# ── G: cross-module error containment ─────────────────────────────────

def test_t2_commit_failure_atomic_rollback(pt, monkeypatch):
    """A crash at commit time rolls back EVERYTHING — balance, position,
    pnl, counter, and zero ledger rows (no dirty writes)."""
    assert pt.place_market_buy("BTCUSDT", 0.2) is not None
    before = _snap(pt)

    def _boom():
        raise RuntimeError("disk full at commit")
    monkeypatch.setattr(pt, "_commit_transaction", _boom)
    assert pt.place_market_sell("BTCUSDT", 0.2) is None

    # rollback restored every dimension
    after = _snap(pt)
    assert after["balance"] == pytest.approx(before["balance"])
    assert after["positions"] == before["positions"]
    assert after["pnl"] == pytest.approx(before["pnl"])
    assert after["counter"] == before["counter"]
    assert after["trades"] == before["trades"]
    assert after["paper_trades"] == before["paper_trades"]


def test_t2_ledger_failure_does_not_block_fill(pt, monkeypatch):
    """A broken trades-ledger write must not undo the paper fill (the
    dual-write is best-effort by design — ruling A)."""
    assert pt.place_market_buy("BTCUSDT", 0.5) is not None
    bal_before = pt._get_sim_balance()

    real_db = pt._get_db()

    class _BrokenTradeAdd:
        def __getattr__(self, name):
            if name == "trade_add":
                def _raise(*a, **k):
                    raise RuntimeError("ledger down")
                return _raise
            return getattr(real_db, name)

    monkeypatch.setattr(pt, "_get_db", lambda: _BrokenTradeAdd())
    r = pt.place_market_sell("BTCUSDT", 0.5)
    assert r is not None, "fill must survive ledger outage"
    assert pt._get_sim_balance() > bal_before
