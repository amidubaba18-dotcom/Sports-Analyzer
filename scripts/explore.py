"""Inspect what is actually in the database.

Run this before trusting any backtest number, because a backtest is only ever
as good as the rows it ran over, and the row counts have changed as seasons
were imported.

Usage:
    python scripts/explore.py
    python scripts/explore.py --season 2025-26
    python scripts/explore.py --league "Premier League"
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "sports_analyzer.db"


def coverage(conn, league=None, season=None):
    """Row counts per season and league.

    The last column is the number of UNPLAYED fixtures in that group. It should
    be 0 for every historical season and non-zero only if you have started
    entering upcoming fixtures. A backtest silently ignores those rows (they
    have no score), so if this number is large your effective sample is smaller
    than the total suggests.
    """
    clause = []
    params: list = []
    if league:
        clause.append("league = ?")
        params.append(league)
    if season:
        clause.append("season = ?")
        params.append(season)
    where = f"WHERE {' AND '.join(clause)}" if clause else ""

    sql = (
        "SELECT league, season, COUNT(*) AS total, "
        "       SUM(home_goals IS NULL) AS unplayed, "
        "       MIN(date) AS first_date, MAX(date) AS last_date "
        f"FROM matches {where} "
        "GROUP BY season, league ORDER BY season, league"
    )
    return conn.execute(sql, params).fetchall()


def main():
    parser = argparse.ArgumentParser(description="Inspect the database")
    parser.add_argument("--league", default=None)
    parser.add_argument("--season", default=None)
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    try:
        print(f"DB: {DB_PATH}\n")

        print(f"{'league':17} {'season':9} {'total':>6} {'unplayed':>9}  "
              f"{'first':11} {'last':11}")
        print("-" * 72)

        total = unplayed = 0
        for league, season, count, un_played, first, last in coverage(
                conn, args.league, args.season):
            print(f"{league:17} {season:9} {count:>6} {un_played or 0:>9}  "
                  f"{first:11} {last:11}")
            total += count
            unplayed += un_played or 0

        playable = total - unplayed
        print("-" * 72)
        print(f"{'TOTAL':17} {'':9} {total:>6} {unplayed:>9}")
        print(f"\nPlayable (has a score, usable by a backtest): {playable}")

        # Which markets actually have data? A market with no recorded values is
        # skipped by the backtester, so this explains "n/a" rows.
        print("\nMarket coverage (non-null rows):")
        from stats import STAT_COLUMNS
        for market, (home_col, away_col) in STAT_COLUMNS.items():
            row = conn.execute(
                f"SELECT COUNT(*) FROM matches "
                f"WHERE {home_col} IS NOT NULL AND {away_col} IS NOT NULL"
            ).fetchone()[0]
            bar = "#" * min(40, row // 100)
            print(f"  {market:17} {row:>6}  {bar}")

        # Hand-entered stats overlay these columns; worth knowing how many exist.
        manual = conn.execute("SELECT COUNT(*) FROM manual_stats").fetchone()[0]
        published = conn.execute("SELECT COUNT(*) FROM published_matches").fetchone()[0]
        dupes = conn.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM matches GROUP BY league, season, "
            "date, home_team, away_team HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        print(f"\nmanual_stats rows: {manual}")
        print(f"published_matches rows: {published}")
        print(f"duplicate fixtures: {dupes}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
