#!/usr/bin/env bash
# SQLite-consistent backups. Run via systemd timer; this script never deletes
# source data and keeps the most recent 30 backup sets.
set -euo pipefail
: "${DATABASE_PATH:?DATABASE_PATH is required}"
: "${BACKUP_DIR:?BACKUP_DIR is required}"
mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
sqlite3 "$DATABASE_PATH" ".backup '$BACKUP_DIR/homework_${stamp}.db'"
if [[ -n "${WORKBENCH_DB:-}" && -f "$WORKBENCH_DB" ]]; then
  sqlite3 "$WORKBENCH_DB" ".backup '$BACKUP_DIR/workbench_${stamp}.db'"
fi
find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.db' -mtime +30 -print -delete
