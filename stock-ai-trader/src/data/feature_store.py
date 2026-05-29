"""
Feature store for factor values and IC history using DuckDB.
Provides efficient columnar storage for quantitative factor data.
"""
import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from shared.core.db_lock import DuckDBLock

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data/feature_store.duckdb")


class FeatureStore:
    """
    DuckDB-backed feature store for storing and retrieving:
    - Factor values (date × symbol × factor matrix)
    - IC (Information Coefficient) history per factor
    """

    def __init__(self, db_path: Optional[str | Path] = None):
        """
        Args:
            db_path: Path to DuckDB database file. Created if it doesn't exist.
        """
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = None
        self._init_tables()

    @property
    def conn(self):
        if self._conn is None:
            import duckdb
            self._conn = duckdb.connect(str(self.db_path))
        return self._conn

    def _init_tables(self) -> None:
        """Create tables if they don't exist."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS factor_values (
                date DATE NOT NULL,
                symbol VARCHAR NOT NULL,
                factor_name VARCHAR NOT NULL,
                value DOUBLE,
                PRIMARY KEY (date, symbol, factor_name)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS ic_history (
                date DATE NOT NULL,
                factor_name VARCHAR NOT NULL,
                ic_value DOUBLE,
                PRIMARY KEY (date, factor_name)
            )
        """)
        logger.info("Feature store initialized at %s", self.db_path)

    def save_factor_values(self, date: str, factor_df: pd.DataFrame) -> int:
        """
        Save factor values for a given date.

        Args:
            date: Date string (YYYY-MM-DD).
            factor_df: DataFrame with 'symbol' column and one column per factor.
                       Example: symbol | momentum_20d | vol_20d | rsi_14
                                AAPL    | 0.05         | 0.22    | 65.3

        Returns:
            Number of rows upserted.
        """
        if factor_df.empty:
            return 0

        # Melt from wide to long format
        id_vars = ["symbol"]
        value_vars = [c for c in factor_df.columns if c != "symbol"]
        melted = factor_df.melt(id_vars=id_vars, value_vars=value_vars,
                                var_name="factor_name", value_name="value")
        melted["date"] = date
        melted = melted.dropna(subset=["value"])

        # Upsert using INSERT OR REPLACE
        records = melted[["date", "symbol", "factor_name", "value"]].dropna(subset=["value"]).to_records(index=False)
        record_list = [tuple(r) for r in records]
        if not record_list:
            return 0
        with DuckDBLock(self.db_path):
            self.conn.executemany(
                "INSERT OR REPLACE INTO factor_values (date, symbol, factor_name, value) VALUES (?, ?, ?, ?)",
                record_list,
            )
        count = len(records)
        logger.info("Saved %d factor values for %s", count, date)
        return count

    def get_factor_values(
        self,
        date: Optional[str] = None,
        symbols: Optional[list[str]] = None,
        factor_names: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        Retrieve factor values for a given date.

        Args:
            date: Date string (YYYY-MM-DD). None = latest available date.
            symbols: Optional list of symbols to filter.
            factor_names: Optional list of factor names to filter.

        Returns:
            DataFrame with columns: date, symbol, factor_name, value.
        """
        if date is None:
            # Get latest date
            latest = self.conn.execute("SELECT MAX(date) FROM factor_values").fetchone()
            if not latest or not latest[0]:
                return pd.DataFrame()
            date = str(latest[0])
        query = "SELECT * FROM factor_values WHERE date = ?"
        params: list = [date]

        if symbols:
            placeholders = ",".join(["?"] * len(symbols))
            query += f" AND symbol IN ({placeholders})"
            params.extend(symbols)

        if factor_names:
            placeholders = ",".join(["?"] * len(factor_names))
            query += f" AND factor_name IN ({placeholders})"
            params.extend(factor_names)

        return self.conn.execute(query, params).fetchdf()

    def get_factor_matrix(
        self,
        start_date: str,
        end_date: str,
        factor_name: str,
    ) -> pd.DataFrame:
        """
        Get a date × symbol matrix for a single factor.

        Returns:
            Pivot table with dates as index, symbols as columns.
        """
        df = self.conn.execute(
            "SELECT date, symbol, value FROM factor_values "
            "WHERE factor_name = ? AND date BETWEEN ? AND ? "
            "ORDER BY date, symbol",
            [factor_name, start_date, end_date],
        ).fetchdf()

        if df.empty:
            return pd.DataFrame()

        return df.pivot(index="date", columns="symbol", values="value")

    def save_ic_history(self, factor_name: str, ic_values: dict[str, float]) -> int:
        """
        Save IC (Information Coefficient) values for a factor.

        Args:
            factor_name: Name of the factor.
            ic_values: Dict mapping date string -> IC value.

        Returns:
            Number of rows upserted.
        """
        records = [(date, factor_name, ic) for date, ic in ic_values.items()]
        with DuckDBLock(self.db_path):
            self.conn.executemany(
                "INSERT OR REPLACE INTO ic_history (date, factor_name, ic_value) VALUES (?, ?, ?)",
                records,
            )
        count = len(records)
        logger.info("Saved %d IC values for factor '%s'", count, factor_name)
        return count

    def get_ic_history(self, factor_name: str) -> pd.Series:
        """
        Get IC history for a factor as a time series.

        Args:
            factor_name: Name of the factor.

        Returns:
            Series with dates as index and IC values.
        """
        df = self.conn.execute(
            "SELECT date, ic_value FROM ic_history WHERE factor_name = ? ORDER BY date",
            [factor_name],
        ).fetchdf()

        if df.empty:
            return pd.Series(dtype=float, name=factor_name)

        return df.set_index("date")["ic_value"].rename(factor_name)

    def get_all_factors(self) -> list[str]:
        """Get a list of all stored factor names."""
        df = self.conn.execute("SELECT DISTINCT factor_name FROM factor_values ORDER BY factor_name").fetchdf()
        return df["factor_name"].tolist() if not df.empty else []

    def get_factor_stats(self, factor_name: str, start_date: str, end_date: str) -> dict:
        """
        Get summary statistics for a factor over a date range.

        Returns:
            Dict with keys: mean, std, min, max, count, ic_mean, ic_std.
        """
        vals = self.conn.execute(
            "SELECT AVG(value), STDDEV(value), MIN(value), MAX(value), COUNT(*) "
            "FROM factor_values WHERE factor_name = ? AND date BETWEEN ? AND ?",
            [factor_name, start_date, end_date],
        ).fetchone()

        ic = self.conn.execute(
            "SELECT AVG(ic_value), STDDEV(ic_value) FROM ic_history "
            "WHERE factor_name = ? AND date BETWEEN ? AND ?",
            [factor_name, start_date, end_date],
        ).fetchone()

        return {
            "mean": float(vals[0] or 0),
            "std": float(vals[1] or 0),
            "min": float(vals[2] or 0),
            "max": float(vals[3] or 0),
            "count": int(vals[4] or 0),
            "ic_mean": float(ic[0] or 0),
            "ic_std": float(ic[1] or 0),
        }

    def close(self) -> None:
        """Close the DuckDB connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
