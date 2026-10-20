"""Create the NBA tables. Football tables are not touched.

Why a separate namespace
------------------------
The NBA is a second sport, not a new market list. Its counting unit is a
team-game (one row per team per game), its identity key is the provider's
`TEAM_ID` rather than a name, and its model is possessions-based. Sharing the
football tables would force those differences into the frozen football
pipeline, so everything here is prefixed `nba_`.

The three-concept separation is kept, because it is the part that carries over:

    nba_games        a completed game the engine may count (evidence)
    nba_fixtures     the schedule says this game exists (a candidate)
    nba_published    the user is allowed to see it (a decision)

`nba_games` and `nba_fixtures` are never merged. Ingesting history writes only
`nba_games`; refreshing the schedule writes only `nba_fixtures`; publishing
writes only `nba_published`.

Quarter scores live on `nba_games` because they are a property of the game
result, while the team box score lives on `nba_team_games` because it is the
unit the averages are computed from.

Usage:
    python scripts/nba_schema.py
    python scripts/nba_schema.py --db some/other.db
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "sports_analyzer.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nba_teams (
    team_id      INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    city         TEXT,
    tricode      TEXT NOT NULL,
    slug         TEXT
);

CREATE TABLE IF NOT EXISTS nba_games (
    id                INTEGER PRIMARY KEY,
    game_id           TEXT NOT NULL UNIQUE,
    season            TEXT NOT NULL,
    season_type       TEXT NOT NULL DEFAULT 'Regular Season',
    game_date         TEXT NOT NULL,
    game_datetime_utc TEXT,
    home_team_id      INTEGER NOT NULL,
    away_team_id      INTEGER NOT NULL,
    home_score        INTEGER,
    away_score        INTEGER,
    home_q1           INTEGER,
    home_q2           INTEGER,
    home_q3           INTEGER,
    home_q4           INTEGER,
    home_ot           INTEGER NOT NULL DEFAULT 0,
    away_q1           INTEGER,
    away_q2           INTEGER,
    away_q3           INTEGER,
    away_q4           INTEGER,
    away_ot           INTEGER NOT NULL DEFAULT 0,
    went_to_ot        INTEGER NOT NULL DEFAULT 0,
    has_quarters      INTEGER NOT NULL DEFAULT 0,
    source            TEXT NOT NULL DEFAULT 'nba_api',
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_nba_games_date ON nba_games(game_date);
CREATE INDEX IF NOT EXISTS idx_nba_games_home ON nba_games(home_team_id);
CREATE INDEX IF NOT EXISTS idx_nba_games_away ON nba_games(away_team_id);

-- The counting unit: one row per team per game. `possessions`, `off_rating`,
-- `def_rating` and `pace` are derived at ingest time so every reader agrees on
-- the formula instead of re-deriving it (and disagreeing) later.
CREATE TABLE IF NOT EXISTS nba_team_games (
    id           INTEGER PRIMARY KEY,
    game_id      TEXT NOT NULL,
    team_id      INTEGER NOT NULL,
    opponent_id  INTEGER NOT NULL,
    is_home      INTEGER NOT NULL,
    season       TEXT NOT NULL,
    game_date    TEXT NOT NULL,
    minutes      REAL,
    points       INTEGER,
    fgm          INTEGER,
    fga          INTEGER,
    fg3m         INTEGER,
    fg3a         INTEGER,
    ftm          INTEGER,
    fta          INTEGER,
    oreb         INTEGER,
    dreb         INTEGER,
    reb          INTEGER,
    ast          INTEGER,
    stl          INTEGER,
    blk          INTEGER,
    tov          INTEGER,
    pf           INTEGER,
    possessions  REAL,
    off_rating   REAL,
    def_rating   REAL,
    pace         REAL,
    UNIQUE (game_id, team_id)
);

CREATE INDEX IF NOT EXISTS idx_nba_team_games_team
    ON nba_team_games(team_id, game_date);
CREATE INDEX IF NOT EXISTS idx_nba_team_games_game
    ON nba_team_games(game_id);

CREATE TABLE IF NOT EXISTS nba_fixtures (
    id                INTEGER PRIMARY KEY,
    game_id           TEXT NOT NULL UNIQUE,
    season            TEXT NOT NULL,
    game_date         TEXT NOT NULL,
    game_datetime_utc TEXT,
    home_team_id      INTEGER NOT NULL,
    away_team_id      INTEGER NOT NULL,
    status            TEXT NOT NULL DEFAULT 'scheduled',
    published_id      INTEGER,
    last_seen         TEXT NOT NULL DEFAULT (datetime('now')),
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_nba_fixtures_date ON nba_fixtures(game_date);

CREATE TABLE IF NOT EXISTS nba_published (
    id                INTEGER PRIMARY KEY,
    game_id           TEXT NOT NULL UNIQUE,
    season            TEXT NOT NULL,
    game_date         TEXT NOT NULL,
    game_datetime_utc TEXT,
    home_team_id      INTEGER NOT NULL,
    away_team_id      INTEGER NOT NULL,
    status            TEXT NOT NULL DEFAULT 'scheduled',
    -- The line the admin saw and chose to show. NULL means "not entered";
    -- the analysis then uses the league-reference line and says so.
    market_total      REAL,
    market_home_total REAL,
    market_away_total REAL,
    published_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_nba_published_date ON nba_published(game_date);

CREATE TABLE IF NOT EXISTS nba_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str,
                   ddl: str) -> None:
    """Add a column when an older database predates it."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create every NBA table and index. Safe to run repeatedly."""
    conn.executescript(SCHEMA_SQL)
    # Columns added after the first release of the NBA tables.
    _ensure_column(conn, "nba_published", "market_total", "REAL")
    _ensure_column(conn, "nba_published", "market_home_total", "REAL")
    _ensure_column(conn, "nba_published", "market_away_total", "REAL")
    conn.commit()


def main(db_path: Path = DB_PATH) -> int:
    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'nba_%' ORDER BY name"
            )
        ]
    finally:
        conn.close()
    print(f"NBA schema ready in {db_path}")
    for name in tables:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create the NBA tables.")
    parser.add_argument("--db", default=str(DB_PATH), help="database path")
    args = parser.parse_args()
    raise SystemExit(main(Path(args.db)))
