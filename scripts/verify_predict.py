"""Verify the prediction board without touching the real database.

The board is the one screen that puts a percentage on a match, so the checks
here are about the honesty of that number rather than about formatting:

    * a bigger edge can never produce a smaller probability;
    * a thin sample can never look as certain as a large one;
    * a probability is clamped, so one match cannot be called a certainty;
    * grading settles a win, a loss and a push from the real result, and leaves
      an unplayed pick alone;
    * every football pick comes from a CSV-backed market, never a manual one.

Runs against a throwaway copy. No network.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "scripts"))

import predict  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    if not condition:
        failures.append(label)
    print(f"  {mark}  {label}" + (f"  [{detail}]" if detail and not condition else
                                  (f"  ({detail})" if detail else "")))


def main() -> int:
    source = BASE / "data" / "sports_analyzer.db"
    if not source.exists():
        print(f"No database at {source}")
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="sa_predict_"))
    db = workdir / "test.db"
    shutil.copy2(source, db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    try:
        print("\n1. Tables and measurement")
        predict.ensure_tables(conn)
        predict.ensure_tables(conn)
        check("ensure_tables is repeatable", True)

        residual = [float(v) for v in range(-30, 31)] * 4   # n = 244
        summary = predict._summarise(residual, residual)
        grid = json.loads(summary["quantiles"])
        check("the quantile grid is ordered by value",
              all(grid[i][0] <= grid[i + 1][0] for i in range(len(grid) - 1)))
        check("the cumulative probabilities never decrease",
              all(grid[i][1] <= grid[i + 1][1] for i in range(len(grid) - 1)))
        check("sigma and MAE are measured, not guessed",
              summary["sigma"] > 0 and summary["mae"] > 0,
              f"{summary['sigma']}/{summary['mae']}")
        check("the median residual is near zero for a symmetric sample",
              abs(summary["bias"]) < 0.5, str(summary["bias"]))

        print("\n2. From edge to probability")
        stats_row = {"n": 400, "quantiles": summary["quantiles"]}
        p0 = predict.probability_over(stats_row, 0.0)
        p1 = predict.probability_over(stats_row, 1.0)
        p2 = predict.probability_over(stats_row, 3.0)
        check("a zero edge is about a coin flip", 0.45 <= p0 <= 0.55, f"{p0:.3f}")
        check("a bigger edge never lowers the probability",
              p0 <= p1 <= p2, f"{p0:.3f}/{p1:.3f}/{p2:.3f}")
        check("an enormous edge is still clamped",
              predict.probability_over(stats_row, 1e6) <= 0.95)
        check("an impossible edge is still clamped",
              predict.probability_over(stats_row, -1e6) >= 0.05)

        thin = {"n": 120, "quantiles": summary["quantiles"]}
        fat = {"n": 2000, "quantiles": summary["quantiles"]}
        p_thin = predict.probability_over(thin, 2.0)
        p_fat = predict.probability_over(fat, 2.0)
        check("a thin sample is pulled toward 50%",
              abs(p_thin - 0.5) < abs(p_fat - 0.5),
              f"{p_thin:.3f} vs {p_fat:.3f}")

        print("\n3. Board contents")
        picks = conn.execute("SELECT * FROM board_picks ORDER BY date").fetchall()
        check("the board has picks to check", len(picks) > 0, f"{len(picks)} rows")
        check("every pick is at or above the confidence bar",
              all(p["probability"] >= predict.MIN_CONFIDENCE for p in picks))
        check("every pick was measured on enough matches",
              all(p["sample"] >= predict.MIN_SAMPLE for p in picks))
        check("every pick is over or under",
              all(p["side"] in ("over", "under") for p in picks))
        stats_map = {(r["sport"], r["market"]): r for r in conn.execute(
            "SELECT * FROM prediction_stats")}
        mismatched = []
        frozen = 0
        for p in picks:
            s = stats_map.get((p["sport"], p["market"]))
            if s is None:
                continue
            if s["n"] != p["sample"]:
                # The pick was frozen against an older measurement; it is not
                # expected to reproduce from today's distribution.
                frozen += 1
                continue
            over = predict.probability_over(s, p["projection"] - p["line"])
            side = "over" if over >= 0.5 else "under"
            probability = max(over, 1.0 - over)
            if side != p["side"] or abs(probability - p["probability"]) > 1e-3:
                mismatched.append((p["id"], side, p["side"]))
        check("a stored pick reproduces from its market's measured error",
              not mismatched, str(mismatched[:3]))
        print(f"        ({frozen} pick(s) frozen against an older measurement)")
        check("every pick names its line source",
              all(p["line_source"] for p in picks))

        check("every stored pick has a rank",
              all(p["pick_rank"] >= 1 for p in picks))
        top_per_game: dict[tuple, int] = {}
        for p in picks:
            if p["pick_rank"] > predict.DEFAULT_TOP_PER_GAME:
                continue
            key = (p["date"], p["sport"], p["game_key"])
            top_per_game[key] = top_per_game.get(key, 0) + 1
        check("the default view caps at two picks per fixture",
              all(n <= predict.DEFAULT_TOP_PER_GAME
                  for n in top_per_game.values()),
              str(max(top_per_game.values(), default=0)))

        group_row = conn.execute(
            "SELECT date, sport, market_group, COUNT(*) AS n FROM board_picks "
            "GROUP BY date, sport, market_group ORDER BY n DESC LIMIT 1"
        ).fetchone()
        check("a market filter has a multi-rank market to test",
              group_row is not None and group_row["n"] > 2)
        if group_row is not None:
            filtered = predict.board(
                conn, group_row["date"], group_row["sport"], limit=200,
                market=group_row["market_group"])
            check("a market filter shows every stored rank, not just two",
                  filtered["total"] == group_row["n"],
                  f"{filtered['total']} vs {group_row['n']}")

        football_markets = {p["market"] for p in picks if p["sport"] == "football"}
        check("football picks only use CSV-backed markets",
              football_markets <= set(predict.FOOTBALL_MARKETS),
              ", ".join(sorted(football_markets)))
        nba_markets = {p["market"] for p in picks if p["sport"] == "nba"}
        check("nba picks only use measured markets",
              nba_markets <= set(predict.NBA_MARKETS),
              ", ".join(sorted(nba_markets)))

        day = picks[0]["date"] if picks else date.today().isoformat()
        ordered = predict.board(conn, day, "all", limit=50)["picks"]
        check("the board is ranked by probability, descending",
              all(ordered[i]["probability"] >= ordered[i + 1]["probability"]
                  for i in range(len(ordered) - 1)))
        check("the board respects its limit",
              len(predict.board(conn, day, "all", limit=3)["picks"]) <= 3)

        print("\n4. Grading")
        match = conn.execute(
            "SELECT * FROM matches WHERE home_goals IS NOT NULL "
            "  AND home_corners IS NOT NULL ORDER BY date DESC LIMIT 1"
        ).fetchone()
        check("a completed match is available to grade against", match is not None)
        if match is not None:
            actual = match["home_corners"] + match["away_corners"]
            label = f"{match['home_team']} v {match['away_team']}"
            next_day = (date.fromisoformat(match["date"]) + timedelta(days=1)).isoformat()

            def insert(side: str, line: float, key: str,
                       day: str = match["date"]) -> int:
                cur = conn.execute(
                    "INSERT INTO board_picks (date, sport, game_key, game_label, "
                    "  league, market, market_label, side, line, projection, "
                    "  probability, sample, line_source, start_time) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (day, "football", key, label, match["league"],
                     "corners", "corners", side, line, line + 1, 0.62, 400,
                     "league", day + "T12:00:00"))
                return cur.lastrowid

            win_id = insert("over", actual - 0.5, "verify-win")
            loss_id = insert("under", actual - 0.5, "verify-loss")
            push_id = insert("over", float(actual), "verify-push")
            pending_id = insert("over", actual - 0.5, "verify-pending",
                                "2099-01-01")

            graded = predict.grade(conn, before=next_day, log=lambda _: None)
            rows = {r["id"]: r for r in conn.execute(
                "SELECT * FROM board_picks WHERE id IN (?,?,?,?)",
                (win_id, loss_id, push_id, pending_id))}
            check("an over that lands above the line wins",
                  rows[win_id]["result"] == "win", str(rows[win_id]["result"]))
            check("an under that lands above the line loses",
                  rows[loss_id]["result"] == "loss", str(rows[loss_id]["result"]))
            check("an exact hit is a push, not a win",
                  rows[push_id]["result"] == "push", str(rows[push_id]["result"]))
            check("an unplayed pick is never graded",
                  rows[pending_id]["result"] is None,
                  str(rows[pending_id]["result"]))
            check("grading records the actual value",
                  rows[win_id]["actual"] == float(actual))
            check("grading does not rewrite the line or the probability",
                  rows[win_id]["line"] == actual - 0.5
                  and rows[win_id]["probability"] == 0.62)
            check("a graded pick is not graded twice",
                  predict.grade(conn, before=next_day, log=lambda _: None)
                  ["win"] == 0)

            # The complete matchup table is what the card view reads. It must
            # settle with the same rules while leaving its line untouched.
            conn.execute(
                "INSERT INTO board_markets (date, sport, game_key, game_label, "
                "  home_team, away_team, league, market, market_label, "
                "  market_group, scope, side, line, projection, probability, "
                "  sample, line_source, start_time, qualified) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (match["date"], "football", "verify-card", label,
                 match["home_team"], match["away_team"], match["league"],
                 "corners", "Corners", "corners", "combined", "over",
                 actual - 0.5, actual - 0.1, 0.62, 400, "league",
                 match["date"] + "T12:00:00", 1))
            predict.grade(conn, before=next_day, log=lambda _: None)
            card = conn.execute(
                "SELECT * FROM board_markets WHERE game_key = 'verify-card'"
            ).fetchone()
            check("a matchup line settles from the stored actual",
                  card["result"] == "win" and card["actual"] == float(actual),
                  f"{card['result']} {card['actual']}")
            check("settling a matchup line leaves its numbers alone",
                  card["line"] == actual - 0.5
                  and card["probability"] == 0.62)

        print("\n5. Isolation")
        before = conn.execute("SELECT COUNT(*) FROM published_matches").fetchone()[0]
        predict.build_board(db, days=1, replace=False, log=lambda _: None)
        after = conn.execute("SELECT COUNT(*) FROM published_matches").fetchone()[0]
        check("building the board publishes nothing", before == after)
        board_columns = {r["name"] for r in conn.execute(
            "PRAGMA table_info(board_picks)")}
        check("the snapshot has no stake or odds column",
              not ({"stake", "odds", "price", "bet"} & board_columns),
              ", ".join(sorted(board_columns)))
    finally:
        conn.close()
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("All prediction checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
