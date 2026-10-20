"""Round-trip the database to plain CSV files, and back again.

Why this exists
---------------
Everything the admin types by hand - published fixtures, hand-entered stats,
hand-added matches - lives only inside data/sports_analyzer.db. A SQLite file
is a perfectly good place for it right up until it is deleted, replaced, or
rebuilt from the raw CSVs, at which point the typing is gone.

This script writes the database out as plain CSV, in a layout that mirrors
data/raw/, and can read it back. So "rebuild the database from source" stops
meaning "lose the admin's work".

What it writes
--------------
    data/export/matches/<league>/<season>.csv   one file per league+season
    data/export/manual_stats.csv                every hand-entered stat
    data/export/published_matches.csv           the dashboard list
    data/export/manifest.json                   when it ran and what was in it

The files are a faithful copy: every column, every value, `id` included.
Nothing is reformatted for football-data.co.uk - that is `import_data.py`'s
job, and keeping the two formats apart means neither can corrupt the other.

How restore stays consistent
----------------------------
`manual_stats` rows are keyed by `match_id`, so ids have to survive. Restoring
a match therefore reuses its id when it is free and otherwise remaps it,
remembering the mapping so the stats that point at it are rewritten to match.
Without that, a restore would silently orphan every hand-entered stat - the
exact failure this whole exercise is meant to prevent.

Usage
-----
    python scripts/export_data.py             # database -> data/export/
    python scripts/export_data.py --verify    # compare the two (changes nothing)
    python scripts/export_data.py --restore   # data/export/ -> database
    python scripts/export_data.py --out some/dir
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "sports_analyzer.db"
EXPORT_DIR = BASE_DIR / "data" / "export"

if str(BASE_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(BASE_DIR / "scripts"))

from import_data import LEAGUE_NAMES  # noqa: E402  (keeps folder names in step)

# League display name -> folder name on disk, so exports land beside the raw
# CSVs they came from instead of inventing a second naming scheme.
LEAGUE_SLUGS = {name: folder for folder, name in LEAGUE_NAMES.items()}

# Tables dumped in full, with the natural key used to match a row on restore.
SIMPLE_TABLES = {
    "manual_stats": "match_id",
    "published_matches": "id",
    # The admin's other work. Leaving these out would make `--restore` silently
    # discard typed averages and every provider->team mapping learned so far.
    "team_profiles": "id",
    "team_aliases": "id",
    "fixture_settings": "league",
    # The NBA. Its history is the evidence the NBA engine counts, so a backup
    # without these rows would restore a working app with an empty model.
    "nba_teams": "team_id",
    "nba_games": "game_id",
    "nba_team_games": "id",
    "nba_fixtures": "game_id",
    "nba_published": "game_id",
    "nba_settings": "key",
    # Rosters and availability. A backup without these restores a model that
    # can price a game but cannot say who is playing in it.
    "nba_players": "player_id",
    "nba_rosters": "season+team_id+player_id",
    "nba_player_stats": "season+season_type+player_id+team_id",
    "nba_injuries": "captured_at+player_name",
    "nba_sync_log": "id",
    # The football season calendar. Browsable, never published from.
    "season_fixtures": "provider_id",
    # The board. `prediction_stats` is the measured error behind every
    # percentage; `board_markets` is the complete home/away/combined snapshot
    # the cards read; `board_picks` is the qualifying frozen record. A backup
    # without them would restore a board with no memory and no cards.
    "prediction_stats": "market",
    "board_picks": "id",
    "board_markets": "id",
}

# Tables restored by natural key with a plain upsert. manual_stats and
# published_matches need bespoke handling (match-id remapping, case-insensitive
# duplicate detection), so they are not in this map.
RESTORE_KEYS = {
    "team_profiles": ("league", "season", "team"),
    "team_aliases": ("provider", "provider_name"),
    "fixture_settings": ("league",),
    # NBA ids are reassigned by SQLite on restore, so the natural key is the
    # provider's game id plus the team id.
    "nba_teams": ("team_id",),
    "nba_games": ("game_id",),
    "nba_team_games": ("game_id", "team_id"),
    "nba_fixtures": ("game_id",),
    "nba_published": ("game_id",),
    "nba_settings": ("key",),
    "nba_players": ("player_id",),
    "nba_rosters": ("season", "team_id", "player_id"),
    "nba_player_stats": ("season", "season_type", "player_id", "team_id"),
    "nba_injuries": ("captured_at", "player_name"),
    "nba_sync_log": ("id",),
    # The provider id is stable across restores; `match_id` is rebuilt below
    # because it points at a `matches` row whose id may have been remapped.
    "season_fixtures": ("provider", "provider_id"),
    # Measured error is keyed by sport+market; a pick by the tuple the board
    # itself treats as unique, so a restore can never duplicate a snapshot.
    "prediction_stats": ("sport", "market"),
    "board_picks": ("date", "sport", "game_key", "market"),
    "board_markets": ("date", "sport", "game_key", "market"),
}

MATCH_KEY = ("league", "season", "date", "home_team", "away_team")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def league_slug(league: str) -> str:
    """Folder name for a league, reusing import_data's names where possible."""
    if league in LEAGUE_SLUGS:
        return LEAGUE_SLUGS[league]
    return "".join(c if c.isalnum() else "-" for c in league.lower()).strip("-")


def table_info(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    """[(column, declared type)] in table order. Drives every read and write."""
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return [(row[1], (row[2] or "TEXT").upper()) for row in cur.fetchall()]


def coerce(value, decl_type: str):
    """Turn a CSV cell (always a string, or None) back into a Python value.

    The declared column type decides, so nothing depends on guessing from the
    text. Empty cells are NULL for every type we store.
    """
    if value is None:
        return None
    text = value.strip() if isinstance(value, str) else value
    if text == "":
        return None
    if "INT" in decl_type:
        try:
            return int(float(text))
        except (TypeError, ValueError):
            return None
    if any(t in decl_type for t in ("REAL", "FLOA", "DOUB", "NUMERIC")):
        try:
            return float(text)
        except (TypeError, ValueError):
            return None
    return text


def write_csv(path: Path, header: list[str], rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" is required by the csv module; utf-8-sig so Excel opens it
    # with the right accent handling, same as the raw files we read.
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def scalar(conn: sqlite3.Connection, sql: str, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------
def export_all(db_path: Path = DB_PATH, out_dir: Path = EXPORT_DIR) -> dict:
    """Write the whole database out as CSV. Returns a small summary."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        match_cols = [name for name, _ in table_info(conn, "matches")]
        cur = conn.cursor()

        # --- one file per league + season --------------------------------
        cur.execute("SELECT DISTINCT league, season FROM matches ORDER BY league, season")
        pairs = cur.fetchall()
        files_written = 0
        matches_written = 0
        for pair in pairs:
            rows = cur.execute(
                f"SELECT {', '.join(match_cols)} FROM matches "
                f"WHERE league = ? AND season = ? ORDER BY date, id",
                (pair["league"], pair["season"])).fetchall()
            target = out_dir / "matches" / league_slug(pair["league"]) / f"{pair['season']}.csv"
            write_csv(target, match_cols, [tuple(r) for r in rows])
            files_written += 1
            matches_written += len(rows)

        # --- the admin's own tables --------------------------------------
        table_rows = {}
        for table in SIMPLE_TABLES:
            cols = [name for name, _ in table_info(conn, table)]
            rows = cur.execute(
                f"SELECT {', '.join(cols)} FROM {table} ORDER BY 1").fetchall()
            write_csv(out_dir / f"{table}.csv", cols, [tuple(r) for r in rows])
            table_rows[table] = len(rows)
            files_written += 1

        manifest = {
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "database": db_path.name,
            "matches": matches_written,
            "leagues": sorted({p["league"] for p in pairs}),
            "tables": table_rows,
        }
        (out_dir / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")

        summary = {"files": files_written, "matches": matches_written,
                   "tables": table_rows, "out_dir": str(out_dir)}
        return summary
    finally:
        conn.close()


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------
def verify(db_path: Path = DB_PATH, in_dir: Path = EXPORT_DIR) -> dict:
    """Compare the database with what is on disk. Writes nothing."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        db_matches = scalar(conn, "SELECT COUNT(*) FROM matches")
        file_matches = 0
        mismatched_files = []
        for path in sorted((in_dir / "matches").rglob("*.csv")):
            rows = read_csv(path)
            season = path.stem
            league = next((r.get("league") for r in rows if r.get("league")), None)
            db_n = 0
            if league:
                db_n = scalar(conn, "SELECT COUNT(*) FROM matches WHERE league = ?"
                                    " AND season = ?", (league, season))
            file_matches += len(rows)
            if db_n != len(rows):
                mismatched_files.append(f"{path.name}: file {len(rows)} vs db {db_n}")

        result = {
            "db_matches": db_matches,
            "file_matches": file_matches,
            "files_checked": len(list((in_dir / "matches").rglob("*.csv"))),
            "mismatched_files": mismatched_files,
        }

        for table in SIMPLE_TABLES:
            path = in_dir / f"{table}.csv"
            db_n = scalar(conn, f"SELECT COUNT(*) FROM {table}")
            file_n = len(read_csv(path)) if path.exists() else None
            result[table] = {"db": db_n, "file": file_n,
                             "match": file_n == db_n}
        # The number that actually matters: hand-entered values that would be
        # lost, and whether the copy holds the same count of them.
        stat_cols = [c for c, _ in table_info(conn, "manual_stats") if c != "match_id"]
        filled_db = 0
        for col in stat_cols:
            filled_db += scalar(conn, f"SELECT COUNT(*) FROM manual_stats "
                                      f"WHERE {col} IS NOT NULL") or 0
        filled_file = 0
        stats_path = in_dir / "manual_stats.csv"
        if stats_path.exists():
            for row in read_csv(stats_path):
                filled_file += sum(1 for c in stat_cols if (row.get(c) or "").strip() != "")
        result["manual_values"] = {"db": filled_db, "file": filled_file,
                                   "match": filled_db == filled_file}
        result["ok"] = (db_matches == file_matches
                        and not mismatched_files
                        and result["manual_stats"]["match"]
                        and all(result[table]["match"] for table in SIMPLE_TABLES))
        return result
    finally:
        conn.close()


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------
def restore_all(db_path: Path = DB_PATH, in_dir: Path = EXPORT_DIR) -> dict:
    """Read data/export/ back into the database, matching on natural keys.

    Existing rows are updated, new rows inserted. Match ids are preserved when
    possible and remapped when not, so `manual_stats` keeps pointing at the
    right match either way.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        match_cols = [name for name, _ in table_info(conn, "matches")]
        types = dict(table_info(conn, "matches"))

        inserted = updated = 0
        id_map: dict[int, int] = {}

        for path in sorted((in_dir / "matches").rglob("*.csv")):
            rows = read_csv(path)
            for raw in rows:
                row = {c: coerce(raw.get(c), types.get(c, "TEXT")) for c in match_cols}
                old_id = row.pop("id", None)
                key = tuple(row[k] for k in MATCH_KEY)
                existing_id = scalar(
                    conn,
                    "SELECT id FROM matches WHERE league = ? AND season = ? AND date = ?"
                    " AND home_team = ? AND away_team = ?", key)

                if existing_id:
                    sets = ", ".join(f"{c} = ?" for c in row)
                    cur.execute(f"UPDATE matches SET {sets} WHERE id = ?",
                                list(row.values()) + [existing_id])
                    if old_id is not None:
                        id_map[int(old_id)] = int(existing_id)
                    updated += 1
                else:
                    new_id = None
                    if old_id is not None and not scalar(
                            conn, "SELECT 1 FROM matches WHERE id = ?", (int(old_id),)):
                        new_id = int(old_id)
                    cols = list(row.keys())
                    placeholders = ", ".join("?" for _ in cols)
                    if new_id is not None:
                        cur.execute(
                            f"INSERT INTO matches (id, {', '.join(cols)}) "
                            f"VALUES (?, {placeholders})", [new_id] + list(row.values()))
                    else:
                        cur.execute(
                            f"INSERT INTO matches ({', '.join(cols)}) "
                            f"VALUES ({placeholders})", list(row.values()))
                        new_id = cur.lastrowid
                    if old_id is not None:
                        id_map[int(old_id)] = int(new_id)
                    inserted += 1

        # --- manual stats, with match_id rewritten through the map --------
        stats_types = dict(table_info(conn, "manual_stats"))
        stats_cols = [c for c, _ in table_info(conn, "manual_stats")]
        stats_path = in_dir / "manual_stats.csv"
        stats_restored = stats_skipped = 0
        if stats_path.exists():
            for raw in read_csv(stats_path):
                row = {c: coerce(raw.get(c), stats_types.get(c, "TEXT")) for c in stats_cols}
                old_id = row.get("match_id")
                if old_id is None:
                    continue
                match_id = id_map.get(int(old_id), int(old_id))
                if not scalar(conn, "SELECT 1 FROM matches WHERE id = ?", (match_id,)):
                    stats_skipped += 1
                    continue
                row["match_id"] = match_id
                cols = list(row.keys())
                updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "match_id")
                cur.execute(
                    f"INSERT INTO manual_stats ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' for _ in cols)}) "
                    f"ON CONFLICT(match_id) DO UPDATE SET {updates}",
                    list(row.values()))
                stats_restored += 1

        # --- published matches -------------------------------------------
        pub_types = dict(table_info(conn, "published_matches"))
        pub_cols = [c for c, _ in table_info(conn, "published_matches")]
        pub_path = in_dir / "published_matches.csv"
        pub_restored = 0
        if pub_path.exists():
            for raw in read_csv(pub_path):
                row = {c: coerce(raw.get(c), pub_types.get(c, "TEXT")) for c in pub_cols}
                pub_id = row.pop("id", None)
                existing_id = scalar(
                    conn,
                    "SELECT id FROM published_matches WHERE date = ? AND"
                    " lower(home_team) = lower(?) AND lower(away_team) = lower(?)",
                    (row["date"], row["home_team"], row["away_team"]))
                if existing_id:
                    sets = ", ".join(f"{c} = ?" for c in row)
                    cur.execute(f"UPDATE published_matches SET {sets} WHERE id = ?",
                                list(row.values()) + [existing_id])
                else:
                    cols = list(row.keys())
                    cur.execute(
                        f"INSERT INTO published_matches ({', '.join(cols)}) "
                        f"VALUES ({', '.join('?' for _ in cols)})", list(row.values()))
                pub_restored += 1

        # --- the admin's other work, by natural key ----------------------
        # `id` is dropped: nothing references these rows by id, and keeping an
        # old id could collide with a different row in a rebuilt database.
        simple_restored: dict[str, int] = {}
        for table, key_cols in RESTORE_KEYS.items():
            path = in_dir / f"{table}.csv"
            if not path.exists():
                continue
            cols = [c for c, _ in table_info(conn, table)]
            if not cols:
                continue
            types = dict(table_info(conn, table))
            where = " AND ".join(f"{k} = ?" for k in key_cols)
            restored_here = 0
            for raw in read_csv(path):
                row = {c: coerce(raw.get(c), types.get(c, "TEXT")) for c in cols}
                row.pop("id", None)
                keys = [row.get(k) for k in key_cols]
                if scalar(conn, f"SELECT 1 FROM {table} WHERE {where}", keys):
                    updates = [c for c in row if c not in key_cols]
                    sets = ", ".join(f"{c} = ?" for c in updates)
                    cur.execute(f"UPDATE {table} SET {sets} WHERE {where}",
                                [row[c] for c in updates] + keys)
                else:
                    cur.execute(
                        f"INSERT INTO {table} ({', '.join(row)}) "
                        f"VALUES ({', '.join('?' for _ in row)})", list(row.values()))
                restored_here += 1
            simple_restored[table] = restored_here

        # nba_fixtures.published_id points at nba_published.id, and ids are
        # reassigned on restore, so rebuild the link instead of trusting it.
        try:
            cur.execute(
                "UPDATE nba_fixtures SET published_id = ("
                "  SELECT id FROM nba_published "
                "  WHERE nba_published.game_id = nba_fixtures.game_id)")
        except sqlite3.OperationalError:
            pass  # an older backup with no NBA tables

        # season_fixtures.match_id points at a `matches` row, and match ids can
        # be remapped on restore. Rebuild the link from the natural key instead
        # of trusting an id from the backup.
        try:
            cur.execute("UPDATE season_fixtures SET match_id = NULL")
            cur.execute(
                "UPDATE season_fixtures SET match_id = ("
                "  SELECT m.id FROM matches m"
                "  WHERE m.league = season_fixtures.league"
                "    AND m.date = season_fixtures.date"
                "    AND lower(m.home_team) = lower(season_fixtures.home_team)"
                "    AND lower(m.away_team) = lower(season_fixtures.away_team)"
                "  LIMIT 1)"
                " WHERE home_team IS NOT NULL AND away_team IS NOT NULL")
        except sqlite3.OperationalError:
            pass  # a backup taken before the calendar existed

        conn.commit()
        return {"matches_inserted": inserted, "matches_updated": updated,
                "ids_remapped": sum(1 for o, n in id_map.items() if o != n),
                "manual_stats_restored": stats_restored,
                "manual_stats_skipped": stats_skipped,
                "published_restored": pub_restored,
                **{f"{table}_restored": n for table, n in simple_restored.items()}}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Export/restore the database as CSV")
    parser.add_argument("--restore", action="store_true",
                        help="read the CSVs back into the database")
    parser.add_argument("--verify", action="store_true",
                        help="compare database and CSVs, change nothing")
    parser.add_argument("--out", default=str(EXPORT_DIR),
                        help="folder to write to / read from")
    parser.add_argument("--db", default=str(DB_PATH), help="database path")
    args = parser.parse_args()

    folder = Path(args.out)
    db_path = Path(args.db)

    if args.verify:
        report = verify(db_path, folder)
        print(f"Database matches : {report['db_matches']}")
        print(f"Files contain    : {report['file_matches']} "
              f"in {report['files_checked']} file(s)")
        for table in SIMPLE_TABLES:
            info = report[table]
            flag = "ok" if info["match"] else "MISMATCH"
            print(f"{table:18}: db {info['db']:>6}  file {info['file']}  {flag}")
        mv = report["manual_values"]
        print(f"Hand-entered values: db {mv['db']}, file {mv['file']} "
              f"({'ok' if mv['match'] else 'MISMATCH'})")
        for line in report["mismatched_files"]:
            print(f"  size mismatch: {line}")
        print("\nVERIFY OK" if report["ok"] else "\nVERIFY FAILED")
        return 0 if report["ok"] else 1

    if args.restore:
        if not (folder / "manifest.json").exists() and not list((folder / "matches").rglob("*.csv")):
            print(f"Nothing to restore: {folder} has no export in it.")
            return 1
        result = restore_all(db_path, folder)
        print("Restored.")
        for key, value in result.items():
            print(f"  {key:24} {value}")
        return 0

    summary = export_all(db_path, folder)
    print(f"Exported {summary['matches']} matches to {summary['files']} file(s)")
    for table, count in summary["tables"].items():
        print(f"  {table:18} {count:>6}")
    print(f"  folder             {summary['out_dir']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
