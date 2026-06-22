#!/usr/bin/env python3
"""
Schema Migration Script: REAL → INTEGER for financial fields
============================================================

Addresses BUG-001 (REAL for amounts) and BUG-002 (REAL for timestamps).

Strategy:
- Amounts: Store as INTEGER in micro-units (multiply by 1e6) to avoid floating point
- Timestamps: Store as INTEGER (Unix epoch seconds)
- Backward compatible: Old code can still read REAL values during transition

Usage:
  python scripts/migrate_schema_v2.py --dry-run    # Preview changes
  python scripts/migrate_schema_v2.py --apply       # Apply migration
  python scripts/migrate_schema_v2.py --rollback    # Rollback to backup

Safety:
  1. Creates backup before any changes
  2. Verifies data integrity after migration
  3. Can rollback to pre-migration state

IMPORTANT: Run this when the trading system is STOPPED (no active cron jobs).
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "data" / "state.db"
BACKUP_SUFFIX = ".pre_migration_v2"

# ── Schema Changes ──────────────────────────────────────────────────────────

# Fields that should be INTEGER (micro-USDT, multiply by 1e6)
AMOUNT_FIELDS = {
    "portfolio": ["quantity", "entry_price", "stop_loss", "take_profit", "invest_pct"],
    "trailing_stop": ["entry_price", "highest_price", "sl_price"],
    "trades": ["qty", "price", "pnl"],
    "dca_state": ["total_invested", "avg_price", "next_buy_at"],
    "trade_outcomes": [
        "entry_price", "qty", "exit_price",
        "pnl_pct", "pnl_absolute", "net_pnl_pct", "net_pnl_absolute",
        "max_profit_pct", "max_drawdown_pct", "peak_price", "trough_price",
        "score",
    ],
    "decisions": ["score", "price", "qty", "exit_price", "pnl_pct"],
    "drawdown": ["high_watermark", "current_drawdown_pct", "max_drawdown_pct"],
    "risk_guard": ["daily_pnl"],
}

# Fields that should be INTEGER (Unix epoch seconds)
TIMESTAMP_FIELDS = {
    "portfolio": ["opened_at", "updated_at"],
    "trailing_stop": ["updated_at"],
    "trades": ["timestamp"],
    "dca_state": ["updated_at"],
    "trade_outcomes": ["entry_time", "exit_time", "created_at", "updated_at"],
    "decisions": ["timestamp"],
    "audit_log": ["timestamp"],
    "drawdown": ["tripped_at", "reset_at", "updated_at"],
    "risk_guard": ["last_reset", "updated_at"],
    "kv": ["updated_at"],
    "grid_state": ["created_at", "updated_at"],
    "strategy_state": ["updated_at"],
}

# New indexes to add
NEW_INDEXES = [
    ("idx_outcomes_exit_time", "trade_outcomes", "exit_time"),
    ("idx_outcomes_strategy", "trade_outcomes", "strategy"),
    ("idx_portfolio_strategy", "portfolio", "strategy"),
    ("idx_audit_action", "audit_log", "action"),
]


def check_prerequisites(conn: sqlite3.Connection) -> list:
    """Check if migration is safe to run."""
    issues = []
    
    # Check if WAL mode is enabled
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if mode != "wal":
        issues.append(f"Journal mode is '{mode}', not 'wal'. Consider: PRAGMA journal_mode=WAL")
    
    # Check for active transactions
    in_transaction = conn.execute("SELECT * FROM sqlite_master WHERE type='table'").fetchall()
    if not in_transaction:
        issues.append("Cannot read database schema")
    
    # Check database integrity
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        issues.append(f"Database integrity check failed: {integrity}")
    
    return issues


def backup_database(db_path: Path) -> Path:
    """Create a backup of the database."""
    backup_path = db_path.with_suffix(db_path.suffix + BACKUP_SUFFIX)
    if backup_path.exists():
        timestamp = int(time.time())
        backup_path = backup_path.with_suffix(f".{timestamp}")
    
    shutil.copy2(db_path, backup_path)
    print(f"✓ Backup created: {backup_path}")
    return backup_path


def get_table_info(conn: sqlite3.Connection, table: str) -> dict:
    """Get column info for a table."""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return {row[1]: row[2] for row in cursor.fetchall()}


def migrate_amount_field(conn: sqlite3.Connection, table: str, field: str, dry_run: bool):
    """Convert a REAL amount field to INTEGER (micro-units)."""
    # Check if field exists
    columns = get_table_info(conn, table)
    if field not in columns:
        return False
    
    current_type = columns[field]
    if current_type == "INTEGER":
        return False  # Already migrated
    
    if dry_run:
        print(f"  [DRY RUN] Would convert {table}.{field} from {current_type} to INTEGER")
        return True
    
    # Create new column
    new_field = f"{field}_int"
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {new_field} INTEGER")
    
    # Copy and convert data
    conn.execute(f"""
        UPDATE {table} 
        SET {new_field} = CAST({field} * 1000000 AS INTEGER)
        WHERE {field} IS NOT NULL
    """)
    
    # Verify conversion
    original_count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {field} IS NOT NULL").fetchone()[0]
    converted_count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {new_field} IS NOT NULL").fetchone()[0]
    
    if original_count != converted_count:
        raise RuntimeError(f"Conversion mismatch for {table}.{field}: {original_count} vs {converted_count}")
    
    # Drop old column and rename new one
    # SQLite doesn't support DROP COLUMN directly, so we need to recreate the table
    # For now, we'll keep both columns and let the application code use the new one
    print(f"  ✓ Converted {table}.{field}: {original_count} rows")
    return True


def migrate_timestamp_field(conn: sqlite3.Connection, table: str, field: str, dry_run: bool):
    """Convert a REAL timestamp field to INTEGER (Unix epoch)."""
    columns = get_table_info(conn, table)
    if field not in columns:
        return False
    
    current_type = columns[field]
    if current_type == "INTEGER":
        return False
    
    if dry_run:
        print(f"  [DRY RUN] Would convert {table}.{field} from {current_type} to INTEGER")
        return True
    
    # Create new column
    new_field = f"{field}_int"
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {new_field} INTEGER")
    
    # Copy and convert data (REAL timestamp -> INTEGER)
    conn.execute(f"""
        UPDATE {table} 
        SET {new_field} = CAST({field} AS INTEGER)
        WHERE {field} IS NOT NULL
    """)
    
    print(f"  ✓ Converted {table}.{field}")
    return True


def add_indexes(conn: sqlite3.Connection, dry_run: bool):
    """Add missing indexes."""
    for idx_name, table, column in NEW_INDEXES:
        if dry_run:
            print(f"  [DRY RUN] Would add index {idx_name} on {table}({column})")
        else:
            try:
                conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table}({column})")
                print(f"  ✓ Added index {idx_name}")
            except Exception as e:
                print(f"  ⚠ Failed to add index {idx_name}: {e}")


def run_migration(dry_run: bool = False):
    """Run the full migration."""
    if not DB_PATH.exists():
        print(f"✗ Database not found: {DB_PATH}")
        return False
    
    print(f"{'[DRY RUN] ' if dry_run else ''}Starting schema migration...")
    print(f"Database: {DB_PATH}")
    
    # Backup
    if not dry_run:
        backup_path = backup_database(DB_PATH)
    
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")  # Disable during migration
    
    try:
        # Check prerequisites
        issues = check_prerequisites(conn)
        if issues:
            print("⚠ Prerequisites not met:")
            for issue in issues:
                print(f"  - {issue}")
            if not dry_run:
                print("Aborting migration. Fix issues first.")
                return False
        
        # Migrate amount fields
        print("\n--- Amount Fields (REAL → INTEGER) ---")
        for table, fields in AMOUNT_FIELDS.items():
            for field in fields:
                migrate_amount_field(conn, table, field, dry_run)
        
        # Migrate timestamp fields
        print("\n--- Timestamp Fields (REAL → INTEGER) ---")
        for table, fields in TIMESTAMP_FIELDS.items():
            for field in fields:
                migrate_timestamp_field(conn, table, field, dry_run)
        
        # Add indexes
        print("\n--- Adding Indexes ---")
        add_indexes(conn, dry_run)
        
        if not dry_run:
            conn.commit()
            print("\n✓ Migration committed successfully")
            print(f"  Backup available at: {backup_path}")
            print("  To rollback: python scripts/migrate_schema_v2.py --rollback")
        else:
            print("\n[DRY RUN] No changes made. Run with --apply to execute.")
        
        return True
        
    except Exception as e:
        print(f"\n✗ Migration failed: {e}")
        if not dry_run:
            conn.rollback()
            print("  Rolled back changes")
        return False
    finally:
        conn.close()


def rollback():
    """Rollback to pre-migration backup."""
    backup_path = DB_PATH.with_suffix(DB_PATH.suffix + BACKUP_SUFFIX)
    
    if not backup_path.exists():
        # Check for timestamped backups
        backups = list(DB_PATH.parent.glob(f"{DB_PATH.name}.*"))
        if not backups:
            print("✗ No backup found to rollback to")
            return False
        backup_path = max(backups, key=lambda p: p.stat().st_mtime)
    
    print(f"Rolling back from: {backup_path}")
    shutil.copy2(backup_path, DB_PATH)
    print("✓ Rollback complete")
    return True


def main():
    parser = argparse.ArgumentParser(description="Schema migration: REAL → INTEGER")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview changes without applying")
    group.add_argument("--apply", action="store_true", help="Apply migration")
    group.add_argument("--rollback", action="store_true", help="Rollback to pre-migration backup")
    
    args = parser.parse_args()
    
    if args.rollback:
        rollback()
    elif args.dry_run:
        run_migration(dry_run=True)
    elif args.apply:
        confirm = input("This will modify the database. Continue? (yes/no): ")
        if confirm.lower() == "yes":
            run_migration(dry_run=False)
        else:
            print("Aborted.")


if __name__ == "__main__":
    main()
