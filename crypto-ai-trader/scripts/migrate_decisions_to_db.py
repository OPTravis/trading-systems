#!/usr/bin/env python3
"""Migrate existing JSON decision files into state.db decisions table.

Run once after TradeJournal migration. Idempotent (skips already-migrated records).
"""
import json
import sys
import os
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from state_db import get_state_db

def migrate():
    decisions_dir = project_root / "data" / "decisions"
    if not decisions_dir.exists():
        print("No decisions directory found. Nothing to migrate.")
        return

    db = get_state_db()
    total = 0
    skipped = 0

    for json_file in sorted(decisions_dir.glob("*.json")):
        print(f"Migrating {json_file.name}...")
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"  SKIP (read error): {e}")
            continue

        entries = data.get("decisions", [])
        for entry in entries:
            entry_type = entry.get("type", "decision")
            symbol = entry.get("symbol", "")
            timestamp = entry.get("timestamp", "")

            # Check for duplicates by timestamp + symbol + type
            existing = db._get_conn().execute(
                "SELECT id FROM decisions WHERE timestamp = ? AND symbol = ? AND type = ?",
                (_parse_timestamp(timestamp), symbol, entry_type),
            ).fetchone()
            if existing:
                skipped += 1
                continue

            # Map JSON fields to DB columns
            bear_result = entry.get("bear_result")
            bear_score = None
            bear_veto = None
            bear_reasons = None
            bear_confidence = None
            if isinstance(bear_result, dict):
                bear_score = bear_result.get("bear_score")
                bear_veto = 1 if bear_result.get("veto") else 0
                bear_reasons = json.dumps(bear_result.get("reasons", []))
                bear_confidence = bear_result.get("confidence")

            db._get_conn().execute(
                """INSERT INTO decisions
                (timestamp, date, symbol, type, decision, score, price, qty, side, strategy,
                 reasons, signals, bear_score, bear_veto, bear_reasons, bear_confidence,
                 research, exit_price, pnl_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _parse_timestamp(timestamp),
                    timestamp[:10] if timestamp else "",
                    symbol,
                    entry_type,
                    entry.get("decision", entry.get("side", "")),
                    entry.get("score", 0),
                    entry.get("price", 0),
                    entry.get("qty", 0),
                    entry.get("side", ""),
                    entry.get("strategy", ""),
                    json.dumps(entry.get("reasons", [])),
                    json.dumps(entry.get("signals", [])),
                    bear_score,
                    bear_veto,
                    bear_reasons,
                    bear_confidence,
                    _serialize_research(entry.get("research", "")),
                    entry.get("exit_price", 0),
                    entry.get("pnl_pct", 0),
                ),
            )
            total += 1

        db._get_conn().commit()

    print(f"\nMigration complete: {total} records imported, {skipped} skipped (already exist)")


def _parse_timestamp(ts_str):
    """Convert ISO timestamp string to epoch float."""
    if not ts_str:
        return 0
    try:
        from datetime import datetime
        # Handle ISO format with or without timezone
        ts_clean = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_clean)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0


def _serialize_research(research):
    """Convert research field to string for storage."""
    if isinstance(research, str):
        return research
    if isinstance(research, dict):
        return json.dumps(research, ensure_ascii=False)
    return str(research) if research else ""


if __name__ == "__main__":
    migrate()
