"""
In-Process Event Bus - lightweight pub/sub replacing Kafka microservices.
Thread-safe, SQLite-persisted, singleton.
"""

import json
import logging
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

DB_PATH = Path.home() / "crypto-ai-trader" / "data" / "events.db"
MAX_EVENTS = 10000

VALID_EVENT_TYPES = {
    "trade_executed",
    "position_opened",
    "position_closed",
    "risk_alert",
    "circuit_breaker_trip",
    "score_update",
    "regime_change",
}


class EventBus:
    def __init__(self, db_path: Path = DB_PATH):
        self._db_path = str(db_path)
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._sub_lock = threading.Lock()
        self._db_lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._db_lock:
            conn = sqlite3.connect(self._db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    event_type TEXT,
                    data TEXT,
                    timestamp REAL,
                    processed INTEGER DEFAULT 0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_event_type ON events(event_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON events(timestamp)")
            conn.commit()
            conn.close()

    def _conn(self):
        return sqlite3.connect(self._db_path)

    def publish(self, event_type: str, data: Dict) -> str:
        event_id = str(uuid.uuid4())
        ts = time.time()
        data_json = json.dumps(data, default=str)

        with self._db_lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO events (id, event_type, data, timestamp, processed) VALUES (?, ?, ?, ?, 0)",
                    (event_id, event_type, data_json, ts),
                )
                # Auto-prune
                count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                if count > MAX_EVENTS:
                    excess = count - MAX_EVENTS
                    conn.execute(
                        "DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY timestamp ASC LIMIT ?)",
                        (excess,),
                    )
                conn.commit()
            finally:
                conn.close()

        # Notify subscribers
        with self._sub_lock:
            callbacks = list(self._subscribers.get(event_type, []))
            all_callbacks = list(self._subscribers.get("*", []))

        for cb in callbacks + all_callbacks:
            try:
                cb(event_id, event_type, data, ts)
            except Exception as e:
                logger.error("Event callback error: %s", e)

        return event_id

    def subscribe(self, event_type: str, callback: Callable):
        with self._sub_lock:
            self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: str, callback: Callable):
        with self._sub_lock:
            if event_type in self._subscribers:
                try:
                    self._subscribers[event_type].remove(callback)
                except ValueError:
                    logger.debug("Callback already removed from event_type=%s (not subscribed)", event_type)

    def get_events(self, event_type: str = None, since: float = None, limit: int = 100) -> List[Dict]:
        with self._db_lock:
            conn = self._conn()
            try:
                query = "SELECT id, event_type, data, timestamp, processed FROM events WHERE 1=1"
                params: list = []
                if event_type:
                    query += " AND event_type = ?"
                    params.append(event_type)
                if since is not None:
                    query += " AND timestamp > ?"
                    params.append(since)
                query += " ORDER BY timestamp DESC LIMIT ?"
                params.append(limit)

                rows = conn.execute(query, params).fetchall()
                return [
                    {"id": r[0], "event_type": r[1], "data": json.loads(r[2]), "timestamp": r[3], "processed": r[4]}
                    for r in rows
                ]
            finally:
                conn.close()

    def get_event_count(self) -> int:
        with self._db_lock:
            conn = self._conn()
            try:
                return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            finally:
                conn.close()


_instance: Optional[EventBus] = None
_init_lock = threading.Lock()


def get_event_bus() -> EventBus:
    global _instance
    if _instance is None:
        with _init_lock:
            if _instance is None:
                _instance = EventBus()
    return _instance


def reset_event_bus():
    """Reset singleton for testing."""
    global _instance
    with _init_lock:
        _instance = None
