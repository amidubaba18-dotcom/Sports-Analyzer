"""Create or upgrade the whole database schema, safely and repeatedly.

Run this before anything else, on an empty folder or on a database that has
been in use for months. It brings `data/sports_analyzer.db` up to the current
shape and creates the file if it does not exist yet, so a from-scratch rebuild
is: create schema, import the raw CSVs, restore the exported admin work.

What it guarantees
------------------
    matches            scores nullable, `status`, `source`, unique fixture index
    manual_stats       exists (hand-entered stats, per match)
    published_matches  exists (what the dashboard may show)
    team_profiles      exists (hand-entered per-team averages, per season)

The three fixes it carries, in the order they were needed:
    * home_goals / away_goals were NOT NULL, so a fixture with no score yet
      could not be stored, and there was no way to tell scheduled from played.
    * `source` ('csv' | 'manual') so a CSV refresh knows which rows it is
      allowed to delete.
    * `manual_stats` / `published_matches` were never created by any script,
      so rebuilding from raw CSVs produced a database that could not hold the
      admin's own work.

How it stays safe
-----------------
    * Each step checks before it changes anything, so it is idempotent.
    * The one destructive step rebuilds `matches` in a new table, verifies the
      row count and an id/goal aggregate, and only then swaps the names.
    * It writes a .db.bak copy of the file before that step.

Usage:
    python scripts/migrate_schema.py
    python scripts/migrate_schema.py --db some/other.db
"""
from pathlib import Path
import argparse
import shutil
import sqlite3
import sys

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "sports_analyzer.db"


# The full column list we want after migration, in order.
NEW_COLUMNS = [
    ("id", "INTEGER PRIMARY KEY"),
    ("date", "TEXT NOT NULL"),
    ("home_team", "TEXT NOT NULL"),
    ("away_team", "TEXT NOT NULL"),
    ("league", "TEXT NOT NULL"),
    ("season", "TEXT NOT NULL"),
    ("status", "TEXT NOT NULL DEFAULT 'completed'"),
    ("source", "TEXT NOT NULL DEFAULT 'csv'"),
    ("referee", "TEXT"),
    ("home_goals", "INTEGER"),
    ("away_goals", "INTEGER"),
    ("home_ht_goals", "INTEGER"),
    ("away_ht_goals", "INTEGER"),
    ("ht_result", "TEXT"),
    ("home_shots", "INTEGER"),
    ("away_shots", "INTEGER"),
    ("home_shots_on_target", "INTEGER"),
    ("away_shots_on_target", "INTEGER"),
    ("home_fouls", "INTEGER"),
    ("away_fouls", "INTEGER"),
    ("home_corners", "INTEGER"),
    ("away_corners", "INTEGER"),
    ("home_yellow_cards", "INTEGER"),
    ("away_yellow_cards", "INTEGER"),
    ("home_red_cards", "INTEGER"),
    ("away_red_cards", "INTEGER"),
    ("home_tackles", "INTEGER"),
    ("away_tackles", "INTEGER"),
    ("home_throw_ins", "INTEGER"),
    ("away_throw_ins", "INTEGER"),
    ("home_aerial_duels", "INTEGER"),
    ("away_aerial_duels", "INTEGER"),
    ("home_offsides", "INTEGER"),
    ("away_offsides", "INTEGER"),
    ("home_interceptions", "INTEGER"),
    ("away_interceptions", "INTEGER"),
    ("home_goal_kicks", "INTEGER"),
    ("away_goal_kicks", "INTEGER"),
    ("home_saves", "INTEGER"),
    ("away_saves", "INTEGER"),
    ("home_substitutions", "INTEGER"),
    ("away_substitutions", "INTEGER"),
    ("odds_home_win", "REAL"),
    ("odds_draw", "REAL"),
    ("odds_away_win", "REAL"),
    ("odds_over_2_5", "REAL"),
    ("odds_under_2_5", "REAL"),
    ("handicap_line", "REAL"),
    ("odds_handicap_home", "REAL"),
    ("odds_handicap_away", "REAL"),
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_matches_date ON matches(date)",
    "CREATE INDEX IF NOT EXISTS idx_matches_home ON matches(home_team)",
    "CREATE INDEX IF NOT EXISTS idx_matches_away ON matches(away_team)",
    "CREATE INDEX IF NOT EXISTS idx_matches_home_date ON matches(home_team, date)",
    "CREATE INDEX IF NOT EXISTS idx_matches_away_date ON matches(away_team, date)",
    "CREATE INDEX IF NOT EXISTS idx_matches_league_season ON matches(league, season)",
]

# The two side tables. They hold the admin's own work - published fixtures and
# hand-entered stats - so a rebuilt database needs them just as much as
# `matches` does, otherwise a restore has nowhere to put anything.
SIDE_TABLES = {
    "manual_stats": """
        CREATE TABLE IF NOT EXISTS manual_stats (
          match_id INTEGER PRIMARY KEY,
          home_tackles REAL, away_tackles REAL,
          home_throw_ins REAL, away_throw_ins REAL,
          home_aerial_duels REAL, away_aerial_duels REAL,
          home_offsides REAL, away_offsides REAL,
          home_interceptions REAL, away_interceptions REAL,
          home_goal_kicks REAL, away_goal_kicks REAL,
          home_saves REAL, away_saves REAL,
          home_substitutions REAL, away_substitutions REAL,
          updated_at TEXT, updated_by TEXT,
          FOREIGN KEY(match_id) REFERENCES matches(id)
        )""",
    "published_matches": """
        CREATE TABLE IF NOT EXISTS published_matches (
          id INTEGER PRIMARY KEY,
          league TEXT,
          season TEXT,
          date TEXT,
          kickoff TEXT,
          home_team TEXT,
          away_team TEXT,
          markets TEXT,
          status TEXT DEFAULT 'published',
          sort_order INTEGER DEFAULT 0,
          created_at TEXT DEFAULT (datetime('now')),
          updated_at TEXT DEFAULT (datetime('now'))
        )""",
    "team_profiles": """
        CREATE TABLE IF NOT EXISTS team_profiles (
          id INTEGER PRIMARY KEY,
          league TEXT NOT NULL,
          season TEXT NOT NULL,
          team TEXT NOT NULL,
          as_of TEXT NOT NULL,
          tackles REAL, throw_ins REAL, aerial_duels REAL, offsides REAL,
          interceptions REAL, goal_kicks REAL, saves REAL, substitutions REAL,
          note TEXT,
          updated_at TEXT,
          updated_by TEXT
        )""",
    # A fixture the provider says exists. This is NOT a published match and NOT a
    # historical match: it is a candidate. The admin (or the auto-publish gate)
    # decides whether it becomes visible, and the CSV import decides whether it
    # becomes evidence.
    "available_fixtures": """
        CREATE TABLE IF NOT EXISTS available_fixtures (
          id INTEGER PRIMARY KEY,
          provider TEXT NOT NULL DEFAULT 'football-data.org',
          provider_id TEXT,
          competition TEXT,
          league TEXT NOT NULL,
          season TEXT NOT NULL,
          date TEXT NOT NULL,
          kickoff TEXT,
          home_provider_name TEXT NOT NULL,
          away_provider_name TEXT NOT NULL,
          home_provider_short TEXT,
          away_provider_short TEXT,
          home_team TEXT,
          away_team TEXT,
          status TEXT NOT NULL DEFAULT 'scheduled',
          mapping_state TEXT NOT NULL DEFAULT 'needs_attention',
          mapping_note TEXT,
          published_id INTEGER,
          first_seen TEXT DEFAULT (datetime('now')),
          last_seen TEXT DEFAULT (datetime('now')),
          raw TEXT,
          UNIQUE(provider, provider_id)
        )""",
    # The provider's full-season calendar, played and scheduled alike. This is
    # a CALENDAR, not a candidate: it exists so the season can be browsed end to
    # end. It is never published from. Publishing still flows through
    # available_fixtures and the 14-day window, so a fixture nine months away
    # cannot reach the dashboard wearing today's form.
    "season_fixtures": """
        CREATE TABLE IF NOT EXISTS season_fixtures (
          id INTEGER PRIMARY KEY,
          provider TEXT NOT NULL DEFAULT 'football-data.org',
          provider_id TEXT NOT NULL,
          competition TEXT,
          league TEXT NOT NULL,
          season TEXT NOT NULL,
          matchday INTEGER,
          date TEXT NOT NULL,
          kickoff TEXT,
          home_provider_name TEXT NOT NULL,
          away_provider_name TEXT NOT NULL,
          home_team TEXT,
          away_team TEXT,
          status TEXT NOT NULL DEFAULT 'scheduled',
          home_goals INTEGER,
          away_goals INTEGER,
          match_id INTEGER,
          first_seen TEXT DEFAULT (datetime('now')),
          last_seen TEXT DEFAULT (datetime('now')),
          raw TEXT,
          UNIQUE(provider, provider_id)
        )""",
    # Provider name -> the name the database already uses. The whole point is
    # that "Manchester United FC" and "Man United" must never become two teams.
    "team_aliases": """
        CREATE TABLE IF NOT EXISTS team_aliases (
          id INTEGER PRIMARY KEY,
          provider TEXT NOT NULL DEFAULT 'football-data.org',
          provider_name TEXT NOT NULL,
          team TEXT NOT NULL,
          created_at TEXT DEFAULT (datetime('now')),
          UNIQUE(provider, provider_name)
        )""",
    # Per-league switch for the automatic pipeline. A missing row means "on",
    # so a newly added league does not silently stay off.
    "fixture_settings": """
        CREATE TABLE IF NOT EXISTS fixture_settings (
          league TEXT PRIMARY KEY,
          auto_publish INTEGER NOT NULL DEFAULT 1,
          updated_at TEXT
        )""",
    # Small key/value store: last fetch time, lookahead, provider choices.
    "app_settings": """
        CREATE TABLE IF NOT EXISTS app_settings (
          key TEXT PRIMARY KEY,
          value TEXT,
          updated_at TEXT
        )""",
}

# One profile per team per season. `as_of` is not part of the identity on
# purpose: the admin keeps ONE current picture of a team and moves its date
# forward, rather than accumulating a history nobody maintains.
SIDE_INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_team_profiles_identity "
    "ON team_profiles(league, season, team)",
    "CREATE INDEX IF NOT EXISTS idx_available_date "
    "ON available_fixtures(date)",
    "CREATE INDEX IF NOT EXISTS idx_available_state "
    "ON available_fixtures(mapping_state, date)",
    "CREATE INDEX IF NOT EXISTS idx_available_league_date "
    "ON available_fixtures(league, date)",
    "CREATE INDEX IF NOT EXISTS idx_available_published "
    "ON available_fixtures(published_id)",
    "CREATE INDEX IF NOT EXISTS idx_aliases_provider_name "
    "ON team_aliases(provider, provider_name)",
    "CREATE INDEX IF NOT EXISTS idx_season_fixtures_league "
    "ON season_fixtures(league, season, date)",
    "CREATE INDEX IF NOT EXISTS idx_season_fixtures_match "
    "ON season_fixtures(match_id)",
]


def column_info(conn, table):
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    # (cid, name, type, notnull, default, pk)
    return cur.fetchall()


def table_exists(conn, name):
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,))
    return cur.fetchone() is not None


def create_matches_table(conn):
    """Create `matches` from scratch, for a database that has none yet."""
    cur = conn.cursor()
    cols_sql = ", ".join(f"{name} {decl}" for name, decl in NEW_COLUMNS)
    cur.execute(f"CREATE TABLE matches ({cols_sql})")
    for stmt in INDEXES:
        cur.execute(stmt)
    conn.commit()
    print(f"Created empty `matches` table ({len(NEW_COLUMNS)} columns).")


def ensure_side_tables(conn):
    """Create `manual_stats` / `published_matches` / `team_profiles` if missing."""
    cur = conn.cursor()
    created = []
    for name, ddl in SIDE_TABLES.items():
        if not table_exists(conn, name):
            cur.execute(ddl)
            created.append(name)
    for stmt in SIDE_INDEXES:
        try:
            cur.execute(stmt)
        except sqlite3.IntegrityError:
            # Duplicate profiles already exist, so the unique index cannot be
            # built. Not fatal for a restore, but it must not pass silently.
            print(f"  WARNING: could not create index: {stmt.split()[5]}")
    conn.commit()
    print(f"Created missing table(s): {', '.join(created)}" if created
          else "Side tables already present.")


def already_migrated(conn):
    cols = {row[1]: row for row in column_info(conn, "matches")}
    if "status" not in cols:
        return False
    home_goals = cols.get("home_goals")
    if home_goals is None:
        return False
    # notnull flag is index 3
    return home_goals[3] == 0


def ensure_source_column(conn):
    """Add `source` ('csv' | 'manual') so a re-import knows what it may delete.

    Why this exists
    ---------------
    Refreshing a season CSV is the normal way to bring in newly played
    matches. `import_season` removes rows the CSV no longer lists, because a
    postponed fixture really can move to a different date. But it cannot tell
    a row that vanished from the CSV from a row a human typed in, so it would
    delete hand-entered matches along with them.

    `source` removes the ambiguity. Every row already in the table came from a
    CSV, so the column defaults to 'csv'. Rows created through the admin UI
    are recorded as 'manual' and are never swept.

    Idempotent: does nothing if the column is already there.
    """
    cols = {row[1] for row in column_info(conn, "matches")}
    if "source" in cols:
        print("`source` column already present.")
        return
    conn.execute("ALTER TABLE matches ADD COLUMN source TEXT NOT NULL DEFAULT 'csv'")
    conn.commit()
    cur = conn.cursor()
    cur.execute("SELECT source, COUNT(*) FROM matches GROUP BY source")
    print(f"Added `source` column. Breakdown: {cur.fetchall()}")


def ensure_unique_index(conn):
    """Create the unique identity index, but only if the data allows it.

    A match is identified by league + season + date + both teams. Enforcing
    that in the database means a duplicate cannot be introduced even by a bug
    in a script or a double-clicked button.

    If duplicates already exist we do NOT create the index (SQLite would raise)
    and instead print exactly which fixtures clash, so they can be merged
    deliberately rather than automatically.
    """
    cur = conn.cursor()

    cur.execute("""
        SELECT league, season, date, home_team, away_team, COUNT(*) AS n
        FROM matches
        GROUP BY league, season, date, home_team, away_team
        HAVING n > 1
        ORDER BY n DESC
    """)
    duplicates = cur.fetchall()

    if duplicates:
        print(f"\nCannot create unique index: {len(duplicates)} duplicate fixture(s) exist.")
        for league, season, date, home, away, n in duplicates[:20]:
            print(f"  {n}x  {date}  {league} {season}  {home} v {away}")
        if len(duplicates) > 20:
            print(f"  ... and {len(duplicates) - 20} more")
        print("Resolve these first, then re-run this script.")
        return False

    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_matches_identity "
        "ON matches(league, season, date, home_team, away_team)")
    conn.commit()
    print("Unique identity index ensured on (league, season, date, home_team, away_team).")
    return True


def main(db_path: Path = DB_PATH) -> int:
    """Bring `db_path` up to the current schema. Creates it if it is missing.

    Safe to run any number of times: each step checks before it changes
    anything, and the one destructive step (rebuilding `matches` for nullable
    scores) verifies the row count and a checksum before it drops anything.
    """
    db_path = Path(db_path)
    existed = db_path.exists()

    conn = sqlite3.connect(db_path)
    try:
        if not table_exists(conn, "matches"):
            create_matches_table(conn)
        elif already_migrated(conn):
            print("Already migrated - nothing to do.")
        else:
            _rebuild_matches_table(conn, db_path)
        ensure_source_column(conn)
        ensure_side_tables(conn)
        ensure_unique_index(conn)

        cur = conn.cursor()
        for name in ("matches", "manual_stats", "published_matches", "team_profiles",
                     "available_fixtures", "season_fixtures", "team_aliases",
                     "fixture_settings"):
            cur.execute(f"SELECT COUNT(*) FROM {name}")
            print(f"  {name:18} {cur.fetchone()[0]:>6} row(s)")
    finally:
        conn.close()

    print(f"\n{'Created' if not existed else 'Updated'} {db_path}")
    return 0


def _rebuild_matches_table(conn, db_path: Path = DB_PATH):
    """Rebuild `matches` with nullable scores and a `status` column.

    Uses the connection it is given (main() owns the lifetime) and verifies
    row count plus an id/goal aggregate before dropping the original table.
    """
    existing = {row[1] for row in column_info(conn, "matches")}
    copy_cols = [name for name, _ in NEW_COLUMNS if name in existing]
    print(f"Migrating. Existing columns: {len(existing)}. Will copy: {len(copy_cols)}")

    # For rows that predate the status column, derive it from whether a score exists.
    has_status = "status" in existing
    if has_status:
        select_status = ("COALESCE(status, CASE WHEN home_goals IS NULL "
                         "THEN 'scheduled' ELSE 'completed' END)")
    else:
        select_status = ("CASE WHEN home_goals IS NULL THEN 'scheduled' "
                         "ELSE 'completed' END")

    select_exprs = []
    for name in copy_cols:
        if name == "status":
            select_exprs.append(f"{select_status} AS status")
        else:
            select_exprs.append(name)

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM matches")
    before_count = cur.fetchone()[0]
    cur.execute("SELECT MIN(id), MAX(id), SUM(COALESCE(home_goals,-1)) FROM matches")
    before_agg = cur.fetchone()

    # Back up the file before touching anything.
    backup = Path(db_path).with_suffix(".db.bak")
    shutil.copy2(db_path, backup)
    print(f"Backup written: {backup}")

    try:
        cur.execute("BEGIN")
        cols_sql = ", ".join(f"{n} {t}" for n, t in NEW_COLUMNS)
        cur.execute(f"CREATE TABLE matches_new ({cols_sql})")

        col_list = ", ".join(copy_cols)
        cur.execute(f"INSERT INTO matches_new ({col_list}) "
                    f"SELECT {', '.join(select_exprs)} FROM matches")

        cur.execute("SELECT COUNT(*) FROM matches_new")
        after_count = cur.fetchone()[0]
        cur.execute("SELECT MIN(id), MAX(id), SUM(COALESCE(home_goals,-1)) FROM matches_new")
        after_agg = cur.fetchone()

        if before_count != after_count:
            raise RuntimeError(f"row count changed: {before_count} -> {after_count}")
        if before_agg != after_agg:
            raise RuntimeError(f"id/goal aggregate changed: {before_agg} -> {after_agg}")

        cur.execute("DROP TABLE matches")
        cur.execute("ALTER TABLE matches_new RENAME TO matches")
        for stmt in INDEXES:
            cur.execute(stmt)
        conn.commit()
    except Exception:
        conn.rollback()
        print("MIGRATION FAILED - rolled back. Original table intact.")
        raise

    cur.execute("SELECT COUNT(*) FROM matches")
    final_count = cur.fetchone()[0]
    cols = {row[1]: row for row in column_info(conn, "matches")}
    cur.execute("SELECT status, COUNT(*) FROM matches GROUP BY status")
    statuses = cur.fetchall()

    print(f"\nRows: {before_count} -> {final_count}")
    print(f"home_goals notnull flag: {cols['home_goals'][3]} (0 = nullable, good)")
    print(f"status column present: {'status' in cols}")
    print(f"status breakdown: {statuses}")
    print("Migration OK." if final_count == before_count else "WARNING: counts differ")


if __name__ == "__main__":
    _parser = argparse.ArgumentParser(description="Create/upgrade the Sports Analyzer schema")
    _parser.add_argument("--db", default=str(DB_PATH), help="database path")
    _args = _parser.parse_args()
    sys.exit(main(Path(_args.db)))
