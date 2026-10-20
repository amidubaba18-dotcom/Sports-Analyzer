"""Decide which NBA games the dashboard may show.

The football gate exists because provider names must be reconciled with a
history table that uses different names. The NBA does not have that problem:
`TEAM_ID` is the same integer in the schedule, the game log and the box score.
So the gate here is deliberately small, and it is still a gate:

    * the game is on the official regular-season schedule (game id 002...)
    * both teams are known (the provider publishes a few TBD cup rows)
    * the status is `scheduled` (never auto-publish a live/postponed game)
    * both teams have at least one completed game the engine can count
    * the game is inside the lookahead window

Auto-publishing writes only `nba_published`. It never touches `nba_games`,
never deletes a publication, and never un-publishes because a refresh stopped
seeing a fixture.

Unlike football, publishing does not depend on a separate alias step, so there
is nothing for the user to do after the schedule refresh.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

DEFAULT_LOOKAHEAD_DAYS = 14


def _today() -> str:
    return dt.date.today().isoformat()


def _history_count(conn: sqlite3.Connection, team_id: int,
                   before_date: str | None = None) -> int:
    if before_date is None:
        return conn.execute(
            "SELECT COUNT(*) FROM nba_team_games WHERE team_id = ? "
            "AND points IS NOT NULL", (team_id,)).fetchone()[0]
    return conn.execute(
        "SELECT COUNT(*) FROM nba_team_games WHERE team_id = ? "
        "AND game_date < ? AND points IS NOT NULL",
        (team_id, before_date)).fetchone()[0]


def publish(conn: sqlite3.Connection, game_id: str,
            lines: dict | None = None) -> dict:
    """Publish one fixture. Idempotent: publishing twice changes nothing."""
    fixture = conn.execute(
        "SELECT game_id, season, game_date, game_datetime_utc, home_team_id, "
        "       away_team_id, status FROM nba_fixtures WHERE game_id = ?",
        (game_id,)).fetchone()
    if fixture is None:
        raise ValueError(f"{game_id} is not on the fixture list")

    (gid, season, game_date, kickoff, home_id, away_id, status) = fixture
    if status not in ("scheduled", "live", "finished"):
        raise ValueError(f"{game_id} is {status}, not publishable")
    if _history_count(conn, home_id) == 0 or _history_count(conn, away_id) == 0:
        raise ValueError(f"{game_id} has a team with no completed games")

    lines = lines or {}
    existed = conn.execute(
        "SELECT id FROM nba_published WHERE game_id = ?", (gid,)).fetchone()
    conn.execute(
        """
        INSERT INTO nba_published (game_id, season, game_date,
                                   game_datetime_utc, home_team_id,
                                   away_team_id, status, market_total,
                                   market_home_total, market_away_total)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id) DO UPDATE SET
            market_total = COALESCE(excluded.market_total,
                                    nba_published.market_total),
            market_home_total = COALESCE(excluded.market_home_total,
                                         nba_published.market_home_total),
            market_away_total = COALESCE(excluded.market_away_total,
                                         nba_published.market_away_total)
        """,
        (gid, season, game_date, kickoff, home_id, away_id, status,
         lines.get("game_total"), lines.get("home_team_total"),
         lines.get("away_team_total")),
    )
    published_id = conn.execute(
        "SELECT id FROM nba_published WHERE game_id = ?", (gid,)).fetchone()[0]
    conn.execute("UPDATE nba_fixtures SET published_id = ? WHERE game_id = ?",
                 (published_id, gid))
    conn.commit()
    return {"game_id": gid, "published_id": published_id,
            "created": existed is None}


def unpublish(conn: sqlite3.Connection, game_id: str) -> dict:
    conn.execute("DELETE FROM nba_published WHERE game_id = ?", (game_id,))
    conn.execute("UPDATE nba_fixtures SET published_id = NULL "
                 "WHERE game_id = ?", (game_id,))
    conn.commit()
    return {"game_id": game_id, "unpublished": True}


def auto_publish(conn: sqlite3.Connection, start: str | None = None,
                 days: int = DEFAULT_LOOKAHEAD_DAYS) -> dict:
    """Publish every eligible fixture in the window. Never un-publishes."""
    start = start or _today()
    end = (dt.date.fromisoformat(start) + dt.timedelta(days=days)).isoformat()

    rows = conn.execute(
        """
        SELECT f.game_id, f.home_team_id, f.away_team_id
        FROM nba_fixtures f
        LEFT JOIN nba_published p ON p.game_id = f.game_id
        WHERE f.game_date >= ? AND f.game_date <= ?
          AND f.status = 'scheduled'
          AND p.id IS NULL
        ORDER BY f.game_date, f.game_date
        """,
        (start, end),
    ).fetchall()

    published = 0
    skipped: list[dict] = []
    for game_id, home_id, away_id in rows:
        home_games = _history_count(conn, home_id, before_date=None)
        away_games = _history_count(conn, away_id, before_date=None)
        if home_games == 0 or away_games == 0:
            skipped.append({"game_id": game_id,
                            "reason": "a team has no completed games"})
            continue
        publish(conn, game_id)
        published += 1

    return {"window": [start, end], "published": published,
            "skipped": skipped, "candidates": len(rows)}


def clear_lines(conn: sqlite3.Connection, game_id: str) -> dict:
    """Remove the saved market lines, so the league-reference line returns.

    Without this, a mistyped line could never be taken back: `publish` keeps an
    existing value when the new one is None, which is right for auto-publish and
    wrong for "I want to undo that".
    """
    conn.execute(
        "UPDATE nba_published SET market_total = NULL, "
        "market_home_total = NULL, market_away_total = NULL WHERE game_id = ?",
        (game_id,))
    conn.commit()
    return {"game_id": game_id, "lines_cleared": True}


def published_for_date(conn: sqlite3.Connection, iso_date: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT p.game_id, p.game_date, p.game_datetime_utc, p.status,
               p.market_total, p.market_home_total, p.market_away_total,
               ht.team_id AS home_id, ht.name AS home_name,
               ht.tricode AS home_tricode,
               at.team_id AS away_id, at.name AS away_name,
               at.tricode AS away_tricode
        FROM nba_published p
        JOIN nba_teams ht ON ht.team_id = p.home_team_id
        JOIN nba_teams at ON at.team_id = p.away_team_id
        WHERE p.game_date = ?
        ORDER BY COALESCE(p.game_datetime_utc, ''), p.game_id
        """,
        (iso_date,),
    ).fetchall()
    return [{
        "game_id": r[0], "game_date": r[1], "game_datetime_utc": r[2],
        "status": r[3],
        "market_total": r[4], "market_home_total": r[5],
        "market_away_total": r[6],
        "home_team_id": r[7], "home_team": r[8], "home_tricode": r[9],
        "away_team_id": r[10], "away_team": r[11], "away_tricode": r[12],
    } for r in rows]


def published_dates(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT game_date, COUNT(*) FROM nba_published GROUP BY game_date "
        "ORDER BY game_date").fetchall()
    return [{"date": r[0], "games": r[1]} for r in rows]


def upcoming(conn: sqlite3.Connection, start: str | None = None,
             days: int = DEFAULT_LOOKAHEAD_DAYS) -> list[dict]:
    start = start or _today()
    end = (dt.date.fromisoformat(start) + dt.timedelta(days=days)).isoformat()
    rows = conn.execute(
        """
        SELECT f.game_id, f.game_date, f.game_datetime_utc, f.status,
               f.published_id,
               ht.team_id, ht.name, ht.tricode,
               at.team_id, at.name, at.tricode
        FROM nba_fixtures f
        JOIN nba_teams ht ON ht.team_id = f.home_team_id
        JOIN nba_teams at ON at.team_id = f.away_team_id
        WHERE f.game_date >= ? AND f.game_date <= ?
        ORDER BY f.game_date, COALESCE(f.game_datetime_utc, ''), f.game_id
        """,
        (start, end),
    ).fetchall()
    return [{
        "game_id": r[0], "game_date": r[1], "game_datetime_utc": r[2],
        "status": r[3], "published_id": r[4],
        "home_team_id": r[5], "home_team": r[6], "home_tricode": r[7],
        "away_team_id": r[8], "away_team": r[9], "away_tricode": r[10],
    } for r in rows]


def market_lines(conn: sqlite3.Connection, game_id: str) -> dict:
    row = conn.execute(
        "SELECT market_total, market_home_total, market_away_total "
        "FROM nba_published WHERE game_id = ?", (game_id,)).fetchone()
    if not row:
        return {}
    keys = ("game_total", "home_team_total", "away_team_total")
    return {k: v for k, v in zip(keys, row) if v is not None}
