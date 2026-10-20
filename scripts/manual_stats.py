"""Enter the stats the free CSVs do not contain.

football-data.co.uk gives us goals, shots, shots on target, fouls, corners and
cards. It does NOT give tackles, throw-ins, aerial duels, offsides,
interceptions, goal kicks, saves or substitutions. Those are entered here by
hand from a livescore app, and they live in their own `manual_stats` table.

WHY A SEPARATE TABLE
--------------------
The weekly workflow re-imports a season CSV with DELETE + INSERT. If manual
values were stored in the `matches` table they would be wiped every week. By
keeping them in `manual_stats` keyed by match_id, re-importing a CSV cannot
destroy hand-entered work, and the engine overlays them at read time.

Usage
-----
    # by teams and date
    python scripts/manual_stats.py --date 2025-08-15 \
        --home Liverpool --away Bournemouth \
        --home-tackles 17 --away-tackles 21 --home-throw-ins 22

    # by match id (faster once you know it)
    python scripts/manual_stats.py --match-id 1234 --home-saves 5 --away-saves 3

    # see what is still missing
    python scripts/manual_stats.py --list
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "sports_analyzer.db"

# Manual stat names -> (home column, away column) in manual_stats.
MANUAL_FIELDS = {
    "tackles": ("home_tackles", "away_tackles"),
    "throw-ins": ("home_throw_ins", "away_throw_ins"),
    "throw_ins": ("home_throw_ins", "away_throw_ins"),
    "aerial-duels": ("home_aerial_duels", "away_aerial_duels"),
    "aerial_duels": ("home_aerial_duels", "away_aerial_duels"),
    "offsides": ("home_offsides", "away_offsides"),
    "interceptions": ("home_interceptions", "away_interceptions"),
    "goal-kicks": ("home_goal_kicks", "away_goal_kicks"),
    "goal_kicks": ("home_goal_kicks", "away_goal_kicks"),
    "saves": ("home_saves", "away_saves"),
    "substitutions": ("home_substitutions", "away_substitutions"),
}


def find_match_id(conn: sqlite3.Connection, date: str, home: str, away: str):
    """Look up a match by date + both team names.

    Returns None when nothing matches, which almost always means a spelling
    difference (the CSV data uses short names like 'Man United', 'Paris SG').
    The caller must report that rather than assume success.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT id, league, season FROM matches "
        "WHERE date = ? AND lower(home_team) = lower(?) AND lower(away_team) = lower(?)",
        (date, home, away),
    )
    return cur.fetchone()


def add_manual_stats(date: str, home_team: str, away_team: str, db_path=DB_PATH,
                     match_id: int | None = None, conn: sqlite3.Connection | None = None,
                     **stats):
    """Upsert manual stats for one match. Returns (match_id, columns_written)."""
    field_map = stats.pop("field_map", MANUAL_FIELDS)

    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(db_path)

    try:
        cur = conn.cursor()

        if match_id is None:
            found = find_match_id(conn, date, home_team, away_team)
            if found is None:
                raise LookupError(
                    f"No match found for {home_team} v {away_team} on {date}. "
                    f"Check the spellings (use: python scripts/cli.py find <text>)."
                )
            match_id = found[0]

        # Turn friendly keys ('tackles') plus side prefixes into real columns.
        # Both `tackles=5` (home) and `home_tackles=5` are accepted.
        columns: dict[str, float] = {}
        for key, value in stats.items():
            if value is None:
                continue
            if key.startswith("home_") or key.startswith("away_"):
                columns[key] = float(value)
                continue
            pair = field_map.get(key)
            if pair is None:
                raise KeyError(f"unknown manual stat '{key}'. Known: {sorted(set(field_map))}")
            columns[pair[0]] = float(value)

        if not columns:
            raise ValueError("No stats given. Nothing to write.")

        cur.execute(
            "INSERT INTO manual_stats (match_id) VALUES (?) "
            "ON CONFLICT(match_id) DO NOTHING",
            (match_id,),
        )

        sets = ", ".join(f"{col} = ?" for col in columns)
        values = list(columns.values()) + [match_id]
        cur.execute(
            f"UPDATE manual_stats SET {sets}, updated_at = datetime('now') WHERE match_id = ?",
            values,
        )
        conn.commit()
        return match_id, len(columns)
    finally:
        if own_conn:
            conn.close()


def list_missing(conn: sqlite3.Connection, limit: int = 30):
    """Completed matches that still have no manual stats."""
    cur = conn.cursor()
    cur.execute(
        "SELECT m.id, m.date, m.home_team, m.away_team, m.league "
        "FROM matches m LEFT JOIN manual_stats ms ON ms.match_id = m.id "
        "WHERE m.status = 'completed' AND ms.match_id IS NULL "
        "ORDER BY m.date DESC LIMIT ?",
        (limit,),
    )
    return cur.fetchall()


def main():
    parser = argparse.ArgumentParser(description="Enter manual match stats")
    parser.add_argument("--date", help="ISO date, e.g. 2025-08-15")
    parser.add_argument("--home")
    parser.add_argument("--away")
    parser.add_argument("--match-id", type=int)
    parser.add_argument("--list", action="store_true", help="list matches missing manual stats")
    parser.add_argument("--db", default=str(DB_PATH))
    args, unknown = parser.parse_known_args()

    conn = sqlite3.connect(Path(args.db))
    try:
        if args.list:
            rows = list_missing(conn)
            if not rows:
                print("Nothing missing (or no completed matches).")
            for mid, date, home, away, league in rows:
                print(f"  {mid:>6}  {date}  {league:16} {home} v {away}")
            return 0

        # Unknown args are the stats themselves: --home-tackles 17 -> home_tackles
        stats = {}
        index = 0
        while index < len(unknown):
            token = unknown[index]
            if not token.startswith("--"):
                index += 1
                continue
            name = token[2:].replace("-", "_")
            value = unknown[index + 1] if index + 1 < len(unknown) else None
            stats[name] = value
            index += 2

        if not stats:
            parser.print_help()
            print("\nNo stats given. Example:")
            print("  python scripts/manual_stats.py --date 2025-08-15 --home Liverpool "
                  "--away Bournemouth --home-tackles 17 --away-tackles 21")
            return 1

        if args.match_id is None and not (args.date and args.home and args.away):
            print("Provide --match-id, or --date with --home and --away.")
            return 1

        match_id, written = add_manual_stats(
            args.date, args.home, args.away, db_path=Path(args.db),
            match_id=args.match_id, conn=conn, **stats)
        print(f"Saved {written} value(s) to match {match_id}.")
        return 0
    except (LookupError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
