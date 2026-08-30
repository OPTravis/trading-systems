"""bug#32 (2026-08-31): stale in-run position view -> ghost SL placement.

Timeline anchor -- HEMI 8/31 (06:00 HKT trailing-check run):
  05:11  cron-scan opens HEMI 459.7 @ $0.01313, SL placed.
  05:46  TP fills @ $0.01405 -- exchange side fully exited.
  06:00  trailing-check run:
           - cmd_trailing_check() snapshots positions via get_account() FIRST.
             Binance account snapshots lag (eventual consistency): still shows
             459.7 HEMI free.
           - reconcile_fills correctly books the SELL fill and closes the
             outcome (06:00:06 OUTCOME_CLOSE log).
           - the uncovered-position loop below STILL uses the stale snapshot,
             sees a "naked" 459.7 HEMI position, and fires a
             STOP_LOSS_LIMIT that the exchange rejects with -2010
             (insufficient balance) 3x -> false "uncovered SL failed" alarm.
  06:05  next run: account snapshot caught up -> no_positions -> self-heals.

Fix:
  Before protecting a position in the uncovered-SL loop, consult the ledger
  (trade_outcomes). If the ledger knows the symbol and shows NO open entry,
  the position is already fully exited -- skip ghost protection (fail-open on
  guard errors). Symbols the ledger has never seen keep the old behavior.
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.cmd_trailing_check import cmd_trailing_check
from src.state_db import StateDB


# ----------------------------------------------------------------- fixtures

@pytest.fixture
def ledger_db(tmp_path):
    db = StateDB(str(tmp_path / "bug32_state.db"))
    yield db
    db.close()


def _insert_outcome(db, symbol, status, entry_price=0.01313, qty=459.7):
    conn = db._get_conn()
    cur = conn.execute(
        """INSERT INTO trade_outcomes (symbol, entry_time, entry_price, qty,
           status, peak_price, trough_price, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (symbol, 1788124281.0, entry_price, qty, status,
         entry_price, entry_price, 1788124281.0, 1788125000.0),
    )
    conn.commit()
    return cur.lastrowid


def _stale_client(price="0.01406"):
    """Client replaying the 06:00 stale snapshot: HEMI 459.7 still shows in
    get_account (exchange lag), but no open orders exist (TP consumed them)."""
    c = MagicMock()
    c.get_account.return_value = {"balances": [
        {"asset": "USDT", "free": "500", "locked": "0"},
        {"asset": "HEMI", "free": "459.7", "locked": "0"},  # stale!
    ]}
    c.get_24hr_stats.return_value = {"last_price": price}
    c.get_klines.return_value = [{"close": price}] * 20
    c.get_open_orders.return_value = []          # TP filled -> nothing open
    c.place_order.return_value = {"orderId": 1, "status": "NEW"}
    c.cancel_order.return_value = {"status": "CANCELED"}
    c.cancel_all_orders.return_value = True
    c.get_price_precision.return_value = 6
    c.get_my_trades.return_value = []
    c.get_ticker_price.return_value = float(price)
    return c


def _run(client, ledger_db):
    ts = MagicMock()
    ts.get_all.return_value = {}
    risk_mgr = MagicMock()
    notifier = MagicMock()
    notifier.send_text.return_value = True

    ind_mock = MagicMock()
    ind_mock.atr.return_value = 0.0005

    # create=True: on pre-fix code the get_state_db attribute doesn't exist,
    # so the patch never takes effect there and the ghost-SL path runs (red).
    with patch("src.cmd_trailing_check.BinanceClient", return_value=client), \
         patch("src.cmd_trailing_check.TrailingStop", return_value=ts), \
         patch("src.cmd_trailing_check.get_risk_manager", return_value=risk_mgr), \
         patch("src.cmd_trailing_check.FeishuNotifier", return_value=notifier), \
         patch("src.cmd_trailing_check.Indicators", ind_mock), \
         patch("src.cmd_trailing_check.get_state_db", return_value=ledger_db, create=True), \
         patch("scripts.reconcile_fills.reconcile_fills", return_value={}), \
         patch("src.tp_sl_tracker.get_all_tracked", return_value={}):
        cmd_trailing_check()
    return notifier, client


# -------------------------------------------------------------------- tests

class TestBug32GhostSL:

    def test_ghost_sl_not_placed_after_ledger_close(self, ledger_db, capsys):
        """THE 06:00 case: stale snapshot + ledger already closed -> the
        uncovered-SL loop must NOT attempt any placement, and must NOT page."""
        _insert_outcome(ledger_db, "HEMIUSDT", status="closed")
        client = _stale_client()
        notifier, client = _run(client, ledger_db)

        client.place_order.assert_not_called()
        notifier.send_text.assert_not_called()
        out = capsys.readouterr().out
        assert "uncovered_sl_skipped_position_closed" in out
        assert "uncovered_sl_failed" not in out

    def test_open_position_still_protected(self, ledger_db, capsys):
        """A genuinely open ledger entry must still get its SL (no regression
        on the core safety net)."""
        _insert_outcome(ledger_db, "HEMIUSDT", status="open")
        client = _stale_client()
        notifier, client = _run(client, ledger_db)

        assert client.place_order.call_count >= 1
        args, kwargs = client.place_order.call_args
        assert args[2] == "STOP_LOSS_LIMIT"

    def test_unknown_symbol_keeps_protection(self, ledger_db, capsys):
        """A balance the ledger has never seen (external/manual) keeps the old
        behavior -- guard must not silently strip protection."""
        client = _stale_client()
        notifier, client = _run(client, ledger_db)

        assert client.place_order.call_count >= 1

    def test_guard_fail_open_on_db_error(self, ledger_db, capsys):
        """If the ledger is unreadable, protection must proceed (fail-open)."""
        broken = MagicMock()
        broken._get_conn.side_effect = RuntimeError("db unavailable")
        client = _stale_client()
        notifier, client = _run(client, broken)

        assert client.place_order.call_count >= 1
