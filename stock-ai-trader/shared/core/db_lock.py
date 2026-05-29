"""File-based lock for DuckDB concurrent write protection.

Uses fcntl.flock() to prevent multiple processes from writing to the
same DuckDB file simultaneously. DuckDB supports multiple readers but
only one writer — concurrent writes cause "Conflicting lock" errors.

Usage:
    with DuckDBLock(db_path):
        conn.execute("INSERT ...")
"""
import fcntl
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class DuckDBLock:
    """Context manager that acquires an exclusive file lock for DuckDB writes.

    Creates a .lock file next to the DB file (e.g., data/feature_store.duckdb.lock).
    Blocks until the lock is acquired. Prevents concurrent write corruption.
    """

    def __init__(self, db_path: str | Path):
        self.lock_path = Path(db_path).with_suffix(".duckdb.lock")
        self._lock_file = None

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = open(self.lock_path, "w")
        fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX)  # Blocking exclusive
        return self

    def __exit__(self, *args):
        if self._lock_file:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None
