"""Lightweight SQLite state DB for stock-ai-trader."""
import sqlite3
import os
from contextlib import contextmanager
from typing import Optional

class StateDB:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
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

    def get(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (key, value)
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
            self._conn.commit()
        finally:
            self._batch_mode = False

    def close(self) -> None:
        """Close the underlying database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    @classmethod
    def reset_for_testing(cls) -> None:
        """Close and clear the global singleton for test isolation."""
        global _instance
        if _instance is not None:
            _instance.close()
            _instance = None

_instance: Optional[StateDB] = None

def get_state_db(db_path: Optional[str] = None) -> StateDB:
    global _instance
    if _instance is None:
        db_path = db_path or os.path.join("data", "state.db")
        _instance = StateDB(db_path)
    return _instance
