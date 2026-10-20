"""Match-level helpers: turning "a match" into "all its market analyses".

This module is the batch layer over `stats.py`. It adds no statistics of its
own; it orchestrates.

Design rules:
  * DEFAULT_MARKET_LINES lives in stats.py only. This module imports it rather
    than keeping a second copy that can drift.
  * Exceptions are NOT swallowed into a silent result. If one market fails we
    record the error in that market's entry, but the error text is preserved
    so it can be seen, never discarded.
  * before_date / exclude_match_id are passed through everywhere, so analysing
    a stored match can never let it see itself.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from stats import (
    DEFAULT_MARKET_LINES,
    STAT_COLUMNS,
    analyze_market,
    get_h2h_matches,
    get_h2h_summary,
    get_matches_by_date,
    get_team_away_averages,
    get_team_home_averages,
    get_team_recent_averages,
    get_upcoming_matches,
)

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "sports_analyzer.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def row_to_dict(cursor, row) -> dict | None:
    """Turn one row into a dict without depending on the connection's
    row_factory.

    This matters: `sqlite3.Row` supports dict(row), but a plain connection
    returns a tuple and dict(tuple) raises "object is not iterable". Reading
    the column names off the cursor description works either way, so callers
    can pass any connection they like.
    """
    if row is None:
        return None
    columns = [d[0] for d in cursor.description]
    return dict(zip(columns, row))


def get_match_by_id(match_id: int) -> dict | None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM matches WHERE id = ?", (match_id,))
        row = cur.fetchone()
        return row_to_dict(cur, row)


def get_matches_for_date(date: str | None = None) -> pd.DataFrame:
    with get_conn() as conn:
        return get_matches_by_date(conn, date)


def get_todays_matches() -> pd.DataFrame:
    return get_matches_for_date(None)


def get_fixtures(from_date: str | None = None, limit: int = 50) -> pd.DataFrame:
    with get_conn() as conn:
        return get_upcoming_matches(conn, from_date, limit)


def select_match(conn: sqlite3.Connection, match_id: int) -> dict | None:
    cur = conn.cursor()
    cur.execute("SELECT * FROM matches WHERE id = ?", (match_id,))
    return row_to_dict(cur, cur.fetchone())


def analyze_match(conn, home_team, away_team, markets=None, lines=None,
                  recent_n=5, before_date=None, exclude_match_id=None,
                  weights=None, league=None, season=None):
    """Analyse every requested market for one fixture.

    `lines=None` (or a market missing from `lines`) means the engine derives a
    fair line from the league average strictly before kickoff, instead of
    comparing every fixture on the planet against one hardcoded number.
    """
    markets = markets or list(STAT_COLUMNS)
    lines = lines or {}

    unknown = [m for m in markets if m not in STAT_COLUMNS]
    if unknown:
        raise ValueError(f"unknown market(s): {', '.join(unknown)}")

    results = []
    for market in markets:
        line = lines.get(market)  # None -> fair line, resolved inside stats
        try:
            result = analyze_market(
                conn, home_team, away_team, f"avg_{market}", line,
                recent_n=recent_n, before_date=before_date,
                exclude_match_id=exclude_match_id, weights=weights,
                league=league, season=season)
            result["market"] = market
            results.append(result)
        except Exception as exc:
            # Recorded, not hidden: the caller can see exactly which market
            # failed and why.
            results.append({
                "market": market,
                "stat": f"avg_{market}",
                "line": line if line is not None else DEFAULT_MARKET_LINES.get(market),
                "weighted_avg": None,
                "supported": None,
                "evidence_points": 0,
                "error": f"{type(exc).__name__}: {exc}",
            })
    return results


__all__ = [
    "BASE_DIR",
    "DB_PATH",
    "DEFAULT_MARKET_LINES",
    "analyze_match",
    "get_conn",
    "get_fixtures",
    "get_h2h_matches",
    "get_h2h_summary",
    "get_match_by_id",
    "get_matches_for_date",
    "get_team_away_averages",
    "get_team_home_averages",
    "get_team_recent_averages",
    "get_todays_matches",
    "select_match",
]
