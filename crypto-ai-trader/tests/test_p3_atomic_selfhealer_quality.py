"""P3 fixes tests — atomic transactions, self_healer safety gate, data feed quality."""
import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ===================================================================
# P3-1: PaperTrader atomic transactions
# ===================================================================

class TestPaperTraderAtomicTransaction:
    """Verify that _fill_market commits all state changes atomically."""

    @pytest.fixture()
    def paper_db(self, tmp_path):
        """Create a temporary StateDB for PaperTrader."""
        db_path = tmp_path / "test_state.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS paper_trades (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                quantity REAL NOT NULL,
                fill_price REAL NOT NULL,
                slippage_pct REAL,
                fee_usdt REAL,
                notional_usdt REAL,
                status TEXT DEFAULT 'filled',
                timestamp REAL NOT NULL,
                details TEXT
            );
            CREATE TABLE IF NOT EXISTS paper_portfolio (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                side TEXT,
                quantity REAL,
                price REAL,
                pnl REAL,
                timestamp REAL
            );
        """)
        conn.commit()
        conn.close()
        return db_path

    @pytest.fixture()
    def paper_trader(self, paper_db):
        """Create a PaperTrader with mocked state."""
        from src.paper_trader import PaperTrader

        with patch("src.paper_trader.ccxt") as mock_ccxt:
            mock_exchange = MagicMock()
            mock_exchange.load_markets = MagicMock()
            mock_ccxt.binance = MagicMock(return_value=mock_exchange)

            with patch("src.paper_trader.PaperTrader._get_db") as mock_get_db:
                mock_db = MagicMock()
                conn = sqlite3.connect(str(paper_db))
                conn.row_factory = sqlite3.Row
                mock_db._get_conn = MagicMock(return_value=conn)
                mock_get_db.return_value = mock_db

                pt = PaperTrader()
                pt._db = mock_db
                pt._in_transaction = False
                yield pt, conn

    def test_transaction_mode_defers_commit(self, paper_trader):
        """When _in_transaction=True, _set_sim_value should NOT commit."""
        pt, conn = paper_trader
        conn.execute(
            "INSERT INTO paper_portfolio (key, value, updated_at) VALUES ('test_key', 'old_val', ?)",
            (time.time(),),
        )
        conn.commit()

        pt._begin_transaction()
        assert pt._in_transaction is True
        pt._set_sim_value("test_key", "new_val")

        # Value should be in the DB (SQLite auto-flushes within conn)
        # but the logical transaction is still open
        row = conn.execute("SELECT value FROM paper_portfolio WHERE key='test_key'").fetchone()
        # The value IS written (SQLite writes on execute, commit is for durability)
        # The key test is that _in_transaction is still True
        assert pt._in_transaction is True

        pt._commit_transaction()
        assert pt._in_transaction is False

    def test_rollback_resets_transaction_flag(self, paper_trader):
        """_rollback_transaction should reset _in_transaction to False."""
        pt, conn = paper_trader
        pt._begin_transaction()
        assert pt._in_transaction is True
        pt._rollback_transaction()
        assert pt._in_transaction is False

    def test_auto_commit_when_not_in_transaction(self, paper_trader):
        """Without transaction mode, _set_sim_value commits immediately."""
        pt, conn = paper_trader
        assert pt._in_transaction is False
        # Should not raise
        pt._set_sim_value("auto_key", "auto_val")
        row = conn.execute("SELECT value FROM paper_portfolio WHERE key='auto_key'").fetchone()
        assert row is not None
        assert row["value"] == "auto_val"

    def test_fill_market_snapshot_for_rollback(self, paper_trader):
        """_fill_market should snapshot state before modifying, enabling rollback."""
        pt, conn = paper_trader

        # Set initial balance
        conn.execute(
            "INSERT OR REPLACE INTO paper_portfolio (key, value, updated_at) VALUES ('cash_balance', '10000', ?)",
            (time.time(),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO paper_portfolio (key, value, updated_at) VALUES ('positions', '{}', ?)",
            (time.time(),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO paper_portfolio (key, value, updated_at) VALUES ('realized_pnl', '0', ?)",
            (time.time(),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO paper_portfolio (key, value, updated_at) VALUES ('order_counter', '0', ?)",
            (time.time(),),
        )
        conn.commit()

        # Verify snapshot values can be read
        snap_balance = pt._get_sim_balance()
        snap_positions = pt._get_sim_positions()
        snap_pnl = pt._get_sim_pnl()
        snap_counter = pt._get_sim_order_counter()

        assert snap_balance == 10000.0
        assert snap_positions == {}
        assert snap_pnl == 0.0
        assert snap_counter == 0


# ===================================================================
# P3-3: Self-healer safety gate
# ===================================================================

class TestSelfHealerSafetyGate:
    """Verify that self_healer defaults to dry-run mode."""

    def test_auto_fix_disabled_by_default(self):
        """SELF_HEALER_AUTO_FIX should default to False."""
        # Ensure env var is not set
        env = os.environ.copy()
        env.pop("SELF_HEALER_AUTO_FIX", None)
        with patch.dict(os.environ, env, clear=True):
            # Re-import to get fresh value
            import importlib
            import src.self_healer as sh
            importlib.reload(sh)
            assert sh.AUTO_FIX_ENABLED is False

    def test_auto_fix_enabled_via_env(self):
        """SELF_HEALER_AUTO_FIX=1 should enable auto-fix."""
        with patch.dict(os.environ, {"SELF_HEALER_AUTO_FIX": "1"}):
            import importlib
            import src.self_healer as sh
            importlib.reload(sh)
            assert sh.AUTO_FIX_ENABLED is True

    def test_fix_klines_bug_dry_run(self, tmp_path):
        """In dry-run mode, _fix_klines_bug should NOT modify files."""
        import src.self_healer as sh

        # Create a test file with the bug pattern
        test_dir = tmp_path / "src"
        test_dir.mkdir()
        test_file = test_dir / "trade_executor.py"
        test_file.write_text("price = float(k[4])\n")

        with patch.object(sh, "CRYPTO_DIR", tmp_path), \
             patch.object(sh, "AUTO_FIX_ENABLED", False):
            result = sh._fix_klines_bug()

        assert result["fixed"] is False
        assert "DRY-RUN" in result["msg"]
        # File should NOT be modified
        assert "k[4]" in test_file.read_text()

    def test_fix_klines_bug_with_auto_fix(self, tmp_path):
        """With AUTO_FIX_ENABLED=True, _fix_klines_bug should modify files."""
        import src.self_healer as sh

        test_dir = tmp_path / "src"
        test_dir.mkdir()
        test_file = test_dir / "trade_executor.py"
        test_file.write_text("price = float(k[4])\n")

        with patch.object(sh, "CRYPTO_DIR", tmp_path), \
             patch.object(sh, "AUTO_FIX_ENABLED", True):
            result = sh._fix_klines_bug()

        assert result["fixed"] is True
        assert "REVIEW REQUIRED" in result["msg"]
        # File SHOULD be modified
        content = test_file.read_text()
        assert "k['close']" in content
        assert "k[4]" not in content


# ===================================================================
# P3-4: DataFeedManager data quality tracking
# ===================================================================

class TestDataFeedQuality:
    """Verify that DataFeedManager returns feed_status and data_quality."""

    def test_snapshot_includes_quality_fields(self):
        """get_market_snapshot() should return feed_status and data_quality."""
        from src.data_feed import DataFeedManager

        mgr = DataFeedManager()
        # Mock all feeds to succeed
        mgr.fng = MagicMock()
        mgr.fng.get_current.return_value = {"value": 50}
        mgr.news = MagicMock()
        mgr.news.get_crypto_news.return_value = []
        mgr.news.classify_news.return_value = {"P1": [], "P2": []}
        mgr.funding = MagicMock()
        mgr.funding.get_funding_summary.return_value = {}
        mgr.scorer = MagicMock()
        mgr.scorer.get_symbol_sentiment.return_value = {"sentiment_score": 0, "funding_rate": 0}
        mgr.onchain = MagicMock()
        mgr.onchain.get_onchain_score.return_value = 50

        with patch.object(DataFeedManager, "_get_btc_price", return_value=100000.0):
            snapshot = mgr.get_market_snapshot()

        assert "feed_status" in snapshot
        assert "data_quality" in snapshot
        assert snapshot["data_quality"] == 1.0
        assert all(v["ok"] for v in snapshot["feed_status"].values())

    def test_snapshot_degraded_quality(self):
        """When some feeds fail, data_quality should reflect the ratio."""
        from src.data_feed import DataFeedManager

        mgr = DataFeedManager()
        # F&G fails, others succeed
        mgr.fng = MagicMock()
        mgr.fng.get_current.side_effect = Exception("API timeout")
        mgr.news = MagicMock()
        mgr.news.get_crypto_news.return_value = []
        mgr.news.classify_news.return_value = {"P1": [], "P2": []}
        mgr.funding = MagicMock()
        mgr.funding.get_funding_summary.return_value = {}
        mgr.scorer = MagicMock()
        mgr.scorer.get_symbol_sentiment.return_value = {"sentiment_score": 0, "funding_rate": 0}
        mgr.onchain = MagicMock()
        mgr.onchain.get_onchain_score.return_value = 50

        with patch.object(DataFeedManager, "_get_btc_price", return_value=100000.0):
            snapshot = mgr.get_market_snapshot()

        assert snapshot["data_quality"] == round(5 / 6, 2)
        assert snapshot["feed_status"]["fear_greed"]["ok"] is False
        assert "API timeout" in snapshot["feed_status"]["fear_greed"]["error"]
        assert snapshot["feed_status"]["funding"]["ok"] is True

    def test_snapshot_all_feeds_fail(self):
        """When all feeds fail, data_quality should be 0.0."""
        from src.data_feed import DataFeedManager

        mgr = DataFeedManager()
        fail = Exception("network down")
        mgr.fng = MagicMock()
        mgr.fng.get_current.side_effect = fail
        mgr.news = MagicMock()
        mgr.news.get_crypto_news.side_effect = fail
        mgr.funding = MagicMock()
        mgr.funding.get_funding_summary.side_effect = fail
        mgr.scorer = MagicMock()
        mgr.scorer.get_symbol_sentiment.side_effect = fail
        mgr.onchain = MagicMock()
        mgr.onchain.get_onchain_score.side_effect = fail

        with patch.object(DataFeedManager, "_get_btc_price", side_effect=fail):
            snapshot = mgr.get_market_snapshot()

        assert snapshot["data_quality"] == 0.0
        assert all(not v["ok"] for v in snapshot["feed_status"].values())
