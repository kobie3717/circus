#!/bin/bash
# Circus SQLite backup script
# Usage: ./backup.sh
# Add to cron: 0 2 * * * /root/circus/scripts/backup.sh

set -e

# Database path from environment or default
DB_PATH="${CIRCUS_DATABASE_PATH:-$HOME/.circus/circus.db}"
BACKUP_DIR="${BACKUP_DIR:-/root/backups/circus}"

# Create backup directory if it doesn't exist
mkdir -p "$BACKUP_DIR"

# Generate backup filename with timestamp
DEST="$BACKUP_DIR/circus_$(date +%Y%m%d_%H%M%S).db"

# Perform online backup using SQLite's .backup command
# This is safer than just copying the file, especially with WAL mode
sqlite3 "$DB_PATH" ".backup '$DEST'"

echo "Backup created: $DEST"

# Keep only last 30 days of backups
find "$BACKUP_DIR" -name "circus_*.db" -mtime +30 -delete

# Count remaining backups
BACKUP_COUNT=$(find "$BACKUP_DIR" -name "circus_*.db" | wc -l)
echo "Total backups: $BACKUP_COUNT"
