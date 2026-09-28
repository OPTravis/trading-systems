"""WO-0928: OCO server-side fill reconciliation (event-anchored).

Production blind spots that motivated this (9/28):
- ENA 06:35 — a foreign trades writer landed the SELL row (fill ts +
  client_order_id) minutes after the server-side OCO exit, but never
  told the ledger; the net gap collapsed so the reconciler's axes never
  queued the symbol again while events/shadow stayed wrong.
- ADA 12:05 — poll-gap fill with no writer at all; sync dropped the
  portfolio row, Path B demanded an exchange-flat balance (dust residue
  blocked it) and only a manual SOP closed the books.
reconcile_exchange_fills anchors on ledger_events.order_id instead.
"""

import time

import pytest

from src.state_db import get_state_db
from src.portfolio_reconciler import reconcile_exchange_fills


class FakeClient:
    def __init__(self, balances, trades_by_symbol, live_price=None):
        self._balances = balances
        self._trades = trades_by_symbol
        self._live = live_price or {}
        self.my_trades_calls = []

    def get_account(self):
        return {"balances": self._balances}

    def get_my_trades(self, symbol, limit=100, from_id=None):
        self.my_trades_calls.append(symbol)
        return self._trades.get(symbol, [])

    def get_ticker_price(self, symbol):
        return self._live.get(symbol)


class RaisingAccountClient(FakeClient):
    def get_account(self):
        raise Exception("proxy down")


def _bal(asset, total, locked=0.0):
    return {"asset": asset, "free": str(total - locked), "locked": str(locked)}


def _fill(symbol, oid, qty, price, ts, is_buyer=False, commission="0",
          commission_asset="USDT"):
    return {
        "id": oid * 10, "price": str(price), "qty": str(qty),
        "quoteQty": str(round(qty * price, 8)), "commission": commission,
        "commissionAsset": commission_asset, "time": int(ts * 1000),
        "isBuyer": is_buyer, "isMaker": False, "orderId": oid,
        "symbol": symbol,
    }


@pytest.fixture
def db():
    # conftest isolates STATE_DB_PATH; the singleton IS the test DB, so
    # record_fill's internal get_state_db() lands on the same instance.
    d = get_state_db()
    d.kv_set("ledger:shadow:bootstrap_ts", time.time() - 86400)
    yield d


def _seed_buy(db, symbol, qty, price, oid, ts=None):
    """Full BUY lifecycle: trades row + ledger event + shadow position."""
    if ts is None:
        ts = time.time() - 7200
    db.trade_add(symbol, "BUY", qty, price)
    conn = db._get_conn()
    conn.execute(
        "UPDATE trades SET timestamp = ? WHERE symbol = ? AND side = 'BUY'",
        (ts, symbol))
    conn.commit()
    from src.ledger import record_fill
    st = record_fill(
        {"type": "BUY", "symbol": symbol, "qty": qty, "price": price,
         "ts": ts, "order_id": str(oid), "source": "test.seed"},
        observe_only=True, db=db)
    assert st["status"] == "ok", st


def _shadow_qty(db, symbol):
    row = db._get_conn().execute(
        "SELECT net_qty FROM ledger_shadow_positions WHERE symbol = ?",
        (symbol,)).fetchone()
    return float(row["net_qty"]) if row else 0.0


def _events(db, symbol):
    return db._get_conn().execute(
        "SELECT type, qty, price, order_id FROM ledger_events "
        "WHERE symbol = ? ORDER BY id", (symbol,)).fetchall()


def _audit_actions(db, action):
    rows = db._get_conn().execute(
        "SELECT details FROM audit_log WHERE action = ?", (action,)).fetchall()
    return rows


# ---------- ENA mode: trades row present, ledger event missing ----------

def test_ena_mode_shadow_backfill_restores_event_shadow_portfolio(db):
    # BUY booked everywhere; server-side SL exit landed in trades via a
    # foreign writer (fill ts + client_order_id) but never in the ledger.
    oid = 3448481729
    fill_ts = time.time() - 1800
    _seed_buy(db, "ENAUSDT", 37.43253, 0.2875, 9001)
    db.trade_add("ENAUSDT", "SELL", 37.43, 0.2731, -0.54,
                 client_order_id=str(oid))
    conn = db._get_conn()
    conn.execute(
        "UPDATE trades SET timestamp = ? WHERE symbol = ? AND side = 'SELL' "
        "AND client_order_id = ?", (fill_ts, "ENAUSDT", str(oid)))
    conn.commit()
    db.portfolio_set("ENAUSDT", {"quantity": 37.43253, "entry_price": 0.2875,
                                 "strategy": "trend", "opened_at": time.time()})
    assert _shadow_qty(db, "ENAUSDT") == pytest.approx(37.43253)

    fills = [_fill("ENAUSDT", oid, 37.43, 0.2731, fill_ts,
                   commission="0.00253", commission_asset="ENA")]
    client = FakeClient(
        [_bal("ENA", 0.00253), _bal("USDT", 405)],
        {"ENAUSDT": fills}, live_price={"ENAUSDT": 0.2731})

    repaired = reconcile_exchange_fills(client, db)

    assert len(repaired) == 1
    r = repaired[0]
    assert r["source"] == "oco_recon/shadow_backfill"
    assert r["order_id"] == str(oid)
    # ledger event injected with the exchange orderId (idempotency anchor)
    evs = _events(db, "ENAUSDT")
    sell_rows = [e for e in evs if e["type"] == "SELL"]
    assert len(sell_rows) == 1
    assert sell_rows[0]["order_id"] == str(oid)
    # shadow decremented to the exchange dust residue
    assert _shadow_qty(db, "ENAUSDT") == pytest.approx(
        37.43253 - (37.43 - 0.00253), abs=1e-9)
    # stale portfolio row trimmed to the exchange residue
    pos = db.portfolio_get_all().get("ENAUSDT")
    assert pos and float(pos["quantity"]) == pytest.approx(0.00253)
    # audit trail
    audits = _audit_actions(db, "OCO_RECON_AUTO")
    assert len(audits) == 1
    # no duplicate trades row (UNIQUE client_order_id held)
    sells = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE symbol='ENAUSDT' AND side='SELL'"
    ).fetchone()
    assert sells[0] == 1


def test_idempotent_second_round_is_silent(db):
    oid = 3448481729
    fill_ts = time.time() - 1800
    _seed_buy(db, "ENAUSDT", 37.43253, 0.2875, 9001)
    db.trade_add("ENAUSDT", "SELL", 37.43, 0.2731, -0.54,
                 client_order_id=str(oid))
    fills = [_fill("ENAUSDT", oid, 37.43, 0.2731, fill_ts,
                   commission="0.00253", commission_asset="ENA")]
    client = FakeClient([_bal("ENA", 0.00253)], {"ENAUSDT": fills})
    assert reconcile_exchange_fills(client, db)
    # second round: orderId already in ledger_events -> nothing to do
    assert reconcile_exchange_fills(client, db) == []
    assert len(_audit_actions(db, "OCO_RECON_AUTO")) == 1


# ---------- ADA mode: no writer at all ----------

def test_ada_mode_full_chain_books_everything(db):
    oid = 8811425190
    fill_ts = time.time() - 1800
    _seed_buy(db, "ADAUSDT", 41.8581, 0.2573, 9002)
    db.portfolio_set("ADAUSDT", {"quantity": 41.8581, "entry_price": 0.2573,
                                 "strategy": "bollinger",
                                 "opened_at": time.time()})
    fills = [_fill("ADAUSDT", oid, 41.8, 0.2471, fill_ts)]
    client = FakeClient(
        [_bal("ADA", 0.0581), _bal("USDT", 405)],
        {"ADAUSDT": fills}, live_price={"ADAUSDT": 0.2471})

    repaired = reconcile_exchange_fills(client, db)

    assert len(repaired) == 1
    assert repaired[0]["source"] == "oco_recon/full_chain"
    conn = db._get_conn()
    # trades row landed with the exchange orderId
    row = conn.execute(
        "SELECT qty, price, client_order_id FROM trades "
        "WHERE symbol='ADAUSDT' AND side='SELL'").fetchone()
    assert row and row["client_order_id"] == str(oid)
    assert float(row["price"]) == pytest.approx(0.2471)
    # ledger event + shadow at the dust residue
    assert _shadow_qty(db, "ADAUSDT") == pytest.approx(41.8581 - 41.8)
    sell_rows = [e for e in _events(db, "ADAUSDT") if e["type"] == "SELL"]
    assert len(sell_rows) == 1 and sell_rows[0]["order_id"] == str(oid)
    # portfolio row trimmed to residue
    pos = db.portfolio_get_all().get("ADAUSDT")
    assert pos and float(pos["quantity"]) == pytest.approx(0.0581)
    assert len(_audit_actions(db, "OCO_RECON_AUTO")) == 1


# ---------- clean round (GRAM: OCO still resting) ----------

def test_clean_round_gram_position_no_writes(db):
    _seed_buy(db, "GRAMUSDT", 6.62337, 1.677, 9003)
    db.portfolio_set("GRAMUSDT", {"quantity": 6.62337, "entry_price": 1.677,
                                  "strategy": "bollinger",
                                  "opened_at": time.time()})
    client = FakeClient([_bal("GRAM", 6.62337), _bal("USDT", 405)],
                        {"GRAMUSDT": []})
    assert reconcile_exchange_fills(client, db) == []
    assert _shadow_qty(db, "GRAMUSDT") == pytest.approx(6.62337)
    assert len(_audit_actions(db, "OCO_RECON_AUTO")) == 0
    # no SELL rows appeared
    conn = db._get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE symbol='GRAMUSDT' "
        "AND side='SELL'").fetchone()
    assert n[0] == 0


# ---------- fail-open ----------

def test_account_failure_returns_empty(db):
    client = RaisingAccountClient([], {})
    assert reconcile_exchange_fills(client, db) == []


def test_my_trades_failure_skips_symbol_not_round(db):
    _seed_buy(db, "SOLUSDT", 0.05, 121.93, 9004)
    oid = 7001
    fills = [_fill("ADAUSDT", 7001, 41.8, 0.2471, time.time() - 900)]
    # SOL listed via ledger_events recent symbols; its myTrades raises
    class PartialClient(FakeClient):
        def get_my_trades(self, symbol, limit=100, from_id=None):
            self.my_trades_calls.append(symbol)
            if symbol == "SOLUSDT":
                raise Exception("proxy flake")
            return self._trades.get(symbol, [])
    pclient = PartialClient([_bal("USDT", 405)], {"ADAUSDT": fills})
    # ADA has no local books (no BUY) -> foreign skip; round still returns
    out = reconcile_exchange_fills(pclient, db)
    assert out == []
    assert "SOLUSDT" in pclient.my_trades_calls


# ---------- guards ----------

def test_foreign_fill_predating_latest_buy_skipped(db):
    # old fill predates our BUY -> foreign leg, never booked
    _seed_buy(db, "TRXUSDT", 100.0, 0.05, 9005, ts=time.time() - 600)
    fills = [_fill("TRXUSDT", 8001, 100.0, 0.048, time.time() - 7200)]
    client = FakeClient([_bal("TRX", 100.0)], {"TRXUSDT": fills})
    assert reconcile_exchange_fills(client, db) == []
    assert _shadow_qty(db, "TRXUSDT") == pytest.approx(100.0)
    assert len(_audit_actions(db, "OCO_RECON_AUTO")) == 0


def test_fill_with_zero_ledger_net_skipped(db):
    # ledger never held this symbol -> the SELL cannot be ours
    fills = [_fill("XYZUSDT", 9001, 50.0, 1.0, time.time() - 900)]
    client = FakeClient([_bal("USDT", 405)], {"XYZUSDT": fills})
    assert reconcile_exchange_fills(client, db) == []
    conn = db._get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE symbol='XYZUSDT'").fetchone()
    assert n[0] == 0


def test_non_usdt_symbols_ignored(db):
    _seed_buy(db, "SOLBTC", 0.05, 0.001, 9006)
    fills = [_fill("SOLBTC", 9101, 0.05, 0.0011, time.time() - 900)]
    client = FakeClient([_bal("SOL", 0.0)], {"SOLBTC": fills})
    assert reconcile_exchange_fills(client, db) == []
    assert "SOLBTC" not in client.my_trades_calls
