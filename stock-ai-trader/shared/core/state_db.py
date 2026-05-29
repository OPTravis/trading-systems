"""Lightweight SQLite state DB for stock-ai-trader."""
import sqlite3
import os
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

    def get(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (key, value)
        )
        self._conn.commit()

_instance: Optional[StateDB] = None

def get_state_db(db_path: Optional[str] = None) -> StateDB:
    global _instance
    if _instance is None:
        db_path = db_path or os.path.join("data", "state.db")
        _instance = StateDB(db_path)
    return _instance
