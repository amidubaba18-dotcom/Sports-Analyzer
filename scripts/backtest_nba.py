"""Walk the NBA model forward and score it against what actually happened.

The method is the same one the football backtester uses, because it is the only
honest one: every projection for a game may use only games strictly before it.
No season aggregate is read, no `LeagueDashTeamStats`, nothing that already
knows the result.

It reports three things:

    MAE of the projected total, against a naive "league average so far" baseline
    MAE of each team's points
    How often the actual total landed on the same side of the fair line as the
    model said it would, at lines where the model and the league line differ

A model that cannot beat the naive baseline is not worth showing, so the
baseline is printed next to it rather than left implicit.

Usage:
    python scripts/backtest_nba.py
    python scripts/backtest_nba.py --season 2025-26 --start 2026-01-01
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "sports_analyzer.db"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nba_stats  # noqa: E402


def run(conn: sqlite3.Connection, season: str | None, start: str | None,
        limit: int | None) -> dict:
    where = ["home_score IS NOT NULL", "away_score IS NOT NULL"]
    params: list = []
    if season:
        where.append("season = ?")
        params.append(season)
    if start:
        where.append("game_date >= ?")
        params.append(start)
    sql = ("SELECT game_id, game_date, season, home_team_id, away_team_id, "
           "home_score, away_score FROM nba_games WHERE " + " AND ".join(where)
           + " ORDER BY game_date, game_id")
    if limit:
        sql += f" LIMIT {int(limit)}"
    games = conn.execute(sql, params).fetchall()

    errors = []
    home_errors = []
    away_errors = []
    naive_errors = []
    sides = {"agree": 0, "disagree": 0}
    skipped = 0
    per_season: dict[str, list[float]] = {}
    naive_by_season: dict[str, list[float]] = {}

    for i, (game_id, game_date, season_label, home_id, away_id,
            home_score, away_score) in enumerate(games, 1):
        try:
            projection = nba_stats.project(conn, home_id, away_id, game_date,
                                           season_label)
        except Exception:
            skipped += 1
            continue
        actual_total = home_score + away_score
        naive = 2.0 * projection["league"]["off"] * projection["league"]["pace"] / 100.0

        errors.append(abs(projection["total"] - actual_total))
        naive_errors.append(abs(naive - actual_total))
        home_errors.append(abs(projection["home_points"] - home_score))
        away_errors.append(abs(projection["away_points"] - away_score))
        per_season.setdefault(season_label, []).append(
            abs(projection["total"] - actual_total))
        naive_by_season.setdefault(season_label, []).append(
            abs(naive - actual_total))

        # Side agreement only where the model actually disagrees with the
        # league-reference line by enough to have a side at all.
        line = nba_stats.snap_to_half_line(projection["fair_total"])
        if abs(projection["total"] - line) >= 1.0:
            model_over = projection["total"] > line
            actual_over = actual_total > line
            if model_over == actual_over:
                sides["agree"] += 1
            else:
                sides["disagree"] += 1

        if i % 200 == 0:
            print(f"    {i}/{len(games)} games ...", flush=True)

    decided = sides["agree"] + sides["disagree"]
    return {
        "games": len(errors),
        "skipped": skipped,
        "mae": statistics.mean(errors) if errors else None,
        "naive_mae": statistics.mean(naive_errors) if naive_errors else None,
        "home_mae": statistics.mean(home_errors) if home_errors else None,
        "away_mae": statistics.mean(away_errors) if away_errors else None,
        "side_agreement": (sides["agree"] / decided) if decided else None,
        "side_sample": decided,
        "seasons": {
            s: {
                "games": len(per_season[s]),
                "mae": statistics.mean(per_season[s]),
                "naive_mae": statistics.mean(naive_by_season[s]),
            } for s in sorted(per_season)
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest the NBA model.")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--season", default=None,
                        help="one season, e.g. 2025-26")
    parser.add_argument("--start", default=None,
                        help="only games on/after this date")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    try:
        result = run(conn, args.season, args.start, args.limit)
    finally:
        conn.close()

    print()
    print(f"Games scored: {result['games']}  (skipped {result['skipped']})")
    if not result["games"]:
        print("Nothing to score. Import NBA history first.")
        return 1
    print(f"MAE total:        {result['mae']:.2f} points")
    print(f"Naive baseline:   {result['naive_mae']:.2f} points  "
          f"(league average so far)")
    delta = result["naive_mae"] - result["mae"]
    print(f"Improvement:      {delta:+.2f} points per game "
          f"({'better' if delta > 0 else 'WORSE'} than naive)")
    print(f"MAE home points:  {result['home_mae']:.2f}")
    print(f"MAE away points:  {result['away_mae']:.2f}")
    if result["side_sample"]:
        print(f"Side agreement:   {result['side_agreement'] * 100:.1f}% "
              f"over {result['side_sample']} games where the model was at "
              f"least 1 point away from the league line")
    print("\nBy season:")
    for season, row in result["seasons"].items():
        print(f"  {season}: {row['games']:4} games  MAE {row['mae']:.2f}  "
              f"naive {row['naive_mae']:.2f}  "
              f"delta {row['naive_mae'] - row['mae']:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
