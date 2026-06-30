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
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default DB path: project_root/data/state.db
DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "state.db"


class StateDB:
    """Thread-safe SQLite state persistence with connection pooling."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
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
                # Run WAL checkpoint before recycling connection
                try:
                    self._local.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception as e:
                    logger.warning(f"StateDB: WAL checkpoint failed: {e}")
                try:
                    self._local.conn.close()
                except Exception:
                    logger.error("Failed to close stale DB connection", exc_info=True)
                self._local.conn = None
                self._local.conn_created = 0

        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self.db_path), check_same_thread=False
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
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
            logger.warning(f"StateDB: WAL checkpoint failed: {e}")
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
                from datetime import datetime

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
            """)
        conn.commit()

        # Migration: add invest_pct column if missing (for existing databases)
        try:
            conn.execute("ALTER TABLE portfolio ADD COLUMN invest_pct REAL DEFAULT 0")
        except Exception as e:
            logger.warning("state_db._init_db: " + str(e))
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
        self, symbol: str, side: str, qty: float, price: float, pnl: float = 0
    ):
        self._get_conn().execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (symbol, side, qty, price, pnl, time.time()),
        )
        self._get_conn().commit()

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

    def audit_get_recent(self, limit: int = 50) -> List[Dict]:
        """Get recent audit log entries."""
        rows = (
            self._get_conn()
            .execute(
                "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
            .fetchall()
        )
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
