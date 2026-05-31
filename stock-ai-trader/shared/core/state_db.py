"""Lightweight SQLite state DB for stock-ai-trader."""
import json
import sqlite3
import os
import threading
from contextlib import contextmanager
from typing import Dict, Optional

class StateDB:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._conn.commit()
        self._batch_mode = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (key, value)
            )
            if not self._batch_mode:
                self._conn.commit()

    # ── JSON Key-Value Aliases ────────────────────────────────────────────

    def kv_set(self, key: str, value) -> None:
        """Store value as JSON string (alias for set with JSON serialization)."""
        self.set(key, json.dumps(value))

    def kv_get(self, key: str):
        """Retrieve value parsed from JSON (alias for get with JSON deserialization)."""
        raw = self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    # ── Portfolio Helpers ─────────────────────────────────────────────────

    def portfolio_set(self, symbol: str, data: dict) -> None:
        """Store portfolio entry as JSON under key 'portfolio:{symbol}'."""
        self.kv_set(f"portfolio:{symbol}", data)

    def portfolio_get_all(self) -> Dict[str, dict]:
        """Return all entries matching 'portfolio:*' prefix, keyed by symbol."""
        with self._lock:
            prefix = "portfolio:"
            rows = self._conn.execute(
                "SELECT key, value FROM kv WHERE key LIKE ?",
                (f"{prefix}%",),
            ).fetchall()
        result = {}
        for key, value in rows:
            symbol = key[len(prefix):]
            try:
                result[symbol] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                result[symbol] = value
        return result

    def portfolio_remove(self, symbol: str) -> None:
        """Delete the 'portfolio:{symbol}' key."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM kv WHERE key=?", (f"portfolio:{symbol}",)
            )
            if not self._batch_mode:
                self._conn.commit()

    @contextmanager
    def batch_write(self):
        """Context manager that defers commits until exit.

        Usage:
            with state_db.batch_write():
                state_db.set("a", "1")
                state_db.set("b", "2")
            # Single commit at exit
        """
        self._batch_mode = True
        try:
            yield
            with self._lock:
                self._conn.commit()
        finally:
            self._batch_mode = False

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None  # type: ignore[assignment]

    @classmethod
    def reset_for_testing(cls) -> None:
        """Close and clear the global singleton for test isolation."""
        global _instance, _singleton_lock
        with _singleton_lock:
            if _instance is not None:
                _instance.close()
                _instance = None

_singleton_lock = threading.Lock()
_instance: Optional[StateDB] = None

def get_state_db(db_path: Optional[str] = None) -> StateDB:
    global _instance
    with _singleton_lock:
        if _instance is None:
            db_path = db_path or os.path.join("data", "state.db")
            _instance = StateDB(db_path)
        return _instance
