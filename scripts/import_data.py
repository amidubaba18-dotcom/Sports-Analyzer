"""Import football-data.co.uk season CSVs into the SQLite database.

The CSVs from football-data.co.uk use these columns:
    Div, Date, Time, HomeTeam, AwayTeam, FTHG, FTAG, FTR, HTHG, HTAG, HTR,
    Referee, HS, AS, HST, AST, HF, AF, HC, AC, HY, AY, HR, AR, ...
    AvgH, AvgD, AvgA (average 1X2 odds)
    Avg>2.5, Avg<2.5 (average over/under 2.5 odds)
    AHh, AvgAHH, AvgAHA (Asian handicap)

Column names are short and inconsistent, so we map them explicitly rather than
guessing.

Three behaviours that matter for the weekly workflow:

  * Import is an id-preserving upsert on the natural key
    (league, season, date, home_team, away_team). Re-importing a freshly
    downloaded file refreshes that season with the newest data and never
    duplicates rows: existing matches keep their id, genuinely new matches are
    inserted, and a match the file no longer lists is removed.

  * THE SEASON LABEL COMES FROM THE FILE NAME, so it is validated rather than
    trusted. `data/raw/premier-league/2026-27.csv` is season 2026-27; a file
    named `2026_27.csv` or `2026-2027.csv` is read as the same season. A file
    named `E0.csv` is not a season at all, and importing it would create a
    second season holding a duplicate of every match, so it is skipped with an
    explanation instead. See `scripts/seasons.py`.

  * Hand-entered stats live in the separate `manual_stats` table, which this
    script never touches, so replacing a season's CSV cannot destroy manual
    work. A match created by hand rather than downloaded (`source = 'manual'`)
    is never removed by the sweep either.

Usage
-----
    # import everything under data/raw/
    python scripts/import_data.py

    # one league/season
    python scripts/import_data.py --league premier-league --season 2025-26

    # add a league not in the default map
    python scripts/import_data.py --folder portugal --league-name "Primeira Liga" \
        --season 2025-26 --file data/raw/portugal/2025-26.csv
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

import pandas as pd

from seasons import normalize_season, season_from_filename

BASE_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = BASE_DIR / "data" / "raw"
DB_PATH = BASE_DIR / "data" / "sports_analyzer.db"

# Folder name on disk -> human league name used everywhere else.
LEAGUE_NAMES = {
    "premier-league": "Premier League",
    "la-liga": "La Liga",
    "bundesliga": "Bundesliga",
    "serie-a": "Serie A",
    "ligue-1": "Ligue 1",
    "eredivisie": "Eredivisie",
    "portugal": "Primeira Liga",
    "belgium": "Jupiler Pro League",
    "turkey": "Super Lig",
    "greece": "Super League",
    "austria": "Bundesliga (AT)",
    "denmark": "Superliga",
    "sweden": "Allsvenskan",
    "switzerland": "Super League (CH)",
    "norway": "Eliteserien",
}

# football-data.co.uk's own division code per folder. Read out of the real files,
# and used only to sanity-check a file dropped in the wrong folder: the league a
# match is filed under comes from the folder, never from this column, so a
# misplaced file would silently be stored as the wrong league. Warning only - a
# folder that is not listed here (a genuinely new league) still imports.
LEAGUE_DIV_CODES = {
    "premier-league": "E0",
    "bundesliga": "D1",
    "la-liga": "SP1",
    "serie-a": "I1",
    "ligue-1": "F1",
    "eredivisie": "N1",
}

# CSV column -> database column.
COLUMN_MAP = {
    "HomeTeam": "home_team",
    "AwayTeam": "away_team",
    "Referee": "referee",
    "FTHG": "home_goals",
    "FTAG": "away_goals",
    "HTHG": "home_ht_goals",
    "HTAG": "away_ht_goals",
    "HTR": "ht_result",
    "HS": "home_shots",
    "AS": "away_shots",
    "HST": "home_shots_on_target",
    "AST": "away_shots_on_target",
    "HF": "home_fouls",
    "AF": "away_fouls",
    "HC": "home_corners",
    "AC": "away_corners",
    "HY": "home_yellow_cards",
    "AY": "away_yellow_cards",
    "HR": "home_red_cards",
    "AR": "away_red_cards",
    "AvgH": "odds_home_win",
    "AvgD": "odds_draw",
    "AvgA": "odds_away_win",
    "Avg>2.5": "odds_over_2_5",
    "Avg<2.5": "odds_under_2_5",
    "AHh": "handicap_line",
    "AvgAHH": "odds_handicap_home",
    "AvgAHA": "odds_handicap_away",
}

REQUIRED = ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"]

INT_COLUMNS = {
    "home_goals", "away_goals", "home_ht_goals", "away_ht_goals",
    "home_shots", "away_shots", "home_shots_on_target", "away_shots_on_target",
    "home_fouls", "away_fouls", "home_corners", "away_corners",
    "home_yellow_cards", "away_yellow_cards", "home_red_cards", "away_red_cards",
}
FLOAT_COLUMNS = {
    "odds_home_win", "odds_draw", "odds_away_win", "odds_over_2_5",
    "odds_under_2_5", "handicap_line", "odds_handicap_home", "odds_handicap_away",
}


def to_iso_date(value) -> str | None:
    """football-data dates are DD/MM/YYYY; store ISO so text sorting works."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            from datetime import datetime
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def clean_number(value, as_int: bool):
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text == "" or text.lower() in ("na", "n/a", "nan", "-"):
            return None
        value = text
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN check without importing math
        return None
    return int(number) if as_int else round(number, 4)


def load_csv(csv_path: Path, league_name: str, season: str, log=print) -> pd.DataFrame:
    """Read a CSV and return rows already shaped like the `matches` table."""
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    raw = pd.read_csv(csv_path, encoding="utf-8-sig")
    raw.columns = [c.strip() for c in raw.columns]

    missing = [c for c in REQUIRED if c not in raw.columns]
    if missing:
        raise ValueError(
            f"{csv_path.name} is missing required column(s): {missing}. "
            f"Found: {list(raw.columns)[:12]}..."
        )

    # Optional columns are tolerated: only take the ones actually present.
    present = {csv_col: db_col for csv_col, db_col in COLUMN_MAP.items() if csv_col in raw.columns}

    rows = []
    skipped_no_date = 0
    for _, record in raw.iterrows():
        iso_date = to_iso_date(record.get("Date"))
        home = str(record.get("HomeTeam", "")).strip()
        away = str(record.get("AwayTeam", "")).strip()
        if not iso_date or not home or not away:
            skipped_no_date += 1
            continue

        row = {
            "date": iso_date,
            "home_team": home,
            "away_team": away,
            "league": league_name,
            "season": season,
            "status": "completed",
            # Where this row came from. Only 'csv' rows may be swept when the
            # CSV no longer lists them; 'manual' rows are the admin's work.
            "source": "csv",
        }
        for csv_col, db_col in present.items():
            raw_value = record.get(csv_col)
            if db_col in INT_COLUMNS:
                row[db_col] = clean_number(raw_value, as_int=True)
            elif db_col in FLOAT_COLUMNS:
                row[db_col] = clean_number(raw_value, as_int=False)
            else:
                text = raw_value
                if isinstance(text, float) and pd.isna(text):
                    text = None
                row[db_col] = (str(text).strip() if text is not None else None)

        # A row with no score at all is not a completed match.
        if row.get("home_goals") is None or row.get("away_goals") is None:
            row["status"] = "scheduled"

        rows.append(row)

    if skipped_no_date:
        log(f"      skipped {skipped_no_date} row(s) with no usable date/team")

    return pd.DataFrame(rows)


def _sanitize(value):
    """NaN -> None, so SQLite gets a real NULL instead of a NaN float."""
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    return value


def import_season(csv_path: Path, league_name: str, season: str,
                  db_path: Path = DB_PATH, conn: sqlite3.Connection | None = None,
                  log=print):
    """Replace/refresh one (league, season) from a CSV, PRESERVING row ids.

    Why not DELETE + INSERT
    -----------------------
    The earlier version deleted the season and appended it again. That works,
    but SQLite assigns brand-new AUTOINCREMENT ids, so every `manual_stats`
    row (which is keyed by match_id) ended up pointing at a match that no
    longer existed. Manual work wasn't deleted - it was orphaned, which is
    arguably worse because nothing looks broken.

    This version upserts on the natural key
        (league, season, date, home_team, away_team)
    which is the same identity the API uses for duplicate protection. A match
    that already exists keeps its id and its manual stats; a genuinely new
    fixture is inserted; a fixture the CSV no longer contains is removed,
    unless it was hand-entered (`source = 'manual'`).

    The season label is canonicalised first. It is half of that natural key, so
    a label in another shape ('2026-2027') would not match the existing rows and
    would insert a second copy of the whole season instead of refreshing it.
    """
    canonical = normalize_season(season, None)
    if canonical is None:
        raise ValueError(
            f"{csv_path.name}: '{season}' is not a season label I can read. "
            f"Name the file <season>.csv, e.g. 2026-27.csv. Refusing to import "
            f"it, because a label the database does not recognise would create a "
            f"duplicate season rather than refreshing the real one.")
    season = canonical

    frame = load_csv(csv_path, league_name, season, log=log)

    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        cur = conn.cursor()

        cur.execute(
            "SELECT id, date, home_team, away_team, source FROM matches "
            "WHERE league = ? AND season = ?", (league_name, season))
        existing: dict[tuple, int] = {}
        sources: dict[int, str] = {}
        for mid, date, home, away, src in cur.fetchall():
            existing[(date, home.strip().lower(), away.strip().lower())] = mid
            sources[mid] = src or "csv"
        before_count = len(existing)

        inserted = updated = 0
        touched: set[int] = set()

        for record in frame.to_dict(orient="records"):
            row = {k: _sanitize(v) for k, v in record.items() if k != "id"}
            key = (row["date"],
                   str(row["home_team"]).strip().lower(),
                   str(row["away_team"]).strip().lower())

            if key in existing:
                mid = existing[key]
                # `source` is deliberately left alone on update: a row a human
                # created stays 'manual' even once the provider's CSV carries
                # it, so a later CSV that drops it cannot silently delete it.
                update_cols = [c for c in row if c != "source"]
                sets = ", ".join(f"{col} = ?" for col in update_cols)
                cur.execute(f"UPDATE matches SET {sets} WHERE id = ?",
                            [row[c] for c in update_cols] + [mid])
                touched.add(mid)
                updated += 1
            else:
                cols = list(row.keys())
                marks = ", ".join("?" for _ in cols)
                cur.execute(
                    f"INSERT INTO matches ({', '.join(cols)}) VALUES ({marks})",
                    list(row.values()))
                touched.add(cur.lastrowid)
                inserted += 1

        # Rows the CSV no longer lists (e.g. a postponed game moved to another
        # date). Only CSV-sourced rows may go: deleting a hand-entered match
        # would throw away work the provider cannot reproduce. Deleting any row
        # would also orphan its manual stats, so both are reported.
        stale = [mid for mid in existing.values() if mid not in touched]
        removed = protected = orphaned = 0
        for mid in stale:
            if sources.get(mid) == "manual":
                protected += 1
                continue
            cur.execute("SELECT COUNT(*) FROM manual_stats WHERE match_id = ?", (mid,))
            if cur.fetchone()[0]:
                orphaned += 1
            cur.execute("DELETE FROM matches WHERE id = ?", (mid,))
            removed += 1

        conn.commit()

        cur.execute("SELECT COUNT(*) FROM matches WHERE league = ? AND season = ?",
                    (league_name, season))
        final_count = cur.fetchone()[0]

        log(f"  {league_name} {season}: {final_count} rows "
            f"({updated} updated, {inserted} inserted, {removed} removed)"
            f"  [was {before_count}]  from {csv_path.name}")
        if protected:
            log(f"    kept {protected} hand-entered match(es) the CSV does not list")
        if orphaned:
            log(f"    WARNING: {orphaned} removed match(es) had manual stats, "
                f"which are now orphaned. Check for re-scheduled fixtures.")

        # Returning the counts (not just the final size) lets the admin Data
        # view show what a refresh actually changed, rather than a line of text.
        return {"league": league_name, "season": season, "file": csv_path.name,
                "rows": final_count, "before": before_count, "updated": updated,
                "inserted": inserted, "removed": removed,
                "kept_manual": protected, "orphaned": orphaned}
    finally:
        if own_conn:
            conn.close()


def import_tree(root: Path = RAW_DIR, db_path: Path = DB_PATH, log=print,
                warn=None) -> dict:
    """Import every usable CSV under `root`, one connection, one summary.

    This is what the admin "Re-import CSVs" button calls. It is the same code
    path as the command line, so the button and the terminal cannot drift into
    two different behaviours.
    """
    if warn is None:
        warn = log

    results = []
    conn = sqlite3.connect(db_path)
    try:
        for csv_path, league_name, season in discover(root, warn=warn):
            results.append(import_season(csv_path, league_name, season,
                                         db_path, conn, log=log))
    finally:
        conn.close()

    return {"files": results,
            "inserted": sum(r["inserted"] for r in results),
            "updated": sum(r["updated"] for r in results),
            "removed": sum(r["removed"] for r in results),
            "kept_manual": sum(r["kept_manual"] for r in results),
            "orphaned": sum(r["orphaned"] for r in results)}



def _first_div(csv_path: Path) -> str | None:
    """The provider's own division code from the file's first data row.

    Read with the plain `csv` module rather than pandas: this runs before the
    expensive load, purely to sanity-check which league the file claims to be.
    """
    try:
        with csv_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            for row in csv.DictReader(fh):
                code = (row.get("Div") or "").strip()
                if code:
                    return code
    except (OSError, csv.Error):
        return None
    return None


def discover(root: Path, warn=None):
    """Yield (csv_path, league_name, season) for every usable CSV under raw/.

    The file name is the season label, so it is validated here rather than in
    the import loop: a file called `E0.csv` claims a season called 'E0', which
    the database has no rows for and would happily create 380 under. Files whose
    name is not a season are skipped and reported through `warn`.

    The league comes from the FOLDER, never from the file's own `Div` column, so
    a Bundesliga file dropped into `premier-league/` would be stored as Premier
    League matches - splitting both leagues' history in half. That is checked
    here and warned about. A warning, not an error: a genuinely new league folder
    is legitimate and still imports.
    """
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        known = folder.name in LEAGUE_NAMES
        league_name = LEAGUE_NAMES.get(folder.name, folder.name.replace("-", " ").title())
        if not known and warn:
            warn(f"folder '{folder.name}' is not in LEAGUE_NAMES, so its matches "
                 f"will be imported as league '{league_name}'. If that league "
                 f"already exists under another name, rename the folder.")
        expected_div = LEAGUE_DIV_CODES.get(folder.name)
        for csv_file in sorted(folder.glob("*.csv")):
            season = season_from_filename(csv_file.stem)
            if season is None:
                if warn:
                    warn(f"skipped {csv_file.relative_to(root)}: '{csv_file.stem}' is "
                         f"not a season label. Rename it <season>.csv (e.g. 2026-27.csv) "
                         f"and run again.")
                continue
            if season != csv_file.stem and warn:
                warn(f"{csv_file.name}: reading its season as '{season}'")
            if expected_div and warn:
                actual_div = _first_div(csv_file)
                if actual_div and actual_div != expected_div:
                    warn(f"{csv_file.relative_to(root)}: its Div column says "
                         f"'{actual_div}' but this folder means '{expected_div}'. It "
                         f"will be stored as {league_name}. Move the file to the "
                         f"right folder.")
            yield csv_file, league_name, season


def main():
    parser = argparse.ArgumentParser(description="Import football-data CSVs into SQLite")
    parser.add_argument("--league", help="folder name under data/raw, e.g. premier-league")
    parser.add_argument("--season", help="season label, e.g. 2025-26")
    parser.add_argument("--folder", help="raw folder name (with --file)")
    parser.add_argument("--league-name", help="league display name (with --file)")
    parser.add_argument("--file", help="path to a specific CSV")
    parser.add_argument("--db", default=str(DB_PATH), help="database path")
    parser.add_argument("--list", action="store_true", help="show CSVs and exit")
    args = parser.parse_args()

    root = RAW_DIR
    if not root.exists():
        print(f"No raw data folder at {root}")
        return 1

    db_path = Path(args.db)

    # Warnings are collected, not printed inline, so they can be repeated at the
    # end where they will actually be read - and so a skipped file can make the
    # command exit non-zero instead of looking like a successful import.
    warnings: list[str] = []
    def warn(message: str) -> None:
        warnings.append(message)

    def report_tail() -> None:
        if warnings:
            print(f"\n{len(warnings)} thing(s) to look at:")
            for message in warnings:
                print(f"  ! {message}")

    if args.list:
        for csv_file, league_name, season in discover(root, warn=warn):
            print(f"{csv_file.relative_to(root)}  ->  {league_name} / {season}")
        report_tail()
        return 1 if any("skipped" in w for w in warnings) else 0

    conn = sqlite3.connect(db_path)
    try:
        if args.file:
            if not args.league_name or not args.season:
                print("--file requires --league-name and --season")
                return 1
            import_season(Path(args.file), args.league_name, args.season, db_path, conn)
            return 0

        if args.league or args.season:
            targets = [t for t in discover(root, warn=warn)
                       if (not args.league or t[0].parent.name == args.league)
                       and (not args.season or t[2] == args.season)]
            if not targets:
                print("No matching CSVs. Try --list")
                report_tail()
                return 1
            for csv_file, league_name, season in targets:
                import_season(csv_file, league_name, season, db_path, conn)
            report_tail()
            return 0

        # Import everything.
        for csv_file, league_name, season in discover(root, warn=warn):
            import_season(csv_file, league_name, season, db_path, conn)

        cur = conn.cursor()
        cur.execute("SELECT league, season, COUNT(*) FROM matches "
                    "GROUP BY league, season ORDER BY league, season")
        print("\nDatabase now contains:")
        for league, season, count in cur.fetchall():
            print(f"  {league:16} {season:10} {count:>5}")
        report_tail()
    finally:
        conn.close()

    return 1 if any("skipped" in w for w in warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
