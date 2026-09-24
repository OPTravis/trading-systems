"""WO-0924-z2 P6-B1: single data-access layer for the bull-paper A/B
experiment tables (paper_bull_*).

Why a dedicated store instead of more StateDB methods: z2-P7(3) plans to
isolate the A/B experiment schema from the main trading DB; keeping all
paper_bull SQL in one class makes that future split a single-file change.
The store shares the caller's StateDB thread-local connection (WAL, Row
factory) so it adds no second connection.

Business modules (bull_paper_portfolio / bull_paper_ab_metrics /
bull_paper_engine_b) keep their domain math and call this store; raw SQL
for these tables lives only here. Atomicity: multi-statement sequences
run inside store.transaction() (one commit at exit — same granularity as
the pre-migration with-blocks); single statements commit in their own
with-block.
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from .state_db import StateDB

# P0-C deploy boundary (2026-08-26 ~23:13 HKT, first P0-C scan): rows
# created after this must carry an explicit ab_group tag.
P0C_DEPLOY_MS = 1787757200000

_SCHEMA_V1 = """
    CREATE TABLE IF NOT EXISTS paper_bull_positions (
        id TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        quantity REAL NOT NULL,
        entry_price REAL NOT NULL,
        entry_time INTEGER NOT NULL,
        stop_loss REAL DEFAULT 0,
        take_profit REAL DEFAULT 0,
        atr_entry REAL DEFAULT 0,
        tier INTEGER DEFAULT 1,
        status TEXT DEFAULT 'open',
        exit_price REAL DEFAULT 0,
        exit_time INTEGER DEFAULT 0,
        realized_pnl REAL DEFAULT 0,
        fees REAL DEFAULT 0,
        notes TEXT DEFAULT '',
        hold_seconds REAL DEFAULT 0,
        slippage_bps REAL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_pbp_symbol ON paper_bull_positions(symbol);
    CREATE INDEX IF NOT EXISTS idx_pbp_side ON paper_bull_positions(side);
    CREATE INDEX IF NOT EXISTS idx_pbp_status ON paper_bull_positions(status);

    CREATE TABLE IF NOT EXISTS paper_bull_trades (
        id TEXT PRIMARY KEY,
        position_id TEXT,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        action TEXT NOT NULL,
        quantity REAL NOT NULL,
        price REAL NOT NULL,
        fee REAL DEFAULT 0,
        notional REAL DEFAULT 0,
        timestamp INTEGER NOT NULL,
        details TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_pbt_time ON paper_bull_trades(timestamp);
    CREATE INDEX IF NOT EXISTS idx_pbt_symbol ON paper_bull_trades(symbol);

    CREATE TABLE IF NOT EXISTS paper_bull_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    );
"""

_SCHEMA_V2 = """
    CREATE TABLE IF NOT EXISTS paper_bull_scaleouts (
        id TEXT PRIMARY KEY,
        position_id TEXT NOT NULL,
        ab_group TEXT NOT NULL,
        symbol TEXT NOT NULL,
        stage INTEGER NOT NULL,
        r_multiple REAL NOT NULL,
        fraction REAL NOT NULL,
        entry_price REAL DEFAULT 0,
        atr_at_entry REAL DEFAULT 0,
        original_qty REAL DEFAULT 0,
        trigger_price REAL NOT NULL,
        status TEXT DEFAULT 'pending',
        fired_time INTEGER DEFAULT 0,
        fired_price REAL DEFAULT 0,
        created_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_pbs_pos ON paper_bull_scaleouts(position_id);
    CREATE INDEX IF NOT EXISTS idx_pbs_status ON paper_bull_scaleouts(ab_group, status);

    CREATE TABLE IF NOT EXISTS paper_bull_filter_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_time INTEGER NOT NULL,
        ab_group TEXT NOT NULL,
        symbol TEXT NOT NULL,
        score REAL,
        decision TEXT NOT NULL,
        fail_filter TEXT DEFAULT '',
        atr14_4h REAL, atr22_1d REAL,
        ema20_4h REAL, ema50_4h REAL, ema200_4h REAL,
        adx14_4h REAL, rvol20 REAL,
        r_multiple REAL,
        regime TEXT,
        notes TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_pbfd_scan ON paper_bull_filter_decisions(scan_time);
    CREATE INDEX IF NOT EXISTS idx_pbfd_group ON paper_bull_filter_decisions(ab_group, decision);

    CREATE TABLE IF NOT EXISTS paper_bull_ab_daily (
        snapshot_date TEXT NOT NULL,
        ab_group TEXT NOT NULL,
        start_cash REAL, cash REAL, market_value REAL,
        equity REAL, total_return REAL, daily_return REAL,
        n_trades INTEGER, n_wins INTEGER, win_rate REAL,
        gross_profit REAL, gross_loss REAL, profit_factor REAL,
        sharpe REAL, max_drawdown REAL,
        avg_hold_hours REAL, median_hold_hours REAL,
        min_hold_hours REAL, max_hold_hours REAL,
        sl_sweep_count INTEGER, sl_sweep_rate REAL,
        whipsaw_count INTEGER,
        kelly_f REAL, kelly_tstat REAL,
        grid_active_count INTEGER,
        exploration_count INTEGER,
        n_open INTEGER,
        reentry_after_sl_count INTEGER DEFAULT 0,
        core_sl_count INTEGER DEFAULT 0,
        PRIMARY KEY (snapshot_date, ab_group)
    );
"""


class BullPaperStore:
    """Owns every paper_bull_* SQL statement (schema + CRUD + analytics)."""

    def __init__(self, db: StateDB):
        self.db = db
        self._tx_conn = None  # bound inside transaction()

    # ── transaction plumbing ─────────────────────────────────────────────
    @contextmanager
    def transaction(self):
        """Group statements into one commit (BEGIN..COMMIT granularity
        identical to the pre-migration single with-blocks)."""
        conn = self.db._get_conn()
        prev = self._tx_conn
        self._tx_conn = conn
        try:
            yield self
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._tx_conn = prev

    def _run(self, fn):
        """Run fn(conn). Inside transaction(): reuse the bound conn (no
        per-statement commit). Outside: own with-block (commit on exit)."""
        if self._tx_conn is not None:
            return fn(self._tx_conn)
        with self.db._get_conn() as conn:
            return fn(conn)

    # ── schema ───────────────────────────────────────────────────────────
    def ensure_tables(self):
        """Create/migrate all six paper_bull tables (idempotent)."""
        with self.db._get_conn() as conn:
            # P0-C review (2026-08-26): columns added after first migration run
            for tbl, col, ddl in (
                ("paper_bull_scaleouts", "entry_price",
                 "ALTER TABLE paper_bull_scaleouts ADD COLUMN entry_price REAL DEFAULT 0"),
                ("paper_bull_scaleouts", "atr_at_entry",
                 "ALTER TABLE paper_bull_scaleouts ADD COLUMN atr_at_entry REAL DEFAULT 0"),
                ("paper_bull_scaleouts", "original_qty",
                 "ALTER TABLE paper_bull_scaleouts ADD COLUMN original_qty REAL DEFAULT 0"),
                ("paper_bull_ab_daily", "min_hold_hours",
                 "ALTER TABLE paper_bull_ab_daily ADD COLUMN min_hold_hours REAL"),
                ("paper_bull_ab_daily", "max_hold_hours",
                 "ALTER TABLE paper_bull_ab_daily ADD COLUMN max_hold_hours REAL"),
                ("paper_bull_ab_daily", "reentry_after_sl_count",
                 # WO-0924-z2 P6-B1: also in _SCHEMA_V2 below — fresh DBs
                 # previously skipped these ALTERs (bug#33's "full schema in
                 # executescript" assumption didn't cover these two) and then
                 # crashed in upsert_ab_daily's 30-col INSERT.
                 "ALTER TABLE paper_bull_ab_daily ADD COLUMN reentry_after_sl_count INTEGER DEFAULT 0"),
                ("paper_bull_ab_daily", "core_sl_count",
                 "ALTER TABLE paper_bull_ab_daily ADD COLUMN core_sl_count INTEGER DEFAULT 0"),
            ):
                _cols = {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
                if not _cols:
                    # bug#33: fresh DB — table is created with the full schema
                    # by the executescript() below; nothing to ALTER yet.
                    continue
                if col not in _cols:
                    conn.execute(ddl)
            conn.executescript(_SCHEMA_V1)
            conn.commit()

        with self.db._get_conn() as conn:
            # P0-A6 (2026-08-26): tracking columns for existing paper DBs
            for col, ddl in (
                ("hold_seconds", "ALTER TABLE paper_bull_positions ADD COLUMN hold_seconds REAL DEFAULT 0"),
                ("slippage_bps", "ALTER TABLE paper_bull_positions ADD COLUMN slippage_bps REAL DEFAULT 0"),
                # P0-C (2026-08-26): A/B engine grouping
                ("ab_group", "ALTER TABLE paper_bull_positions ADD COLUMN ab_group TEXT DEFAULT 'A'"),
            ):
                cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_bull_positions)").fetchall()}
                if col not in cols:
                    conn.execute(ddl)
            # P0-C: trades table also tagged with ab_group (NULL for pre-P0-C)
            tcols = {r[1] for r in conn.execute("PRAGMA table_info(paper_bull_trades)").fetchall()}
            if "ab_group" not in tcols:
                conn.execute("ALTER TABLE paper_bull_trades ADD COLUMN ab_group TEXT DEFAULT 'A'")
            conn.executescript(_SCHEMA_V2)
            conn.commit()

    def reset_core_tables(self):
        """Drop positions/trades/state and re-bootstrap (scaleouts,
        filter_decisions and ab_daily history survive — pinned behavior)."""
        with self.db._get_conn() as conn:
            conn.executescript("""
                DROP TABLE IF EXISTS paper_bull_positions;
                DROP TABLE IF EXISTS paper_bull_trades;
                DROP TABLE IF EXISTS paper_bull_state;
            """)
            conn.commit()
        self.ensure_tables()

    # ── state KV ─────────────────────────────────────────────────────────
    def state_get(self, key: str) -> Optional[str]:
        def q(conn):
            return conn.execute(
                "SELECT value FROM paper_bull_state WHERE key = ?", (key,)
            ).fetchone()
        row = self._run(q)
        return row["value"] if row else None

    def state_set(self, key: str, value: str):
        def q(conn):
            conn.execute(
                """INSERT INTO paper_bull_state (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, int(time.time() * 1000)),
            )
        self._run(q)

    def state_key_updated_at(self, key: str) -> Optional[int]:
        def q(conn):
            return conn.execute(
                "SELECT updated_at FROM paper_bull_state WHERE key=?", (key,)
            ).fetchone()
        row = self._run(q)
        return row["updated_at"] if row else None

    def state_value(self, key: str) -> Optional[str]:
        return self.state_get(key)

    def state_keys_matching_cash_or_start(self) -> List[str]:
        def q(conn):
            return conn.execute(
                "SELECT key FROM paper_bull_state"
                " WHERE key LIKE 'cash_balance%' OR key LIKE 'start_cash%'"
            ).fetchall()
        return [r[0] for r in self._run(q)]

    # ── positions ────────────────────────────────────────────────────────
    def get_position(self, position_id: str) -> Optional[Dict]:
        def q(conn):
            return conn.execute(
                "SELECT * FROM paper_bull_positions WHERE id=?", (position_id,)
            ).fetchone()
        row = self._run(q)
        return dict(row) if row else None

    def get_open_position(self, position_id: str) -> Optional[Dict]:
        def q(conn):
            return conn.execute(
                "SELECT * FROM paper_bull_positions WHERE id = ? AND status = 'open'",
                (position_id,),
            ).fetchone()
        row = self._run(q)
        return dict(row) if row else None

    def get_open_positions(self, group: str, side: Optional[str] = None) -> List[Dict]:
        def q(conn):
            sql = ("SELECT * FROM paper_bull_positions"
                   " WHERE status = 'open' AND COALESCE(ab_group,'A') = ?")
            params: list = [group]
            if side:
                sql += " AND side = ?"
                params.append(side)
            sql += " ORDER BY entry_time DESC"
            return conn.execute(sql, params).fetchall()
        return [dict(r) for r in self._run(q)]

    def get_all_positions(self, group: str, limit: int = 100) -> List[Dict]:
        def q(conn):
            return conn.execute(
                "SELECT * FROM paper_bull_positions WHERE COALESCE(ab_group,'A') = ? "
                "ORDER BY entry_time DESC LIMIT ?",
                (group, limit),
            ).fetchall()
        return [dict(r) for r in self._run(q)]

    def get_open_satellites_by_symbol(self, symbol: str, group: str) -> List[Dict]:
        def q(conn):
            return conn.execute(
                "SELECT * FROM paper_bull_positions WHERE symbol=? AND side='satellite' "
                "AND status='open' AND COALESCE(ab_group,'A') = ?",
                (symbol, group),
            ).fetchall()
        return [dict(r) for r in self._run(q)]

    def closed_positions_since(self, group: str, since_ms: int,
                               side: Optional[str] = None) -> List[Dict]:
        """Closed rows with exit_time >= since_ms (ab_metrics window)."""
        def q(conn):
            sql = ("SELECT * FROM paper_bull_positions"
                   " WHERE status='closed' AND COALESCE(ab_group,'A')=?"
                   " AND exit_time >= ? ORDER BY exit_time")
            params: list = [group, since_ms]
            if side:
                sql += " AND side=?"
                params.append(side)
            return conn.execute(sql, params).fetchall()
        return [dict(r) for r in self._run(q)]

    def count_open_positions(self, group: str) -> int:
        def q(conn):
            return conn.execute(
                "SELECT count(*) FROM paper_bull_positions WHERE status='open' "
                "AND COALESCE(ab_group,'A')=?", (group,)).fetchone()[0]
        return self._run(q)

    def count_positions(self, status: str, ab_group: Optional[str] = None) -> int:
        def q(conn):
            if ab_group is not None:
                return conn.execute(
                    "SELECT count(*) FROM paper_bull_positions"
                    " WHERE status=? AND ab_group=?",
                    (status, ab_group)).fetchone()[0]
            return conn.execute(
                "SELECT count(*) FROM paper_bull_positions WHERE status=?",
                (status,)).fetchone()[0]
        return self._run(q)

    def open_position_mv_rows(self, group: str) -> List[Dict]:
        def q(conn):
            return conn.execute(
                "SELECT symbol, quantity, entry_price FROM paper_bull_positions "
                "WHERE status='open' AND COALESCE(ab_group,'A')=?",
                (group,),
            ).fetchall()
        return [dict(r) for r in self._run(q)]

    def insert_position(self, *, pos_id: str, symbol: str, side: str,
                        quantity: float, entry_price: float, entry_time: int,
                        stop_loss: float, take_profit: float, atr_entry: float,
                        tier: int, fees: float, notes: str, ab_group: str):
        def q(conn):
            conn.execute(
                """INSERT INTO paper_bull_positions
                   (id, symbol, side, quantity, entry_price, entry_time,
                    stop_loss, take_profit, atr_entry, tier, status, fees, notes, ab_group)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
                (pos_id, symbol, side, quantity, entry_price, entry_time,
                 stop_loss, take_profit, atr_entry, tier, fees, notes, ab_group),
            )
        self._run(q)

    def update_position_full_close(self, position_id: str, *, exit_price: float,
                                   exit_time: int, realized_pnl: float,
                                   fees: float, hold_seconds: float):
        def q(conn):
            conn.execute(
                """UPDATE paper_bull_positions
                   SET status='closed', exit_price=?, exit_time=?,
                       realized_pnl=?, fees=?, hold_seconds=?
                   WHERE id=?""",
                (exit_price, exit_time, realized_pnl, fees, hold_seconds,
                 position_id),
            )
        self._run(q)

    def update_position_partial_close(self, position_id: str, *,
                                      remaining_qty: float, fees: float,
                                      realized_pnl: float, note_append: str):
        def q(conn):
            conn.execute(
                """UPDATE paper_bull_positions
                   SET quantity=?, fees=?, realized_pnl=?, notes=notes || ?
                   WHERE id=?""",
                (remaining_qty, fees, realized_pnl, note_append, position_id),
            )
        self._run(q)

    def update_stops(self, position_id: str, stop_loss: float, take_profit: float):
        def q(conn):
            conn.execute(
                "UPDATE paper_bull_positions SET stop_loss=?, take_profit=? WHERE id=?",
                (stop_loss, take_profit, position_id),
            )
        self._run(q)

    # ── trades ───────────────────────────────────────────────────────────
    def insert_trade(self, *, trade_id: str, position_id: str, symbol: str,
                     side: str, action: str, quantity: float, price: float,
                     fee: float, notional: float, timestamp: int,
                     details: str, ab_group: str):
        def q(conn):
            conn.execute(
                """INSERT INTO paper_bull_trades
                   (id, position_id, symbol, side, action, quantity, price, fee, notional, timestamp, details, ab_group)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (trade_id, position_id, symbol, side, action, quantity,
                 price, fee, notional, timestamp, details, ab_group),
            )
        self._run(q)

    def get_trade_history(self, group: str, limit: int = 50) -> List[Dict]:
        def q(conn):
            return conn.execute(
                "SELECT * FROM paper_bull_trades WHERE COALESCE(ab_group,'A') = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (group, limit),
            ).fetchall()
        return [dict(r) for r in self._run(q)]

    def entry_buy_fee(self, position_id: str) -> Optional[float]:
        """bug#33: entry fee of the first BUY leg (paid at open)."""
        def q(conn):
            return conn.execute(
                """SELECT fee FROM paper_bull_trades
                   WHERE position_id=? AND action='BUY'
                   ORDER BY timestamp LIMIT 1""",
                (position_id,),
            ).fetchone()
        row = self._run(q)
        return float(row["fee"]) if (row is not None and row["fee"] is not None) else None

    def entry_buy_qty(self, position_id: str) -> Optional[float]:
        """bug#33 scaleout fallback: qty of the entry BUY leg."""
        def q(conn):
            return conn.execute(
                """SELECT quantity FROM paper_bull_trades
                   WHERE position_id=? AND action='BUY'
                   ORDER BY timestamp LIMIT 1""",
                (position_id,),
            ).fetchone()
        row = self._run(q)
        return float(row["quantity"]) if row is not None else None

    def sl_sweep_rows(self, group: str) -> List[Dict]:
        """SELL legs tagged SL on closed positions (sl-sweep analytics)."""
        def q(conn):
            return conn.execute(
                """SELECT p.entry_time, p.exit_time, t.details
                   FROM paper_bull_positions p
                   JOIN paper_bull_trades t ON t.position_id=p.id
                   WHERE p.status='closed' AND COALESCE(p.ab_group,'A')=?
                     AND t.action='SELL' AND t.details LIKE '%SL%'""",
                (group,),
            ).fetchall()
        return [dict(r) for r in self._run(q)]

    def distinct_position_symbols(self, group: str) -> List[str]:
        def q(conn):
            return conn.execute(
                "SELECT DISTINCT symbol FROM paper_bull_positions"
                " WHERE COALESCE(ab_group,'A')=?", (group,)).fetchall()
        return [r["symbol"] for r in self._run(q)]

    def sl_exit_times(self, group: str, symbol: str) -> List[int]:
        def q(conn):
            return conn.execute(
                """SELECT p.exit_time FROM paper_bull_positions p
                   JOIN paper_bull_trades t ON t.position_id=p.id
                   WHERE p.status='closed' AND COALESCE(p.ab_group,'A')=? AND p.symbol=?
                     AND p.exit_time IS NOT NULL
                     AND t.action='SELL' AND t.details LIKE '%SL%'
                   GROUP BY p.id
                   ORDER BY p.exit_time""",
                (group, symbol),
            ).fetchall()
        return [r["exit_time"] for r in self._run(q)]

    def reentry_buy_exists(self, group: str, symbol: str,
                           t0: int, t1: int) -> bool:
        def q(conn):
            return conn.execute(
                """SELECT 1 FROM paper_bull_trades
                   WHERE ab_group=? AND symbol=? AND action='BUY'
                     AND timestamp > ? AND timestamp <= ?
                   LIMIT 1""",
                (group, symbol, t0, t1),
            ).fetchone()
        return self._run(q) is not None

    def core_sl_count(self, group: str) -> int:
        def q(conn):
            return conn.execute(
                """SELECT count(*) FROM paper_bull_positions p
                   JOIN paper_bull_trades t ON t.position_id=p.id
                   WHERE p.status='closed' AND COALESCE(p.ab_group,'A')=?
                     AND p.side='core' AND t.action='SELL' AND t.details LIKE '%SL%'""",
                (group,),
            ).fetchone()[0]
        return self._run(q)

    def buy_notional_sum(self, group: str) -> float:
        def q(conn):
            return conn.execute(
                """SELECT COALESCE(SUM(notional+fee),0) FROM paper_bull_trades
                   WHERE COALESCE(ab_group,'A')=? AND action='BUY'""",
                (group,)).fetchone()[0]
        return self._run(q) or 0

    def sell_notional_sum(self, group: str) -> float:
        def q(conn):
            return conn.execute(
                """SELECT COALESCE(SUM(notional-fee),0) FROM paper_bull_trades
                   WHERE COALESCE(ab_group,'A')=? AND action='SELL'""",
                (group,)).fetchone()[0]
        return self._run(q) or 0

    # ── scaleouts ────────────────────────────────────────────────────────
    def insert_scaleouts(self, rows: List[tuple]):
        def q(conn):
            conn.executemany(
                """INSERT INTO paper_bull_scaleouts
                   (id, position_id, ab_group, symbol, stage, r_multiple,
                    fraction, entry_price, atr_at_entry, original_qty, trigger_price,
                    status, fired_time, fired_price, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        self._run(q)

    def pending_scaleouts(self, position_id: str) -> List[Dict]:
        def q(conn):
            return conn.execute(
                """SELECT * FROM paper_bull_scaleouts
                   WHERE position_id=? AND status='pending' ORDER BY stage""",
                (position_id,),
            ).fetchall()
        return [dict(r) for r in self._run(q)]

    def fire_scaleout(self, scaleout_id: str, fired_time: int, fired_price: float):
        def q(conn):
            conn.execute(
                """UPDATE paper_bull_scaleouts SET status='fired',
                   fired_time=?, fired_price=? WHERE id=?""",
                (fired_time, fired_price, scaleout_id))
        self._run(q)

    def void_pending_scaleouts(self, position_id: str):
        def q(conn):
            conn.execute(
                """UPDATE paper_bull_scaleouts SET status='voided'
                   WHERE position_id=? AND status='pending'""",
                (position_id,))
        self._run(q)

    # ── filter decisions ─────────────────────────────────────────────────
    def log_filter_decision(self, *, symbol: str, decision: str, ab_group: str,
                            score=None, fail_filter: str = "",
                            atr=None, ema20=None, ema50=None, ema200=None,
                            adx=None, rvol=None, r_multiple=None,
                            regime: str = "", notes: str = ""):
        def q(conn):
            conn.execute(
                """INSERT INTO paper_bull_filter_decisions
                   (scan_time, ab_group, symbol, score, decision, fail_filter,
                    atr14_4h, ema20_4h, ema50_4h, ema200_4h,
                    adx14_4h, rvol20, r_multiple, regime, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (int(time.time() * 1000), ab_group, symbol, score, decision,
                 fail_filter, atr, ema20, ema50, ema200, adx, rvol,
                 r_multiple, regime, notes),
            )
        self._run(q)

    def top_reject_fail_filters(self, ab_group: str, limit: int = 3) -> List[Dict]:
        def q(conn):
            return conn.execute(
                """SELECT fail_filter, count(*) c FROM paper_bull_filter_decisions
                   WHERE ab_group=? AND decision='reject'
                   GROUP BY fail_filter ORDER BY c DESC LIMIT ?""",
                (ab_group, limit)).fetchall()
        return [dict(r) for r in self._run(q)]

    # ── ab_daily ─────────────────────────────────────────────────────────
    def daily_equity_series(self, group: str) -> List[Dict]:
        def q(conn):
            return conn.execute(
                """SELECT snapshot_date, equity FROM paper_bull_ab_daily
                   WHERE ab_group=? ORDER BY snapshot_date""",
                (group,),
            ).fetchall()
        return [dict(r) for r in self._run(q)]

    def upsert_ab_daily(self, *, snapshot_date: str, ab_group: str,
                        start_cash: float, cash: float, market_value: float,
                        equity: float, total_return: float,
                        n_trades: int, n_wins: int, win_rate: float,
                        gross_profit: float, gross_loss: float,
                        profit_factor: float, sharpe: float,
                        max_drawdown: float, avg_hold_hours: float,
                        median_hold_hours: float, min_hold_hours: float,
                        max_hold_hours: float, sl_sweep_count: int,
                        sl_sweep_rate: float, whipsaw_count: int,
                        kelly_f: float, kelly_tstat: float,
                        grid_active_count: int, exploration_count: int,
                        n_open: int, reentry_after_sl_count: int,
                        core_sl_count: int):
        def q(conn):
            conn.execute(
                """INSERT INTO paper_bull_ab_daily
                   (snapshot_date, ab_group, start_cash, cash, market_value,
                    equity, total_return, daily_return,
                    n_trades, n_wins, win_rate, gross_profit, gross_loss,
                    profit_factor, sharpe, max_drawdown,
                    avg_hold_hours, median_hold_hours,
                    min_hold_hours, max_hold_hours,
                    sl_sweep_count, sl_sweep_rate, whipsaw_count,
                    kelly_f, kelly_tstat, grid_active_count,
                    exploration_count, n_open,
                    reentry_after_sl_count, core_sl_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(snapshot_date, ab_group) DO UPDATE SET
                     cash=excluded.cash, market_value=excluded.market_value,
                     equity=excluded.equity, total_return=excluded.total_return,
                     n_trades=excluded.n_trades, n_wins=excluded.n_wins,
                     win_rate=excluded.win_rate, gross_profit=excluded.gross_profit,
                     gross_loss=excluded.gross_loss,
                     profit_factor=excluded.profit_factor, sharpe=excluded.sharpe,
                     max_drawdown=excluded.max_drawdown,
                     avg_hold_hours=excluded.avg_hold_hours,
                     median_hold_hours=excluded.median_hold_hours,
                     min_hold_hours=excluded.min_hold_hours,
                     max_hold_hours=excluded.max_hold_hours,
                     sl_sweep_count=excluded.sl_sweep_count,
                     sl_sweep_rate=excluded.sl_sweep_rate,
                     whipsaw_count=excluded.whipsaw_count,
                     kelly_f=excluded.kelly_f, kelly_tstat=excluded.kelly_tstat,
                     grid_active_count=excluded.grid_active_count,
                     exploration_count=excluded.exploration_count,
                     n_open=excluded.n_open,
                     reentry_after_sl_count=excluded.reentry_after_sl_count,
                     core_sl_count=excluded.core_sl_count""",
                (snapshot_date, ab_group, start_cash, cash, market_value,
                 equity, total_return, 0.0,
                 n_trades, n_wins, win_rate, gross_profit, gross_loss,
                 profit_factor, sharpe, max_drawdown,
                 avg_hold_hours, median_hold_hours, min_hold_hours,
                 max_hold_hours, sl_sweep_count, sl_sweep_rate,
                 whipsaw_count, kelly_f, kelly_tstat, grid_active_count,
                 exploration_count, n_open, reentry_after_sl_count,
                 core_sl_count),
            )
        self._run(q)

    # ── integrity protocol ───────────────────────────────────────────────
    def verify_isolation(self) -> Dict[str, Any]:
        """P0-C protocol: A/B sleeves never cross-contaminate. All checks
        are SQL integrity probes; anomaly strings are part of the pinned
        protocol output."""
        anomalies: List[str] = []
        with self.db._get_conn() as c:
            untagged = c.execute(
                """SELECT count(*) FROM paper_bull_positions
                   WHERE ab_group IS NULL AND entry_time > ?""",
                (P0C_DEPLOY_MS,),
            ).fetchone()[0]
            if untagged:
                anomalies.append(f"{untagged} post-deploy position(s) with NULL ab_group")
            untagged_t = c.execute(
                """SELECT count(*) FROM paper_bull_trades
                   WHERE ab_group IS NULL AND timestamp > ?""",
                (P0C_DEPLOY_MS,),
            ).fetchone()[0]
            if untagged_t:
                anomalies.append(f"{untagged_t} post-deploy trade(s) with NULL ab_group")

            mixed = c.execute(
                """SELECT position_id, count(DISTINCT COALESCE(ab_group,'A')) g
                   FROM paper_bull_trades GROUP BY position_id HAVING g > 1""").fetchall()
            if mixed:
                anomalies.append(f"{len(mixed)} position_id(s) span multiple ab_groups")

            keys = {r[0] for r in c.execute(
                "SELECT key FROM paper_bull_state WHERE key LIKE 'cash_balance%' OR key LIKE 'start_cash%'")}
            if "cash_balance_B" not in keys:
                anomalies.append("B cash key cash_balance_B missing")
            b_pos = c.execute("SELECT count(*) FROM paper_bull_positions WHERE ab_group='B'").fetchone()[0]
            b_cash = c.execute("SELECT value FROM paper_bull_state WHERE key='cash_balance_B'").fetchone()
            if b_pos > 0 and not b_cash:
                anomalies.append(f"{b_pos} B positions but no B cash balance")

            for grp, ck in (("A", "cash_balance"), ("B", "cash_balance_B")):
                crow = c.execute("SELECT value FROM paper_bull_state WHERE key=?", (ck,)).fetchone()
                if not crow:
                    continue
                cash_now = float(crow[0])
                bought = c.execute(
                    """SELECT COALESCE(SUM(notional+fee),0) FROM paper_bull_trades
                       WHERE COALESCE(ab_group,'A')=? AND action='BUY'""", (grp,)).fetchone()[0] or 0
                sold = c.execute(
                    """SELECT COALESCE(SUM(notional-fee),0) FROM paper_bull_trades
                       WHERE COALESCE(ab_group,'A')=? AND action='SELL'""", (grp,)).fetchone()[0] or 0
                sk = "start_cash" if grp == "A" else "start_cash_B"
                srow = c.execute("SELECT value FROM paper_bull_state WHERE key=?", (sk,)).fetchone()
                start = float(srow[0]) if srow else 0.0
                expected = start - bought + sold
                if abs(cash_now - expected) > 0.05:
                    anomalies.append(
                        f"{grp} cash mismatch: state=${cash_now:.2f} vs ledger=${expected:.2f}")

        return {"ok": not anomalies, "anomalies": anomalies}


    # ── bootstrap/debug helpers ──────────────────────────────────────────
    _COUNTABLE = frozenset({
        "paper_bull_positions", "paper_bull_trades", "paper_bull_state",
        "paper_bull_scaleouts", "paper_bull_filter_decisions",
        "paper_bull_ab_daily",
    })

    def table_row_count(self, table: str) -> int:
        """Whitelisted COUNT(*) for bootstrap/summary tooling (phase2_init).
        Keeps table-name SQL out of scripts."""
        if table not in self._COUNTABLE:
            raise ValueError(f"table not managed by BullPaperStore: {table}")
        def q(conn):
            return conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return self._run(q)


def new_position_id() -> str:
    return f"paper_{uuid.uuid4().hex[:12]}"


def new_trade_id() -> str:
    return f"trade_{uuid.uuid4().hex[:12]}"


def new_scaleout_id() -> str:
    return f"so_{uuid.uuid4().hex[:10]}"
