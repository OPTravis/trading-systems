"""Test that proves test contamination is impossible.

If this test passes, the three-layer protection is working:
1. STATE_DB_PATH env var redirects to temp file
2. TESTING env var triggers hard guard
3. Singleton hot-swap works when path changes
"""

import os
import time
from pathlib import Path


class TestTestIsolation:
    """Verify that tests CANNOT touch the production state DB."""

    # bug#15: production DB moved to local ext4 (/root/trading-state);
    # state_db.DEFAULT_DB_PATH resolves there when the file exists.
    from src.state_db import DEFAULT_DB_PATH
    PRODUCTION_DB = str(DEFAULT_DB_PATH)

    def test_state_db_path_is_not_production(self):
        """During tests, STATE_DB_PATH must point to a temp file."""
        from src.state_db import get_state_db
        db = get_state_db()
        assert str(db.db_path) != self.PRODUCTION_DB, (
            f"FATAL: Test is using production DB at {self.PRODUCTION_DB}! "
            f"Test isolation is broken."
        )

    def test_testing_env_is_set(self):
        """TESTING env var must be set during test runs."""
        assert os.environ.get("TESTING"), (
            "TESTING env var not set! conftest fixture is broken."
        )

    def test_kv_writes_do_not_leak_to_production(self):
        """Write to KV during test, then verify production DB is clean."""
        import sqlite3
        from src.state_db import get_state_db

        # Write a unique marker to the test DB
        marker = f"isolation_check_{int(time.time())}"
        db = get_state_db()
        db.kv_set(marker, "contaminated")

        # Read directly from production DB to verify it's NOT there
        if Path(self.PRODUCTION_DB).exists():
            prod_conn = sqlite3.connect(self.PRODUCTION_DB, timeout=5)
            row = prod_conn.execute(
                "SELECT value FROM kv WHERE key = ?", (marker,)
            ).fetchone()
            prod_conn.close()
            assert row is None, (
                f"FATAL: Test data leaked to production DB! "
                f"Key '{marker}' found in {self.PRODUCTION_DB}"
            )

    def test_hard_guard_blocks_production_during_testing(self):
        """If TESTING is set and db_path is production, must raise."""
        from src.state_db import get_state_db, DEFAULT_DB_PATH
        import src.state_db as sd_mod

        # Save and clear singleton
        saved = sd_mod._state_db_instance
        sd_mod._state_db_instance = None

        # Remove STATE_DB_PATH so only TESTING guard remains
        saved_env = os.environ.pop("STATE_DB_PATH", None)

        try:
            # TESTING is still set by conftest, DEFAULT_DB_PATH is production
            with pytest.raises(RuntimeError, match="BLOCKED"):
                get_state_db(str(DEFAULT_DB_PATH))
        finally:
            # Restore
            if saved_env:
                os.environ["STATE_DB_PATH"] = saved_env
            sd_mod._state_db_instance = saved

    def test_hot_swap_when_env_path_changes(self):
        """Singleton should recreate when STATE_DB_PATH changes."""
        import src.state_db as sd_mod
        from src.state_db import get_state_db

        db1 = get_state_db()
        path1 = str(db1.db_path)

        # Change to a new temp path
        saved_env = os.environ.get("STATE_DB_PATH")
        new_path = str(Path(path1).parent / "test_state2.db")
        os.environ["STATE_DB_PATH"] = new_path

        try:
            db2 = get_state_db()
            assert str(db2.db_path) == new_path, (
                f"Hot-swap failed: expected {new_path}, got {db2.db_path}"
            )
        finally:
            if saved_env:
                os.environ["STATE_DB_PATH"] = saved_env
            sd_mod._state_db_instance = None  # reset for other tests


import pytest  # noqa: E402
