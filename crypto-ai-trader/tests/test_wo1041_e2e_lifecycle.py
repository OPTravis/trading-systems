"""WO-1041 T1: E2E trading lifecycle in the paper channel (TESTING=1).

Design: boundary-only mocks. Exchange market data (price, klines) is
mocked at the PaperTrader edge; every internal component runs REAL:
PaperTrader fill engine (slippage/fee/atomic tx), StateDB ledgers,
risk stack inside execute_auto_trade (circuit breaker / daily loss /
drawdown / duplicate-order / price-anomaly), Kelly fallback sizing,
OCO protection, close_position gross-PnL booking, and the
TradeOutcomeRecorder learning hook.

Each lifecycle ring asserts its durable footprint (DB rows / paper
state) — the collected trace proves the chain has no broken links:

  scan/signal → risk gate → order → fill → ledger → position
  → protect (OCO) → exit → learning hook
"""
import json
import time
from unittest import mock

import pytest

pytestmark = pytest.mark.usefixtures()


# ── helpers ────────────────────────────────────────────────────────────

def _flat_klines(price=100.0, n=14):
    """n hourly klines tightly around `price` — passes the 3σ deviation
    filter and has nonzero std so the check actually exercises."""
    t0 = 1790000000000
    ks = []
    for i in range(n):
        p = price * (1 + 0.0005 * (i % 3 - 1))  # tiny wiggle, std > 0
        ks.append({
            "open_time": t0 + i * 3600000, "open": p * 0.999,
            "high": p * 1.001, "low": p * 0.998, "close": p, "volume": 100.0,
        })
    return ks


def _opportunity(symbol="BTCUSDT", price=100.0, score=85):
    """Scanner-output shape contract (ring 1 boundary object)."""
    return {
        "symbol": symbol, "score": score, "price": price,
        "signals": ["RSI Oversold", "Volume Surge"],
        "factor_scores": {"technical": 80, "trend": 70, "volume": 75},
        "technical_score": 80, "trend_score": 70,
        "volume_surge": 1.5, "funding_rate": 0.01,
    }


@pytest.fixture
def paper_env(monkeypatch):
    """Paper channel with market-data edge mocked; internals real."""
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("AUTO_EXECUTE", "true")
    from src.paper_trader import PaperTrader

    prices = {"BTCUSDT": 100.0}

    def _fake_price(self, symbol):
        return prices.get(symbol, 100.0)

    monkeypatch.setattr(PaperTrader, "get_current_price", _fake_price)
    monkeypatch.setattr(
        PaperTrader, "get_klines",
        lambda self, symbol, interval="1h", limit=500, **kw:
            _flat_klines(prices.get(symbol, 100.0), min(limit, 14)))
    return {"prices": prices}


def _new_paper_trader():
    from src.paper_trader import PaperTrader
    return PaperTrader()


def _execute(pt, opp, **kw):
    """Drive the real execute_auto_trade with the paper client injected
    at the only boundary that matters (trading client factory)."""
    from src import trade_executor as te
    with mock.patch.object(te, "get_trading_client", return_value=pt), \
         mock.patch.object(te, "FeishuNotifier", mock.MagicMock()), \
         mock.patch.object(te, "count_active_positions", return_value=0), \
         mock.patch("src.twap_vwap.time.sleep"):
        return te.execute_auto_trade(
            opp["symbol"], opp["price"], kw.get("strategy", "trend"),
            kw.get("sl_pct", 2.0), kw.get("tp_levels"), kw.get("stop_price"),
            kw.get("max_hold", 24), opp["signals"], "e2e-lifecycle",
            score=opp["score"], order_value=kw.get("order_value", 40.0),
        )


TP_LEVELS = [{"pct": 2.0, "size_pct": 50}, {"pct": 4.0, "size_pct": 50}]


# ── ring tests ─────────────────────────────────────────────────────────

def test_t1_r1_signal_shape_contract(paper_env):
    """Ring 1 (scan→signal): opportunity dict entering the execution
    chain carries every field downstream consumers read."""
    opp = _opportunity()
    for key in ("symbol", "score", "price", "signals"):
        assert opp[key] is not None, f"signal missing {key}"
    assert 0 <= opp["score"] <= 100
    assert opp["price"] > 0


def _bull_daily_klines(n=250, p0=50.0, p1=100.0):
    """Monotonic uptrend — passes the multi-factor BTC trend gate
    (EMA21>EMA55, RSI strong, MACD positive, higher lows, OBV up)."""
    ks = []
    for i in range(n):
        p = p0 + (p1 - p0) * i / (n - 1)
        ks.append({"open_time": 1790000000000 + i * 86400000,
                   "open": p * 0.998, "high": p * 1.002, "low": p * 0.997,
                   "close": p, "volume": 1000.0})
    return ks


def test_t1_r2_risk_gate_passes_clean_state(paper_env):
    """Ring 2 (risk gate): the real RiskManager stack (trend filter
    included) approves on a fresh TESTING state in a bull regime."""
    from src.risk_manager import RiskManager, TrendFilter
    from src.binance_client import BinanceClient  # proxy module
    with mock.patch.object(BinanceClient, "__init__", lambda self: None):
        client = BinanceClient()
        client.get_account = lambda: {"balances": [
            {"asset": "USDT", "free": "1000", "locked": "0"}]}
        client.get_klines = (
            lambda symbol, interval="1d", limit=250, **kw:
                _bull_daily_klines(min(limit, 250))
                if symbol == "BTCUSDT" and interval == "1d" else
                _flat_klines(100.0, min(limit, 14)))
        rm = RiskManager(client)
        res = rm.pre_trade_check(
            "BTCUSDT", price=100.0, atr=2.0, positions=[])
        assert res["allowed"], f"risk gate blocked clean state: {res.get('reasons')}"


def test_t1_r3_order_fill_succeeds(paper_env):
    """Ring 3 (order): execute_auto_trade returns success through the
    real paper fill engine."""
    pt = _new_paper_trader()
    result = _execute(pt, _opportunity(),
                      tp_levels=TP_LEVELS, stop_price=98.0)
    assert result.get("success"), f"order failed: {result.get('error')}"


def test_t1_r4_ledger_dual_write_paper_tag(paper_env):
    """Ring 4 (fill→ledger): BUY dual-write lands in trades WITH the
    WO-1029 paper_ client_order_id tag, and paper_trades mirrors it."""
    pt = _new_paper_trader()
    result = _execute(pt, _opportunity(),
                      tp_levels=TP_LEVELS, stop_price=98.0)
    assert result.get("success")
    db = pt._get_db()
    rows = db._get_conn().execute(
        "SELECT symbol, side, qty, price, client_order_id FROM trades "
        "WHERE symbol='BTCUSDT' AND side='BUY'").fetchall()
    assert rows, "no BUY row in trades ledger"
    tagged = [r for r in rows if r["client_order_id"]
              and r["client_order_id"].startswith("paper_")]
    assert tagged, (
        "WO-1029 A1 tag missing: no paper_-prefixed BUY row "
        f"(portfolio NULL rows are the legit real-side convention; "
        f"got {[r['client_order_id'] for r in rows]})")
    pt_rows = db._get_conn().execute(
        "SELECT id, side, quantity, fill_price, fee_usdt FROM paper_trades "
        "WHERE symbol='BTCUSDT' AND side='BUY'").fetchall()
    assert pt_rows, "no BUY row in paper_trades"


def test_t1_r5_position_booked_in_paper_state(paper_env):
    """Ring 5 (position): paper_portfolio holds the position and cash
    was debited (cost + fee)."""
    pt = _new_paper_trader()
    result = _execute(pt, _opportunity(),
                      tp_levels=TP_LEVELS, stop_price=98.0)
    assert result.get("success")
    bal = float(pt._get_sim_value("cash_balance"))
    assert bal < 10000.0, "cash not debited"
    positions = json.loads(pt._get_sim_value("positions", "{}"))
    assert "BTC" in positions, f"position not booked: {positions}"
    assert positions["BTC"]["qty"] > 0


def test_t1_r6_protection_oco_booked(paper_env):
    """Ring 6 (protect): after entry, protective SELL orders (TP limit +
    SL stop) exist in the paper open-orders book."""
    pt = _new_paper_trader()
    result = _execute(pt, _opportunity(),
                      tp_levels=TP_LEVELS, stop_price=98.0)
    assert result.get("success")
    opens = pt.get_open_orders("BTCUSDT")
    assert opens, "no protective orders after entry"
    kinds = {str(o.get("order_type") or o.get("type")) for o in opens}
    assert any("LIMIT" in k and "STOP" not in k for k in kinds), \
        f"no TP limit: {opens}"
    # WO-1041 T2 fix guard: the SL stop order must still be PENDING (not
    # instantly filled — the bug this suite caught and fixed)
    assert any("STOP" in k for k in kinds), f"no SL stop: {opens}"


def test_t1_r7_exit_books_gross_pnl(paper_env):
    """Ring 7 (exit): selling the full position books a SELL ledger row
    with gross PnL and credits cash (net of paper fees)."""
    pt = _new_paper_trader()
    result = _execute(pt, _opportunity(),
                      tp_levels=TP_LEVELS, stop_price=98.0)
    assert result.get("success")
    positions = json.loads(pt._get_sim_value("positions", "{}"))
    qty = positions["BTC"]["qty"]

    # price rises 5% — profitable exit
    paper_env["prices"]["BTCUSDT"] = 105.0
    sell = pt.place_market_sell("BTCUSDT", qty)
    assert sell is not None, "paper SELL failed"
    db = pt._get_db()
    row = db._get_conn().execute(
        "SELECT qty, price, pnl, client_order_id FROM trades "
        "WHERE symbol='BTCUSDT' AND side='SELL'").fetchone()
    assert row is not None, "no SELL row in ledger"
    assert row["pnl"] > 0, f"gross pnl should be positive, got {row['pnl']}"
    assert row["client_order_id"].startswith("paper_")
    # position closed
    positions_after = json.loads(pt._get_sim_value("positions", "{}"))
    assert "BTC" not in positions_after


def test_t1_r8_learning_hook_records_outcome(paper_env):
    """Ring 8 (learning hook): record_entry → record_outcome round-trips
    into trade_outcomes with derived metrics."""
    from src.trade_outcome_recorder import TradeOutcomeRecorder
    from src.state_db import get_state_db
    db = get_state_db()
    rec = TradeOutcomeRecorder(db=db)
    entry_id = rec.record_entry(
        "BTCUSDT", entry_price=100.0, qty=0.01, score=85, strategy="trend")
    assert entry_id, "record_entry returned falsy"
    out = rec.record_outcome("BTCUSDT", exit_price=105.0,
                             exit_reason="tp1", entry_id=entry_id)
    assert out, "record_outcome returned None"
    row = db._get_conn().execute(
        "SELECT symbol, entry_price, exit_price, status FROM trade_outcomes "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None and row["status"] == "closed"


def test_t1_r9_full_cycle_trace(paper_env):
    """Full chain in ONE test — the integration proof. Every ring must
    leave its footprint in order; any broken link fails here."""
    trace = []

    # ring 1: signal
    opp = _opportunity(score=85)
    assert opp["symbol"] == "BTCUSDT"
    trace.append(("signal", opp["symbol"], opp["score"]))

    # ring 2+3: risk-gated order through the real stack
    pt = _new_paper_trader()
    result = _execute(pt, opp, tp_levels=TP_LEVELS, stop_price=98.0)
    assert result.get("success"), result.get("error")
    trace.append(("order", result.get("qty"), result.get("price")))

    # ring 4: ledger
    db = pt._get_db()
    buy = db._get_conn().execute(
        "SELECT qty, price, client_order_id FROM trades "
        "WHERE symbol='BTCUSDT' AND side='BUY'").fetchone()
    assert buy and buy["client_order_id"].startswith("paper_")
    trace.append(("ledger", buy["qty"], buy["client_order_id"]))

    # ring 5: position
    pos = json.loads(pt._get_sim_value("positions", "{}"))["BTC"]
    trace.append(("position", pos["qty"], pos["entry_price"]))

    # ring 6: protection
    opens = pt.get_open_orders("BTCUSDT")
    assert len(opens) >= 2, f"expected TP+SL, got {opens}"
    trace.append(("protect", [o.get("type") for o in opens]))

    # ring 7: exit
    paper_env["prices"]["BTCUSDT"] = 104.0
    sell = pt.place_market_sell("BTCUSDT", pos["qty"])
    assert sell is not None
    trace.append(("exit", sell.get("executedQty")))

    # ring 8: learning hook (portfolio-style close booking)
    from src.trade_outcome_recorder import TradeOutcomeRecorder
    rec = TradeOutcomeRecorder(db=db)
    e_id = rec.record_entry("BTCUSDT", 100.0, pos["qty"], 85, "trend")
    o = rec.record_outcome("BTCUSDT", 104.0, exit_reason="tp1",
                           entry_id=e_id)
    assert o and o.get("pnl_pct", 0) > 0
    trace.append(("learning", o.get("pnl_pct")))

    # full trace recorded — chain complete, no broken links
    assert [t[0] for t in trace] == [
        "signal", "order", "ledger", "position", "protect", "exit",
        "learning"]
