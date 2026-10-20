"""Rosters, player production and availability for the NBA module.

What this adds
--------------
The existing NBA pipeline knows teams and team box scores. It has never known
*who* plays. This module fills that gap from three sources, each labelled with
where it came from and when it was captured:

    CommonTeamRoster        the official 30-man roster       -> nba_players, nba_rosters
    LeagueDashPlayerStats   official season per-game totals  -> nba_player_stats
    ESPN injuries feed      current availability             -> nba_injuries

Two rules the rest of the app depends on:

1. **Confirmed is not projected.** A roster row is `confirmed`: the league
   lists the player on the team. A role (starter, bench, minutes) is derived
   from minutes and is `projected`. Nothing here claims a starting lineup.
2. **Availability is displayed, not priced.** The injury feed has no history,
   so its effect cannot be measured yet. It is stored with a timestamp and
   shown as context; it never moves a probability until there is enough stored
   history to clear the same gates as every other factor.

The sync is additive and idempotent: players who leave a roster are marked
inactive rather than deleted, and every pull is written to `nba_sync_log` so
the admin can see what ran, when, and what failed.

Usage:
    python scripts/nba_people.py --rosters --stats --injuries
    python scripts/nba_people.py --status
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "sports_analyzer.db"

ROSTER_SEASON = "2026-27"
# Seasons whose player production is kept as evidence. The current season is
# included: it is empty in October and fills as games are played.
STAT_SEASONS = ("2023-24", "2024-25", "2025-26", "2026-27")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nba_players (
    player_id   INTEGER PRIMARY KEY,
    full_name   TEXT NOT NULL,
    first_name  TEXT,
    last_name   TEXT,
    slug        TEXT,
    position    TEXT,
    height      TEXT,
    weight      TEXT,
    birth_date  TEXT,
    age         INTEGER,
    experience  TEXT,
    school      TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per player per team per season. `active` is the current roster
-- status; rows are never deleted, so a mid-season trade leaves a trace.
CREATE TABLE IF NOT EXISTS nba_rosters (
    season       TEXT NOT NULL,
    team_id      INTEGER NOT NULL,
    player_id    INTEGER NOT NULL,
    jersey       TEXT,
    position     TEXT,
    height       TEXT,
    weight       TEXT,
    birth_date   TEXT,
    age          INTEGER,
    experience   TEXT,
    school       TEXT,
    how_acquired TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    source       TEXT NOT NULL DEFAULT 'nba_api:CommonTeamRoster',
    first_seen   TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (season, team_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_nba_rosters_team
    ON nba_rosters(team_id, season, active);

-- Season aggregates, one row per player. The provider attributes a player's
-- whole season to his final team; `gp` and `minutes` are season-wide, so a
-- team's rows are a roster of individuals, never a rotation. `team_id = 0`
-- is kept for providers that return a "total across teams" row.
CREATE TABLE IF NOT EXISTS nba_player_stats (
    season        TEXT NOT NULL,
    season_type   TEXT NOT NULL DEFAULT 'Regular Season',
    player_id     INTEGER NOT NULL,
    team_id       INTEGER NOT NULL,
    player_name   TEXT,
    team_abbr     TEXT,
    age           REAL,
    gp            INTEGER,
    gs            INTEGER,
    minutes       REAL,
    fgm           REAL,
    fga           REAL,
    fg_pct        REAL,
    fg3m          REAL,
    fg3a          REAL,
    fg3_pct       REAL,
    ftm           REAL,
    fta           REAL,
    ft_pct        REAL,
    oreb          REAL,
    dreb          REAL,
    reb           REAL,
    ast           REAL,
    tov           REAL,
    stl           REAL,
    blk           REAL,
    pf            REAL,
    pts           REAL,
    plus_minus    REAL,
    ts_pct        REAL,
    usage_proxy   REAL,
    source        TEXT NOT NULL DEFAULT 'nba_api:LeagueDashPlayerStats',
    captured_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (season, season_type, player_id, team_id)
);

CREATE INDEX IF NOT EXISTS idx_nba_player_stats_team
    ON nba_player_stats(team_id, season, minutes);

-- Availability is a *snapshot*, not a fact with a history in the feed. Every
-- pull is kept so the app can later measure whether it matters.
CREATE TABLE IF NOT EXISTS nba_injuries (
    id            INTEGER PRIMARY KEY,
    captured_at   TEXT NOT NULL,
    team_id       INTEGER,
    player_id     INTEGER,
    player_name   TEXT NOT NULL,
    status        TEXT,
    detail        TEXT,
    short_comment TEXT,
    long_comment  TEXT,
    reported_at   TEXT,
    source        TEXT NOT NULL DEFAULT 'espn',
    source_id     TEXT,
    UNIQUE (captured_at, player_name)
);

CREATE INDEX IF NOT EXISTS idx_nba_injuries_team
    ON nba_injuries(team_id, captured_at);

CREATE TABLE IF NOT EXISTS nba_sync_log (
    id          INTEGER PRIMARY KEY,
    job         TEXT NOT NULL,
    status      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    rows        INTEGER,
    detail      TEXT,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_nba_sync_job
    ON nba_sync_log(job, started_at);
"""


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    # Columns added after the first release of these tables.
    have = {row[1] for row in conn.execute("PRAGMA table_info(nba_injuries)")}
    if "source_id" not in have:
        conn.execute("ALTER TABLE nba_injuries ADD COLUMN source_id TEXT")
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(value):
    """NaN/None -> None, so SQLite stores NULL rather than a string."""
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    return value


def _log_start(conn: sqlite3.Connection, job: str) -> int:
    cur = conn.execute(
        "INSERT INTO nba_sync_log (job, status, started_at) VALUES (?, 'running', ?)",
        (job, _now()))
    conn.commit()
    return int(cur.lastrowid)


def _log_end(conn: sqlite3.Connection, row_id: int, rows: int,
             detail: str = "", error: str | None = None) -> None:
    conn.execute(
        "UPDATE nba_sync_log SET status = ?, finished_at = ?, rows = ?, "
        "detail = ?, error = ? WHERE id = ?",
        ("error" if error else "ok", _now(), rows, detail, error, row_id))
    conn.commit()


def _with_retries(label: str, fn, attempts: int = 3, log=print):
    """stats.nba.com intermittently times out; retry with a longer wait."""
    last = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - network layer is unreliable
            last = exc
            wait = 2 + attempt * 3
            log(f"  {label}: attempt {attempt + 1} failed ({exc}); "
                f"retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"{label} failed after {attempts} attempts: {last}")


# --------------------------------------------------------------------------
# Rosters
# --------------------------------------------------------------------------
def sync_rosters(conn: sqlite3.Connection, season: str = ROSTER_SEASON,
                 log=print, pause: float = 0.7) -> int:
    """Pull all 30 official rosters. Returns the number of active players."""
    from nba_api.stats.endpoints import commonteamroster

    ensure_tables(conn)
    row_id = _log_start(conn, "rosters")
    teams = [r[0] for r in conn.execute(
        "SELECT team_id FROM nba_teams ORDER BY team_id")]
    total = 0
    errors: list[str] = []
    try:
        for index, team_id in enumerate(teams, start=1):
            try:
                frame = _with_retries(
                    f"roster {team_id}",
                    lambda t=team_id: commonteamroster.CommonTeamRoster(
                        team_id=t, season=season, timeout=45
                    ).get_data_frames()[0],
                    log=log)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{team_id}: {exc}")
                log(f"  roster {team_id} skipped: {exc}")
                continue

            conn.execute(
                "UPDATE nba_rosters SET active = 0 WHERE season = ? AND team_id = ?",
                (season, team_id))
            for _, row in frame.iterrows():
                player_id = int(row["PLAYER_ID"])
                name = str(row["PLAYER"])
                birth = str(row.get("BIRTH_DATE") or "") or None
                conn.execute(
                    "INSERT INTO nba_players (player_id, full_name, slug, "
                    "  position, height, weight, birth_date, age, experience, "
                    "  school, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now')) "
                    "ON CONFLICT(player_id) DO UPDATE SET "
                    "  full_name=excluded.full_name, slug=excluded.slug, "
                    "  position=excluded.position, height=excluded.height, "
                    "  weight=excluded.weight, birth_date=excluded.birth_date, "
                    "  age=excluded.age, experience=excluded.experience, "
                    "  school=excluded.school, updated_at=datetime('now')",
                    (player_id, name, row.get("PLAYER_SLUG"),
                     row.get("POSITION"), row.get("HEIGHT"), row.get("WEIGHT"),
                     birth, _clean(row.get("AGE")), row.get("EXP"),
                     row.get("SCHOOL")))
                conn.execute(
                    "INSERT INTO nba_rosters (season, team_id, player_id, "
                    "  jersey, position, height, weight, birth_date, age, "
                    "  experience, school, how_acquired, active, first_seen, "
                    "  last_seen) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,datetime('now'),datetime('now')) "
                    "ON CONFLICT(season, team_id, player_id) DO UPDATE SET "
                    "  jersey=excluded.jersey, position=excluded.position, "
                    "  height=excluded.height, weight=excluded.weight, "
                    "  birth_date=excluded.birth_date, age=excluded.age, "
                    "  experience=excluded.experience, school=excluded.school, "
                    "  how_acquired=excluded.how_acquired, active=1, "
                    "  last_seen=datetime('now')",
                    (season, team_id, player_id, _clean(row.get("NUM")),
                     row.get("POSITION"), row.get("HEIGHT"), row.get("WEIGHT"),
                     birth, _clean(row.get("AGE")), row.get("EXP"),
                     row.get("SCHOOL"), row.get("HOW_ACQUIRED")))
                total += 1
            conn.commit()
            if index % 5 == 0 or index == len(teams):
                log(f"  rosters {index}/{len(teams)} teams, {total} players")
            time.sleep(pause)
        detail = f"{season}, {len(teams)} teams"
        _log_end(conn, row_id, total, detail,
                 "; ".join(errors) if errors else None)
        return total
    except Exception as exc:  # noqa: BLE001
        _log_end(conn, row_id, total, "aborted", str(exc))
        raise


# --------------------------------------------------------------------------
# Player production
# --------------------------------------------------------------------------
def sync_player_stats(conn: sqlite3.Connection, seasons=STAT_SEASONS,
                      log=print) -> int:
    """Season per-game aggregates, used for minutes-based role profiles."""
    from nba_api.stats.endpoints import leaguedashplayerstats

    ensure_tables(conn)
    row_id = _log_start(conn, "player_stats")
    total = 0
    errors: list[str] = []
    try:
        for season in seasons:
            try:
                frame = _with_retries(
                    f"player stats {season}",
                    lambda s=season: leaguedashplayerstats.LeagueDashPlayerStats(
                        season=s, per_mode_detailed="PerGame", timeout=90
                    ).get_data_frames()[0],
                    log=log)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{season}: {exc}")
                continue
            if frame is None or frame.empty:
                log(f"  player stats {season}: no rows yet")
                continue
            for _, row in frame.iterrows():
                minutes = _clean(row.get("MIN")) or 0.0
                fga = _clean(row.get("FGA")) or 0.0
                fta = _clean(row.get("FTA")) or 0.0
                pts = _clean(row.get("PTS")) or 0.0
                tov = _clean(row.get("TOV")) or 0.0
                ts = (pts / (2 * (fga + 0.44 * fta))
                      if (fga + 0.44 * fta) > 0 else None)
                usage = ((fga + 0.44 * fta + tov) / minutes
                         if minutes else None)
                conn.execute(
                    "INSERT INTO nba_player_stats (season, season_type, "
                    "  player_id, team_id, player_name, team_abbr, age, gp, gs, "
                    "  minutes, fgm, fga, fg_pct, fg3m, fg3a, fg3_pct, ftm, "
                    "  fta, ft_pct, oreb, dreb, reb, ast, tov, stl, blk, pf, "
                    "  pts, plus_minus, ts_pct, usage_proxy, captured_at) "
                    "VALUES (?, 'Regular Season', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                    "        ?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) "
                    "ON CONFLICT(season, season_type, player_id, team_id) "
                    "DO UPDATE SET player_name=excluded.player_name, "
                    "  team_abbr=excluded.team_abbr, age=excluded.age, "
                    "  gp=excluded.gp, gs=excluded.gs, minutes=excluded.minutes, "
                    "  fgm=excluded.fgm, fga=excluded.fga, fg_pct=excluded.fg_pct, "
                    "  fg3m=excluded.fg3m, fg3a=excluded.fg3a, "
                    "  fg3_pct=excluded.fg3_pct, ftm=excluded.ftm, fta=excluded.fta, "
                    "  ft_pct=excluded.ft_pct, oreb=excluded.oreb, "
                    "  dreb=excluded.dreb, reb=excluded.reb, ast=excluded.ast, "
                    "  tov=excluded.tov, stl=excluded.stl, blk=excluded.blk, "
                    "  pf=excluded.pf, pts=excluded.pts, "
                    "  plus_minus=excluded.plus_minus, ts_pct=excluded.ts_pct, "
                    "  usage_proxy=excluded.usage_proxy, captured_at=datetime('now')",
                    (season, int(row["PLAYER_ID"]), int(row["TEAM_ID"]),
                     row.get("PLAYER_NAME"), row.get("TEAM_ABBREVIATION"),
                     _clean(row.get("AGE")), _clean(row.get("GP")), None,
                     minutes, _clean(row.get("FGM")), fga,
                     _clean(row.get("FG_PCT")), _clean(row.get("FG3M")),
                     _clean(row.get("FG3A")), _clean(row.get("FG3_PCT")),
                     _clean(row.get("FTM")), fta, _clean(row.get("FT_PCT")),
                     _clean(row.get("OREB")), _clean(row.get("DREB")),
                     _clean(row.get("REB")), _clean(row.get("AST")), tov,
                     _clean(row.get("STL")), _clean(row.get("BLK")),
                     _clean(row.get("PF")), pts, _clean(row.get("PLUS_MINUS")),
                     ts, usage))
                total += 1
            conn.commit()
            log(f"  player stats {season}: {len(frame)} players")
        _log_end(conn, row_id, total, ", ".join(seasons),
                 "; ".join(errors) if errors else None)
        return total
    except Exception as exc:  # noqa: BLE001
        _log_end(conn, row_id, total, "aborted", str(exc))
        raise


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------
ESPN_INJURIES = ("https://site.web.api.espn.com/apis/site/v2/sports/"
                 "basketball/nba/injuries")


def _normalise(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace(".", "").replace("'", "")
    text = re.sub(r"\s+(jr|sr|ii|iii|iv)$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def sync_injuries(conn: sqlite3.Connection, log=print,
                  url: str = ESPN_INJURIES) -> int:
    """Snapshot the current injury feed. Each pull is timestamped and kept."""
    ensure_tables(conn)
    row_id = _log_start(conn, "injuries")
    captured = _now()
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (Sports-Analyzer)"})
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))

        # Player -> (NBA id, team) from the roster we just synced. ESPN uses
        # its own athlete ids, so the name is the only reliable bridge; a
        # player who cannot be matched keeps a NULL id rather than a guess.
        lookup: dict[str, tuple[int, int]] = {}
        for name, player_id, team_id in conn.execute(
                "SELECT p.full_name, p.player_id, r.team_id FROM nba_rosters r "
                "JOIN nba_players p ON p.player_id = r.player_id "
                "WHERE r.season = ? AND r.active = 1", (ROSTER_SEASON,)):
            lookup[_normalise(name)] = (player_id, team_id)

        total = 0
        for group in payload.get("injuries", []):
            for item in group.get("injuries", []):
                athlete = item.get("athlete") or {}
                name = athlete.get("displayName") or athlete.get("fullName")
                if not name:
                    continue
                player_id, team_id = lookup.get(_normalise(name), (None, None))
                detail = item.get("type") or {}
                if isinstance(detail, dict):
                    detail = detail.get("description") or detail.get("name")
                conn.execute(
                    "INSERT INTO nba_injuries (captured_at, team_id, player_id, "
                    "  player_name, status, detail, short_comment, "
                    "  long_comment, reported_at, source, source_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'espn',?) "
                    "ON CONFLICT(captured_at, player_name) DO UPDATE SET "
                    "  team_id=excluded.team_id, player_id=excluded.player_id, "
                    "  status=excluded.status, detail=excluded.detail, "
                    "  short_comment=excluded.short_comment, "
                    "  long_comment=excluded.long_comment, "
                    "  reported_at=excluded.reported_at, "
                    "  source_id=excluded.source_id",
                    (captured, team_id, player_id, name, item.get("status"),
                     detail, item.get("shortComment"), item.get("longComment"),
                     item.get("date"), athlete.get("id")))
                total += 1
        conn.commit()
        _log_end(conn, row_id, total, f"captured {captured}")
        log(f"  injuries: {total} entries at {captured}")
        return total
    except Exception as exc:  # noqa: BLE001
        _log_end(conn, row_id, 0, "aborted", str(exc))
        raise


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
STATUS_WEIGHT = {
    "out": 1.0, "doubtful": 0.75, "questionable": 0.5,
    "day-to-day": 0.5, "probable": 0.25, "available": 0.0,
}


def availability(conn: sqlite3.Connection, team_id: int,
                 as_of: str | None = None) -> dict:
    """Latest snapshot for one team, with estimated minutes unavailable.

    The minutes figure is descriptive: it is the sum of each listed player's
    per-game minutes, weighted by how likely the status makes them to miss the
    game. It is not an input to any projection.
    """
    conn.row_factory = sqlite3.Row
    captured = conn.execute(
        "SELECT MAX(captured_at) FROM nba_injuries WHERE team_id = ?",
        (team_id,)).fetchone()[0]
    if not captured:
        return {"captured_at": None, "players": [], "minutes_unavailable": 0.0}
    if as_of:
        row = conn.execute(
            "SELECT MAX(captured_at) FROM nba_injuries "
            "WHERE team_id = ? AND captured_at <= ?", (team_id, as_of)).fetchone()
        captured = row[0] if row else captured

    players = []
    minutes = 0.0
    for row in conn.execute(
            "SELECT i.player_name, i.status, i.detail, i.short_comment, "
            "       i.reported_at, "
            "       (SELECT s.minutes FROM nba_player_stats s "
            "         WHERE s.player_id = i.player_id AND s.gp >= 5 "
            "         ORDER BY s.season DESC, s.minutes DESC LIMIT 1) AS minutes "
            "FROM nba_injuries i WHERE i.team_id = ? AND i.captured_at = ? "
            "ORDER BY i.status, i.player_name", (team_id, captured)):
        weight = STATUS_WEIGHT.get((row["status"] or "").lower(), 0.0)
        value = row["minutes"] or 0.0
        minutes += weight * value
        players.append({**dict(row), "minutes": value, "weight": weight})
    return {"captured_at": captured, "players": players,
            "minutes_unavailable": round(minutes, 1)}


def roster_overview(conn: sqlite3.Connection, team_id: int,
                    season: str = ROSTER_SEASON) -> list[dict]:
    """Roster with the latest season's production, ordered by minutes.

    Production is joined on the player, not on the team: a summer signing's
    most recent evidence was earned somewhere else, and hiding it would make
    every new arrival look like a rookie. `stats_team` says where it came from.
    """
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(
        "WITH latest AS ("
        "  SELECT *, ROW_NUMBER() OVER ("
        "    PARTITION BY player_id ORDER BY season DESC, minutes DESC) AS rn "
        "  FROM nba_player_stats WHERE gp >= 5"
        ") "
        "SELECT r.player_id, p.full_name, r.jersey, r.position, r.age, "
        "       r.experience, r.school, "
        "       s.season AS stats_season, s.team_abbr AS stats_team, "
        "       s.gp, s.minutes, s.pts, s.reb, s.ast, s.stl, s.blk, s.tov, "
        "       s.fg3m, s.ts_pct, s.usage_proxy "
        "FROM nba_rosters r JOIN nba_players p ON p.player_id = r.player_id "
        "LEFT JOIN latest s ON s.player_id = r.player_id AND s.rn = 1 "
        "WHERE r.season = ? AND r.team_id = ? AND r.active = 1 "
        "ORDER BY COALESCE(s.minutes, 0) DESC, p.full_name", (season, team_id))]


def status(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row
    jobs = [dict(r) for r in conn.execute(
        "SELECT job, status, started_at, finished_at, rows, error "
        "FROM nba_sync_log ORDER BY id DESC LIMIT 12")]
    counts = {
        "players": conn.execute("SELECT COUNT(*) FROM nba_players").fetchone()[0],
        "rosters": conn.execute(
            "SELECT COUNT(*) FROM nba_rosters WHERE active = 1").fetchone()[0],
        "rostered_teams": conn.execute(
            "SELECT COUNT(DISTINCT team_id) FROM nba_rosters "
            "WHERE active = 1").fetchone()[0],
        "player_stat_rows": conn.execute(
            "SELECT COUNT(*) FROM nba_player_stats").fetchone()[0],
        "injury_snapshots": conn.execute(
            "SELECT COUNT(DISTINCT captured_at) FROM nba_injuries").fetchone()[0],
        "injuries_latest": conn.execute(
            "SELECT COUNT(*) FROM nba_injuries WHERE captured_at = "
            "(SELECT MAX(captured_at) FROM nba_injuries)").fetchone()[0],
    }
    return {"counts": counts, "jobs": jobs}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Sync NBA rosters and availability.")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--rosters", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--injuries", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--season", default=ROSTER_SEASON)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        ensure_tables(conn)
        if args.status:
            report = status(conn)
            print(json.dumps(report, indent=2))
            return 0
        if args.rosters:
            print("Syncing official rosters ...")
            print("  players:", sync_rosters(conn, args.season))
        if args.stats:
            print("Syncing player production ...")
            print("  rows:", sync_player_stats(conn))
        if args.injuries:
            print("Snapshotting availability ...")
            print("  entries:", sync_injuries(conn))
        if not (args.rosters or args.stats or args.injuries):
            parser.print_help()
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
