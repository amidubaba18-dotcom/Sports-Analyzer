"""Ingest NBA history, the forward schedule and quarter scores.

Three sources, three jobs. They are kept apart on purpose.

    LeagueGameLog(season)      completed team box scores -> nba_games + nba_team_games
    ScheduleLeagueV2(season)   the forward calendar      -> nba_fixtures
    ScoreboardV2(date)         quarter and half totals   -> nba_games (quarter columns)

`nba_team_games` stores one row per team per game, and derives the three numbers
the model is built from, once, at ingest time:

    possessions = FGA + 0.44 * FTA - OREB + TOV
    off_rating  = 100 * points / possessions
    pace        = possessions * 48 / game_minutes

The 0.44 free-throw coefficient is the standard estimate; `game_minutes` is
`MIN / 5`, so a regulation game is 48 and an overtime game is longer, which is
what makes pace comparable across both.

Team identity is the provider's `TEAM_ID`. There is no alias gate here because
unlike football there is nothing to reconcile: the same integer identifies a
club in the schedule, the game log and the box score.

Usage:
    python scripts/nba_fetch.py --schema
    python scripts/nba_fetch.py --history 2023-24 2024-25 2025-26
    python scripts/nba_fetch.py --schedule 2026-27
    python scripts/nba_fetch.py --quarters --season 2025-26
    python scripts/nba_fetch.py --all
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "sports_analyzer.db"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nba_schema import ensure_schema  # noqa: E402

# stats.nba.com is not a paid, documented API. Being slow is cheap; being
# blocked is not.
CALL_PAUSE_SECONDS = 0.9
RETRIES = 3


def current_season(today: dt.date | None = None) -> str:
    """The NBA season label for a date: October starts a new season."""
    today = today or dt.date.today()
    start = today.year if today.month >= 10 else today.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def _num(value):
    """Float or None, so NaN never reaches SQLite."""
    if value is None:
        return None
    try:
        if value != value:  # NaN
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value):
    number = _num(value)
    return None if number is None else int(number)


def _text(value):
    if value is None or value != value:
        return None
    return str(value)


def _with_retries(label: str, fn):
    """Run a provider call, retrying transient failures without hammering."""
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            return fn()
        except Exception as exc:  # nba_api raises a variety of errors
            last = exc
            if attempt < RETRIES:
                print(f"    {label}: attempt {attempt} failed ({type(exc).__name__}), "
                      f"retrying in {attempt * 3}s")
                time.sleep(attempt * 3)
    raise RuntimeError(f"{label} failed after {RETRIES} attempts: {last}") from last


def _upsert_team(conn: sqlite3.Connection, team_id, name, city, tricode, slug=None):
    if team_id is None:
        return
    conn.execute(
        """
        INSERT INTO nba_teams (team_id, name, city, tricode, slug)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(team_id) DO UPDATE SET
            name = COALESCE(excluded.name, nba_teams.name),
            city = COALESCE(excluded.city, nba_teams.city),
            tricode = COALESCE(excluded.tricode, nba_teams.tricode),
            slug = COALESCE(excluded.slug, nba_teams.slug)
        """,
        (int(team_id), name, city, tricode, slug),
    )


def _derive(rows: list[dict]) -> dict:
    """Turn one team's box score into possessions, ratings and pace."""
    fga = _num(rows.get("fga"))
    fta = _num(rows.get("fta"))
    oreb = _num(rows.get("oreb"))
    tov = _num(rows.get("tov"))
    minutes = _num(rows.get("minutes"))
    points = _num(rows.get("points"))

    possessions = None
    if None not in (fga, fta, oreb, tov):
        possessions = fga + 0.44 * fta - oreb + tov

    game_minutes = minutes / 5.0 if minutes else None
    pace = None
    off_rating = None
    if possessions and possessions > 0:
        if game_minutes:
            pace = possessions * 48.0 / game_minutes
        if points is not None:
            off_rating = 100.0 * points / possessions

    return {
        "possessions": possessions,
        "pace": pace,
        "off_rating": off_rating,
        "game_minutes": game_minutes,
    }


# ---------------------------------------------------------------------------
# History: completed games
# ---------------------------------------------------------------------------

def schedule_team_map(season: str) -> dict[str, tuple[int, int]]:
    """game_id -> (home_team_id, away_team_id) from the official schedule.

    The game log's MATCHUP field cannot be trusted for neutral-site games: both
    sides are printed as the away team (e.g. `DAL @ DET` and `DET @ DAL`), so
    parsing it would drop the game. The schedule states home and away
    explicitly, so it is the authority here.
    """
    from nba_api.stats.endpoints import scheduleleaguev2

    df = _with_retries(
        f"ScheduleLeagueV2 {season}",
        lambda: scheduleleaguev2.ScheduleLeagueV2(
            season=season, timeout=90).get_data_frames()[0],
    )
    mapping: dict[str, tuple[int, int]] = {}
    if df is None:
        return mapping
    for _, row in df.iterrows():
        game_id = _text(row.get("gameId"))
        home_id = _int(row.get("homeTeam_teamId"))
        away_id = _int(row.get("awayTeam_teamId"))
        if game_id and game_id.startswith("002") and home_id and away_id:
            mapping[game_id] = (home_id, away_id)
    return mapping


def ingest_history(conn: sqlite3.Connection, season: str,
                   team_map: dict[str, tuple[int, int]] | None = None) -> int:
    """Pull one regular season's team game log into the database."""
    from nba_api.stats.endpoints import leaguegamelog

    print(f"  history {season} ...", end="", flush=True)
    df = _with_retries(
        f"LeagueGameLog {season}",
        lambda: leaguegamelog.LeagueGameLog(
            season=season,
            season_type_all_star="Regular Season",
            player_or_team_abbreviation="T",
            timeout=90,
        ).get_data_frames()[0],
    )
    if df is None or not len(df):
        print(" 0 games")
        return 0

    if team_map is None:
        team_map = schedule_team_map(season)
        time.sleep(CALL_PAUSE_SECONDS)

    games: dict[str, dict] = {}
    skipped = 0
    for _, row in df.iterrows():
        game_id = _text(row.get("GAME_ID"))
        team_id = _int(row.get("TEAM_ID"))
        if not game_id or team_id is None:
            continue

        matchup = _text(row.get("MATCHUP")) or ""
        home_id, away_id = team_map.get(game_id, (None, None))
        if home_id is not None:
            side = "home" if team_id == home_id else (
                "away" if team_id == away_id else None)
        else:
            # No schedule row: fall back to the match string, and refuse to
            # guess when it is ambiguous.
            side = "home" if " vs. " in matchup else (
                "away" if " @ " in matchup else None)
        if side is None:
            skipped += 1
            continue

        game_date = _text(row.get("GAME_DATE"))
        if game_date and len(game_date) >= 10:
            game_date = game_date[:10]

        stat = {
            "game_id": game_id,
            "team_id": team_id,
            "is_home": 1 if side == "home" else 0,
            "season": season,
            "game_date": game_date,
            "minutes": _num(row.get("MIN")),
            "points": _int(row.get("PTS")),
            "fgm": _int(row.get("FGM")),
            "fga": _int(row.get("FGA")),
            "fg3m": _int(row.get("FG3M")),
            "fg3a": _int(row.get("FG3A")),
            "ftm": _int(row.get("FTM")),
            "fta": _int(row.get("FTA")),
            "oreb": _int(row.get("OREB")),
            "dreb": _int(row.get("DREB")),
            "reb": _int(row.get("REB")),
            "ast": _int(row.get("AST")),
            "stl": _int(row.get("STL")),
            "blk": _int(row.get("BLK")),
            "tov": _int(row.get("TOV")),
            "pf": _int(row.get("PF")),
        }
        derived = _derive(stat)
        stat.update({k: derived[k] for k in ("possessions", "pace", "off_rating")})

        entry = games.setdefault(game_id, {"season": season, "game_date": game_date,
                                           "home": None, "away": None})
        entry[side] = stat

        _upsert_team(
            conn, team_id,
            _text(row.get("TEAM_NAME")),
            None,
            _text(row.get("TEAM_ABBREVIATION")) or "",
        )

    written = 0
    for game_id, entry in games.items():
        home, away = entry["home"], entry["away"]
        if not home or not away:
            # A half game is not evidence; skip it rather than store a mismatch.
            continue
        home["opponent_id"] = away["team_id"]
        away["opponent_id"] = home["team_id"]
        home["def_rating"] = (
            100.0 * away["points"] / home["possessions"]
            if home["possessions"] and away["points"] is not None else None
        )
        away["def_rating"] = (
            100.0 * home["points"] / away["possessions"]
            if away["possessions"] and home["points"] is not None else None
        )

        conn.execute(
            """
            INSERT INTO nba_games (game_id, season, season_type, game_date,
                                   home_team_id, away_team_id, home_score,
                                   away_score, source)
            VALUES (?, ?, 'Regular Season', ?, ?, ?, ?, ?, 'nba_api')
            ON CONFLICT(game_id) DO UPDATE SET
                home_team_id = excluded.home_team_id,
                away_team_id = excluded.away_team_id,
                home_score = excluded.home_score,
                away_score = excluded.away_score,
                game_date = excluded.game_date
            """,
            (game_id, entry["season"], entry["game_date"], home["team_id"],
             away["team_id"], home["points"], away["points"]),
        )

        for stat in (home, away):
            conn.execute(
                """
                INSERT INTO nba_team_games (
                    game_id, team_id, opponent_id, is_home, season, game_date,
                    minutes, points, fgm, fga, fg3m, fg3a, ftm, fta, oreb, dreb,
                    reb, ast, stl, blk, tov, pf, possessions, off_rating,
                    def_rating, pace
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(game_id, team_id) DO UPDATE SET
                    opponent_id = excluded.opponent_id,
                    is_home = excluded.is_home,
                    season = excluded.season,
                    game_date = excluded.game_date,
                    minutes = excluded.minutes,
                    points = excluded.points,
                    fgm = excluded.fgm,
                    fga = excluded.fga,
                    fg3m = excluded.fg3m,
                    fg3a = excluded.fg3a,
                    ftm = excluded.ftm,
                    fta = excluded.fta,
                    oreb = excluded.oreb,
                    dreb = excluded.dreb,
                    reb = excluded.reb,
                    ast = excluded.ast,
                    stl = excluded.stl,
                    blk = excluded.blk,
                    tov = excluded.tov,
                    pf = excluded.pf,
                    possessions = excluded.possessions,
                    off_rating = excluded.off_rating,
                    def_rating = excluded.def_rating,
                    pace = excluded.pace
                """,
                (stat["game_id"], stat["team_id"], stat["opponent_id"],
                 stat["is_home"], stat["season"], stat["game_date"],
                 stat["minutes"], stat["points"], stat["fgm"], stat["fga"],
                 stat["fg3m"], stat["fg3a"], stat["ftm"], stat["fta"],
                 stat["oreb"], stat["dreb"], stat["reb"], stat["ast"],
                 stat["stl"], stat["blk"], stat["tov"], stat["pf"],
                 stat["possessions"], stat["off_rating"], stat["def_rating"],
                 stat["pace"]),
            )
        written += 1

    conn.commit()
    print(f" {written} games" + (f" ({skipped} rows skipped)" if skipped else ""))
    return written


# ---------------------------------------------------------------------------
# Forward calendar
# ---------------------------------------------------------------------------

def _schedule_status(row) -> str:
    text = f"{_text(row.get('gameStatusText')) or ''} {_text(row.get('gameLabel')) or ''}"
    lowered = text.lower()
    if "postpon" in lowered:
        return "postponed"
    if "cancel" in lowered:
        return "cancelled"
    status = _int(row.get("gameStatus"))
    if status == 3:
        return "finished"
    if status == 2:
        return "live"
    return "scheduled"


def ingest_schedule(conn: sqlite3.Connection, season: str) -> int:
    """Pull the season schedule into nba_fixtures (candidates only)."""
    from nba_api.stats.endpoints import scheduleleaguev2

    print(f"  schedule {season} ...", end="", flush=True)
    df = _with_retries(
        f"ScheduleLeagueV2 {season}",
        lambda: scheduleleaguev2.ScheduleLeagueV2(
            season=season, timeout=90).get_data_frames()[0],
    )
    if df is None or not len(df):
        print(" 0 games")
        return 0

    written = 0
    tbd = 0
    for _, row in df.iterrows():
        game_id = _text(row.get("gameId"))
        if not game_id:
            continue
        # 002 = regular season. 001 is preseason, 003 all-star, 004 playoffs.
        if not game_id.startswith("002"):
            continue

        raw_date = _text(row.get("gameDate")) or ""
        game_date = None
        for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y", "%Y-%m-%d"):
            try:
                game_date = dt.datetime.strptime(raw_date, fmt).date().isoformat()
                break
            except ValueError:
                continue
        if game_date is None:
            continue

        home_id = _int(row.get("homeTeam_teamId"))
        away_id = _int(row.get("awayTeam_teamId"))
        home_name = _text(row.get("homeTeam_teamName"))
        away_name = _text(row.get("awayTeam_teamName"))
        if home_id is None or away_id is None or not home_name or not away_name:
            # The schedule carries a few TBD rows (cup/playoff placeholders)
            # whose teams are not known yet. A fixture with no teams cannot be
            # analysed or shown, so it waits for a later refresh.
            tbd += 1
            continue

        _upsert_team(conn, home_id, home_name,
                     _text(row.get("homeTeam_teamCity")),
                     _text(row.get("homeTeam_teamTricode")) or "",
                     _text(row.get("homeTeam_teamSlug")))
        _upsert_team(conn, away_id, away_name,
                     _text(row.get("awayTeam_teamCity")),
                     _text(row.get("awayTeam_teamTricode")) or "",
                     _text(row.get("awayTeam_teamSlug")))

        conn.execute(
            """
            INSERT INTO nba_fixtures (game_id, season, game_date,
                                      game_datetime_utc, home_team_id,
                                      away_team_id, status, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(game_id) DO UPDATE SET
                game_date = excluded.game_date,
                game_datetime_utc = excluded.game_datetime_utc,
                status = excluded.status,
                last_seen = datetime('now')
            """,
            (game_id, season, game_date, _text(row.get("gameDateTimeUTC")),
             home_id, away_id, _schedule_status(row)),
        )
        written += 1

    conn.commit()
    print(f" {written} regular-season games"
          + (f" ({tbd} TBD rows skipped)" if tbd else ""))
    return written


# ---------------------------------------------------------------------------
# Quarter and half totals
# ---------------------------------------------------------------------------


def ingest_quarters_for_date(conn: sqlite3.Connection, game_date: str) -> int:
    """Fill quarter scores for the completed games on one date."""
    from nba_api.stats.endpoints import scoreboardv2

    payload = _with_retries(
        f"ScoreboardV2 {game_date}",
        lambda: scoreboardv2.ScoreboardV2(
            game_date=game_date, timeout=90).get_data_frames(),
    )
    if not payload or len(payload) < 2:
        return 0
    line = payload[1]
    if line is None or not len(line):
        return 0

    by_game: dict[str, list] = {}
    for _, row in line.iterrows():
        game_id = _text(row.get("GAME_ID"))
        if game_id:
            by_game.setdefault(game_id, []).append(row)

    updated = 0
    for game_id, rows in by_game.items():
        game = conn.execute(
            "SELECT home_team_id, away_team_id, home_score, away_score "
            "FROM nba_games WHERE game_id = ?", (game_id,)).fetchone()
        if game is None:
            continue
        home_id, away_id = game[0], game[1]

        sides = {}
        for row in rows:
            team_id = _int(row.get("TEAM_ID"))
            quarters = [_int(row.get(f"PTS_QTR{i}")) for i in range(1, 5)]
            overtime = sum(
                _int(row.get(f"PTS_OT{i}")) or 0 for i in range(1, 11)
            )
            if team_id == home_id:
                sides["home"] = (quarters, overtime)
            elif team_id == away_id:
                sides["away"] = (quarters, overtime)

        if set(sides) != {"home", "away"}:
            continue
        home_q, home_ot = sides["home"]
        away_q, away_ot = sides["away"]
        if any(q is None for q in home_q + away_q):
            # The provider has no line score for this game (a known gap in
            # 2025-26). Leave has_quarters=0 rather than store a partial sum.
            continue

        conn.execute(
            """
            UPDATE nba_games SET
                home_q1 = ?, home_q2 = ?, home_q3 = ?, home_q4 = ?,
                home_ot = ?,
                away_q1 = ?, away_q2 = ?, away_q3 = ?, away_q4 = ?,
                away_ot = ?,
                went_to_ot = ?, has_quarters = 1
            WHERE game_id = ?
            """,
            (*home_q, home_ot, *away_q, away_ot,
             1 if (home_ot or away_ot) else 0, game_id),
        )
        updated += 1

    conn.commit()
    return updated


def ingest_quarters(conn: sqlite3.Connection, season: str | None = None,
                    limit_dates: int | None = None) -> int:
    """Backfill quarter scores for every completed game still missing them."""
    params: list = []
    where = "has_quarters = 0 AND home_score IS NOT NULL"
    if season:
        where += " AND season = ?"
        params.append(season)
    dates = [row[0] for row in conn.execute(
        f"SELECT DISTINCT game_date FROM nba_games WHERE {where} "
        "ORDER BY game_date", params)]
    if limit_dates:
        dates = dates[:limit_dates]
    print(f"  quarters: {len(dates)} date(s) to fill")
    total = 0
    for i, day in enumerate(dates, 1):
        try:
            count = ingest_quarters_for_date(conn, day)
        except Exception as exc:
            print(f"    {day}: FAILED {type(exc).__name__}: {str(exc)[:120]}")
            count = 0
        total += count
        if i % 20 == 0 or i == len(dates):
            print(f"    {i}/{len(dates)} dates, {total} games updated")
        time.sleep(CALL_PAUSE_SECONDS)
    return total


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO nba_settings (key, value, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                       updated_at = datetime('now')
        """,
        (key, value),
    )
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM nba_settings WHERE key = ?",
                       (key,)).fetchone()
    return row[0] if row else default


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest NBA data.")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--schema", action="store_true", help="create tables only")
    parser.add_argument("--history", nargs="*", metavar="SEASON",
                        help="seasons to ingest, e.g. 2024-25 2025-26")
    parser.add_argument("--schedule", nargs="*", metavar="SEASON",
                        help="seasons whose schedule to refresh")
    parser.add_argument("--quarters", action="store_true",
                        help="backfill quarter scores")
    parser.add_argument("--season", default=None,
                        help="limit --quarters to one season")
    parser.add_argument("--all", action="store_true",
                        help="current season schedule + history + quarters")
    args = parser.parse_args(argv)

    season = current_season()
    previous = [f"{int(season[:4]) - i}-{str(int(season[:4]) - i + 1)[2:]}"
                for i in (1, 2, 3)]

    conn = sqlite3.connect(args.db)
    try:
        ensure_schema(conn)

        if args.schema and not (args.history or args.schedule
                                or args.quarters or args.all):
            print(f"NBA schema ready in {args.db}")
            return 0

        history = args.history
        schedule = args.schedule
        if args.all:
            history = previous
            schedule = [season]

        if history is not None:
            seasons = history or previous
            print("History:")
            for s in seasons:
                ingest_history(conn, s)
            set_setting(conn, "last_history_fetch",
                        dt.datetime.now(dt.timezone.utc).isoformat())
            time.sleep(CALL_PAUSE_SECONDS)

        if schedule is not None:
            seasons = schedule or [season]
            print("Schedule:")
            for s in seasons:
                ingest_schedule(conn, s)
            set_setting(conn, "last_schedule_fetch",
                        dt.datetime.now(dt.timezone.utc).isoformat())
            # The user should never have to press publish for the NBA: the
            # schedule is authoritative and TEAM_ID needs no reconciliation.
            from nba_fixtures import auto_publish
            result = auto_publish(conn)
            print(f"  auto-published {result['published']} of "
                  f"{result['candidates']} candidate game(s) in "
                  f"{result['window'][0]} .. {result['window'][1]}")
            for skipped in result["skipped"][:5]:
                print(f"    held {skipped['game_id']}: {skipped['reason']}")
            set_setting(conn, "last_publish",
                        dt.datetime.now(dt.timezone.utc).isoformat())

        if args.quarters or args.all:
            print("Quarters:")
            ingest_quarters(conn, season=args.season)
            set_setting(conn, "last_quarter_fetch",
                        dt.datetime.now(dt.timezone.utc).isoformat())

        counts = {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("nba_games", "nba_team_games", "nba_fixtures",
                         "nba_teams")
        }
        print("Totals: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        missing = conn.execute(
            "SELECT COUNT(*) FROM nba_games WHERE has_quarters = 0"
        ).fetchone()[0]
        print(f"Games without quarter scores: {missing}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
