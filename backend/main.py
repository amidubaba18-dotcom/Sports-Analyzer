"""FastAPI backend for Sports Analyzer.

This is a thin HTTP wrapper around the Python statistical engine in
`scripts/`. It performs NO statistics of its own - it validates input, calls
`stats.py`, and returns JSON shaped for the frontend.

Three separate concepts live here (this separation is the core design rule):

  * Historical matches  - the `matches` table, what the engine learns from.
  * Published matches   - the `published_matches` table, what users may see.
  * Manual stats        - the `manual_stats` table, hand-entered values that
                          overlay the CSV columns without overwriting them.

A match being published does NOT put it into the historical dataset. A match
being in the historical dataset does NOT put it on the user dashboard.

Run:
    cd backend
    ..\\.venv\\Scripts\\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8001
"""
from __future__ import annotations

import hmac
import os
import json
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

BASE_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = BASE_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import pandas as pd  # noqa: E402

import stats  # noqa: E402
import export_data  # noqa: E402  (DB -> CSV, used by the admin Data view)
import fetch_csvs  # noqa: E402  (provider -> data/raw, used by the admin Data view)
import fixtures  # noqa: E402  (provider -> available_fixtures, the forward calendar)
import import_data  # noqa: E402  (CSV -> DB, used by the admin Data view)
import migrate_schema  # noqa: E402  (side tables; the app must be self-sufficient)
import predict  # noqa: E402  (the measured prediction board)
import nba_fetch  # noqa: E402  (NBA provider -> nba_games / nba_fixtures)
import nba_fixtures  # noqa: E402  (NBA publish gate and dashboard queries)
import nba_people  # noqa: E402  (NBA rosters, player production, availability)
import nba_stats  # noqa: E402  (the NBA engine)
from nba_schema import ensure_schema as ensure_nba_schema  # noqa: E402
from seasons import normalize_season  # noqa: E402
from stats import (  # noqa: E402
    DEFAULT_MARKET_LINES,
    MANUAL_STAT_COLUMNS,
    PROFILE_STATS,
    STAT_COLUMNS,
    analyze_market,
    find_teams,
    get_h2h_matches,
    get_h2h_summary,
    get_league_average,
    get_matches_by_date,
    get_team_away_averages,
    get_team_home_averages,
    get_team_profile,
    get_team_recent_averages,
    get_team_recent_matches,
    get_upcoming_matches,
    list_leagues,
    list_markets,
)

DB_PATH = Path(os.environ.get("SPORTS_ANALYZER_DB", BASE_DIR / "data" / "sports_analyzer.db"))
APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()

# CORS is only needed when the frontend is served from a different origin.
# The production image serves the UI from this same process, so the safe
# default is no cross-origin browser access at all. Set ALLOWED_ORIGINS to a
# comma-separated list only when a separate frontend host is intentional.
_allowed = [o.strip().rstrip("/") for o in
            os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if not _allowed and APP_ENV != "production":
    _allowed = ["http://127.0.0.1:8001", "http://localhost:8001"]

app = FastAPI(
    title="Sports Analyzer API",
    version="2.0.0",
    description="Evidence-based football statistics. Reports support and sample size; never a bet.",
    docs_url=None if APP_ENV == "production" else "/docs",
    redoc_url=None,
    openapi_url=None if APP_ENV == "production" else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Accept", "Content-Type", "X-Admin-Token"],
)


def _admin_supplied(request: Request) -> str:
    """Read the admin token from the explicit header or a Bearer header."""
    token = request.headers.get("x-admin-token", "").strip()
    if token:
        return token
    auth = request.headers.get("authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


@app.middleware("http")
async def _production_guard(request: Request, call_next):
    """Fail closed for admin writes and add basic response hardening.

    The public read API and the static UI remain open. Every `/admin` API
    call requires ADMIN_TOKEN when it is configured. In production the app
    refuses to start without one, so a missing environment variable cannot
    silently publish the admin panel.
    """
    path = request.url.path
    protected = (
        path.startswith("/admin")
        or path == "/nba/publish" or path.startswith("/nba/publish/")
        or path == "/nba/refresh" or path.startswith("/nba/refresh/")
    )
    if protected and request.method != "OPTIONS":
        if ADMIN_TOKEN:
            supplied = _admin_supplied(request)
            if not hmac.compare_digest(supplied, ADMIN_TOKEN):
                return JSONResponse(
                    {"detail": "Admin authentication required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        elif APP_ENV == "production":
            return JSONResponse(
                {"detail": "Admin access is not configured"},
                status_code=503,
            )

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy",
                                "camera=(), microphone=(), geolocation=()")
    if protected:
        response.headers.setdefault("Cache-Control", "no-store")
    if request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.on_event("startup")
def _require_production_admin_token() -> None:
    if APP_ENV == "production" and not ADMIN_TOKEN:
        raise RuntimeError(
            "ADMIN_TOKEN must be set when APP_ENV=production; refusing to "
            "expose write endpoints without authentication."
        )


# --------------------------------------------------------------------------
# Connection handling
# --------------------------------------------------------------------------
@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """Open a connection for the duration of a request and always close it.

    The `with` statement + contextmanager means an exception still closes the
    connection; the old code leaked connections on the error path.
    """
    # One process owns the schedulers, but an HTTP request and a daemon
    # refresh can still overlap. WAL (set at startup) plus a generous busy
    # timeout avoids "database is locked" during that hand-off.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=15000")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def rows_to_dicts(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def parse_markets(raw: str | None) -> list[str]:
    if not raw:
        return list(STAT_COLUMNS)
    markets = [m.strip() for m in raw.split(",") if m.strip()]
    unknown = [m for m in markets if m not in STAT_COLUMNS]
    if unknown:
        raise HTTPException(400, f"unknown market(s): {', '.join(unknown)}")
    return markets


def parse_lines(raw: str | None) -> dict[str, float]:
    """Parse 'goals=2.5,corners=10.5' into {'goals': 2.5, 'corners': 10.5}."""
    out: dict[str, float] = {}
    if not raw:
        return out
    for part in raw.split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise HTTPException(400, f"bad line '{part}', expected market=value")
        key, value = part.split("=", 1)
        key = key.strip()
        if key not in STAT_COLUMNS:
            raise HTTPException(400, f"unknown market in line: {key}")
        try:
            out[key] = float(value)
        except ValueError:
            raise HTTPException(400, f"bad number for {key}: {value}")
    return out


def parse_weights(raw: str | None) -> dict[str, float] | None:
    """Parse 'home_away=0.5,recent_form=0.3,h2h=0.2'. Must sum to ~1."""
    if not raw:
        return None
    out: dict[str, float] = {}
    for part in raw.split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise HTTPException(400, f"bad weight '{part}', expected key=value")
        key, value = part.split("=", 1)
        key = key.strip()
        if key not in ("home_away", "recent_form", "h2h"):
            raise HTTPException(400, f"unknown weight key: {key}")
        try:
            out[key] = float(value)
        except ValueError:
            raise HTTPException(400, f"bad number for weight {key}: {value}")
    if not out:
        return None
    total = sum(out.values())
    if abs(total - 1.0) > 0.001:
        raise HTTPException(400, f"weights must sum to 1.0 (got {total})")
    if len(out) != 3:
        raise HTTPException(400, "provide all three weights or none")
    return out


def shape_analysis(result: dict[str, Any]) -> dict[str, Any]:
    """Turn the engine's flat dict into the nested shape the UI consumes.

    The UI draws three source rows, each needing a value, how much it is trusted
    and its sample size. The engine already returns all of that; this groups it.

    `reliability` (not `weight`) is what the UI shows now: n/(n+k), how much of
    a source survives shrinkage. It is deliberately NOT a set of percentages
    that add to 100, because under the shrunk estimator the sources are not
    competing for a fixed share of the vote - a source with five matches is
    simply mostly the league average, and the number says so.
    """
    reliability = result.get("reliability") or {}
    samples = result.get("sample_sizes") or {}
    sources = {}
    # A typed team average feeds the home/away row, so that row must stop
    # claiming to be something the database counted.
    home_away_label = "Home / Away average"
    if result.get("profile_used"):
        home_away_label = "Home / Away average (partly typed, not measured)"
    mapping = {
        "home_away": (home_away_label, "home_away_combined", "home_away_sample"),
        "recent_form": ("Recent form (last 5)", "recent_form_combined", "recent_form_sample"),
        "h2h": ("Head to head", "h2h_combined", "h2h_sample"),
    }
    for key, (label, value_key, sample_key) in mapping.items():
        sources[key] = {
            "label": label,
            "value": result.get(value_key),
            "reliability": reliability.get(key),
            "sample": result.get(sample_key, samples.get(key, 0)),
        }

    return {
        "market": result.get("market") or result.get("stat"),
        "line": result.get("line"),
        "line_source": result.get("line_source", "user"),
        "line_reference": result.get("line_reference"),
        "method": result.get("method", "shrunk"),
        "method_note": result.get("method_note"),
        "shrink_k": result.get("shrink_k"),
        "league_reference": result.get("league_reference"),
        "weighted_avg": result.get("weighted_avg"),
        "supported": result.get("supported"),
        "evidence_points": result.get("evidence_points", 0),
        "low_confidence": result.get("low_confidence", True),
        "sources": sources,
        "sources_used": result.get("sources_used", []),
        "note": result.get("note"),
        # Whether a league average was actually available to shrink toward. When
        # it was not, the number is the team data alone, and saying "rest league
        # average" would describe a computation that did not happen.
        "has_league_reference": result.get("league_reference") is not None,
        "profile_used": result.get("profile_used", False),
        "profile_as_of": result.get("profile_as_of"),
        "profile_teams": result.get("profile_teams"),
        "profile_note": result.get("profile_note"),
        "season_home_matches": result.get("season_home_matches", 0),
        "season_away_matches": result.get("season_away_matches", 0),
        "h2h_matches": result.get("h2h_matches", 0),
    }


def run_analysis(conn, home_team, away_team, markets, lines, weights,
                 before_date=None, exclude_match_id=None, recent_n=5,
                 league=None, season=None) -> list[dict[str, Any]]:
    results = []
    for market in markets:
        line = lines.get(market)  # None -> engine derives a fair line
        try:
            raw = analyze_market(
                conn, home_team, away_team, f"avg_{market}", line,
                recent_n=recent_n, before_date=before_date,
                exclude_match_id=exclude_match_id, weights=weights,
                league=league, season=season)
        except Exception as exc:
            results.append({
                "market": market,
                "error": f"{type(exc).__name__}: {exc}",
                "weighted_avg": None,
                "supported": None,
            })
            continue
        raw["market"] = market
        results.append(shape_analysis(raw))
    return results


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------
class ManualStatsIn(BaseModel):
    home_tackles: float | None = None
    away_tackles: float | None = None
    home_throw_ins: float | None = None
    away_throw_ins: float | None = None
    home_aerial_duels: float | None = None
    away_aerial_duels: float | None = None
    home_offsides: float | None = None
    away_offsides: float | None = None
    home_interceptions: float | None = None
    away_interceptions: float | None = None
    home_goal_kicks: float | None = None
    away_goal_kicks: float | None = None
    home_saves: float | None = None
    away_saves: float | None = None
    home_substitutions: float | None = None
    away_substitutions: float | None = None


class PublishedMatchIn(BaseModel):
    league: str
    home_team: str
    away_team: str
    date: str
    kickoff: str | None = None
    season: str | None = None
    markets: list[str] | None = None
    status: str = "published"

    @field_validator("date")
    @classmethod
    def valid_date(cls, v: str) -> str:
        try:
            _date.fromisoformat(v)
        except ValueError:
            raise ValueError(f"date must be YYYY-MM-DD, got {v!r}")
        return v

    @field_validator("home_team", "away_team")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("team name cannot be blank")
        return v.strip()


class PublishedMatchUpdate(BaseModel):
    league: str | None = None
    home_team: str | None = None
    away_team: str | None = None
    date: str | None = None
    kickoff: str | None = None
    season: str | None = None
    markets: list[str] | None = None
    status: str | None = None
    sort_order: int | None = None


class MatchIn(BaseModel):
    """Insert a completed match (or a scheduled fixture) into the historical DB."""
    date: str
    league: str
    home_team: str
    away_team: str
    season: str | None = None
    status: str | None = None
    home_goals: int | None = None
    away_goals: int | None = None

    @field_validator("date")
    @classmethod
    def valid_date(cls, v: str) -> str:
        try:
            _date.fromisoformat(v)
        except ValueError:
            raise ValueError(f"date must be YYYY-MM-DD, got {v!r}")
        return v


class TeamProfileIn(BaseModel):
    """A team's per-game averages, typed by the admin from an external source.

    The values are PER GAME FOR ONE TEAM, because that is what a stats site
    shows and what the engine would otherwise derive from that team's matches.
    The engine adds the two teams together to get the match total, so typing a
    match total here would double it.

    `le` is a typo guard, not a real limit: no per-game average in these eight
    markets is anywhere near 100, so a value above it is a mistyped match total
    or a slipped decimal.
    """
    league: str
    team: str
    season: str | None = None
    as_of: str | None = None
    tackles: float | None = Field(None, ge=0, le=100)
    throw_ins: float | None = Field(None, ge=0, le=100)
    aerial_duels: float | None = Field(None, ge=0, le=100)
    offsides: float | None = Field(None, ge=0, le=100)
    interceptions: float | None = Field(None, ge=0, le=100)
    goal_kicks: float | None = Field(None, ge=0, le=100)
    saves: float | None = Field(None, ge=0, le=100)
    substitutions: float | None = Field(None, ge=0, le=100)
    note: str | None = None

    @field_validator("as_of")
    @classmethod
    def valid_as_of(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            _date.fromisoformat(v)
        except ValueError:
            raise ValueError(f"as_of must be YYYY-MM-DD, got {v!r}")
        return v

    @field_validator("team")
    @classmethod
    def team_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("team cannot be blank")
        return v.strip()


class DownloadIn(BaseModel):
    season: str | None = None
    import_after: bool = True


class AnalyzeIn(BaseModel):
    home_team: str
    away_team: str
    date: str | None = None
    league: str | None = None
    season: str | None = None
    markets: list[str] | None = None
    lines: dict[str, float] | None = None
    weights: dict[str, float] | None = None
    recent_n: int = Field(5, ge=1, le=30)


class FetchFixturesIn(BaseModel):
    lookahead_days: int = Field(fixtures.LOOKAHEAD_DAYS, ge=1, le=30)
    publish: bool = True


class PublishFixturesIn(BaseModel):
    ids: list[int] = Field(min_length=1)


class FixtureSettingsIn(BaseModel):
    league: str
    auto_publish: bool


class TeamAliasIn(BaseModel):
    provider_name: str = Field(min_length=1, max_length=160)
    team: str = Field(min_length=1, max_length=160)


# --------------------------------------------------------------------------
# Public: reference data
# --------------------------------------------------------------------------
@app.get("/api")
def root():
    return {"status": "ok", "version": app.version}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Browsers request /favicon.ico automatically; answering keeps the log clean."""
    return Response(status_code=204)


@app.get("/health")
def health():
    with db() as conn:
        matches = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        published = conn.execute("SELECT COUNT(*) FROM published_matches").fetchone()[0]
    return {
        "status": "ok",
        "historical_matches": matches,
        "published_matches": published,
        "admin_auth_required": bool(ADMIN_TOKEN),
    }


@app.get("/stats")
def get_stats():
    """The configured markets and their default lines."""
    return {"markets": list_markets()}


@app.get("/leagues")
def get_leagues():
    with db() as conn:
        return {"leagues": list_leagues(conn)}


@app.get("/markets/{market}/base-rate")
def market_base_rate(
    market: str,
    line: float,
    league: str | None = None,
    season: str | None = None,
    before: str | None = None,
):
    """How often the combined total has historically gone OVER a line.

    This is a MEASURED FREQUENCY, not a prediction. It answers "in the matches
    we actually have, how often did this clear the line?", with the sample size
    attached. It is the only kind of percentage this project shows until a
    probability model has been calibrated out-of-sample - a fabricated
    "78% likely" would be exactly the fake confidence meter the spec forbids.

    Strictly before `before`, so it can never peek at the future, and any match
    landing exactly on the line is excluded as a push rather than counted as an
    under.
    """
    if market not in STAT_COLUMNS:
        raise HTTPException(400, f"unknown market: {market}")
    home_col, away_col = STAT_COLUMNS[market]

    clause = [f"{home_col} IS NOT NULL", f"{away_col} IS NOT NULL",
              "home_goals IS NOT NULL"]
    params: list[Any] = []
    if league:
        clause.append("league = ?")
        params.append(league)
    if season:
        clause.append("season = ?")
        params.append(season)
    if before:
        clause.append("date < ?")
        params.append(before)

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT ({home_col} + {away_col}) AS total FROM matches "
            f"WHERE {' AND '.join(clause)}", params)
        totals = [float(r[0]) for r in cur.fetchall() if r[0] is not None]

    scored = [t for t in totals if abs(t - line) >= 1e-9]
    over = sum(1 for t in scored if t > line)
    return {
        "market": market,
        "line": line,
        "league": league,
        "season": season,
        "before": before,
        "over": over,
        "total": len(scored),
        "pushes_excluded": len(totals) - len(scored),
        "rate": round(over / len(scored), 4) if scored else None,
        "average": round(sum(totals) / len(totals), 2) if totals else None,
    }


@app.get("/teams/find")
def teams_find(q: str = Query(..., min_length=1), league: str | None = None):
    with db() as conn:
        rows = find_teams(conn, q, league)
    return {"teams": [{"team": r[0], "league": r[1]} for r in rows]}


# --------------------------------------------------------------------------
# Public: historical matches
# --------------------------------------------------------------------------
@app.get("/matches/today")
def matches_today():
    with db() as conn:
        return get_matches_by_date(conn).to_dict(orient="records")


@app.get("/matches/date/{date_str}")
def matches_date(date_str: str):
    with db() as conn:
        return get_matches_by_date(conn, date_str).to_dict(orient="records")


@app.get("/fixtures")
def upcoming_fixtures(from_date: str | None = None, limit: int = 50):
    """Scheduled matches with no score yet."""
    with db() as conn:
        df = get_upcoming_matches(conn, from_date, limit)
    return df.to_dict(orient="records")


@app.get("/matches/{match_id}")
def get_match(match_id: int):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM matches WHERE id = ?", (match_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "Match not found")
    return dict(row)


@app.get("/matches/{match_id}/analysis")
def analysis_for_match(
    match_id: int,
    markets: str | None = None,
    lines: str | None = None,
    weights: str | None = None,
    recent_n: int = Query(5, ge=1, le=30),
):
    """Analyse a stored match, using only data strictly before its kickoff.

    This is the cutoff-aware path: the match can never see itself or anything
    after it.
    """
    market_list = parse_markets(markets)
    line_map = parse_lines(lines)
    weight_map = parse_weights(weights)

    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM matches WHERE id = ?", (match_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "Match not found")
        row = dict(row)
        results = run_analysis(
            conn, row["home_team"], row["away_team"], market_list, line_map,
            weight_map, before_date=row["date"], exclude_match_id=match_id,
            recent_n=recent_n, league=row.get("league"), season=row.get("season"))

    return {
        "match": {
            "id": row["id"], "date": row["date"], "league": row["league"],
            "season": row["season"], "status": row.get("status"),
            "home_team": row["home_team"], "away_team": row["away_team"],
            "home_goals": row.get("home_goals"), "away_goals": row.get("away_goals"),
        },
        "cutoff": row["date"],
        "analysis": results,
    }


@app.post("/analyze")
def analyze(body: AnalyzeIn):
    """Ad-hoc analysis by team names, no stored match required.

    Used for a fixture that is published but not yet in the historical table.
    """
    markets = body.markets or list(STAT_COLUMNS)
    unknown = [m for m in markets if m not in STAT_COLUMNS]
    if unknown:
        raise HTTPException(400, f"unknown market(s): {', '.join(unknown)}")
    lines = body.lines or {}
    season = normalize_season(body.season, body.date)

    with db() as conn:
        results = run_analysis(
            conn, body.home_team, body.away_team, markets, lines, body.weights,
            before_date=body.date, league=body.league, season=season,
            recent_n=body.recent_n)

    return {
        "match": {
            "home_team": body.home_team, "away_team": body.away_team,
            "date": body.date, "league": body.league, "season": season,
        },
        "cutoff": body.date,
        "analysis": results,
    }


# --------------------------------------------------------------------------
# Public: published matches (the user dashboard)
# --------------------------------------------------------------------------
def _published_row(conn, published_id: int) -> dict[str, Any]:
    cur = conn.cursor()
    cur.execute("SELECT * FROM published_matches WHERE id = ?", (published_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "Published match not found")
    return dict(row)


@app.get("/published/matches")
def published_matches(date: str | None = None):
    """Only admin-published matches. Never the whole historical database."""
    target = date or _date.today().isoformat()
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM published_matches WHERE date = ? AND status != 'hidden' "
            "ORDER BY sort_order ASC, kickoff ASC, id ASC", (target,))
        return rows_to_dicts(cur)


@app.get("/published/dates")
def published_dates(limit: int = 120):
    """The days that actually have published fixtures, oldest first.

    The dashboard shows one day at a time and starts at today. Without this list
    a fixture published for another day is invisible: the visitor reads "nothing
    published" while a match sits in the database. The page uses it to fall back
    to the nearest published day, and says so on screen.

    Only `published` counts — a hidden fixture is not something to point at.
    """
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT date, COUNT(*) AS matches FROM published_matches "
            "WHERE status = 'published' GROUP BY date ORDER BY date ASC LIMIT ?",
            (limit,))
        days = [{"date": r["date"], "matches": r["matches"]}
                for r in cur.fetchall()]
    return {"today": _date.today().isoformat(), "dates": days}


@app.get("/published/matches/{published_id}")
def published_match(published_id: int):
    with db() as conn:
        return _published_row(conn, published_id)


@app.get("/published/matches/{published_id}/analysis")
def published_analysis(
    published_id: int,
    markets: str | None = None,
    lines: str | None = None,
    weights: str | None = None,
    recent_n: int = Query(5, ge=1, le=30),
):
    """Analyse a published fixture from team names + date.

    Crucially this does NOT look up a historical match id: a published fixture
    is its own thing and may have no row in `matches` at all. The engine is
    given the fixture's date as a cutoff, so no future data is used.
    """
    with db() as conn:
        row = _published_row(conn, published_id)

        market_list = parse_markets(markets)
        if not markets and row.get("markets"):
            # The admin can store a per-match market list as a JSON array or CSV.
            market_list = [m.strip() for m in str(row["markets"]).replace("[", "")
                           .replace("]", "").replace('"', "").split(",") if m.strip()]
            market_list = [m for m in market_list if m in STAT_COLUMNS] or list(STAT_COLUMNS)

        line_map = parse_lines(lines)
        weight_map = parse_weights(weights)
        season = normalize_season(row.get("season"), row["date"])

        results = run_analysis(
            conn, row["home_team"], row["away_team"], market_list, line_map,
            weight_map, before_date=row["date"], league=row["league"],
            season=season, recent_n=recent_n)

    return {
        "match": {
            "id": row["id"], "date": row["date"], "kickoff": row.get("kickoff"),
            "league": row["league"], "season": season,
            "home_team": row["home_team"], "away_team": row["away_team"],
        },
        "cutoff": row["date"],
        "analysis": results,
    }


@app.get("/published/matches/{published_id}/evidence")
def published_evidence(published_id: int, stat: str = "corners", recent_n: int = 5):
    """The raw inputs behind an analysis, for the Form and H2H tabs.

    Returns the same season averages, recent matches and head-to-head meetings
    the engine used, so the UI can show *why* a number came out the way it did
    rather than just asserting it.
    """
    if stat not in STAT_COLUMNS:
        raise HTTPException(400, f"unknown stat: {stat}")

    with db() as conn:
        row = _published_row(conn, published_id)
        home, away, cutoff = row["home_team"], row["away_team"], row["date"]

        home_season = get_team_home_averages(conn, home, before_date=cutoff)
        away_season = get_team_away_averages(conn, away, before_date=cutoff)
        home_recent = get_team_recent_averages(conn, home, n=recent_n, before_date=cutoff)
        away_recent = get_team_recent_averages(conn, away, n=recent_n, before_date=cutoff)
        h2h = get_h2h_summary(conn, home, away, before_date=cutoff)

        home_rows = get_team_recent_matches(conn, home, n=recent_n, before_date=cutoff)
        away_rows = get_team_recent_matches(conn, away, n=recent_n, before_date=cutoff)
        h2h_rows = get_h2h_matches(conn, home, away, before_date=cutoff)

        home_col, away_col = STAT_COLUMNS[stat]

        def match_rows(df):
            out = []
            for _, r in df.iterrows():
                hv, av = r[home_col], r[away_col]
                out.append({
                    "id": int(r["id"]),
                    "date": r["date"],
                    "home_team": r["home_team"],
                    "away_team": r["away_team"],
                    "home_value": None if pd.isna(hv) else float(hv),
                    "away_value": None if pd.isna(av) else float(av),
                    "total": None if (pd.isna(hv) or pd.isna(av)) else float(hv) + float(av),
                })
            return out

    key = f"avg_{stat}"
    return {
        "stat": stat,
        "cutoff": cutoff,
        "home_team": home,
        "away_team": away,
        "home_season": {"avg": home_season.get(key), "matches": home_season.get("matches_played")},
        "away_season": {"avg": away_season.get(key), "matches": away_season.get("matches_played")},
        "home_recent": {"avg": home_recent.get(key), "matches": home_recent.get("matches_played")},
        "away_recent": {"avg": away_recent.get(key), "matches": away_recent.get("matches_played")},
        "h2h": {"avg": h2h.get(key), "matches": h2h.get("matches_played")},
        "home_recent_rows": match_rows(home_rows),
        "away_recent_rows": match_rows(away_rows),
        "h2h_rows": match_rows(h2h_rows),
    }


# --------------------------------------------------------------------------
# Admin: published matches
# --------------------------------------------------------------------------
@app.get("/admin/auth")
def admin_auth():
    """Cheap token check used by the admin sign-in card.

    The production guard has already validated the token before this handler
    runs; reaching it means the caller is authorized.
    """
    return {"ok": True}


@app.get("/admin/published/matches")
def admin_list_published(date: str | None = None, limit: int = 100):
    with db() as conn:
        cur = conn.cursor()
        if date:
            cur.execute("SELECT * FROM published_matches WHERE date = ? "
                        "ORDER BY sort_order ASC, id ASC", (date,))
        else:
            cur.execute("SELECT * FROM published_matches ORDER BY date DESC, "
                        "sort_order ASC LIMIT ?", (limit,))
        return rows_to_dicts(cur)


@app.post("/admin/published/matches", status_code=201)
def admin_create_published(body: PublishedMatchIn):
    season = normalize_season(body.season, body.date)
    import json
    markets = json.dumps(body.markets) if body.markets else None

    with db() as conn:
        cur = conn.cursor()
        # Duplicate protection: same league + date + teams = same fixture.
        cur.execute(
            "SELECT id FROM published_matches WHERE date = ? AND lower(home_team) = lower(?) "
            "AND lower(away_team) = lower(?)", (body.date, body.home_team, body.away_team))
        existing = cur.fetchone()
        if existing:
            raise HTTPException(409, {
                "message": f"{body.home_team} vs {body.away_team} already exists on {body.date}",
                "existing_id": existing[0],
            })
        cur.execute(
            "INSERT INTO published_matches (league, season, date, kickoff, home_team, "
            "away_team, markets, status, sort_order) VALUES (?,?,?,?,?,?,?,?,"
            "COALESCE((SELECT MAX(sort_order)+1 FROM published_matches), 0))",
            (body.league, season, body.date, body.kickoff, body.home_team,
             body.away_team, markets, body.status))
        conn.commit()
        new_id = cur.lastrowid
    return {"id": new_id, "season": season}


@app.put("/admin/published/matches/{published_id}")
def admin_update_published(published_id: int, body: PublishedMatchUpdate):
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(400, "no fields to update")
    if "markets" in fields:
        import json
        fields["markets"] = json.dumps(fields["markets"])

    sets = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [published_id]
    with db() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE published_matches SET {sets}, updated_at = datetime('now') "
                    f"WHERE id = ?", values)
        if cur.rowcount == 0:
            raise HTTPException(404, "Published match not found")
        conn.commit()
    return {"ok": True, "id": published_id}


@app.delete("/admin/published/matches/{published_id}")
def admin_delete_published(published_id: int):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM published_matches WHERE id = ?", (published_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Published match not found")
        conn.commit()
    return {"ok": True, "deleted": published_id}


# --------------------------------------------------------------------------
# Admin: historical matches
# --------------------------------------------------------------------------
@app.post("/admin/matches", status_code=201)
def admin_create_match(body: MatchIn):
    """Insert a match or a fixture. Now possible because scores are nullable."""
    season = normalize_season(body.season, body.date)
    status = body.status or ("completed" if body.home_goals is not None else "scheduled")

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM matches WHERE date = ? AND lower(home_team) = lower(?) "
            "AND lower(away_team) = lower(?)", (body.date, body.home_team, body.away_team))
        existing = cur.fetchone()
        if existing:
            raise HTTPException(409, {
                "message": f"{body.home_team} vs {body.away_team} already exists on {body.date}",
                "existing_id": existing[0],
            })
        cur.execute(
            "INSERT INTO matches (date, league, season, status, source, home_team, "
            "away_team, home_goals, away_goals) VALUES (?,?,?,?,?,?,?,?,?)",
            (body.date, body.league, season, status, "manual", body.home_team,
             body.away_team, body.home_goals, body.away_goals))
        conn.commit()
        new_id = cur.lastrowid
    return {"id": new_id, "season": season, "status": status}


@app.get("/admin/matches/missing-manual")
def admin_missing_manual(limit: int = 50, league: str | None = None,
                         since: str | None = None, until: str | None = None):
    """Completed matches with no manual-stat entry yet.

    Filterable by league and date so this can be worked a week at a time -
    "what did I not type in since last Sunday" - instead of scrolling a global
    list of everything ever played. `total` is the full count for the window, so
    the list can be honest about how much is left beyond `limit`.

    Manual stats attach to a match, and a match only exists here once the CSV
    that contains it has been imported. So the order matters: refresh the CSVs
    first, then work this list.
    """
    null_checks = " OR ".join(f"m.{c} IS NULL" for c in MANUAL_STAT_COLUMNS)

    where = ["m.status = 'completed'",
             f"(ms.match_id IS NULL OR ({null_checks}))"]
    params: list[Any] = []
    if league:
        where.append("m.league = ?")
        params.append(league)
    if since:
        where.append("m.date >= ?")
        params.append(since)
    if until:
        where.append("m.date <= ?")
        params.append(until)
    clause = " AND ".join(where)

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT COUNT(*) FROM matches m LEFT JOIN manual_stats ms "
            f"ON ms.match_id = m.id WHERE {clause}", params)
        total = cur.fetchone()[0]

        cur.execute(
            f"SELECT m.id, m.date, m.league, m.season, m.home_team, m.away_team "
            f"FROM matches m LEFT JOIN manual_stats ms ON ms.match_id = m.id "
            f"WHERE {clause} ORDER BY m.date DESC, m.id DESC LIMIT ?",
            params + [limit])
        matches = rows_to_dicts(cur)

        cur.execute("SELECT DISTINCT league FROM matches WHERE status = 'completed' "
                    "ORDER BY league")
        leagues = [r[0] for r in cur.fetchall()]

    return {"total": total, "matches": matches, "leagues": leagues}


@app.get("/admin/matches/{match_id}/manual-stats")
def admin_get_manual(match_id: int):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM manual_stats WHERE match_id = ?", (match_id,))
        row = cur.fetchone()
    return dict(row) if row else {"match_id": match_id}


@app.put("/admin/matches/{match_id}/manual-stats")
def admin_put_manual(match_id: int, body: ManualStatsIn):
    """Upsert hand-entered stats. Overlays the CSV columns; never overwrites them."""
    values = body.model_dump()
    cols = ["match_id"] + list(values.keys()) + ["updated_at"]
    placeholders = ", ".join(["?"] * (len(values) + 1)) + ", datetime('now')"
    updates = ", ".join(f"{k} = excluded.{k}" for k in values)

    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM matches WHERE id = ?", (match_id,))
        if cur.fetchone() is None:
            raise HTTPException(404, "Match not found")
        cur.execute(
            f"INSERT INTO manual_stats ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(match_id) DO UPDATE SET {updates}, updated_at = datetime('now')",
            [match_id] + list(values.values()))
        conn.commit()
    return {"ok": True, "match_id": match_id}


@app.delete("/admin/matches/{match_id}/manual-stats")
def admin_delete_manual(match_id: int):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM manual_stats WHERE match_id = ?", (match_id,))
        conn.commit()
    return {"ok": True, "deleted": match_id}


@app.delete("/admin/matches/{match_id}")
def admin_delete_match(match_id: int):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM manual_stats WHERE match_id = ?", (match_id,))
        cur.execute("DELETE FROM matches WHERE id = ?", (match_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Match not found")
        conn.commit()
    return {"ok": True, "deleted": match_id}


# --------------------------------------------------------------------------
# Admin: team profiles - typed per-team averages for the eight markets no CSV
# publishes. The engine only consults one where nothing was measured, so this
# is the only way those markets ever carry data, and nothing here can displace
# a counted number. See stats.PROFILE_STATS / stats.get_team_profile.
# --------------------------------------------------------------------------
def _profile_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["values"] = {stat: out.get(stat) for stat in PROFILE_STATS}
    return out


@app.get("/admin/team-profiles")
def admin_list_profiles(league: str | None = None, season: str | None = None,
                        search: str | None = None, limit: int = 400):
    """Every team in a league/season, with its typed averages where it has them.

    The team list comes from the matches actually stored, so this doubles as
    duplicate protection: a name that already exists cannot be introduced by
    typing a variant by hand.
    """
    # The season defaults to the current one, because a profile is keyed by
    # league + season + team and a missing label would match no existing row.
    canonical = normalize_season(season, _date.today().isoformat())

    with db() as conn:
        cur = conn.cursor()
        clause, params = [], []
        if league:
            clause.append("league = ?")
            params.append(league)
        if canonical:
            clause.append("season = ?")
            params.append(canonical)
        where = f"WHERE {' AND '.join(clause)}" if clause else ""
        cur.execute(
            f"SELECT team, COUNT(*) AS matches, MAX(date) AS last_date FROM ("
            f"  SELECT home_team AS team, date FROM matches {where}"
            f"  UNION ALL"
            f"  SELECT away_team AS team, date FROM matches {where}"
            f") GROUP BY team ORDER BY team", params + params)
        teams = [dict(r) for r in cur.fetchall()]

        if league:
            cur.execute("SELECT * FROM team_profiles WHERE league = ? AND season = ?",
                        (league, canonical))
        else:
            cur.execute("SELECT * FROM team_profiles WHERE season = ?", (canonical,))
        profiles = {}
        for row in cur.fetchall():
            profiles[(row["league"], row["season"], row["team"])] = _profile_to_dict(row)

    for entry in teams:
        entry["profile"] = profiles.pop((league, canonical, entry["team"]), None)

    if search:
        needle = search.lower()
        teams = [t for t in teams if needle in t["team"].lower()]

    total = len(teams)
    teams = teams[:limit]
    return {
        "league": league,
        "season": canonical,
        "total": total,
        # Counted from the values, not from the existence of a row: an empty row
        # is not a filled-in team, and saying it is would inflate the work.
        "with_profile": sum(
            1 for t in teams
            if t["profile"] and any(t["profile"]["values"].get(m) is not None
                                    for m in PROFILE_STATS)),
        "markets": list(PROFILE_STATS),
        "teams": teams,
    }


@app.put("/admin/team-profiles")
def admin_put_profile(body: TeamProfileIn):
    """Create or replace one team's typed averages.

    Returns the stored row. An analysis for a fixture before `as_of` will not
    use it, which is the whole reason the date is required rather than optional:
    an average with no date is an average that could be used to rewrite the past.
    """
    season = normalize_season(body.season, body.as_of)
    if season is None:
        raise HTTPException(400, "Could not determine the season; pass season or as_of.")

    values = body.model_dump()
    as_of = values.pop("as_of") or _date.today().isoformat()
    league = values.pop("league")
    team = values.pop("team")
    values.pop("season")

    # A row with every market blank is not a profile. Storing one would make the
    # "N of M teams filled in" counter report data that does not exist, and it
    # would survive forever because nothing distinguishes it from a real entry.
    if all(values.get(stat) is None for stat in PROFILE_STATS):
        raise HTTPException(
            400, "Nothing to save: every market is blank, so there is no average "
                 "to store. Clear the team instead if that was the intent.")

    columns = ["league", "season", "team", "as_of"] + list(values)
    row_values = [league, season, team, as_of] + list(values.values())
    updates = ", ".join(f"{k} = excluded.{k}" for k in values)
    placeholders = ", ".join(["?"] * len(columns))

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO team_profiles ({', '.join(columns)}) VALUES ({placeholders})"
            f" ON CONFLICT(league, season, team) DO UPDATE SET {updates},"
            f" updated_at = datetime('now')",
            row_values)
        conn.commit()
        cur.execute("SELECT * FROM team_profiles WHERE league = ? AND season = ?"
                    " AND team = ?", (league, season, team))
        stored = _profile_to_dict(cur.fetchone())

    return {"ok": True, "season": season, "profile": stored}


@app.delete("/admin/team-profiles/{profile_id}")
def admin_delete_profile(profile_id: int):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM team_profiles WHERE id = ?", (profile_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Profile not found")
        conn.commit()
    return {"ok": True, "deleted": profile_id}


# --------------------------------------------------------------------------
# Admin: dashboard overview
# --------------------------------------------------------------------------
@app.get("/admin/dashboard")
def admin_dashboard(date: str | None = None):
    target = date or _date.today().isoformat()
    with db() as conn:
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM published_matches WHERE date = ? "
                    "AND status != 'hidden'", (target,))
        published_today = cur.fetchone()[0]

        cur.execute("SELECT league, COUNT(*) FROM published_matches WHERE date = ? "
                    "AND status != 'hidden' GROUP BY league ORDER BY COUNT(*) DESC", (target,))
        by_league = [{"league": r[0], "count": r[1]} for r in cur.fetchall()]

        cur.execute("SELECT COUNT(*) FROM matches WHERE status = 'completed'")
        historical = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM matches WHERE status = 'scheduled'")
        scheduled = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM manual_stats")
        manual_records = cur.fetchone()[0]
        cur.execute("SELECT MAX(date) FROM matches WHERE status = 'completed'")
        latest = cur.fetchone()[0]

        cur.execute("SELECT COUNT(DISTINCT home_team) FROM matches")
        teams = cur.fetchone()[0]

    return {
        "date": target,
        "published_today": published_today,
        "published_by_league": by_league,
        "historical_completed": historical,
        "historical_scheduled": scheduled,
        "manual_stat_records": manual_records,
        "teams": teams,
        "latest_completed_match": latest,
    }


# --------------------------------------------------------------------------
# Admin: dataset freshness + coverage, and the CSV export
#
# The two questions the admin otherwise has to guess at:
#   * is the dataset current with matches that have actually been played?
#   * which markets have any data behind them? (the eight manual-only ones
#     report "not enough data" until numbers are typed in or team profiles are
#     filled - they do not fall back to the league average, because a league
#     average for a market nothing has ever measured is just a guess)
# --------------------------------------------------------------------------
@app.get("/admin/data")
def admin_data():
    today = _date.today()
    with db() as conn:
        cur = conn.cursor()

        cur.execute("SELECT MAX(date) FROM matches WHERE status = 'completed'")
        latest = cur.fetchone()[0]
        days_stale = None
        if latest:
            days_stale = (today - _date.fromisoformat(latest)).days

        cur.execute(
            "SELECT league, season, COUNT(*), MIN(date), MAX(date) FROM matches "
            "WHERE status = 'completed' GROUP BY league, season "
            "ORDER BY league, season")
        leagues = [
            {"league": league, "season": season, "matches": n,
             "first_date": first, "latest_date": last,
             "days_stale": (today - _date.fromisoformat(last)).days if last else None}
            for league, season, n, first, last in cur.fetchall()
        ]

        # How much data actually stands behind each market. This is the number
        # that decides whether an analysis is worth reading.
        coverage = []
        for market, (home_col, away_col) in STAT_COLUMNS.items():
            cur.execute(
                f"SELECT COUNT(*) FROM matches WHERE status = 'completed' "
                f"AND {home_col} IS NOT NULL AND {away_col} IS NOT NULL")
            n = cur.fetchone()[0]
            coverage.append({
                "market": market,
                "label": market.replace("_", " "),
                "matches_with_data": n,
                "default_line": DEFAULT_MARKET_LINES.get(market),
                "input": "manual" if home_col in MANUAL_STAT_COLUMNS else "csv",
            })
        coverage.sort(key=lambda c: c["matches_with_data"])

        cur.execute("SELECT COUNT(*) FROM manual_stats")
        manual_rows = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM manual_stats ms LEFT JOIN matches m "
            "ON m.id = ms.match_id WHERE m.id IS NULL")
        orphaned = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM matches WHERE source = 'manual'")
        manual_matches = cur.fetchone()[0]

    manifest_path = BASE_DIR / "data" / "export" / "manifest.json"
    last_export = None
    if manifest_path.exists():
        try:
            last_export = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            last_export = None

    return {
        "today": today.isoformat(),
        "latest_completed_match": latest,
        "days_stale": days_stale,
        "leagues": leagues,
        "market_coverage": coverage,
        "manual_stats": {
            "records": manual_rows,
            "orphaned": orphaned,
            "hand_added_matches": manual_matches,
        },
        "last_export": last_export,
    }


@app.post("/admin/download-csvs")
def admin_download_csvs(body: DownloadIn | None = None):
    """Fetch the current season's CSVs from football-data.co.uk into data/raw/.

    The other half of "Refresh from CSVs". The download is done in-process by
    `fetch_csvs`, the same module the command line uses, so the button and
    `python scripts/fetch_csvs.py` cannot drift apart.

    A file that does not look like a season CSV is REFUSED, never written over
    an existing one: an outage or a moved URL returns an HTML error page, and
    letting that replace a good file would make the next import sweep delete the
    whole season. That is why `ok` here is false when anything was refused, even
    though the other leagues did arrive.

    Downloading is not importing and not publishing. The files land in
    data/raw/; `import_after` then runs the same import the Refresh button does.
    """
    body = body or DownloadIn()
    if body.season:
        # Stated explicitly, so it is validated rather than second-guessed. A
        # label the caller got wrong must not be quietly replaced with whatever
        # season today happens to fall in.
        season = normalize_season(body.season, None)
        if season is None:
            raise HTTPException(
                400, f"'{body.season}' is not a season label I can read. "
                     f"Use e.g. 2026-27, or omit season for the current one.")
    else:
        season = normalize_season(None, _date.today().isoformat())

    logs: list[str] = []
    try:
        report = fetch_csvs.download_season(season, log=logs.append)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    result: dict[str, Any] = {
        "ok": report["refused"] == 0 and report["downloaded"] > 0,
        "download": report,
        "log": logs,
    }

    if body.import_after and report["downloaded"]:
        import_logs: list[str] = []
        warnings: list[str] = []
        try:
            summary = import_data.import_tree(
                import_data.RAW_DIR, DB_PATH,
                log=import_logs.append, warn=warnings.append)
        except (ValueError, OSError, sqlite3.Error) as exc:
            raise HTTPException(500, f"Downloaded, but the import failed: {exc}")
        with db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
            latest = conn.execute(
                "SELECT MAX(date) FROM matches WHERE status = 'completed'").fetchone()[0]
        result.update({
            "ok": result["ok"] and not warnings,
            "import": summary,
            "import_log": import_logs,
            "warnings": warnings,
            "total_matches": total,
            "latest_completed_match": latest,
        })

    return result


@app.post("/admin/import-csvs")
def admin_import_csvs():
    """Re-read data/raw/ and refresh the historical dataset from it.

    The second half of the weekly step. `POST /admin/download-csvs` fills
    data/raw/ from the provider; this reads whatever is there. Deliberately the
    same call the command line makes (`import_data.import_tree`), so the button
    and the terminal cannot drift into two behaviours.

    Refreshing the dataset is NOT publishing. A new match becomes visible to
    users only when it is published, which is a separate admin action.
    """
    logs: list[str] = []
    warnings: list[str] = []
    try:
        summary = import_data.import_tree(
            import_data.RAW_DIR, DB_PATH,
            log=logs.append, warn=warnings.append)
    except ValueError as exc:
        # A file name that is not a season. Nothing was imported from it.
        raise HTTPException(400, str(exc))
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(500, f"Import failed: {exc}")

    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM matches")
        total = cur.fetchone()[0]
        cur.execute("SELECT MAX(date) FROM matches WHERE status = 'completed'")
        latest = cur.fetchone()[0]

    return {
        "ok": not warnings,
        "summary": summary,
        "total_matches": total,
        "latest_completed_match": latest,
        "log": logs,
        "warnings": warnings,
    }


@app.post("/admin/export")
def admin_export():
    """Write every table out as CSV under data/export/.

    Deliberately run in-process rather than shelling out to the script: it is
    the same function the command line calls, so the two cannot drift apart.
    """
    try:
        return export_data.export_all(DB_PATH, export_data.EXPORT_DIR)
    except OSError as exc:
        raise HTTPException(500, f"Export failed: {exc}")


# --------------------------------------------------------------------------
# Automatic fixtures (football-data.org)
#
# The forward calendar. These routes only ever write to `available_fixtures`
# and `published_matches`; they never touch `matches`. Publishing a fixture
# does NOT give the engine evidence for it - the CSV import does that, later.
# --------------------------------------------------------------------------
def _lookahead_days(conn: sqlite3.Connection) -> int:
    raw = fixtures.get_setting(conn, "fixture_lookahead_days",
                               str(fixtures.LOOKAHEAD_DAYS))
    try:
        return max(1, min(30, int(raw)))
    except (TypeError, ValueError):
        return fixtures.LOOKAHEAD_DAYS


def _fixture_dict(row: sqlite3.Row, counts: dict[str, int]) -> dict[str, Any]:
    home = row["home_team"]
    away = row["away_team"]
    return {
        "id": row["id"],
        "date": row["date"],
        "kickoff": row["kickoff"],
        "league": row["league"],
        "season": row["season"],
        "competition": row["competition"],
        "home_provider": row["home_provider_name"],
        "away_provider": row["away_provider_name"],
        "home_team": home,
        "away_team": away,
        "status": row["status"],
        "mapping_state": row["mapping_state"],
        "mapping_note": row["mapping_note"],
        "published_id": row["published_id"],
        "evidence": {
            "home_matches": counts.get(home, 0) if home else 0,
            "away_matches": counts.get(away, 0) if away else 0,
        },
    }


@app.get("/admin/fixture-settings")
def get_fixture_settings():
    """Provider status and the per-league auto-publish switches."""
    with db() as conn:
        return {
            "provider": fixtures.PROVIDER,
            "token_configured": bool(fixtures.load_token()),
            "lookahead_days": _lookahead_days(conn),
            "last_fetch": fixtures.get_setting(conn, "last_fixture_fetch"),
            "leagues": fixtures.get_settings(conn),
        }


@app.put("/admin/fixture-settings")
def update_fixture_settings(body: FixtureSettingsIn):
    with db() as conn:
        try:
            return fixtures.set_auto_publish(conn, body.league, body.auto_publish)
        except ValueError as exc:
            raise HTTPException(400, str(exc))


@app.post("/admin/fixtures/fetch")
def fetch_fixtures(body: FetchFixturesIn | None = None):
    """Fetch the next N days, store candidates, then auto-publish what passes.

    Returns 400 (not 502) when the token is missing or rejected: that is a
    configuration problem the admin can fix, not a provider outage.
    """
    body = body or FetchFixturesIn()
    logs: list[str] = []
    try:
        report = fixtures.fetch_and_ingest(
            DB_PATH,
            lookahead_days=body.lookahead_days,
            publish=body.publish,
            log=logs.append,
        )
    except PermissionError as exc:
        raise HTTPException(400, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001 - surface the provider failure
        raise HTTPException(502, f"Fixture fetch failed: {exc}")

    report["ok"] = all(c["error"] is None for c in report["competitions"])
    return {"ok": report["ok"], "report": report, "log": logs}


@app.get("/admin/available-fixtures")
def list_available_fixtures(
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
):
    start = date_from or _date.today().isoformat()
    end = date_to or (_date.today() + timedelta(days=fixtures.LOOKAHEAD_DAYS)).isoformat()
    for label, value in (("from", start), ("to", end)):
        try:
            _date.fromisoformat(value)
        except ValueError:
            raise HTTPException(400, f"{label} must be YYYY-MM-DD, got {value!r}")

    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM available_fixtures WHERE date >= ? AND date <= ? "
            "ORDER BY date ASC, kickoff ASC, league ASC", (start, end)).fetchall()
        # Evidence is per fixture date, not per window: a match on Sunday must
        # not count a result played on Saturday.
        counts_by_date = {
            day: fixtures.team_match_counts(conn, day)
            for day in sorted({row["date"] for row in rows})
        }
        items = [_fixture_dict(row, counts_by_date[row["date"]]) for row in rows]

    return {
        "from": start,
        "to": end,
        "total": len(items),
        "published": sum(1 for item in items if item["published_id"]),
        "needs_attention": sum(1 for item in items if item["mapping_state"] != "ok"),
        "fixtures": items,
    }


@app.post("/admin/available-fixtures/publish")
def publish_available_fixtures(body: PublishFixturesIn):
    results: list[dict[str, Any]] = []
    with db() as conn:
        for fixture_id in body.ids:
            results.append({"id": fixture_id,
                            **fixtures.publish_fixture(conn, fixture_id)})
    return {
        "ok": all(r.get("ok") for r in results),
        "published": sum(1 for r in results if r.get("reason") == "published"),
        "results": results,
    }


@app.post("/admin/available-fixtures/publish-all")
def publish_all_available_fixtures():
    with db() as conn:
        report = fixtures.auto_publish(conn, _lookahead_days(conn))
    return {"ok": True, **report}


@app.get("/admin/team-aliases")
def list_team_aliases():
    """Saved mappings plus every provider name still blocking a fixture."""
    with db() as conn:
        aliases = rows_to_dicts(conn.execute(
            "SELECT id, provider, provider_name, team, created_at "
            "FROM team_aliases ORDER BY provider_name"))
        index = fixtures.canonical_index(conn)
        rows = conn.execute(
            "SELECT home_provider_name AS name, home_provider_short AS short, "
            "COUNT(*) AS n FROM available_fixtures WHERE home_team IS NULL "
            "GROUP BY home_provider_name "
            "UNION ALL "
            "SELECT away_provider_name, away_provider_short, COUNT(*) "
            "FROM available_fixtures WHERE away_team IS NULL "
            "GROUP BY away_provider_name").fetchall()

    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["name"]
        if key in merged:
            merged[key]["fixtures"] += row["n"]
        else:
            merged[key] = {
                "provider_name": key,
                "short": row["short"],
                "fixtures": row["n"],
                "suggestions": fixtures.suggest_teams(row["short"] or key, index),
            }
    return {"aliases": aliases,
            "unmapped": sorted(merged.values(), key=lambda item: -item["fixtures"])}


@app.put("/admin/team-aliases")
def save_team_alias(body: TeamAliasIn):
    with db() as conn:
        try:
            return fixtures.upsert_alias(conn, body.provider_name, body.team)
        except ValueError as exc:
            raise HTTPException(400, str(exc))


@app.delete("/admin/team-aliases/{alias_id}")
def remove_team_alias(alias_id: int):
    with db() as conn:
        if not fixtures.delete_alias(conn, alias_id):
            raise HTTPException(404, "alias not found")
    return {"ok": True, "deleted": alias_id}


# --------------------------------------------------------------------------
# The season calendar (read-only browse)
#
# This is not the dashboard. The dashboard answers "what may I see today" and
# still comes only from `published_matches`. This answers "what is this
# league's season", including results and fixtures months away, and it never
# publishes anything.
# --------------------------------------------------------------------------
_season_refresh_lock = threading.Lock()


@app.get("/season")
def season_calendar(
    league: str = Query(..., description="League name, e.g. Premier League"),
    season: str | None = Query(None, description="e.g. 2026-27; defaults to now"),
):
    today = _date.today().isoformat()
    label = normalize_season(season, today) if season else normalize_season(None, today)
    if not label:
        raise HTTPException(400, f"Unrecognised season {season!r}")

    with db() as conn:
        known = {r[0] for r in conn.execute("SELECT DISTINCT league FROM matches")}
        known |= set(fixtures.COMPETITIONS.values())
        if league not in known:
            raise HTTPException(404, f"Unknown league {league!r}")
        data = fixtures.season_view(conn, league, label)
        data["leagues"] = sorted(known)
        data["seasons"] = [r[0] for r in conn.execute(
            "SELECT DISTINCT season FROM matches WHERE league = ? "
            "ORDER BY season DESC", (league,))]
        return data


@app.get("/admin/season/status")
def season_status():
    with db() as conn:
        row = conn.execute(
            "SELECT season, COUNT(*) AS fixtures, "
            "  SUM(CASE WHEN status = 'finished' THEN 1 ELSE 0 END) AS played, "
            "  SUM(CASE WHEN match_id IS NOT NULL THEN 1 ELSE 0 END) AS linked "
            "FROM season_fixtures GROUP BY season ORDER BY season DESC LIMIT 1"
        ).fetchone()
        return {
            "state": fixtures.get_setting(conn, "calendar_sync_state") or "never",
            "last_sync": fixtures.get_setting(conn, "last_calendar_sync"),
            "error": fixtures.get_setting(conn, "calendar_sync_error") or None,
            "rows": conn.execute(
                "SELECT COUNT(*) FROM season_fixtures").fetchone()[0],
            "latest_season": row["season"] if row else None,
            "latest_fixtures": row["fixtures"] if row else 0,
            "latest_played": row["played"] if row else 0,
            "latest_linked": row["linked"] if row else 0,
            "sync_days": fixtures.CALENDAR_SYNC_DAYS,
        }


def _season_refresh() -> None:
    if not _season_refresh_lock.acquire(blocking=False):
        return
    try:
        report = fixtures.sync_calendar(
            DB_PATH, log=lambda line: print(line, flush=True))
        print(f"[season] calendar sync done: {report['stored']} fixtures "
              f"({report['season']})", flush=True)
    except Exception as exc:  # noqa: BLE001 - the thread must not die silently
        print(f"[season] calendar sync failed: {exc}", flush=True)
    finally:
        _season_refresh_lock.release()


@app.post("/admin/season/refresh")
def refresh_season_calendar():
    """Start the six-call season sync in the background (about 40 seconds)."""
    if _season_refresh_lock.locked():
        return {"started": False, "reason": "a calendar sync is already running"}
    threading.Thread(target=_season_refresh, daemon=True,
                     name="season-sync").start()
    return {"started": True}


# --------------------------------------------------------------------------
# The prediction board
#
# A pick is a projection, a line, and a probability measured on past matches.
# The board never invents a line: football uses the league reference unless the
# admin has entered a market line, and every card says which one it used.
# --------------------------------------------------------------------------
_predict_lock = threading.Lock()


@app.get("/board")
def prediction_board(
    day: str | None = Query(None, alias="date"),
    sport: str = Query("all"),
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
    market: str | None = Query(None),
    scope: str = Query("all"),
    team: str | None = Query(None),
    league: str | None = Query(None),
    source: str = Query("all"),
    confidence: str = Query("any"),
    status: str = Query("all"),
    sort: str = Query("probability"),
    grouped: bool = Query(False),
):
    day = day or _date.today().isoformat()
    try:
        _date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, f"bad date {day!r}, expected YYYY-MM-DD")
    if sport not in ("all", "football", "nba"):
        raise HTTPException(400, "sport must be all, football or nba")
    if scope not in ("all", "combined", "home", "away"):
        raise HTTPException(400, "scope must be all, combined, home or away")
    if source not in ("all", "core", "board", "agree"):
        raise HTTPException(400, "source must be all, core, board or agree")
    if confidence not in ("any", "high", "medium", "low"):
        raise HTTPException(400, "confidence must be any, high, medium or low")
    if status not in ("all", "upcoming", "live", "completed"):
        raise HTTPException(400, "status must be all, upcoming, live or completed")
    if sort not in ("probability", "time", "edge"):
        raise HTTPException(400, "sort must be probability, time or edge")

    market = (market or "").strip() or None
    team = (team or "").strip() or None
    league = (league or "").strip() or None
    with db() as conn:
        if grouped:
            # The user-facing Board is fixture-first: the snapshot already
            # contains every home/away/combined line, so filtering is a read.
            data = predict.board_cards(
                conn, day, sport, limit, offset=offset, market=market,
                league=league)
        else:
            data = predict.board(
                conn, day, sport, limit, offset=offset, market=market,
                scope=scope, team=team, league=league, source=source,
                confidence=confidence, status=status, sort=sort)
        data["sports"] = ["all", "football", "nba"]
        return data


@app.get("/admin/predictions/status")
def predictions_status():
    with db() as conn:
        conn.row_factory = sqlite3.Row
        predict.ensure_tables(conn)
        measured = [dict(r) for r in conn.execute(
            "SELECT sport, market, label, n, sigma, mae, naive_mae, bias, "
            "       updated_at FROM prediction_stats "
            "ORDER BY sport, market")]
        today = _date.today().isoformat()
        upcoming = conn.execute(
            "SELECT COUNT(*) FROM board_picks "
            "WHERE result IS NULL AND date >= ?", (today,)).fetchone()[0]
        graded = conn.execute(
            "SELECT COUNT(*) FROM board_picks WHERE result IS NOT NULL"
        ).fetchone()[0]
        upcoming_lines = conn.execute(
            "SELECT COUNT(*) FROM board_markets "
            "WHERE result IS NULL AND date >= ?", (today,)).fetchone()[0]
        settled_lines = conn.execute(
            "SELECT COUNT(*) FROM board_markets WHERE result IS NOT NULL"
        ).fetchone()[0]
        return {
            "measured": measured,
            "markets_ready": len(measured),
            "upcoming_picks": upcoming,
            "graded_picks": graded,
            "upcoming_lines": upcoming_lines,
            "settled_lines": settled_lines,
            "running": _predict_lock.locked(),
            "record": predict._record(conn, "all", today),
            "last_build": fixtures.get_setting(conn, "last_board_build"),
        }


def _predict_rebuild() -> None:
    if not _predict_lock.acquire(blocking=False):
        return
    try:
        print("[board] rebuild starting", flush=True)
        report = predict.rebuild(
            DB_PATH, log=lambda line: print(line, flush=True))
        with db() as conn:
            fixtures.set_setting(
                conn, "last_board_build",
                datetime.now(timezone.utc).isoformat(timespec="seconds"))
        print(f"[board] rebuild done: {report['built']['stored']} picks",
              flush=True)
    except Exception as exc:  # noqa: BLE001 - the thread must not die silently
        print(f"[board] rebuild failed: {exc}", flush=True)
    finally:
        _predict_lock.release()


@app.post("/admin/predictions/rebuild")
def rebuild_predictions():
    """Measure error, then rebuild the board (about ten minutes, background)."""
    if _predict_lock.locked():
        return {"started": False, "reason": "a rebuild is already running"}
    threading.Thread(target=_predict_rebuild, daemon=True,
                     name="board-rebuild").start()
    return {"started": True}


# --------------------------------------------------------------------------
# Daily scheduler.
#
# A single daemon thread, not a job queue: this is a local app with one admin.
# It waits for the server to settle, then checks every 30 minutes whether the
# last successful fetch is older than 12 hours. That means a laptop that was
# off at 06:00 catches up the moment it starts, and a server left running
# fetches once in the morning and then does nothing for the rest of the day.
# Set SPORTS_ANALYZER_SCHEDULER=0 to disable it (the tests do).
# --------------------------------------------------------------------------
def _fixture_scheduler() -> None:
    time.sleep(10)
    while True:
        try:
            if fixtures.load_token():
                with db() as conn:
                    last = fixtures.get_setting(conn, "last_fixture_fetch")
                    last_calendar = fixtures.get_setting(conn, "last_calendar_sync")
                    lookahead = _lookahead_days(conn)
                due = _older_than(last, timedelta(hours=12))
                if due:
                    print("[fixtures] scheduled fetch starting", flush=True)
                    report = fixtures.fetch_and_ingest(
                        DB_PATH, lookahead_days=lookahead,
                        log=lambda line: print(line, flush=True))
                    print(f"[fixtures] scheduled fetch done: "
                          f"{report['published']} published, "
                          f"{report['needs_attention']} need mapping", flush=True)

                # The calendar changes slowly. A weekly sync is enough, and it
                # runs in this same thread because 40 seconds once a week does
                # not justify a second scheduler.
                if _older_than(last_calendar, timedelta(days=fixtures.CALENDAR_SYNC_DAYS)):
                    print("[season] calendar sync starting", flush=True)
                    report = fixtures.sync_calendar(
                        DB_PATH, log=lambda line: print(line, flush=True))
                    print(f"[season] calendar sync done: {report['stored']} "
                          f"fixtures ({report['season']})", flush=True)
        except Exception as exc:  # noqa: BLE001 - the loop must survive
            print(f"[fixtures] scheduled fetch failed: {exc}", flush=True)

        # Settle finished games every pass (30 minutes), not only when the
        # board is rebuilt. This is what turns a stored prediction into a
        # HIT/MISS mark during the same day, and it is idempotent.
        try:
            with db() as conn:
                graded = predict.grade(conn, log=lambda _line: None)
            if any(graded[key] for key in ("win", "loss", "push", "void")):
                print(f"[board] graded {graded['win']} won, "
                      f"{graded['loss']} lost, {graded['push']} pushed", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[board] scheduled grading failed: {exc}", flush=True)

        # The board does not depend on the football provider: grade what has
        # finished, fill any missing picks, and record when it last ran.
        try:
            with db() as conn:
                last_board = fixtures.get_setting(conn, "last_board_build")
            if _older_than(last_board, timedelta(hours=6)):
                report = predict.daily(
                    DB_PATH, log=lambda line: print(line, flush=True))
                with db() as conn:
                    fixtures.set_setting(
                        conn, "last_board_build",
                        datetime.now(timezone.utc).isoformat(timespec="seconds"))
                print(f"[board] done: {report['built']['stored']} picks stored",
                      flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[board] scheduled build failed: {exc}", flush=True)

        time.sleep(1800)


def _older_than(stamp: str | None, age: timedelta) -> bool:
    """True when a stored UTC timestamp is missing or older than `age`."""
    if not stamp:
        return True
    try:
        moment = datetime.fromisoformat(stamp)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - moment > age
    except ValueError:
        return True


@app.on_event("startup")
def _configure_sqlite() -> None:
    """Put the persistent database in WAL mode before schedulers start."""
    if not DB_PATH.exists():
        raise RuntimeError(f"Database not found: {DB_PATH}")
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")


@app.on_event("startup")
def _ensure_football_tables() -> None:
    """Create the football side tables even when no script was run."""
    with db() as conn:
        migrate_schema.ensure_side_tables(conn)
        predict.ensure_tables(conn)


@app.on_event("startup")
def _ensure_nba_tables() -> None:
    """Create the NBA tables even when the scheduler is disabled."""
    with db() as conn:
        ensure_nba_schema(conn)
        nba_people.ensure_tables(conn)


@app.on_event("startup")
def _start_fixture_scheduler() -> None:
    if os.environ.get("SPORTS_ANALYZER_SCHEDULER", "1") == "0":
        return
    threading.Thread(target=_fixture_scheduler, daemon=True,
                     name="fixture-scheduler").start()
    threading.Thread(target=_nba_scheduler, daemon=True,
                     name="nba-scheduler").start()
    threading.Thread(target=_nba_people_scheduler, daemon=True,
                     name="nba-people-scheduler").start()


# --------------------------------------------------------------------------
# NBA
#
# A separate section with its own tables, engine and publish gate. Nothing here
# reads or writes a football table.
# --------------------------------------------------------------------------
class NbaLines(BaseModel):
    """Optional market lines. None means 'use the league-reference line'."""

    game_total: float | None = None
    home_team_total: float | None = None
    away_team_total: float | None = None
    clear: bool = False


def _nba_game_row(conn: sqlite3.Connection, game_id: str) -> dict | None:
    """Find a game in published, then fixtures, then completed history."""
    for table in ("nba_published", "nba_fixtures", "nba_games"):
        row = conn.execute(
            f"SELECT home_team_id, away_team_id, game_date FROM {table} "
            "WHERE game_id = ?", (game_id,)).fetchone()
        if row:
            return {"game_id": game_id, "home_team_id": row[0],
                    "away_team_id": row[1], "game_date": row[2],
                    "source": table}
    return None


@app.get("/nba/status")
def nba_status():
    with db() as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("nba_games", "nba_team_games", "nba_fixtures",
                          "nba_published")
        }
        with_quarters = conn.execute(
            "SELECT COUNT(*) FROM nba_games WHERE has_quarters = 1"
        ).fetchone()[0]
        settings = {row[0]: row[1] for row in conn.execute(
            "SELECT key, value FROM nba_settings")}
    return {
        "season": nba_fetch.current_season(),
        "counts": counts,
        "with_quarters": with_quarters,
        "last_schedule_fetch": settings.get("last_schedule_fetch"),
        "last_history_fetch": settings.get("last_history_fetch"),
        "last_publish": settings.get("last_publish"),
    }


@app.get("/nba/teams")
def nba_teams():
    with db() as conn:
        return {"teams": nba_stats.list_teams(conn)}


@app.get("/nba/teams/{team_id}/roster")
def nba_team_roster(team_id: int):
    """The official roster, each player's latest production, and availability.

    Production is joined on the player, not the team, so a summer signing is
    shown with the evidence he actually earned. `minutes_unavailable` is
    descriptive and is never an input to a projection.
    """
    with db() as conn:
        team = conn.execute(
            "SELECT team_id, name, city, tricode FROM nba_teams "
            "WHERE team_id = ?", (team_id,)).fetchone()
        if team is None:
            raise HTTPException(status_code=404, detail="Unknown NBA team")
        return {
            "team": dict(team),
            "season": nba_people.ROSTER_SEASON,
            "roster": nba_people.roster_overview(conn, team_id),
            "availability": nba_people.availability(conn, team_id),
        }


@app.get("/admin/nba/people/status")
def nba_people_status():
    """What the roster/availability sync has done, and what it could not do."""
    with db() as conn:
        report = nba_people.status(conn)
        settings = {row[0]: row[1] for row in conn.execute(
            "SELECT key, value FROM nba_settings")}
    return {**report,
            "last_schedule_fetch": settings.get("last_schedule_fetch"),
            "last_history_fetch": settings.get("last_history_fetch")}


@app.post("/admin/nba/people/refresh")
def nba_people_refresh(rosters: bool = True, stats: bool = True,
                       injuries: bool = True):
    """Run the people sync in the background. Returns immediately."""
    if _people_refresh_lock.locked():
        return {"started": False, "detail": "a sync is already running"}
    threading.Thread(target=_nba_people_sync, daemon=True,
                     args=(rosters, stats, injuries),
                     name="nba-people-refresh").start()
    return {"started": True}


@app.get("/nba/published/dates")
def nba_published_dates():
    with db() as conn:
        return {"dates": nba_fixtures.published_dates(conn)}


@app.get("/nba/published")
def nba_published(date: str | None = Query(None)):
    """Published games for one date.

    With no date: today when it has games, otherwise the next matchday, and the
    last one only if nothing is ahead. The frontend passes an explicit date as
    soon as the user navigates, so an explicit date never falls back.
    """
    with db() as conn:
        days = nba_fixtures.published_dates(conn)
        if date is None:
            today = _date.today().isoformat()
            known = [d["date"] for d in days]
            if today in known:
                date = today
            else:
                ahead = [d for d in known if d > today]
                date = ahead[0] if ahead else (known[-1] if known else today)
        games = nba_fixtures.published_for_date(conn, date)
    return {"date": date, "today": _date.today().isoformat(),
            "games": games, "dates": days}


@app.get("/nba/fixtures")
def nba_upcoming(days: int = Query(14, ge=1, le=60)):
    with db() as conn:
        fixtures_now = nba_fixtures.upcoming(conn, days=days)
    return {"fixtures": fixtures_now, "window_days": days}


@app.get("/nba/analysis/{game_id}")
def nba_analysis(game_id: str,
                 game_total: float | None = Query(None),
                 home_team_total: float | None = Query(None),
                 away_team_total: float | None = Query(None)):
    with db() as conn:
        game = _nba_game_row(conn, game_id)
        if game is None:
            raise HTTPException(404, "Unknown NBA game")
        stored = nba_fixtures.market_lines(conn, game_id)
        supplied = {"game_total": game_total,
                    "home_team_total": home_team_total,
                    "away_team_total": away_team_total}
        lines = {key: value if value is not None else stored.get(key)
                 for key, value in supplied.items()}
        lines = {k: v for k, v in lines.items() if v is not None}
        result = nba_stats.analyze(
            conn, game["home_team_id"], game["away_team_id"],
            game["game_date"], lines=lines)
        result["game_id"] = game_id
        result["published"] = game["source"] == "nba_published"
    return result


@app.post("/nba/publish/{game_id}")
def nba_publish(game_id: str, payload: NbaLines | None = None):
    with db() as conn:
        try:
            if payload is not None and payload.clear:
                nba_fixtures.clear_lines(conn, game_id)
                return nba_fixtures.publish(conn, game_id, {})
            lines = payload.model_dump(exclude_none=True) if payload else {}
            lines.pop("clear", None)
            return nba_fixtures.publish(conn, game_id, lines)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc


@app.delete("/nba/publish/{game_id}")
def nba_unpublish(game_id: str):
    with db() as conn:
        return nba_fixtures.unpublish(conn, game_id)


_nba_refresh_lock = threading.Lock()


def _nba_refresh() -> None:
    """Pull today's history and schedule, then auto-publish. One at a time."""
    if not _nba_refresh_lock.acquire(blocking=False):
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            ensure_nba_schema(conn)
            season = nba_fetch.current_season()
            nba_fetch.ingest_history(conn, season)
            nba_fetch.set_setting(
                conn, "last_history_fetch",
                datetime.now(timezone.utc).isoformat(timespec="seconds"))
            nba_fetch.ingest_schedule(conn, season)
            nba_fetch.set_setting(
                conn, "last_schedule_fetch",
                datetime.now(timezone.utc).isoformat(timespec="seconds"))
            report = nba_fixtures.auto_publish(conn)
            nba_fetch.set_setting(
                conn, "last_publish",
                datetime.now(timezone.utc).isoformat(timespec="seconds"))
            nba_fetch.ingest_quarters(conn, season=season, limit_dates=2)
            nba_fetch.set_setting(
                conn, "last_quarter_fetch",
                datetime.now(timezone.utc).isoformat(timespec="seconds"))
            # The board is built from the tables this refresh just changed.
            # Mark it stale so the next scheduler pass uses the new results
            # rather than waiting for the ordinary six-hour rebuild.
            fixtures.set_setting(
                conn, "last_board_build", "1970-01-01T00:00:00+00:00")
            print(f"[nba] refresh {season}: {report['published']} published",
                  flush=True)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - the caller is a background thread
        print(f"[nba] refresh failed: {exc}", flush=True)
    finally:
        _nba_refresh_lock.release()


@app.post("/nba/refresh")
def nba_refresh():
    threading.Thread(target=_nba_refresh, daemon=True,
                     name="nba-refresh").start()
    return {"started": True, "season": nba_fetch.current_season()}


def _nba_scheduler() -> None:
    """Every 30 minutes, refresh when the last pull is more than 6 hours old."""
    time.sleep(25)
    while True:
        try:
            with db() as conn:
                ensure_nba_schema(conn)
                row = conn.execute(
                    "SELECT value FROM nba_settings WHERE key = "
                    "'last_schedule_fetch'").fetchone()
            due = True
            if row:
                try:
                    moment = datetime.fromisoformat(row[0])
                    if moment.tzinfo is None:
                        moment = moment.replace(tzinfo=timezone.utc)
                    due = datetime.now(timezone.utc) - moment > timedelta(hours=6)
                except ValueError:
                    due = True
            if due:
                print("[nba] scheduled refresh starting", flush=True)
                _nba_refresh()
        except Exception as exc:  # noqa: BLE001 - the loop must survive
            print(f"[nba] scheduler error: {exc}", flush=True)
        time.sleep(1800)


_people_refresh_lock = threading.Lock()


def _nba_people_sync(rosters: bool, stats: bool, injuries: bool) -> None:
    """One people sync at a time; the scheduler and the button share a lock."""
    if not _people_refresh_lock.acquire(blocking=False):
        return
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        nba_people.ensure_tables(conn)
        if rosters:
            nba_people.sync_rosters(conn)
        if stats:
            nba_people.sync_player_stats(conn)
        if injuries:
            nba_people.sync_injuries(conn)
    except Exception as exc:  # noqa: BLE001 - background worker
        print(f"[nba-people] sync failed: {exc}", flush=True)
    finally:
        conn.close()
        _people_refresh_lock.release()


def _nba_people_scheduler() -> None:
    """Keep availability fresh (3h) and rosters current (daily).

    The official roster changes rarely; the injury feed changes hourly. They
    are on separate schedules so a roster pull cannot delay an availability
    refresh, and neither blocks the game scheduler.
    """
    time.sleep(90)
    while True:
        try:
            with db() as conn:
                nba_people.ensure_tables(conn)
                last = {
                    job: (conn.execute(
                        "SELECT MAX(started_at) FROM nba_sync_log "
                        "WHERE job = ? AND status = 'ok'", (job,)).fetchone()[0])
                    for job in ("rosters", "player_stats", "injuries")
                }
            if _older_than(last["injuries"], timedelta(hours=3)):
                _nba_people_sync(False, False, True)
            if _older_than(last["rosters"], timedelta(hours=24)):
                _nba_people_sync(
                    True,
                    _older_than(last["player_stats"], timedelta(days=3)),
                    False)
        except Exception as exc:  # noqa: BLE001 - the loop must survive
            print(f"[nba-people] scheduler error: {exc}", flush=True)
        time.sleep(1800)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)


# --------------------------------------------------------------------------
# Serve the PWA from the same origin as the API.
#
# This exists so there is ONE process and ONE URL to remember:
#     python -m uvicorn main:app --port 8001
#     open http://localhost:8001/app/
# No second web server, no CORS, no "which port was it" confusion.
#
# Mounted last on purpose: the API routes above must win, and /app is a
# distinct prefix so nothing collides.
# --------------------------------------------------------------------------
FRONTEND_DIR = BASE_DIR / "frontend"
if FRONTEND_DIR.is_dir():
    app.mount("/app", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

    @app.get("/", include_in_schema=False)
    def _root_redirect():
        return RedirectResponse(url="/app/")
