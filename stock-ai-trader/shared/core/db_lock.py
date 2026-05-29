"""File-based lock for DuckDB concurrent write protection.

Uses fcntl.flock() to prevent multiple processes from writing to the
same DuckDB file simultaneously. DuckDB supports multiple readers but
only one writer — concurrent writes cause "Conflicting lock" errors.

Usage:
    with DuckDBLock(db_path):
        conn.execute("INSERT ...")
"""
import errno
import fcntl
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class DuckDBLock:
    """Context manager that acquires an exclusive file lock for DuckDB writes.

    Creates a .lock file next to the DB file (e.g., data/feature_store.duckdb.lock).
    Uses LOCK_NB with a retry loop and configurable timeout to prevent indefinite
    blocking. Prevents concurrent write corruption.
    """

    def __init__(self, db_path: str | Path, timeout: float = 30.0, retry_interval: float = 0.5):
        self.lock_path = Path(db_path).with_suffix(".duckdb.lock")
        self._lock_file = None
        self._timeout = timeout
        self._retry_interval = retry_interval

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = open(self.lock_path, "w")

        deadline = time.monotonic() + self._timeout
        while True:
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    logger.error("DuckDBLock timed out after %.1fs acquiring %s",
                                 self._timeout, self.lock_path)
                    raise TimeoutError(
                        f"Could not acquire lock on {self.lock_path} "
                        f"within {self._timeout:.1f}s"
                    )
                time.sleep(self._retry_interval)

    def __exit__(self, *args):
        if self._lock_file:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None
