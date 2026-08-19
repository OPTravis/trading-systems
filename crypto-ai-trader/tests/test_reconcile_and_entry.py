"""Tests for bug #6 (market-price entry pollution) and the SL-fill
reconciliation module (scripts/reconcile_fills.py).

Red/green anchor: 2026-08-20 ETH incident — exchange history qty mismatch
(0.0269 vs 0.0027) made get_avg_entry_price() return None, sync fell through
to market_estimate and wrote $2,099.84 as entry (real fill: $1,935.41),
which then locked itself in via db_existing. Same night EDEN's 22:42 SL fill
never hit the ledger (hand-patched trade #76, outcome #36).
"""

import time

import pytest
from unittest.mock import MagicMock, patch

from src.state_db import StateDB


@pytest.fixture
def tmp_db_path(tmp_path):
    return str(tmp_path / "test_state.db")


@pytest.fixture
def fresh_statedb(tmp_db_path):
    db = StateDB(tmp_db_path)
    yield db
    db.close()


# ============================================================================
# Class 1: get_avg_entry_price_from_db — DB-ledger cost basis (bug #6)
# ============================================================================


class TestDbLedgerEntryPrice:
    def _insert(self, db, symbol, side, qty, price, ts):
        conn = db._get_conn()
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, timestamp) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (symbol, side, qty, price, ts),
        )
        conn.commit()

    def test_pure_buy_weighted_avg(self, fresh_statedb):
        from src.entry_price import get_avg_entry_price_from_db

        t0 = time.time() - 3600
        self._insert(fresh_statedb, "ETHUSDT", "BUY", 0.001, 1900.0, t0)
        self._insert(fresh_statedb, "ETHUSDT", "BUY", 0.002, 2000.0, t0 + 60)

        avg = get_avg_entry_price_from_db(fresh_statedb, "ETHUSDT")
        # (0.001*1900 + 0.002*2000) / 0.003 = 1966.67
        assert avg == pytest.approx(1966.6667, rel=1e-3)

    def test_fifo_sells_reduce_lots(self, fresh_statedb):
        from src.entry_price import get_avg_entry_price_from_db

        t0 = time.time() - 7200
        self._insert(fresh_statedb, "EDENUSDT", "BUY", 50.0, 0.05, t0)
        self._insert(fresh_statedb, "EDENUSDT", "BUY", 50.0, 0.07, t0 + 60)
        self._insert(fresh_statedb, "EDENUSDT", "SELL", 50.0, 0.06, t0 + 120)

        avg = get_avg_entry_price_from_db(fresh_statedb, "EDENUSDT")
        # FIFO removes the 0.05 lot → remaining 50 @ 0.07
        assert avg == pytest.approx(0.07, rel=1e-6)

    def test_qty_validation_mismatch_returns_none(self, fresh_statedb):
        """ETH incident shape: ledger qty diverges from actual holdings."""
        from src.entry_price import get_avg_entry_price_from_db

        t0 = time.time() - 3600
        self._insert(fresh_statedb, "ETHUSDT", "BUY", 0.0269, 1935.41, t0)

        avg = get_avg_entry_price_from_db(fresh_statedb, "ETHUSDT", current_qty=0.0027)
        assert avg is None  # >5% divergence → refuse, never guess

    def test_qty_validation_pass_returns_avg(self, fresh_statedb):
        from src.entry_price import get_avg_entry_price_from_db

        t0 = time.time() - 3600
        self._insert(fresh_statedb, "ETHUSDT", "BUY", 0.0027, 1935.41, t0)

        avg = get_avg_entry_price_from_db(fresh_statedb, "ETHUSDT", current_qty=0.0027)
        assert avg == pytest.approx(1935.41, rel=1e-6)

    def test_empty_ledger_returns_none(self, fresh_statedb):
        from src.entry_price import get_avg_entry_price_from_db

        assert get_avg_entry_price_from_db(fresh_statedb, "ZAMAUSDT") is None

    def test_fully_closed_returns_none(self, fresh_statedb):
        from src.entry_price import get_avg_entry_price_from_db

        t0 = time.time() - 3600
        self._insert(fresh_statedb, "EDENUSDT", "BUY", 50.0, 0.05, t0)
        self._insert(fresh_statedb, "EDENUSDT", "SELL", 50.0, 0.05518, t0 + 120)

        assert get_avg_entry_price_from_db(fresh_statedb, "EDENUSDT") is None


# ============================================================================
# Class 2: sync must not lock in a market_estimate entry (bug #6)
# ============================================================================


class TestSyncEstimateNotLockedIn:
    def test_estimated_entry_is_replaced_by_db_ledger(
        self, fresh_statedb, tmp_path
    ):
        """Polluted entry ($2,099.84) + estimate flag → sync must fall back to
        the DB ledger cost basis ($1,935.41) and clear the flag."""
        # Seed: polluted portfolio entry + estimate flag + real ledger fill
        fresh_statedb.portfolio_set(
            "ETHUSDT",
            {
                "quantity": 0.0027,
                "entry_price": 2099.84,  # market_estimate pollution
                "strategy": "synced",
                "opened_at": time.time() - 7200,
                "stop_loss": 0,
                "take_profit": 0,
                "invest_pct": 1.3,
            },
        )
        fresh_statedb.kv_set("entry_est:ETHUSDT", time.time() - 3600)
        conn = fresh_statedb._get_conn()
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, timestamp) "
            "VALUES ('ETHUSDT', 'BUY', 0.0027, 1935.41, 0, ?)",
            (time.time() - 7200,),
        )
        conn.commit()

        # Mock client: holds ETH 0.0027; exchange history is unusable
        client = MagicMock()
        client.get_account.return_value = {
            "balances": [
                {"asset": "USDT", "free": "379.0", "locked": "0"},
                {"asset": "ETH", "free": "0.0027", "locked": "0"},
            ]
        }
        client.get_24hr_stats.return_value = {"last_price": "2100.00"}
        # qty mismatch (0.0269 vs 0.0027) → exchange-history path returns None
        client.get_my_trades.return_value = [
            {"qty": "0.0269", "price": "1500", "isBuyer": True, "time": 1}
        ]

        from src.portfolio import PortfolioManager

        with patch("src.state_db.get_state_db", return_value=fresh_statedb):
            pm = PortfolioManager(config_path=None, binance_client=None)
            pm._client = client
            pm.sync_from_binance(client)

        pos = pm.positions.get("ETHUSDT")
        assert pos is not None
        assert pos["entry_price"] == pytest.approx(1935.41, rel=1e-4), (
            "sync must replace the estimated entry with the DB-ledger cost basis"
        )
        assert fresh_statedb.kv_get("entry_est:ETHUSDT") is None, (
            "estimate flag must be cleared once a real cost basis is stored"
        )


# ============================================================================
# Class 3: reconcile_fills — book exchange SELL fills (SL-fill gap)
# ============================================================================


def _make_client(held_assets=None, sells=None, price="2100"):
    client = MagicMock()
    balances = [{"asset": "USDT", "free": "397.0", "locked": "0"}]
    for a, q in (held_assets or {}).items():
        balances.append({"asset": a, "free": str(q), "locked": "0"})
    client.get_account.return_value = {"balances": balances}
    client.get_24hr_stats.return_value = {"last_price": price}
    client.get_my_trades.return_value = sells or []
    return client


def _seed_open_outcome(db, symbol="EDENUSDT", entry=0.05614, qty=92.6, entry_time=None):
    conn = db._get_conn()
    et = entry_time or (time.time() - 7200)
    conn.execute(
        """INSERT INTO trade_outcomes
           (symbol, entry_time, entry_date, entry_price, qty, status,
            peak_price, trough_price, created_at)
           VALUES (?, ?, '2026-08-19', ?, ?, 'open', ?, ?, ?)""",
        (symbol, et, entry, qty, entry, entry, et),
    )
    conn.commit()
    return db._get_conn().execute(
        "SELECT id FROM trade_outcomes WHERE symbol=? AND status='open'",
        (symbol,),
    ).fetchone()["id"]


class TestReconcileFills:
    def test_sl_fill_books_ledger_and_closes_outcome(self, fresh_statedb):
        """EDEN 22:42 shape: position gone, SELL fill on exchange, ledger empty."""
        from scripts.reconcile_fills import reconcile_fills

        oid = _seed_open_outcome(fresh_statedb)
        fill_ts = time.time() - 1800
        client = _make_client(
            sells=[
                {
                    "id": 991,
                    "qty": "92.6",
                    "price": "0.05518",
                    "isBuyer": False,
                    "time": int(fill_ts * 1000),
                }
            ],
            price="0.055",
        )

        out = reconcile_fills(client=client, db=fresh_statedb)

        assert len(out["patched"]) == 1, out
        assert "EDENUSDT" in out["patched"][0]
        # SELL row booked
        row = (
            fresh_statedb._get_conn()
            .execute(
                "SELECT side, qty, price FROM trades WHERE symbol='EDENUSDT' AND side='SELL'"
            )
            .fetchone()
        )
        assert row is not None
        assert row["qty"] == pytest.approx(92.6)
        assert row["price"] == pytest.approx(0.05518)
        # Outcome closed with sl reason
        oc = (
            fresh_statedb._get_conn()
            .execute("SELECT status, exit_reason, exit_price FROM trade_outcomes WHERE id=?", (oid,))
            .fetchone()
        )
        assert oc["status"] == "closed"
        assert oc["exit_reason"] == "sl"
        assert oc["exit_price"] == pytest.approx(0.05518, rel=1e-4)

    def test_idempotent_second_run_no_double_booking(self, fresh_statedb):
        from scripts.reconcile_fills import reconcile_fills

        _seed_open_outcome(fresh_statedb)
        fill_ts = time.time() - 1800
        client = _make_client(
            sells=[
                {
                    "qty": "92.6",
                    "price": "0.05518",
                    "isBuyer": False,
                    "time": int(fill_ts * 1000),
                }
            ],
            price="0.055",
        )
        reconcile_fills(client=client, db=fresh_statedb)

        out2 = reconcile_fills(client=client, db=fresh_statedb)
        assert out2["patched"] == [], out2
        count = (
            fresh_statedb._get_conn()
            .execute("SELECT COUNT(*) c FROM trades WHERE symbol='EDENUSDT' AND side='SELL'")
            .fetchone()["c"]
        )
        assert count == 1, "second run must not double-book the SELL"

    def test_close_only_when_sell_row_exists(self, fresh_statedb):
        """ensure_tp_sl booked the SELL but outcome stayed open."""
        from scripts.reconcile_fills import reconcile_fills

        _seed_open_outcome(fresh_statedb)
        fill_ts = time.time() - 1800
        conn = fresh_statedb._get_conn()
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, timestamp) "
            "VALUES ('EDENUSDT', 'SELL', 92.6, 0.05518, -0.09, ?)",
            (fill_ts + 5,),
        )
        conn.commit()
        client = _make_client(
            sells=[
                {
                    "qty": "92.6",
                    "price": "0.05518",
                    "isBuyer": False,
                    "time": int(fill_ts * 1000),
                }
            ],
            price="0.055",
        )

        out = reconcile_fills(client=client, db=fresh_statedb)
        assert out["patched"] == []
        assert len(out["closed_only"]) == 1, out
        count = (
            fresh_statedb._get_conn()
            .execute("SELECT COUNT(*) c FROM trades WHERE symbol='EDENUSDT' AND side='SELL'")
            .fetchone()["c"]
        )
        assert count == 1

    def test_still_holding_is_skipped(self, fresh_statedb):
        from scripts.reconcile_fills import reconcile_fills

        _seed_open_outcome(fresh_statedb)
        client = _make_client(
            held_assets={"EDEN": 92.6},
            sells=[],
            price="0.056",
        )

        out = reconcile_fills(client=client, db=fresh_statedb)
        assert out["patched"] == []
        assert out["closed_only"] == []
        assert any("still holding" in s for s in out["skipped"])

    def test_position_gone_no_sell_is_anomaly(self, fresh_statedb):
        from scripts.reconcile_fills import reconcile_fills

        _seed_open_outcome(fresh_statedb)
        client = _make_client(sells=[], price="0.055")

        out = reconcile_fills(client=client, db=fresh_statedb)
        assert out["patched"] == []
        assert len(out["anomalies"]) == 1, out
        # Outcome must remain open — nothing to book against
        oc = (
            fresh_statedb._get_conn()
            .execute("SELECT status FROM trade_outcomes LIMIT 1")
            .fetchone()
        )
        assert oc["status"] == "open"
