#!/usr/bin/env bash
# Backup state.db daily with rotation (keep last 7 days)

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DB_PATH="$PROJECT_DIR/data/state.db"
BACKUP_DIR="$PROJECT_DIR/data/backups"
DATE=$(date +%Y%m%d_%H%M%S)

mkdir -p "$BACKUP_DIR"

if [ -f "$DB_PATH" ]; then
    cp "$DB_PATH" "$BACKUP_DIR/state_$DATE.db"
    # Compress
    gzip -f "$BACKUP_DIR/state_$DATE.db"
    echo "✅ Backup created: state_$DATE.db.gz"
    
    # Rotate: keep last 7 backups
    ls -t "$BACKUP_DIR"/state_*.db.gz 2>/dev/null | tail -n +8 | xargs -r rm -f
    echo "🗑️  Old backups rotated (keep last 7)"
else
    echo "❌ state.db not found at $DB_PATH"
    exit 1
fi
