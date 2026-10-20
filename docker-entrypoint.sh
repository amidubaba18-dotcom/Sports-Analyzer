#!/bin/sh
# Seed the persistent volume on first boot, then drop privileges.
set -eu

DB_PATH="${SPORTS_ANALYZER_DB:-/data/sports_analyzer.db}"
DB_DIR="$(dirname "$DB_PATH")"
SEED="/app/seed/sports_analyzer.db"

mkdir -p "$DB_DIR"

if [ ! -s "$DB_PATH" ]; then
    if [ ! -s "$SEED" ]; then
        echo "ERROR: no database at $DB_PATH and no seed at $SEED" >&2
        exit 1
    fi
    echo "Seeding persistent database from image snapshot..."
    cp "$SEED" "$DB_PATH"
fi

if [ "$(id -u)" = "0" ]; then
    # Platform volumes are often mounted root-owned. The app needs write
    # access for SQLite WAL files, exports and admin changes.
    chown -R app:app "$DB_DIR"
    exec gosu app "$@"
fi

exec "$@"
