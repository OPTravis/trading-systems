"""
Shared utilities for data feed modules.

Provides SQLite connection helpers, cache paths, TTLs, and API constants
used across all data feed sub-modules.
"""

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "data"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DB = str(CACHE_DIR / "cache.db")

# ---------------------------------------------------------------------------
# Cache TTLs (seconds)
# ---------------------------------------------------------------------------
FNG_TTL = 3600  # 1 hour
NEWS_TTL = 600  # 10 minutes
FUNDING_TTL = 300  # 5 minutes

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
FNG_URL = "https://api.alternative.me/fng/"
NEWS_URL = (
    "https://min-api.cryptocompare.com/data/v2/news/?lang=EN&extraParams=CryptoAITrader"
)

# P1 news keywords (high-impact events)
P1_KEYWORDS = [
    "regulation",
    "sec",
    "hack",
    "exploit",
    "whale",
    "etf",
    "ban",
    "compliance",
    "enforcement",
]


# ===================================================================
# SQLite helper
# ===================================================================


def _get_conn(db_path: str = CACHE_DB) -> sqlite3.Connection:
    """Return a SQLite connection with WAL mode and row factory."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_tables() -> None:
    """Create cache tables if they don't exist."""
    conn = _get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS fng_history (
                date        TEXT PRIMARY KEY,
                value       INTEGER NOT NULL,
                classification TEXT NOT NULL,
                fetched_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS news_cache (
                id          INTEGER PRIMARY KEY,
                published_on INTEGER NOT NULL,
                title       TEXT,
                categories  TEXT,
                body        TEXT,
                fetched_at  REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_news_published ON news_cache(published_on);
        """)
        conn.commit()
    finally:
        conn.close()


# Initialise tables on import
_init_tables()
