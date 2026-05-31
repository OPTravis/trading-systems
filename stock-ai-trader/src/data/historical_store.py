"""
Historical OHLCV data store in DuckDB.
Ingests from yfinance, stores for backtesting and factor computation.
"""

import logging
import threading
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from shared.core.db_lock import DuckDBLock

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data/feature_store.duckdb")


class HistoricalStore:
    """
    DuckDB-backed historical OHLCV store.
    Shares the same database file as FeatureStore.
    """

    def __init__(self, db_path: Optional[str | Path] = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[Any] = None
        self._write_lock = threading.Lock()
        self._init_tables()

    @property
    def conn(self):
        if self._conn is None:
            import duckdb

            self._conn = duckdb.connect(str(self.db_path))
        return self._conn

    def _init_tables(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS ohlcv_daily (
                date DATE NOT NULL,
                symbol VARCHAR NOT NULL,
                open DOUBLE,
                high DOUBLE,
                low DOUBLE,
                close DOUBLE,
                volume BIGINT,
                adj_close DOUBLE,
                PRIMARY KEY (date, symbol)
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol ON ohlcv_daily(symbol)
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ohlcv_date ON ohlcv_daily(date)
        """)
        logger.info("Historical store initialized at %s", self.db_path)

    def ingest_symbol(self, symbol: str, period: str = "2y") -> int:
        """
        Fetch OHLCV from yfinance and upsert into DuckDB.

        Args:
            symbol: Ticker symbol (e.g. 'AAPL', '0700.HK')
            period: yfinance period ('1y', '2y', '5y', 'max')

        Returns:
            Number of rows upserted.
        """
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval="1d", auto_adjust=False)
        if df.empty:
            logger.warning("No data for %s", symbol)
            return 0

        # Normalize columns
        df.columns = [c.lower().replace(" ", "_") for c in df.columns]
        df = df.reset_index()
        df["symbol"] = symbol

        # Rename 'date' column if needed (yfinance uses 'date' after reset_index)
        if "date" not in df.columns and "Date" in df.columns:
            df = df.rename(columns={"Date": "date"})

        # Ensure date is date type
        df["date"] = pd.to_datetime(df["date"]).dt.date

        # Select columns we need
        cols = ["date", "symbol", "open", "high", "low", "close", "volume"]
        if "adj_close" in df.columns:
            cols.append("adj_close")
            df["adj_close"] = df["adj_close"].fillna(df["close"])
        else:
            df["adj_close"] = df["close"]
            cols.append("adj_close")

        df = df[[c for c in cols if c in df.columns]]
        df = df.dropna(subset=["close"])

        # Upsert
        records = list(df.itertuples(index=False, name=None))
        with self._write_lock, DuckDBLock(self.db_path):
            self.conn.executemany(
                "INSERT OR REPLACE INTO ohlcv_daily (date, symbol, open, high, low, close, volume, adj_close) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [tuple(r) for r in records],
            )
        count = len(records)
        logger.info("Ingested %d rows for %s", count, symbol)
        return count

    def ingest_batch(self, symbols: list[str], period: str = "2y") -> dict:
        """Ingest multiple symbols. Returns {symbol: row_count}."""
        import yfinance as yf

        results = {}
        logger.info("Batch downloading %d symbols (%s period)", len(symbols), period)

        for sym in symbols:
            try:
                df = yf.Ticker(sym).history(
                    period=period, interval="1d", auto_adjust=False
                )
                if df.empty:
                    logger.warning("No data for %s", sym)
                    results[sym] = 0
                    continue

                df = df.reset_index()
                df.columns = [c.lower().replace(" ", "_") for c in df.columns]
                df["symbol"] = sym
                df["date"] = pd.to_datetime(df["date"]).dt.date

                if "adj_close" not in df.columns:
                    df["adj_close"] = df["close"]

                cols = [
                    "date",
                    "symbol",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "adj_close",
                ]
                df = df[[c for c in cols if c in df.columns]]
                df = df.dropna(subset=["close"])

                # Convert numpy types to Python types (DuckDB requirement)
                for col in df.columns:
                    if df[col].dtype == np.int64:
                        df[col] = df[col].astype(int)
                    elif df[col].dtype == np.float64:
                        df[col] = df[col].astype(float)

                records = list(df.itertuples(index=False, name=None))
                with self._write_lock, DuckDBLock(self.db_path):
                    self.conn.executemany(
                        "INSERT OR REPLACE INTO ohlcv_daily (date, symbol, open, high, low, close, volume, adj_close) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        [tuple(r) for r in records],
                    )
                results[sym] = len(records)
            except Exception as e:
                logger.error("Failed %s: %s", sym, e)
                results[sym] = 0

        total = sum(results.values())
        logger.info(
            "Batch ingest complete: %d rows across %d symbols", total, len(symbols)
        )
        return results

    def get_ohlcv(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Get OHLCV data for a symbol."""
        query = "SELECT * FROM ohlcv_daily WHERE symbol = ?"
        params: list = [symbol]

        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)

        query += " ORDER BY date"
        return self.conn.execute(query, params).fetchdf()

    def get_all_symbols(self) -> list[str]:
        """Get all symbols with data."""
        df = self.conn.execute(
            "SELECT DISTINCT symbol FROM ohlcv_daily ORDER BY symbol"
        ).fetchdf()
        return df["symbol"].tolist() if not df.empty else []

    def get_date_range(self, symbol: str) -> tuple:
        """Get min/max date for a symbol."""
        row = self.conn.execute(
            "SELECT MIN(date), MAX(date) FROM ohlcv_daily WHERE symbol = ?",
            [symbol],
        ).fetchone()
        return (row[0], row[1]) if row and row[0] else (None, None)

    def get_row_count(self, symbol: Optional[str] = None) -> int:
        """Get total row count."""
        if symbol:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM ohlcv_daily WHERE symbol = ?", [symbol]
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) FROM ohlcv_daily").fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
