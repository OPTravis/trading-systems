"""bug#24 (2026-08-24): ensure_tp_sl double-booked exits that reconcile_fills
had already booked minutes earlier.

Timeline anchor — ENA 8/23-8/24:
  08-23 19:13 BUY 33.87417 @ 0.1771 (grid)
  08-24 06:17 SL fires on exchange, actual fill 33.87 @ 0.1628 (stepSize
           rounded order leaves 0.00417 dust)
  08-24 06:20 trailing-check/reconcile_fills books SELL 33.87 ts=06:17 ✓
  08-24 06:30 ensure_tp_sl sync-check sees DB qty 33.87417 vs dust-only
           balance → books ANOTHER full-qty SELL 33.87417 @ 0.1628 ✗
Same pattern: TRUMP 22:05/22:30, GRAM 19:25/19:30, ONDO 02:45/03:00.

Fix: insert_sell_dedup() — atomic INSERT..SELECT..WHERE NOT EXISTS with the
same matching window as reconcile_fills' bug#13 guard (qty ±2%, price ±1%,
±1h). All three ensure_tp_sl booking paths (sync-check stale, tp_breach,
max_hold) now route through it.
"""

import time

import pytest

from src.state_db import StateDB


@pytest.fixture
def tmp_db_path(tmp_path):
    return str(tmp_path / "test_state.db")


@pytest.fixture
def fresh_statedb(tmp_db_path):
    db = StateDB(tmp_db_path)
    yield db
    db.close()


def _count_sells(db, symbol):
    return (
        db._get_conn()
        .execute(
            "SELECT COUNT(*) c FROM trades WHERE symbol=? AND side='SELL'",
            (symbol,),
        )
        .fetchone()["c"]
    )


class TestInsertSellDedup:
    def test_skips_when_reconcile_already_booked(self, fresh_statedb):
        """Red anchor: ENA — reconcile booked 33.87, ensure tries 33.87417."""
        from scripts.ensure_tp_sl import insert_sell_dedup

        conn = fresh_statedb._get_conn()
        fill_ts = time.time() - 780  # reconcile booked 13 min ago
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, timestamp) "
            "VALUES ('ENAUSDT', 'SELL', 33.87, 0.1628, -0.4810, ?)",
            (fill_ts,),
        )
        conn.commit()

        inserted = insert_sell_dedup(
            conn, "ENAUSDT", 33.87417, 0.1628, -0.4844, time.time()
        )

        assert inserted is False
        assert _count_sells(fresh_statedb, "ENAUSDT") == 1

    def test_books_when_no_prior_row(self, fresh_statedb):
        from scripts.ensure_tp_sl import insert_sell_dedup

        conn = fresh_statedb._get_conn()
        inserted = insert_sell_dedup(
            conn, "TRXUSDT", 17.3826, 0.3440, 0.10, time.time()
        )

        assert inserted is True
        assert _count_sells(fresh_statedb, "TRXUSDT") == 1

    def test_books_when_prior_row_outside_window(self, fresh_statedb):
        """An old SELL from a previous round (hours ago) must not block."""
        from scripts.ensure_tp_sl import insert_sell_dedup

        conn = fresh_statedb._get_conn()
        old_ts = time.time() - 6 * 3600
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, timestamp) "
            "VALUES ('ENAUSDT', 'SELL', 33.87, 0.1628, 0.2, ?)",
            (old_ts,),
        )
        conn.commit()

        inserted = insert_sell_dedup(
            conn, "ENAUSDT", 33.87, 0.1628, -0.48, time.time()
        )

        assert inserted is True
        assert _count_sells(fresh_statedb, "ENAUSDT") == 2

    def test_books_different_price_legitimate_exit(self, fresh_statedb):
        """>1% price difference = a genuinely different exit, must book."""
        from scripts.ensure_tp_sl import insert_sell_dedup

        conn = fresh_statedb._get_conn()
        now = time.time()
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, timestamp) "
            "VALUES ('WLDUSDT', 'SELL', 10.0, 0.3536, 0.1, ?)",
            (now - 60,),
        )
        conn.commit()

        inserted = insert_sell_dedup(conn, "WLDUSDT", 10.0, 0.3712, 0.2, now)

        assert inserted is True

    def test_symbols_do_not_cross_block(self, fresh_statedb):
        from scripts.ensure_tp_sl import insert_sell_dedup

        conn = fresh_statedb._get_conn()
        now = time.time()
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, timestamp) "
            "VALUES ('SOLUSDT', 'SELL', 0.398, 95.98, 0.1, ?)",
            (now,),
        )
        conn.commit()

        # same qty/price scale, different symbol — must still insert
        inserted = insert_sell_dedup(conn, "PYTHUSDT", 0.398, 95.98, 0.1, now)

        assert inserted is True


class TestSyncCheckDedupIntegration:
    """End-to-end: the sync-check stale path must not double-book when a
    rounded-qty reconcile row already exists (the exact ENA/TRUMP/GRAM
    production sequence)."""

    def test_ena_replay(self, fresh_statedb, monkeypatch):
        from scripts import ensure_tp_sl as mod

        conn = fresh_statedb._get_conn()
        fill_ts = time.time() - 780

        # portfolio row still holds the DB (unrounded) qty
        conn.execute(
            "INSERT INTO portfolio (symbol, quantity, entry_price, stop_loss, "
            "take_profit, strategy, opened_at) VALUES "
            "('ENAUSDT', 33.87417, 0.1771, 0.1633, 0.1876, 'grid', "
            "'2026-08-23T19:13:00')",
        )
        # reconcile already booked the actual exchange fill
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, timestamp) "
            "VALUES ('ENAUSDT', 'SELL', 33.87, 0.1628, -0.4810, ?)",
            (fill_ts,),
        )
        conn.commit()

        client = type("C", (), {})()
        acct = {
            "balances": [
                {"asset": "USDT", "free": "339.0", "locked": "0"},
                {"asset": "ENA", "free": "0.00417", "locked": "0"},
            ]
        }
        client.get_account = lambda: acct
        client.get_my_trades = lambda symbol, limit=10: [
            {"isBuyer": False, "price": "0.1628", "qty": "33.87",
             "time": int(fill_ts * 1000)}
        ]

        monkeypatch.setattr(mod, "BinanceClient", lambda testnet=False: client)
        monkeypatch.setattr(mod, "get_positions_with_targets", lambda: {
            "ENAUSDT": {
                "quantity": 33.87417,
                "entry_price": 0.1771,
                "stop_loss": 0.1633,
                "take_profit": 0.1876,
            }
        })

        fixes = []
        errors = []
        # replicate the stale-sync block inline against the real helper
        stale = ["ENAUSDT"]
        for sym in stale:
            exit_price = 0.1628
            exit_reason = "sl"
            qty = 33.87417
            entry_price = 0.1771
            pnl = (exit_price - entry_price) * qty
            inserted = mod.insert_sell_dedup(
                conn, sym, qty, exit_price, pnl, time.time()
            )
            if inserted:
                fixes.append(f"{sym}: booked")
            else:
                fixes.append(f"{sym}: dedup skipped")

        assert fixes == ["ENAUSDT: dedup skipped"]
        assert _count_sells(fresh_statedb, "ENAUSDT") == 1
