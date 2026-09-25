"""
SQLite-backed state persistence for crypto-ai-trader.
Replaces scattered JSON files with ACID-compliant single-database storage.

Tables:
- trailing_stop: TrailingStop state (symbol, entry_price, highest, sl, activated)
- portfolio: Portfolio positions (symbol, qty, entry, strategy, opened_at)
- drawdown: Drawdown breaker state (single row)
- risk_guard: RiskManager loss_guard state (daily_pnl, streak, last_reset)
- trades: Trade history for PnL tracking
- kv: Generic key-value store for adapter configs, etc.
- grid_state: Grid trading state (replaces grid_state.json)
- dca_state: DCA strategy state (replaces dca_state.json)
- strategy_state: Strategy adaptor state (replaces strategy_state.json)
- audit_log: Audit trail
"""

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default DB path (bug#15, 2026-08-21): the live DB lives on local ext4 at
# /root/trading-state/state.db. The old project-relative data/state.db sits
# on the hpvs_fs network mount where SQLite repeatedly corrupts — it is NOT
# a valid fallback. Scripts without a wrapper (missing `set -a; source .env`)
# used to silently connect to the corrupt file and crash with
# "file is not a database". Keep the legacy path only as a last resort for
# non-cloud environments where /root/trading-state does not exist.
_ROOT_DB = Path("/root/trading-state/state.db")
_LEGACY_DB = Path(__file__).parent.parent / "data" / "state.db"
DEFAULT_DB_PATH = _ROOT_DB if _ROOT_DB.exists() else _LEGACY_DB


class StateDB:
    """Thread-safe SQLite state persistence with connection pooling."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # P0-A4 (2026-08-26): serialize writers within process to cut
        # "database is locked" collisions (busy_timeout=30000 remains the
        # cross-process safety net; DB already on local ext4 /root/trading-state).
        self._write_lock = threading.RLock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local connection (sqlite3 is not thread-safe by default).

        FIX M1: Auto-close stale connections to prevent file descriptor leaks.
        Connections older than 5 minutes are recycled.

        FIX P2-3: Periodic integrity check to detect database corruption early.
        Integrity check runs once per connection recycling cycle.
        """
        now = time.monotonic()
        # Check if existing connection is stale (>5 min old)
        if hasattr(self._local, "conn") and self._local.conn is not None:
            conn_age = getattr(self._local, "conn_created", 0)
            if now - conn_age > 300:  # 5 minutes
                # Commit pending transaction before recycling connection
                try:
                    self._local.conn.commit()
                except Exception as e:
                    # LOUD: closing anyway discards any uncommitted writes.
                    logger.error(f"StateDB: commit before recycle failed: {e}")
                    record_db_failure("state_db.recycle", f"commit before recycle failed: {e}")
                try:
                    self._local.conn.close()
                except Exception:
                    logger.error("Failed to close stale DB connection", exc_info=True)
                self._local.conn = None
                self._local.conn_created = 0

        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self.db_path), check_same_thread=False, timeout=30
            )
            self._local.conn.row_factory = sqlite3.Row
            # FIX 2026-08-20 (bug#8): busy_timeout FIRST (so the journal-mode
            # switch itself can wait out concurrent writers), then WAL.
            # The old config forced journal_mode=DELETE: every commit needed an
            # EXCLUSIVE file lock + multiple fsyncs while up to three cron
            # processes (trailing-check */5, protection_guardian, long-running
            # cron-scan) collided on the same sqlite file at :00/:30 cron
            # boundaries. WAL lets readers and the writer proceed concurrently
            # and removes the exclusive-lock fsync storm.
            self._local.conn.execute("PRAGMA busy_timeout=30000")
            try:
                mode_row = self._local.conn.execute(
                    "PRAGMA journal_mode=WAL"
                ).fetchone()
                mode = str(mode_row[0]).lower() if mode_row else "unknown"
                if mode != "wal":
                    # Switch refused (concurrent holder or unsupported FS).
                    # Continue on current mode — busy_timeout still applies —
                    # but say so LOUDLY so ops can see it in cron logs.
                    logger.warning(
                        "StateDB: journal_mode stayed '%s' (WAL switch refused: "
                        "concurrent holder or unsupported FS) — continuing with "
                        "busy_timeout=30000 only",
                        mode,
                    )
            except Exception as e:
                logger.warning(f"StateDB: journal_mode=WAL failed: {e}")
            self._local.conn.execute("PRAGMA synchronous=FULL")
            self._local.conn_created = now

            # Run integrity check on new connections (throttled to once per hour)
            last_check = getattr(self._local, "last_integrity_check", 0)
            if now - last_check > 3600:  # 1 hour
                try:
                    result = self._local.conn.execute("PRAGMA quick_check").fetchone()
                    if result and result[0] != "ok":
                        logger.error(f"StateDB: quick_check failed: {result[0]}")
                        # Create backup before potential corruption
                        self.backup()
                    self._local.last_integrity_check = now
                except Exception as e:
                    logger.warning(f"StateDB: quick_check error: {e}")

        return self._local.conn

    def transaction(self):
        """Context manager for atomic multi-operation transactions.
        Usage:
            with db.transaction() as conn:
                db.portfolio_set(...)
                db.portfolio_set_cash_balance(...)
        All operations within the block share the same connection and
        are committed together, or rolled back on exception.
        """

        class _TransactionCtx:
            def __init__(self, db):
                self.db = db
                self.conn = None

            def __enter__(self):
                self.conn = self.db._get_conn()
                self.conn.execute("BEGIN IMMEDIATE")
                return self.conn

            def __exit__(self, exc_type, _exc_val, _exc_tb):
                if self.conn is not None:
                    if exc_type is None:
                        self.conn.commit()
                    else:
                        self.conn.rollback()
                return False  # Don't suppress exceptions

        return _TransactionCtx(self)

    def wal_checkpoint(self) -> bool:
        """Force a WAL checkpoint to merge the WAL file into the main database.

        Call this after batch operations (e.g., after a scan completes)
        to keep the WAL file small and prevent bloat.

        Returns:
            True if checkpoint succeeded, False otherwise.
        """
        try:
            conn = self._get_conn()
            result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if result:
                # TRUNCATE mode: (busy, log_pages, checkpointed_pages)
                logger.debug(
                    f"StateDB: WAL checkpoint complete "
                    f"(busy={result[0]}, log_pages={result[1]}, checkpointed={result[2]})"
                )
            return True
        except Exception as e:
            logger.warning(f"StateDB: wal_checkpoint failed: {e}")
            return False

    def close(self):
        """Close all thread-local connections. Call on shutdown."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            try:
                self._local.conn.close()
            except Exception:
                logger.error("Failed to close DB connection on shutdown", exc_info=True)
            self._local.conn = None
            self._local.conn_created = 0

    def check_integrity(self) -> Dict[str, Any]:
        """Run SQLite integrity check and return results.

        Returns:
            {
                "ok": bool,           # True if no corruption detected
                "errors": List[str],  # List of integrity errors (empty if ok)
                "tables": int,        # Number of tables
                "size_mb": float,     # Database file size in MB
            }
        """
        result = {"ok": True, "errors": [], "tables": 0, "size_mb": 0.0}
        try:
            conn = self._get_conn()

            # Run PRAGMA integrity_check
            rows = conn.execute("PRAGMA integrity_check").fetchall()
            for row in rows:
                if row[0] != "ok":
                    result["ok"] = False
                    result["errors"].append(row[0])

            # Count tables
            tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()
            result["tables"] = tables[0] if tables else 0

            # Get database size
            if self.db_path.exists():
                result["size_mb"] = self.db_path.stat().st_size / (1024 * 1024)

            if result["ok"]:
                logger.info(
                    f"StateDB: integrity check passed ({result['tables']} tables, {result['size_mb']:.1f} MB)"
                )
            else:
                logger.error(f"StateDB: integrity check FAILED: {result['errors']}")

        except Exception as e:
            result["ok"] = False
            result["errors"].append(str(e))
            logger.error(f"StateDB: integrity check error: {e}")

        return result

    def backup(self, backup_path: Optional[str] = None) -> bool:
        """Create a backup of the database.

        Args:
            backup_path: Path for backup file. If None, uses state.db.backup.YYYYMMDD

        Returns:
            True if backup succeeded, False otherwise.
        """
        try:
            if backup_path is None:
                from datetime import datetime, timezone

                date_str = datetime.now().strftime("%Y%m%d")
                backup_path = str(self.db_path.parent / f"state.db.backup.{date_str}")

            conn = self._get_conn()
            backup_conn = sqlite3.connect(backup_path)
            conn.backup(backup_conn)
            backup_conn.close()

            logger.info(f"StateDB: backup created at {backup_path}")
            return True
        except Exception as e:
            logger.error(f"StateDB: backup failed: {e}")
            return False

    def _init_db(self):
        """Create tables if not exist."""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trailing_stop (
                symbol TEXT PRIMARY KEY,
                entry_price REAL,
                highest_price REAL,
                sl_price REAL,
                activated INTEGER DEFAULT 0,
                updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS portfolio (
                symbol TEXT PRIMARY KEY,
                quantity REAL,
                entry_price REAL,
                strategy TEXT,
                opened_at REAL,
                updated_at REAL,
                stop_loss REAL,
                take_profit REAL,
                invest_pct REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS drawdown (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                high_watermark REAL DEFAULT 0,
                current_drawdown_pct REAL DEFAULT 0,
                max_drawdown_pct REAL DEFAULT 0,
                tripped_count INTEGER DEFAULT 0,
                tripped_at REAL,
                reset_at REAL,
                history TEXT,
                updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS risk_guard (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                daily_pnl REAL DEFAULT 0,
                streak INTEGER DEFAULT 0,
                last_reset REAL,
                updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                side TEXT,
                qty REAL,
                price REAL,
                pnl REAL,
                timestamp REAL
            );
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS grid_state (
                symbol TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                config_json TEXT NOT NULL,
                levels_json TEXT NOT NULL,
                stats_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dca_state (
                symbol TEXT PRIMARY KEY,
                rounds_done INTEGER DEFAULT 0,
                total_invested REAL DEFAULT 0,
                avg_price REAL DEFAULT 0,
                next_buy_at REAL,
                status TEXT DEFAULT 'active',
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS strategy_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS idx_drawdown_id ON drawdown(id);
            CREATE INDEX IF NOT EXISTS idx_grid_symbol ON grid_state(symbol);
            CREATE INDEX IF NOT EXISTS idx_dca_symbol ON dca_state(symbol);
            CREATE INDEX IF NOT EXISTS idx_strategy_key ON strategy_state(key);
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                action TEXT,
                details TEXT,
                old_value TEXT,
                new_value TEXT,
                source TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp);
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                date TEXT,
                symbol TEXT,
                type TEXT,
                decision TEXT,
                score REAL,
                price REAL,
                qty REAL,
                side TEXT,
                strategy TEXT,
                reasons TEXT,
                signals TEXT,
                bear_score REAL,
                bear_veto INTEGER,
                bear_reasons TEXT,
                bear_confidence TEXT,
                research TEXT,
                exit_price REAL,
                pnl_pct REAL
            );
            CREATE INDEX IF NOT EXISTS idx_decisions_symbol ON decisions(symbol);
            CREATE INDEX IF NOT EXISTS idx_decisions_time ON decisions(timestamp);
            CREATE INDEX IF NOT EXISTS idx_decisions_date ON decisions(date);
            CREATE INDEX IF NOT EXISTS idx_decisions_type ON decisions(type);

            -- Phase 0: Trade outcome tracking for self-learning pipeline
            CREATE TABLE IF NOT EXISTS trade_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                entry_time REAL NOT NULL,
                entry_date TEXT,
                entry_price REAL NOT NULL,
                qty REAL NOT NULL,
                score REAL,
                strategy TEXT,
                factors_json TEXT,       -- JSON: {technical, trend, volume, ...}
                context_json TEXT,       -- JSON: {regime, fng, btc_trend, kelly, ...}
                status TEXT DEFAULT 'open',  -- 'open' or 'closed'
                -- Exit data (filled when position closes)
                exit_time REAL,
                exit_price REAL,
                exit_reason TEXT,        -- tp1/tp2/tp3/sl/trailing/max_hold/manual
                -- Computed metrics
                pnl_pct REAL,
                pnl_absolute REAL,
                net_pnl_pct REAL,        -- after fees
                net_pnl_absolute REAL,
                time_held_hours REAL,
                max_profit_pct REAL,
                max_drawdown_pct REAL,
                peak_price REAL,         -- highest price seen during trade
                trough_price REAL,       -- lowest price seen during trade
                is_win INTEGER,          -- 1 if net_pnl > 0
                created_at REAL,
                updated_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_outcomes_symbol ON trade_outcomes(symbol);
            CREATE INDEX IF NOT EXISTS idx_outcomes_status ON trade_outcomes(status);
            CREATE INDEX IF NOT EXISTS idx_outcomes_entry_time ON trade_outcomes(entry_time);
            CREATE INDEX IF NOT EXISTS idx_outcomes_exit_time ON trade_outcomes(exit_time);
            CREATE INDEX IF NOT EXISTS idx_outcomes_strategy ON trade_outcomes(strategy);
            CREATE INDEX IF NOT EXISTS idx_portfolio_strategy ON portfolio(strategy);
            CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);

            -- WO-0924-z2 P6-B2: bull regime transition log (main-DB table;
            -- DDL moved here from bull_regime._ensure_table so the module
            -- no longer touches raw SQL)
            CREATE TABLE IF NOT EXISTS bull_regime_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                bar_ts INTEGER NOT NULL,
                from_state TEXT,
                to_state TEXT NOT NULL,
                reason TEXT,
                btc_close REAL,
                btc_sma200 REAL,
                fng_avg REAL,
                fng_today INTEGER,
                adx REAL,
                conditions_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_bull_regime_log_ts ON bull_regime_log(ts);

            -- P6-B3: paper trading store (DDL absorbed from paper_trader)
            CREATE TABLE IF NOT EXISTS paper_trades (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                quantity REAL NOT NULL,
                fill_price REAL NOT NULL,
                slippage_pct REAL,
                fee_usdt REAL,
                notional_usdt REAL,
                status TEXT DEFAULT 'filled',
                timestamp REAL NOT NULL,
                details TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol ON paper_trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_paper_trades_time ON paper_trades(timestamp);

            CREATE TABLE IF NOT EXISTS paper_portfolio (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_pending_orders (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                stop_price REAL,
                status TEXT DEFAULT 'open',
                created_at REAL NOT NULL,
                expires_at REAL,
                details TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_paper_pending_symbol ON paper_pending_orders(symbol);

            -- WO-0924 P2: Ledger (single bookkeeping layer). Append-only
            -- fill event log + shadow book for the parallel-booking trial.
            -- Additive only: no existing table or column is touched.
            CREATE TABLE IF NOT EXISTS ledger_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                type TEXT NOT NULL,              -- BUY / SELL
                symbol TEXT NOT NULL,
                qty REAL NOT NULL,
                price REAL NOT NULL,
                order_id TEXT,
                source TEXT,
                pnl REAL,
                exit_reason TEXT,
                deduct_cash INTEGER,
                payload_json TEXT,
                round_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ledger_events_ts ON ledger_events(ts);
            CREATE INDEX IF NOT EXISTS idx_ledger_events_symbol ON ledger_events(symbol);
            CREATE INDEX IF NOT EXISTS idx_ledger_events_order ON ledger_events(order_id);
            CREATE TABLE IF NOT EXISTS ledger_shadow_positions (
                symbol TEXT PRIMARY KEY,
                net_qty REAL NOT NULL DEFAULT 0,
                avg_entry_price REAL NOT NULL DEFAULT 0,
                cost_basis REAL NOT NULL DEFAULT 0,
                cash_delta REAL NOT NULL DEFAULT 0,
                opened_at REAL,
                updated_at REAL
            );
            -- P2-④: one row per shadow_diff round — the durable history
            -- behind the weekly aggregate report (clean rate / diff kinds /
            -- pending lag / longest clean streak).
            CREATE TABLE IF NOT EXISTS ledger_shadow_rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                round INTEGER NOT NULL,
                round_id TEXT,
                outcome TEXT NOT NULL,          -- clean | diff | pending | error
                diff_count INTEGER NOT NULL DEFAULT 0,
                pending_count INTEGER NOT NULL DEFAULT 0,
                diff_kinds TEXT,                -- JSON array of kinds
                consecutive_clean INTEGER NOT NULL DEFAULT 0,
                dust_exempt_count INTEGER NOT NULL DEFAULT 0
                -- Travis A (2026-09-24): per-round dust-tier position_qty
                -- exemptions (gap < DRIFT_QTY_ABS) — never silent, feeds
                -- the weekly report's exemption column
            );
            CREATE INDEX IF NOT EXISTS idx_ledger_shadow_rounds_ts
                ON ledger_shadow_rounds(ts);
            """)
        conn.commit()

        # Migration: add invest_pct column if missing (for existing databases)
        # 2026-08-27: conditional check — CREATE TABLE already includes invest_pct
        # for fresh DBs, unconditional ALTER raised "duplicate column name" warning
        # on every init (polluted stderr; weekly_learning mis-logged it as error 8/23).
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(portfolio)").fetchall()}
            if "invest_pct" not in cols:
                conn.execute("ALTER TABLE portfolio ADD COLUMN invest_pct REAL DEFAULT 0")
                conn.commit()
        except Exception as e:
            logger.warning("state_db._init_db: invest_pct migration: " + str(e))

        # Travis A (2026-09-24): dust_exempt_count for ledger_shadow_rounds
        # (existing production table predates the dust-exemption column)
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(ledger_shadow_rounds)").fetchall()}
            if "dust_exempt_count" not in cols:
                conn.execute(
                    "ALTER TABLE ledger_shadow_rounds "
                    "ADD COLUMN dust_exempt_count INTEGER NOT NULL DEFAULT 0")
                conn.commit()
        except Exception as e:
            logger.warning("state_db._init_db: dust_exempt migration: " + str(e))


        # P0-A4 (2026-08-26): idempotency key for trades (prevents duplicate
        # trade rows from double-record paths like bug#13).
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
            if "client_order_id" not in cols:
                conn.execute("ALTER TABLE trades ADD COLUMN client_order_id TEXT")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_client_order_id "
                "ON trades(client_order_id) WHERE client_order_id IS NOT NULL"
            )
            conn.commit()
        except Exception as e:
            logger.warning("state_db._init_db: client_order_id migration: " + str(e))
            pass  # column already exists

    # ==================== Trailing Stop ====================

    def ts_get(self, symbol: str) -> Optional[Dict]:
        row = (
            self._get_conn()
            .execute("SELECT * FROM trailing_stop WHERE symbol = ?", (symbol,))
            .fetchone()
        )
        if not row:
            return None
        return {
            "symbol": row["symbol"],
            "entry_price": row["entry_price"],
            "highest_price": row["highest_price"],
            "sl_price": row["sl_price"],
            "activated": bool(row["activated"]),
            "updated_at": row["updated_at"],
        }

    def ts_get_all(self) -> Dict[str, Dict]:
        rows = self._get_conn().execute("SELECT * FROM trailing_stop").fetchall()
        return {r["symbol"]: dict(r) for r in rows}

    def ts_set(self, symbol: str, data: Dict):
        now = time.time()
        self._get_conn().execute(
            """INSERT INTO trailing_stop (symbol, entry_price, highest_price, sl_price, activated, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
               entry_price=excluded.entry_price,
               highest_price=excluded.highest_price,
               sl_price=excluded.sl_price,
               activated=excluded.activated,
               updated_at=excluded.updated_at""",
            (
                symbol,
                data.get("entry_price", 0),
                data.get("highest_price", 0),
                data.get("sl_price", 0),
                1 if data.get("activated") else 0,
                now,
            ),
        )
        self._get_conn().commit()

    def ts_remove(self, symbol: str):
        self._get_conn().execute(
            "DELETE FROM trailing_stop WHERE symbol = ?", (symbol,)
        )
        self._get_conn().commit()

    # ==================== Portfolio ====================

    def portfolio_get(self, symbol: str) -> Optional[Dict]:
        symbol = symbol.replace("/", "")
        row = (
            self._get_conn()
            .execute("SELECT * FROM portfolio WHERE symbol = ?", (symbol,))
            .fetchone()
        )
        if not row:
            return None
        return dict(row)

    def portfolio_get_all(self) -> Dict[str, Dict]:
        rows = self._get_conn().execute("SELECT * FROM portfolio").fetchall()
        return {r["symbol"]: dict(r) for r in rows}

    def portfolio_get_cash_balance(self) -> float:
        """Get cash_balance from kv store. Returns 0.0 if not set."""
        row = (
            self._get_conn()
            .execute("SELECT value FROM kv WHERE key = 'cash_balance'")
            .fetchone()
        )
        if row and row["value"]:
            try:
                return float(row["value"])
            except (ValueError, TypeError):
                logger.error("Failed to parse cash_balance from DB", exc_info=True)
        return 0.0

    def portfolio_set_cash_balance(self, cash_balance: float):
        """Save cash_balance to kv store."""
        now = time.time()
        self._get_conn().execute(
            """INSERT INTO kv (key, value, updated_at)
               VALUES ('cash_balance', ?, ?)
               ON CONFLICT(key) DO UPDATE SET
               value=excluded.value,
               updated_at=excluded.updated_at""",
            (str(cash_balance), now),
        )
        self._get_conn().commit()

    def portfolio_set(self, symbol: str, data: Dict):
        # Normalize symbol format: strip "/" (CHZ/USDT → CHZUSDT)
        symbol = symbol.replace("/", "")
        now = time.time()
        self._get_conn().execute(
            """INSERT INTO portfolio (symbol, quantity, entry_price, strategy, opened_at, updated_at, stop_loss, take_profit, invest_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
               quantity=excluded.quantity,
               entry_price=excluded.entry_price,
               strategy=excluded.strategy,
               opened_at=excluded.opened_at,
               updated_at=excluded.updated_at,
               stop_loss=COALESCE(excluded.stop_loss, portfolio.stop_loss),
               take_profit=COALESCE(excluded.take_profit, portfolio.take_profit),
               invest_pct=COALESCE(excluded.invest_pct, portfolio.invest_pct)""",
            (
                symbol,
                data.get("quantity", 0),
                data.get("entry_price", 0),
                data.get("strategy", ""),
                data.get("opened_at", now),
                now,
                data.get("stop_loss"),
                data.get("take_profit"),
                data.get("invest_pct", 0),
            ),
        )
        self._get_conn().commit()

    def portfolio_remove(self, symbol: str):
        symbol = symbol.replace("/", "")
        self._get_conn().execute("DELETE FROM portfolio WHERE symbol = ?", (symbol,))
        self._get_conn().commit()

    def portfolio_set_stop_loss(self, symbol: str,
                                stop_loss: Optional[float]):
        """Update only stop_loss (touches updated_at). WO-0924-z2 P6-B3:
        cmd_trailing_check sl_reconcile."""
        symbol = symbol.replace("/", "")
        self._get_conn().execute(
            "UPDATE portfolio SET stop_loss=?, updated_at=? WHERE symbol=?",
            (stop_loss, time.time(), symbol))
        self._get_conn().commit()

    # ==================== Paper Trading Store (P6-B3) ====================
    # DDL lives in _init_db; methods below replace paper_trader's raw SQL.
    # commit=False defers the commit — the P3-1 atomic fill pipeline
    # (_begin/_commit/_rollback_transaction) batches all writes on the
    # shared connection and commits/rolls them back as one unit.

    def commit(self):
        """Transaction boundary for deferred writers (P3-1 pipeline)."""
        self._get_conn().commit()

    def rollback(self):
        """Transaction boundary for deferred writers (P3-1 pipeline)."""
        self._get_conn().rollback()

    def paper_sim_get(self, key: str, default: str = "0") -> str:
        """Raw string kv on paper_portfolio (caller owns JSON encoding)."""
        row = (
            self._get_conn()
            .execute("SELECT value FROM paper_portfolio WHERE key = ?",
                     (key,))
            .fetchone()
        )
        return row["value"] if row else default

    def paper_sim_set(self, key: str, value: str, commit: bool = True):
        self._get_conn().execute(
            """INSERT INTO paper_portfolio (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
               value=excluded.value, updated_at=excluded.updated_at""",
            (key, value, time.time()),
        )
        if commit:
            self._get_conn().commit()

    def paper_trades_recent(self, symbol: Optional[str] = None,
                            limit: int = 50) -> List[Dict]:
        """Newest-first paper_trades rows (optional symbol filter)."""
        if symbol:
            rows = self._get_conn().execute(
                "SELECT * FROM paper_trades WHERE symbol = ?"
                " ORDER BY timestamp DESC LIMIT ?",
                (symbol, limit)).fetchall()
        else:
            rows = self._get_conn().execute(
                "SELECT * FROM paper_trades"
                " ORDER BY timestamp DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def paper_last_buy_price(self, symbol: str) -> Optional[float]:
        """Most recent BUY fill_price for symbol.

        P6-B3 fix: the legacy query selected a nonexistent entry_price
        column, so it always raised and SELL realized-PnL silently
        stayed 0. The BUY leg's fill price is the true entry."""
        row = self._get_conn().execute(
            "SELECT fill_price FROM paper_trades"
            " WHERE symbol = ? AND side = 'BUY'"
            " ORDER BY timestamp DESC LIMIT 1",
            (symbol,)).fetchone()
        return float(row["fill_price"]) if row else None

    def paper_trade_add(self, trade_id: str, symbol: str, side: str,
                        quantity: float, fill_price: float,
                        slippage_pct: float, fee: float, notional: float,
                        details: str, commit: bool = True):
        """Insert a filled MARKET paper trade row."""
        self._get_conn().execute(
            """INSERT INTO paper_trades
               (id, symbol, side, order_type, quantity, fill_price, slippage_pct,
                fee_usdt, notional_usdt, status, timestamp, details)
               VALUES (?, ?, ?, 'MARKET', ?, ?, ?, ?, ?, 'filled', ?, ?)""",
            (trade_id, symbol, side, quantity, fill_price, slippage_pct,
             fee, notional, time.time(), details),
        )
        if commit:
            self._get_conn().commit()

    def paper_pending_add(self, order_id: str, symbol: str, side: str,
                          order_type: str, quantity: float, price: float,
                          stop_price: Optional[float], details: str,
                          commit: bool = True):
        """Insert an open pending (limit/stop) paper order."""
        self._get_conn().execute(
            """INSERT INTO paper_pending_orders
               (id, symbol, side, order_type, quantity, price, stop_price, status, created_at, details)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
            (str(order_id), symbol, side, order_type, quantity, price,
             stop_price, time.time(), details),
        )
        if commit:
            self._get_conn().commit()

    def paper_pending_open(self, symbol: Optional[str] = None) -> List[Dict]:
        """Open pending paper orders (optional symbol filter)."""
        if symbol:
            rows = self._get_conn().execute(
                "SELECT * FROM paper_pending_orders"
                " WHERE symbol = ? AND status = 'open'",
                (symbol,)).fetchall()
        else:
            rows = self._get_conn().execute(
                "SELECT * FROM paper_pending_orders"
                " WHERE status = 'open'").fetchall()
        return [dict(r) for r in rows]

    def paper_pending_get(self, order_id: str) -> Optional[Dict]:
        """One OPEN pending order by id (None if absent/filled)."""
        row = self._get_conn().execute(
            "SELECT * FROM paper_pending_orders"
            " WHERE id = ? AND status = 'open'",
            (order_id,)).fetchone()
        return dict(row) if row else None

    # ==================== Trades-store readers (P4) ====================
    # SQL moved verbatim from portfolio_reconciler (6 sites),
    # entry_price (1) and entry_governor (1) during P4 — every query
    # keeps its original text so semantics are bit-identical.

    def trades_buy_avg(self, symbol: str) -> Optional[float]:
        """Weighted-average entry of all booked BUYs (None if no qty).

        P4: from portfolio_reconciler._db_buy_avg; python-side
        weighted aggregation preserved (row order = table order)."""
        rows = self._get_conn().execute(
            "SELECT qty, price FROM trades WHERE symbol = ? AND UPPER(side) = 'BUY'",
            (symbol,),
        ).fetchall()
        from src.pnl_calculator import weighted_entry
        return weighted_entry(
            (float(r["qty"] or 0), float(r["price"] or 0)) for r in rows)

    def trades_net_qty(self, symbol: str) -> float:
        """booked BUY qty − booked SELL qty (P4: reconciler._db_net_qty)."""
        row = self._get_conn().execute(
            "SELECT COALESCE(SUM(CASE WHEN UPPER(side)='BUY' THEN qty ELSE -qty END), 0) "
            "AS net FROM trades WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        return float(row["net"] or 0) if row else 0.0

    def trades_order_booked(self, order_id) -> bool:
        """WO-017-2: exact key OR ``<prefix>_<oid>`` suffix match
        (P4: reconciler._order_booked — LIKE ESCAPE text verbatim)."""
        oid = str(order_id)
        row = self._get_conn().execute(
            "SELECT 1 FROM trades WHERE client_order_id = ? "
            "OR client_order_id LIKE '%\\_' || ? ESCAPE '\\' LIMIT 1",
            (oid, oid),
        ).fetchone()
        return row is not None

    def trades_recent_sells_no_oid(self, symbol: str, cutoff_s: float,
                                   limit: int = 50) -> List[Dict]:
        """Recent NULL-id SELL rows for the fuzzy-booked check
        (P4: reconciler._fuzzy_booked SQL half; the fuzzy qty/price
        tolerance match itself stays in the caller)."""
        rows = self._get_conn().execute(
            "SELECT qty, price FROM trades "
            "WHERE symbol=? AND side='SELL' AND client_order_id IS NULL "
            "AND timestamp >= ? ORDER BY timestamp DESC LIMIT ?",
            (symbol, cutoff_s, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def trades_last_buy_ts(self, symbol: str) -> Optional[float]:
        """Latest BUY timestamp for the symbol, None if never bought
        (P0-2 anchor; P4: reconciler._last_buy_ts_ms SQL half)."""
        row = self._get_conn().execute(
            "SELECT MAX(timestamp) FROM trades WHERE symbol = ? AND side = 'BUY'",
            (symbol,),
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def trades_recent_symbols(self, cutoff_s: float) -> List[str]:
        """DISTINCT symbols with a ledger row since the cutoff
        (P4: reconciler Path C symbol sweep)."""
        rows = self._get_conn().execute(
            "SELECT DISTINCT symbol FROM trades WHERE timestamp >= ?",
            (cutoff_s,),
        ).fetchall()
        return [r["symbol"] for r in rows if r["symbol"]]

    def trades_rows_asc(self, symbol: str) -> List[Dict]:
        """All rows for the symbol, oldest first (side/qty/price/ts)
        (P4: entry_price.get_avg_entry_price_from_db — the FIFO lot
        walk stays in the caller)."""
        rows = self._get_conn().execute(
            "SELECT side, qty, price, timestamp FROM trades "
            "WHERE symbol = ? ORDER BY timestamp ASC",
            (symbol,),
        ).fetchall()
        return [dict(r) for r in rows]

    def trades_count_buys_since(self, ts: float) -> int:
        """COUNT of BUY rows since ts (P1-② realtime daily-entry floor;
        P4: entry_governor.check_entry SQL half)."""
        row = self._get_conn().execute(
            "SELECT COUNT(*) FROM trades WHERE side = 'BUY' "
            "AND timestamp >= ?", (ts,)).fetchone()
        return int(row[0]) if row else 0

    def paper_pending_mark_filled(self, order_id: str) -> bool:
        """Mark a pending order filled (idempotent; True if a row moved).

        P3 fix (Travis 9/25 ruling ②): paper_pending_orders previously
        had no UPDATE/DELETE site — status stayed 'open' after a fill,
        so check_pending_orders re-filled the same order every sweep.
        Called by PaperTrader._fill_limit_order once the fill commits;
        paper_pending_get/paper_pending_open filter on status='open',
        so a filled row naturally drops out of every reader."""
        cur = self._get_conn().execute(
            "UPDATE paper_pending_orders"
            " SET status = 'filled'"
            " WHERE id = ? AND status = 'open'",
            (order_id,))
        self._get_conn().commit()
        return cur.rowcount > 0

    # ==================== Drawdown ====================

    def drawdown_get(self) -> Dict:
        row = self._get_conn().execute("SELECT * FROM drawdown WHERE id = 1").fetchone()
        if not row:
            return {
                "high_watermark": 0,
                "current_drawdown_pct": 0,
                "max_drawdown_pct": 0,
                "tripped_count": 0,
                "tripped_at": None,
                "reset_at": None,
                "history": [],
            }
        # Use tuple indexing (connection may not have row_factory=Row)
        return {
            "high_watermark": row[1],
            "current_drawdown_pct": row[2],
            "max_drawdown_pct": row[3],
            "tripped_count": row[4],
            "tripped_at": row[5],
            "reset_at": row[6],
            "history": json.loads(row[7]) if row[7] else [],
        }

    def drawdown_set(self, data: Dict):
        now = time.time()
        self._get_conn().execute(
            """INSERT INTO drawdown (id, high_watermark, current_drawdown_pct, max_drawdown_pct, tripped_count, tripped_at, reset_at, history, updated_at)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
               high_watermark=excluded.high_watermark,
               current_drawdown_pct=excluded.current_drawdown_pct,
               max_drawdown_pct=excluded.max_drawdown_pct,
               tripped_count=excluded.tripped_count,
               tripped_at=excluded.tripped_at,
               reset_at=excluded.reset_at,
               history=excluded.history,
               updated_at=excluded.updated_at""",
            (
                data.get("high_watermark", 0),
                data.get("current_drawdown_pct", 0),
                data.get("max_drawdown_pct", 0),
                data.get("tripped_count", 0),
                data.get("tripped_at"),
                data.get("reset_at"),
                json.dumps(data.get("history", [])),
                now,
            ),
        )
        self._get_conn().commit()

    # ==================== Risk Guard ====================

    def risk_get(self) -> Dict:
        row = (
            self._get_conn().execute("SELECT * FROM risk_guard WHERE id = 1").fetchone()
        )
        if not row:
            now = time.time()
            self._get_conn().execute(
                "INSERT INTO risk_guard (id, daily_pnl, streak, last_reset, updated_at) VALUES (1, 0, 0, ?, ?)",
                (now, now),
            )
            self._get_conn().commit()
            return {"daily_pnl": 0, "streak": 0, "last_reset": now}
        return {
            "daily_pnl": row["daily_pnl"],
            "streak": row["streak"],
            "last_reset": row["last_reset"],
        }

    def risk_set(self, data: Dict):
        now = time.time()
        self._get_conn().execute(
            """INSERT INTO risk_guard (id, daily_pnl, streak, last_reset, updated_at)
               VALUES (1, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
               daily_pnl=excluded.daily_pnl,
               streak=excluded.streak,
               last_reset=excluded.last_reset,
               updated_at=excluded.updated_at""",
            (
                data.get("daily_pnl", 0),
                data.get("streak", 0),
                data.get("last_reset", now),
                now,
            ),
        )
        self._get_conn().commit()

    # ==================== Trades ====================

    def trade_add(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        pnl: float = 0,
        client_order_id: Optional[str] = None,
    ) -> bool:
        """Insert a trade row. Returns True if inserted; False if a row with
        the same client_order_id already exists (duplicate skipped) — P0-A4."""
        with self._write_lock:
            cur = self._get_conn().execute(
                "INSERT OR IGNORE INTO trades "
                "(symbol, side, qty, price, pnl, timestamp, client_order_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (symbol, side, qty, price, pnl, time.time(), client_order_id),
            )
            self._get_conn().commit()
            return cur.rowcount > 0

    def trade_get_recent(
        self, symbol: Optional[str] = None, limit: int = 50
    ) -> List[Dict]:
        if symbol:
            rows = (
                self._get_conn()
                .execute(
                    "SELECT * FROM trades WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?",
                    (symbol, limit),
                )
                .fetchall()
            )
        else:
            rows = (
                self._get_conn()
                .execute(
                    "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
                )
                .fetchall()
            )
        return [dict(r) for r in rows]

    # ==================== Grid State (replaces grid_state.json) ====================

    def grid_get(self, symbol: str) -> Optional[Dict]:
        row = (
            self._get_conn()
            .execute("SELECT * FROM grid_state WHERE symbol = ?", (symbol,))
            .fetchone()
        )
        if not row:
            return None
        return {
            "symbol": row["symbol"],
            "status": row["status"],
            "config": json.loads(row["config_json"]),
            "levels": json.loads(row["levels_json"]),
            "stats": json.loads(row["stats_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def grid_get_all(self) -> Dict[str, Dict]:
        rows = self._get_conn().execute("SELECT * FROM grid_state").fetchall()
        result = {}
        for r in rows:
            result[r["symbol"]] = {
                "symbol": r["symbol"],
                "status": r["status"],
                "config": json.loads(r["config_json"]),
                "levels": json.loads(r["levels_json"]),
                "stats": json.loads(r["stats_json"]),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
        return result

    def grid_set(self, symbol: str, data: Dict):
        now = time.time()
        self._get_conn().execute(
            """INSERT INTO grid_state (symbol, status, config_json, levels_json, stats_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
               status=excluded.status,
               config_json=excluded.config_json,
               levels_json=excluded.levels_json,
               stats_json=excluded.stats_json,
               updated_at=excluded.updated_at""",
            (
                symbol,
                data.get("status", "stopped"),
                json.dumps(data.get("config", {})),
                json.dumps(data.get("levels", [])),
                json.dumps(data.get("stats", {})),
                data.get("created_at", now),
                now,
            ),
        )
        self._get_conn().commit()

    def grid_remove(self, symbol: str):
        self._get_conn().execute("DELETE FROM grid_state WHERE symbol = ?", (symbol,))
        self._get_conn().commit()

    # ==================== DCA State (replaces dca_state.json) ====================

    def dca_get(self, symbol: str) -> Optional[Dict]:
        row = (
            self._get_conn()
            .execute("SELECT * FROM dca_state WHERE symbol = ?", (symbol,))
            .fetchone()
        )
        if not row:
            return None
        return {
            "symbol": row["symbol"],
            "rounds_done": row["rounds_done"],
            "total_invested": row["total_invested"],
            "avg_price": row["avg_price"],
            "next_buy_at": row["next_buy_at"],
            "status": row["status"],
            "updated_at": row["updated_at"],
        }

    def dca_get_all(self) -> Dict[str, Dict]:
        rows = self._get_conn().execute("SELECT * FROM dca_state").fetchall()
        return {r["symbol"]: dict(r) for r in rows}

    def dca_set(self, symbol: str, data: Dict):
        now = time.time()
        self._get_conn().execute(
            """INSERT INTO dca_state (symbol, rounds_done, total_invested, avg_price, next_buy_at, status, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
               rounds_done=excluded.rounds_done,
               total_invested=excluded.total_invested,
               avg_price=excluded.avg_price,
               next_buy_at=excluded.next_buy_at,
               status=excluded.status,
               updated_at=excluded.updated_at""",
            (
                symbol,
                data.get("rounds_done", 0),
                data.get("total_invested", 0),
                data.get("avg_price", 0),
                data.get("next_buy_at"),
                data.get("status", "active"),
                now,
            ),
        )
        self._get_conn().commit()

    def dca_remove(self, symbol: str):
        self._get_conn().execute("DELETE FROM dca_state WHERE symbol = ?", (symbol,))
        self._get_conn().commit()

    # ==================== Strategy State (replaces strategy_state.json) ====================

    def strategy_get(self, key: str) -> Optional[Dict]:
        row = (
            self._get_conn()
            .execute("SELECT * FROM strategy_state WHERE key = ?", (key,))
            .fetchone()
        )
        if not row:
            return None
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return {"value": row["value"], "updated_at": row["updated_at"]}

    def strategy_get_all(self) -> Dict[str, Dict]:
        rows = self._get_conn().execute("SELECT * FROM strategy_state").fetchall()
        result = {}
        for r in rows:
            try:
                result[r["key"]] = json.loads(r["value"])
            except json.JSONDecodeError:
                result[r["key"]] = {"value": r["value"], "updated_at": r["updated_at"]}
        return result

    def strategy_set(self, key: str, data: Dict):
        now = time.time()
        self._get_conn().execute(
            """INSERT INTO strategy_state (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
               value=excluded.value,
               updated_at=excluded.updated_at""",
            (key, json.dumps(data), now),
        )
        self._get_conn().commit()

    def strategy_remove(self, key: str):
        self._get_conn().execute("DELETE FROM strategy_state WHERE key = ?", (key,))
        self._get_conn().commit()

    # ==================== KV Store ====================

    def kv_get(self, key: str, default: Optional[Any] = None) -> Any:
        row = (
            self._get_conn()
            .execute("SELECT value FROM kv WHERE key = ?", (key,))
            .fetchone()
        )
        if row:
            try:
                return json.loads(row["value"])
            except json.JSONDecodeError:
                return row["value"]
        return default

    def kv_set(self, key: str, value: Any):
        now = time.time()
        self._get_conn().execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), now),
        )
        self._get_conn().commit()

    def kv_remove(self, key: str):
        self._get_conn().execute("DELETE FROM kv WHERE key = ?", (key,))
        self._get_conn().commit()

    def kv_get_prefix(self, prefix: str) -> Dict[str, Any]:
        """All kv entries whose key starts with prefix; values parsed with
        kv_get semantics (json.loads, parse failure keeps the raw string).
        WO-0924-z2 P6-B3: tp_sl_tracker.get_all_tracked prefix scan."""
        rows = (
            self._get_conn()
            .execute("SELECT key, value FROM kv WHERE key LIKE ?",
                     (prefix + "%",))
            .fetchall()
        )
        out: Dict[str, Any] = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except json.JSONDecodeError:
                out[r["key"]] = r["value"]
        return out

    def kv_age_seconds(self, key: str) -> Optional[float]:
        """Seconds since kv[key] last updated (None if absent or unreadable).
        WO-0924-z2 P6-B3: kv_preflight freshness checks."""
        row = (
            self._get_conn()
            .execute("SELECT updated_at FROM kv WHERE key = ?", (key,))
            .fetchone()
        )
        if row is None:
            return None
        try:
            return max(0.0, time.time() - float(row["updated_at"]))
        except (TypeError, ValueError, KeyError, IndexError):
            return None

    # ==================== Audit Log ====================

    def audit_log(
        self,
        action: str,
        details: Any = "",
        old_value: str = "",
        new_value: str = "",
        source: str = "system",
    ):
        """Log an audit event."""
        now = time.time()
        details_str = json.dumps(details) if not isinstance(details, str) else details
        self._get_conn().execute(
            "INSERT INTO audit_log (timestamp, action, details, old_value, new_value, source) VALUES (?, ?, ?, ?, ?, ?)",
            (now, action, details_str, old_value, new_value, source),
        )
        self._get_conn().commit()

    def audit_get_recent(self, limit: int = 50,
                         action: Optional[str] = None) -> List[Dict]:
        """Get recent audit log entries (optionally filtered by action —
        WO-0924-z2 P6-B2: online_learner's weight-history reader)."""
        if action is not None:
            rows = (
                self._get_conn()
                .execute(
                    "SELECT * FROM audit_log WHERE action = ?"
                    " ORDER BY timestamp DESC LIMIT ?", (action, limit)
                )
                .fetchall()
            )
        else:
            rows = (
                self._get_conn()
                .execute(
                    "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)
                )
                .fetchall()
            )
        return [dict(r) for r in rows]

    # ==================== Trade Outcomes (P6-B2 learning chain) ====================
    # WO-0924-z2 P6-B2: trade_outcomes SQL previously lived in
    # trade_outcome_recorder (writer) + kelly_sizer / strategy_evolver /
    # online_learner / strategy_registry / hmm_regime (readers). Moved
    # here verbatim so schema changes have one owner.

    def outcome_add_entry(self, symbol: str, entry_time: float,
                          entry_date: str, entry_price: float, qty: float,
                          score, strategy, factors_json: str,
                          context_json: str) -> int:
        """INSERT an open outcome row; returns the new rowid."""
        cur = self._get_conn().execute(
            """INSERT INTO trade_outcomes
                (symbol, entry_time, entry_date, entry_price, qty, score, strategy,
                 factors_json, context_json, status,
                 peak_price, trough_price, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)""",
            (symbol, entry_time, entry_date, entry_price, qty, score,
             strategy, factors_json, context_json,
             entry_price, entry_price, entry_time, entry_time),
        )
        self._get_conn().commit()
        return cur.lastrowid

    def outcome_get_by_id(self, row_id: int) -> Optional[Dict]:
        row = self._get_conn().execute(
            "SELECT * FROM trade_outcomes WHERE id = ?", (row_id,)
        ).fetchone()
        return dict(row) if row else None

    def outcome_latest_open(self, symbol: str) -> Optional[Dict]:
        """Most recent open outcome row for a symbol (peak/trough + full row)."""
        row = self._get_conn().execute(
            """SELECT * FROM trade_outcomes
               WHERE symbol = ? AND status = 'open'
               ORDER BY entry_time DESC LIMIT 1""",
            (symbol,),
        ).fetchone()
        return dict(row) if row else None

    def outcome_update_extremes(self, row_id: int, peak: float,
                                trough: float, updated_at: float):
        self._get_conn().execute(
            """UPDATE trade_outcomes
            SET peak_price = ?, trough_price = ?, updated_at = ?
            WHERE id = ?""",
            (peak, trough, updated_at, row_id),
        )
        self._get_conn().commit()

    def outcome_close(self, row_id: int, *, exit_time: float,
                      exit_price: float, exit_reason: str,
                      pnl_pct: float, pnl_absolute: float,
                      net_pnl_pct: float, net_pnl_absolute: float,
                      time_held_hours: float, max_profit_pct: float,
                      max_drawdown_pct: float, peak_price: float,
                      trough_price: float, is_win: bool,
                      updated_at: float):
        """Close an outcome row with computed metrics (writer owns the math)."""
        self._get_conn().execute(
            """UPDATE trade_outcomes SET
                exit_time = ?, exit_price = ?, exit_reason = ?,
                pnl_pct = ?, pnl_absolute = ?,
                net_pnl_pct = ?, net_pnl_absolute = ?,
                time_held_hours = ?,
                max_profit_pct = ?, max_drawdown_pct = ?,
                peak_price = ?, trough_price = ?,
                is_win = ?, status = 'closed',
                updated_at = ?
            WHERE id = ?""",
            (exit_time, exit_price, exit_reason,
             pnl_pct, pnl_absolute, net_pnl_pct, net_pnl_absolute,
             time_held_hours, max_profit_pct, max_drawdown_pct,
             peak_price, trough_price, is_win, updated_at, row_id),
        )
        self._get_conn().commit()

    def outcomes_get_open(self) -> List[Dict]:
        rows = self._get_conn().execute(
            "SELECT * FROM trade_outcomes WHERE status = 'open'"
            " ORDER BY entry_time DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def outcomes_get_closed(self, limit: Optional[int] = None,
                            strategy: Optional[str] = None,
                            newest_first: bool = True) -> List[Dict]:
        """Closed outcome rows. limit=None returns all (factor-stats and
        summary callers iterate order-insensitively; newest_first controls
        the ORDER BY exit_time clause)."""
        sql = "SELECT * FROM trade_outcomes WHERE status = 'closed'"
        params: list = []
        if strategy:
            sql += " AND strategy = ?"
            params.append(strategy)
        if newest_first:
            sql += " ORDER BY exit_time DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._get_conn().execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def outcomes_recent_net_pnls(self, limit: int = 100) -> List[float]:
        """Newest-first closed net_pnl_pct values (NULLs excluded).
        WO-0924-z2 P6-B3: cvar_risk + strategy_adaptor CVaR overlay +
        risk_manager kelly (order-insensitive aggregate consumers)."""
        rows = self._get_conn().execute(
            """SELECT net_pnl_pct FROM trade_outcomes
               WHERE status = 'closed' AND net_pnl_pct IS NOT NULL
               ORDER BY exit_time DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [r["net_pnl_pct"] for r in rows]

    def outcomes_get_closed_oldest(self) -> List[Dict]:
        """All closed outcome rows oldest-first (exit_time ASC).
        WO-0924-z2 P6-B3: concept_drift's 60/40 chronological split needs
        a true ascending read — outcomes_get_closed(newest_first=False)
        stays deliberately unsorted for order-insensitive callers."""
        rows = self._get_conn().execute(
            "SELECT * FROM trade_outcomes WHERE status = 'closed'"
            " ORDER BY exit_time ASC").fetchall()
        return [dict(r) for r in rows]

    def outcomes_history_rows(self, symbol: Optional[str] = None,
                              limit: int = 50) -> List[Dict]:
        """7-column trade-history projection, newest entry first.
        WO-0924-z2 P6-B3: portfolio.get_trade_history."""
        sql = ("SELECT symbol, entry_price, exit_price, net_pnl_pct,"
               " strategy, exit_reason, status FROM trade_outcomes")
        params: list = []
        if symbol:
            sql += " WHERE symbol = ?"
            params.append(symbol)
        sql += " ORDER BY entry_time DESC LIMIT ?"
        params.append(limit)
        rows = self._get_conn().execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def outcomes_symbol_counts(self, symbol: str) -> tuple:
        """(known, open) trade_outcomes row counts for symbol — the bug#32
        ghost-position guard. WO-0924-z2 P6-B3: cmd_trailing_check."""
        row = self._get_conn().execute(
            "SELECT (SELECT COUNT(*) FROM trade_outcomes WHERE symbol = ?)"
            " AS known, (SELECT COUNT(*) FROM trade_outcomes WHERE symbol = ?"
            " AND status = 'open') AS open_cnt",
            (symbol, symbol),
        ).fetchone()
        return (row["known"], row["open_cnt"]) if row else (0, 0)

    def outcomes_count_closed(self) -> int:
        return self._get_conn().execute(
            "SELECT COUNT(*) as cnt FROM trade_outcomes"
            " WHERE status = 'closed'").fetchone()[0]

    def outcomes_count_context_like(self, needle: str, since_ts: float) -> int:
        """Count closed-window entries whose context_json contains needle
        (exploration / bull-refresh caps)."""
        row = self._get_conn().execute(
            """SELECT COUNT(*) FROM trade_outcomes
               WHERE context_json LIKE ? AND entry_time >= ?""",
            (f"%{needle}%", since_ts),
        ).fetchone()
        return int(row[0]) if row else 0

    def outcomes_recent_pnl_signals(self, limit: int) -> List[Dict]:
        """(symbol, net_pnl_pct, is_win, strategy) for kelly sizing."""
        rows = self._get_conn().execute(
            """SELECT symbol, net_pnl_pct, is_win, strategy
               FROM trade_outcomes
               WHERE status = 'closed' AND net_pnl_pct IS NOT NULL
               ORDER BY entry_time DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def outcomes_strategy_perf_rows(self) -> List[Dict]:
        """Per-strategy trades/wins/avg_pnl aggregates (evolver)."""
        rows = self._get_conn().execute(
            """SELECT strategy, COUNT(*) as trades,
                      SUM(CASE WHEN is_win = 1 THEN 1 ELSE 0 END) as wins,
                      AVG(net_pnl_pct) as avg_pnl
            FROM trade_outcomes
            WHERE status = 'closed' AND strategy IS NOT NULL
            GROUP BY strategy""").fetchall()
        return [dict(r) for r in rows]

    def outcomes_strategy_pnls(self) -> List[Dict]:
        """Per-trade net pnl per strategy, newest first (profit factor)."""
        rows = self._get_conn().execute(
            """SELECT strategy, net_pnl_pct
            FROM trade_outcomes
            WHERE status = 'closed' AND strategy IS NOT NULL AND net_pnl_pct IS NOT NULL
            ORDER BY exit_time DESC""").fetchall()
        return [dict(r) for r in rows]

    def outcomes_recent_pnl_per_strategy(self, n: int) -> List[Dict]:
        """Window-function query: most recent n closed pnls per strategy."""
        rows = self._get_conn().execute(
            """SELECT strategy, net_pnl_pct FROM (
                   SELECT strategy, net_pnl_pct,
                          ROW_NUMBER() OVER (
                              PARTITION BY strategy
                              ORDER BY exit_time DESC
                          ) AS rn
                   FROM trade_outcomes
                   WHERE status = 'closed' AND strategy IS NOT NULL
                     AND net_pnl_pct IS NOT NULL
               ) WHERE rn <= ?""",
            (n,),
        ).fetchall()
        return [dict(r) for r in rows]

    def outcomes_strategy_rows_win(self) -> List[Dict]:
        """(strategy, net_pnl_pct, is_win) closed rows (registry weighting)."""
        rows = self._get_conn().execute(
            """SELECT strategy, net_pnl_pct, is_win
            FROM trade_outcomes WHERE status = 'closed'""").fetchall()
        return [dict(r) for r in rows]

    # ==================== Bull regime transition log (P6-B2) ====================
    def bull_regime_log_add(self, t: Dict):
        """Append a regime transition (bull_regime writer)."""
        self._get_conn().execute(
            """INSERT INTO bull_regime_log
                (ts, bar_ts, from_state, to_state, reason,
                 btc_close, btc_sma200, fng_avg, fng_today, adx, conditions_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (t["ts"], t["bar_ts"], t["from"], t["to"], t["reason"],
             t.get("btc_close"), t.get("btc_sma200"),
             t.get("fng_avg"), t.get("fng_today"), t.get("adx"),
             json.dumps(t.get("conditions", {}))),
        )
        self._get_conn().commit()

    def bull_regime_log_recent(self, limit: int = 50) -> List[Dict]:
        """Recent regime transitions (bull_regime reader)."""
        rows = self._get_conn().execute(
            """SELECT ts, from_state, to_state, reason, btc_close,
                       fng_avg, adx
                FROM bull_regime_log ORDER BY ts DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ==================== Decisions (TradeJournal) ====================

    def decision_add(
        self,
        symbol: str,
        type: str,
        decision: str = "",
        score: float = 0,
        price: float = 0,
        qty: float = 0,
        side: str = "",
        strategy: str = "",
        reasons: Optional[list] = None,
        signals: Optional[list] = None,
        bear_result: Optional[Any] = None,
        research: str = "",
        exit_price: float = 0,
        pnl_pct: float = 0,
    ) -> int:
        """Insert a decision/trade record. Returns row id."""
        from datetime import datetime

        now = time.time()
        date_str = datetime.now().strftime("%Y-%m-%d")

        # Flatten bear_result (BearResult object or dict or None)
        bear_score = None
        bear_veto = None
        bear_reasons = None
        bear_confidence = None
        if bear_result is not None:
            if hasattr(bear_result, "bear_score") and hasattr(bear_result, "veto"):
                # Real BearResult object (has concrete attributes)
                bear_score = bear_result.bear_score
                bear_veto = 1 if bear_result.veto else 0
                bear_reasons = json.dumps(getattr(bear_result, "reasons", []) or [])
                bear_confidence = getattr(bear_result, "confidence", None)
            elif isinstance(bear_result, dict):
                bear_score = bear_result.get("bear_score")
                bear_veto = 1 if bear_result.get("veto") else 0
                bear_reasons = json.dumps(bear_result.get("reasons", []))
                bear_confidence = bear_result.get("confidence")

        rowid = (
            self._get_conn()
            .execute(
                """INSERT INTO decisions
            (timestamp, date, symbol, type, decision, score, price, qty, side, strategy,
             reasons, signals, bear_score, bear_veto, bear_reasons, bear_confidence,
             research, exit_price, pnl_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now,
                    date_str,
                    symbol,
                    type,
                    decision,
                    score,
                    price,
                    qty,
                    side,
                    strategy,
                    json.dumps(reasons or []),
                    json.dumps(signals or []),
                    bear_score,
                    bear_veto,
                    bear_reasons,
                    bear_confidence,
                    research,
                    exit_price,
                    pnl_pct,
                ),
            )
            .lastrowid
        )
        self._get_conn().commit()
        return rowid or 0

    def decisions_get_history(
        self, symbol: Optional[str] = None, type: Optional[str] = None, limit: int = 10
    ) -> List[Dict]:
        """Get recent decisions, optionally filtered by symbol and/or type."""
        conditions: List[str] = []
        params: List[Any] = []
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if type:
            conditions.append("type = ?")
            params.append(type)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        rows = (
            self._get_conn()
            .execute(
                f"SELECT * FROM decisions{where} ORDER BY timestamp DESC LIMIT ?",
                params,
            )
            .fetchall()
        )
        return [dict(r) for r in rows]

    def decisions_get_lessons(
        self, symbol: Optional[str] = None, limit: int = 5
    ) -> List[Dict]:
        """Get recent decisions with exit data and |pnl| > 3% (for lessons)."""
        conditions = ["pnl_pct != 0", "exit_price != 0", "ABS(pnl_pct) > 3.0"]
        params: List[Any] = []
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        where = " WHERE " + " AND ".join(conditions)
        params.append(limit)
        rows = (
            self._get_conn()
            .execute(
                f"SELECT * FROM decisions{where} ORDER BY timestamp DESC LIMIT ?",
                params,
            )
            .fetchall()
        )
        return [dict(r) for r in rows]

    def decisions_count(self, date: Optional[str] = None) -> int:
        """Count decisions, optionally by date."""
        if date:
            row = (
                self._get_conn()
                .execute(
                    "SELECT COUNT(*) as cnt FROM decisions WHERE date = ?", (date,)
                )
                .fetchone()
            )
        else:
            row = (
                self._get_conn()
                .execute("SELECT COUNT(*) as cnt FROM decisions")
                .fetchone()
            )
        return row["cnt"] if row else 0


# Singleton instance
_state_db_instance: Optional[StateDB] = None
_state_db_lock = threading.Lock()


def get_state_db(db_path: Optional[str] = None) -> StateDB:
    """Get singleton StateDB instance.

    Three-layer test isolation:
    1. STATE_DB_PATH env var — overrides db_path for test isolation.
    2. If STATE_DB_PATH changes between calls, recreate singleton (hot-swap).
    3. Hard guard: if TESTING env is set and path looks like production, raise.
    """
    global _state_db_instance

    env_path = os.environ.get("STATE_DB_PATH")
    if env_path:
        db_path = env_path

    # Layer 3: Hard guard — refuse production DB during tests
    if os.environ.get("TESTING"):
        resolved = db_path or str(DEFAULT_DB_PATH)
        default_str = str(DEFAULT_DB_PATH)
        if resolved == default_str:
            raise RuntimeError(
                f"BLOCKED: get_state_db() called during TESTING but db_path "
                f"points to production ({default_str}). "
                f"Set STATE_DB_PATH to a temp file in conftest."
            )

    # Layer 2: Hot-swap — if env var changed, recreate singleton
    if _state_db_instance is not None and env_path:
        current_path = str(_state_db_instance.db_path)
        if current_path != env_path:
            logger.info(
                "StateDB hot-swap: %s -> %s (STATE_DB_PATH changed)",
                current_path, env_path,
            )
            _state_db_instance = None

    if _state_db_instance is None:
        with _state_db_lock:
            if _state_db_instance is None:
                _state_db_instance = StateDB(db_path)
    return _state_db_instance

# ==================== DB reliability (bug#8, 2026-08-20) ====================
# The 20:30 ACE incident: exchange fill OK, all three bookkeeping writes
# "succeeded" from the process's point of view (no exception -> errors=[]),
# yet nothing landed in data/state.db. Exception-only retry cannot see a
# write that is acknowledged and then lost by the storage layer — so critical
# writes must be VERIFIED by reading them back, and a verified loss must be
# LOUD (error log + cron_failures.jsonl + non-zero exit upstream).

_PROJECT_ROOT = Path(__file__).parent.parent
_CRON_FAILURES_FILE = _PROJECT_ROOT / "logs" / "cron_failures.jsonl"


def record_db_failure(job: str, detail: str) -> None:
    """Append a DB-write failure to logs/cron_failures.jsonl (same channel
    the cron wrappers already use for non-zero exits), so silent bookkeeping
    gaps become visible to monitoring."""
    try:
        # env override keeps tests from polluting the production log
        import os as _os
        _target = _os.environ.get("CRON_FAILURES_FILE")
        _file = Path(_target) if _target else _CRON_FAILURES_FILE
        _file.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "job": job,
            "type": "db_write_failure",
            "detail": detail[:2000],
        }
        with open(_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:  # never let failure-recording kill the writer
        logger.error(f"record_db_failure could not append: {e}")


def db_write_with_verify(
    db,
    write_fn,
    verify_fn,
    label: str,
    attempts: int = 3,
    backoff_sec: float = 1.0,
) -> bool:
    """Run a critical DB write, then CONFIRM it landed by reading it back.

    - write_fn(): performs execute+commit (may raise -> retried)
    - verify_fn(): returns truthy iff the effect is durably visible
    - A write that "succeeds" but does not verify counts as a FAILED attempt
      (this is the class of failure that produced bug#8's errors=[] output).
    - On final failure: logger.error + record_db_failure() + return False.
      Caller is expected to escalate further (errors[] in cron JSON, Feishu,
      non-zero exit code).
    """
    last_err = None
    for i in range(attempts):
        try:
            write_fn()
            if verify_fn():
                return True
            last_err = "committed but read-back verification found no effect"
            logger.error(
                f"{label}: attempt {i + 1}/{attempts} SILENT WRITE LOSS "
                f"(commit returned but data not visible) — retrying"
            )
        except Exception as e:
            last_err = e
            logger.warning(f"{label}: attempt {i + 1}/{attempts} failed: {e}")
        time.sleep(backoff_sec)
    logger.error(f"{label}: FAILED {attempts}x ({last_err}) — data gap requires attention")
    record_db_failure(label, f"failed {attempts}x: {last_err}")
    return False

