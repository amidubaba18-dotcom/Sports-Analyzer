"""Offline checks for the NBA module. Runs on a copy, never the live database.

What this guards, and why each one matters:

    identity      a team-game must belong to a known team and a known game
    arithmetic    the two team rows must sum to the game score
    possessions   the derived number every rating depends on must be right
    cutoff        a projection may not see the game it is projecting
    x.5 lines     a whole-number line admits a push, which silently becomes
                  an under in any `actual > line` test
    baselines     over must never exceed the sample, and the rate must match
    publish       idempotent, reversible, and never touches a football table

Usage:
    python scripts/verify_nba.py
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "sports_analyzer.db"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nba_fixtures  # noqa: E402
import nba_stats  # noqa: E402
import predict  # noqa: E402
from nba_schema import ensure_schema  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        return 1
    workdir = Path(tempfile.mkdtemp(prefix="nba_verify_"))
    db_copy = workdir / "verify.db"
    shutil.copy2(DB_PATH, db_copy)

    conn = sqlite3.connect(db_copy)
    conn.row_factory = sqlite3.Row
    try:
        print(f"Verifying a copy: {db_copy}\n")

        # ---- schema and identity -------------------------------------
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'nba_%'")}
        expected = {"nba_teams", "nba_games", "nba_team_games",
                    "nba_fixtures", "nba_published", "nba_settings"}
        check("all NBA tables exist", expected <= tables,
              f"missing {sorted(expected - tables)}")

        ensure_schema(conn)  # must be safe to run again
        check("ensure_schema is repeatable", True)

        games = conn.execute("SELECT COUNT(*) FROM nba_games").fetchone()[0]
        team_games = conn.execute(
            "SELECT COUNT(*) FROM nba_team_games").fetchone()[0]
        check("history is present", games > 0, f"{games} games")
        check("two team rows per game", team_games == games * 2,
              f"{team_games} rows for {games} games")

        orphans = conn.execute(
            "SELECT COUNT(*) FROM nba_team_games t "
            "LEFT JOIN nba_teams x ON x.team_id = t.team_id "
            "WHERE x.team_id IS NULL").fetchone()[0]
        check("no team-game without a team", orphans == 0, f"{orphans} orphans")

        bad_sides = conn.execute(
            "SELECT COUNT(*) FROM nba_team_games t "
            "JOIN nba_games g ON g.game_id = t.game_id "
            "WHERE (t.is_home = 1 AND t.team_id != g.home_team_id) "
            "   OR (t.is_home = 0 AND t.team_id != g.away_team_id)").fetchone()[0]
        check("home/away flags agree with the game", bad_sides == 0,
              f"{bad_sides} mismatches")

        # ---- arithmetic ----------------------------------------------
        score_mismatch = conn.execute(
            "SELECT COUNT(*) FROM nba_games g "
            "WHERE g.home_score != (SELECT points FROM nba_team_games "
            "  WHERE game_id = g.game_id AND team_id = g.home_team_id) "
            "   OR g.away_score != (SELECT points FROM nba_team_games "
            "  WHERE game_id = g.game_id AND team_id = g.away_team_id)"
        ).fetchone()[0]
        check("team points equal the game score", score_mismatch == 0,
              f"{score_mismatch} disagreements")

        bad_possessions = 0
        for row in conn.execute(
                "SELECT fga, fta, oreb, tov, possessions FROM nba_team_games "
                "WHERE possessions IS NOT NULL"):
            expected_poss = row["fga"] + 0.44 * row["fta"] - row["oreb"] + row["tov"]
            if abs(expected_poss - row["possessions"]) > 0.01:
                bad_possessions += 1
        check("possessions match the stated formula", bad_possessions == 0,
              f"{bad_possessions} rows differ")

        # ---- cutoff ---------------------------------------------------
        game = conn.execute(
            "SELECT game_id, game_date, season, home_team_id, away_team_id "
            "FROM nba_games WHERE game_date > '2026-01-15' "
            "ORDER BY game_date LIMIT 1").fetchone()
        check("a mid-season game exists to test the cutoff", game is not None)

        before = conn.execute(
            "SELECT COUNT(*) FROM nba_team_games WHERE team_id = ? "
            "AND game_date < ? AND season = ?",
            (game["home_team_id"], game["game_date"], game["season"])
        ).fetchone()[0]
        result = nba_stats.analyze(conn, game["home_team_id"],
                                   game["away_team_id"], game["game_date"])
        check("season sample is strictly before the game",
              result["ratings"]["home"]["season_games"] == before,
              f"{result['ratings']['home']['season_games']} vs {before}")

        earlier = nba_stats.analyze(conn, game["home_team_id"],
                                    game["away_team_id"],
                                    game["game_date"])
        later = nba_stats.project(conn, game["home_team_id"],
                                  game["away_team_id"],
                                  game["game_date"])
        check("the projected game is not in its own sample",
              earlier["ratings"]["home"]["season_games"]
              == later["home"]["n_season"])

        self_game = conn.execute(
            "SELECT COUNT(*) FROM nba_team_games WHERE team_id = ? "
            "AND game_id = ? AND game_date < ?",
            (game["home_team_id"], game["game_id"], game["game_date"])
        ).fetchone()[0]
        check("the game's own row fails the cutoff", self_game == 0)

        # ---- market shape --------------------------------------------
        available = [m for m in result["markets"] if m.get("available")]
        check("at least the game and team totals are available",
              len(available) >= 3, f"{len(available)} available")

        bad_lines = [m["key"] for m in available
                     if (m["line"] * 2) % 2 != 1]
        check("every line is x.5", not bad_lines, f"bad: {bad_lines}")

        bad_support = [m["key"] for m in available
                       if m["supported"] != (m["projection"] > m["line"])]
        check("supported == projection above the line", not bad_support,
              f"bad: {bad_support}")

        bad_baselines = []
        for m in available:
            base = m["baseline"]
            if base["total"] and (base["over"] > base["total"]
                                  or abs(base["rate"] - base["over"] / base["total"]) > 1e-9):
                bad_baselines.append(m["key"])
        check("baselines are internally consistent", not bad_baselines,
              f"bad: {bad_baselines}")

        ranges = result["range"]
        check("range brackets the projection",
              ranges["low"] < result["projection"]["total"] < ranges["high"])

        # ---- rebounds --------------------------------------------------
        rebound_markets = {m["key"]: m for m in available
                           if m["key"] in nba_stats.REBOUND_MARKETS}
        check("all three rebound markets are available",
              set(rebound_markets) == set(nba_stats.REBOUND_MARKETS),
              f"available: {sorted(rebound_markets)}")

        rb = result["rebounds"]
        check("rebound parts sum to the combined projection",
              abs((rb["home"] + rb["away"]) - rb["total"]) < 1e-9,
              f"{rb['home']:.2f} + {rb['away']:.2f} != {rb['total']:.2f}")
        check("rebound projections are plausible",
              20 < rb["home"] < 80 and 20 < rb["away"] < 80
              and 40 < rb["total"] < 160,
              f"{rb['home']:.1f}, {rb['away']:.1f}")

        home_reb_sample = conn.execute(
            "SELECT COUNT(*) FROM nba_team_games WHERE team_id = ? "
            "AND game_date < ? AND season = ? AND reb IS NOT NULL",
            (game["home_team_id"], game["game_date"],
             game["season"])).fetchone()[0]
        check("rebound sample is strictly before the game",
              rb["form"]["home"]["own_detail"]["n_season"] == home_reb_sample,
              f"{rb['form']['home']['own_detail']['n_season']} vs {home_reb_sample}")

        # ---- every team counting stat --------------------------------
        expected_stats = set(nba_stats.STAT_COLUMNS)
        check("all eight counting stats are projected",
              set(result["team_stats"]) == expected_stats,
              f"missing: {sorted(expected_stats - set(result['team_stats']))}")
        expected_stat_markets = {
            key for keys in nba_stats.STAT_MARKETS.values() for key in keys}
        check("all three scopes are available for every counting stat",
              set(m["key"] for m in available
                  if m["key"] in expected_stat_markets)
              == expected_stat_markets)
        ranges_by_stat = {
            "rebounds": (20, 80), "two_pm": (10, 50), "three_pm": (2, 30),
            "assists": (10, 50), "turnovers": (2, 30), "steals": (1, 20),
            "blocks": (0, 15), "fouls": (5, 40),
        }
        bad_stats = []
        for stat, block in result["team_stats"].items():
            low, high = ranges_by_stat[stat]
            if not (abs(block["home"] + block["away"] - block["total"]) < 1e-9
                    and low < block["home"] < high
                    and low < block["away"] < high
                    and low < block["total"] < high * 2):
                bad_stats.append(stat)
        check("counting stats sum and stay in a plausible range",
              not bad_stats, f"bad: {bad_stats}")

        home_box = result["game"]["home"]["season"]
        check("two-pointers are field goals minus threes",
              abs(home_box["two_pm"] + home_box["three_pm"]
                  - home_box["fgm"]) < 1e-9,
              f"{home_box['two_pm']} + {home_box['three_pm']} != {home_box['fgm']}")

        team_stat_markets = [m for m in available
                             if m["key"] in expected_stat_markets
                             and m["scope"] in ("home", "away")]
        check("team stat lines use the team's own baseline",
              all(m["line_source"] == "team_baseline"
                  for m in team_stat_markets
                  if m["baseline"]["total"]),
              ", ".join(sorted(m["key"] for m in team_stat_markets
                               if m["line_source"] != "team_baseline")))

        # ---- labels ---------------------------------------------------
        check("season label for October",
              nba_stats.season_for_date("2026-10-20") == "2026-27")
        check("season label for March",
              nba_stats.season_for_date("2026-03-01") == "2025-26")

        # ---- publish --------------------------------------------------
        published_before = conn.execute(
            "SELECT COUNT(*) FROM nba_published").fetchone()[0]
        football_before = conn.execute(
            "SELECT COUNT(*) FROM matches").fetchone()[0]
        football_pub_before = conn.execute(
            "SELECT COUNT(*) FROM published_matches").fetchone()[0]

        first = nba_fixtures.auto_publish(conn)
        second = nba_fixtures.auto_publish(conn)
        after = conn.execute(
            "SELECT COUNT(*) FROM nba_published").fetchone()[0]
        check("auto-publish is idempotent",
              second["published"] == 0 and after == published_before,
              f"first {first['published']}, second {second['published']}")

        check("NBA publishing does not touch football history",
              conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
              == football_before)
        check("NBA publishing does not touch football publications",
              conn.execute("SELECT COUNT(*) FROM published_matches").fetchone()[0]
              == football_pub_before)

        sample = conn.execute(
            "SELECT game_id FROM nba_published ORDER BY game_date LIMIT 1"
        ).fetchone()
        if sample:
            gid = sample["game_id"]
            nba_fixtures.unpublish(conn, gid)
            gone = conn.execute(
                "SELECT COUNT(*) FROM nba_published WHERE game_id = ?",
                (gid,)).fetchone()[0]
            nba_fixtures.publish(conn, gid)
            back = conn.execute(
                "SELECT COUNT(*) FROM nba_published WHERE game_id = ?",
                (gid,)).fetchone()[0]
            check("unpublish then publish round-trips",
                  gone == 0 and back == 1)

        try:
            nba_fixtures.publish(conn, "0000000000")
            refused = False
        except ValueError:
            refused = True
        check("publishing an unknown game is refused", refused)

        stale = conn.execute(
            "SELECT COUNT(*) FROM nba_published p "
            "LEFT JOIN nba_teams h ON h.team_id = p.home_team_id "
            "LEFT JOIN nba_teams a ON a.team_id = p.away_team_id "
            "WHERE h.team_id IS NULL OR a.team_id IS NULL").fetchone()[0]
        check("every published game has two known teams", stale == 0)

        no_history = conn.execute(
            "SELECT COUNT(*) FROM nba_published p "
            "WHERE NOT EXISTS (SELECT 1 FROM nba_team_games t "
            "  WHERE t.team_id = p.home_team_id AND t.points IS NOT NULL) "
            "   OR NOT EXISTS (SELECT 1 FROM nba_team_games t "
            "  WHERE t.team_id = p.away_team_id AND t.points IS NOT NULL)"
        ).fetchone()[0]
        check("every published team has completed games", no_history == 0)

        # ---- quarters -------------------------------------------------
        quarter_games = conn.execute(
            "SELECT COUNT(*) FROM nba_games WHERE has_quarters = 1"
        ).fetchone()[0]
        half = next((m for m in result["markets"]
                     if m["key"] == "first_half_total"), None)
        if quarter_games >= nba_stats.MIN_QUARTER_GAMES:
            check("half totals are available once line scores exist",
                  half is not None and half.get("available") is True)
        else:
            check("half totals are withheld without line scores",
                  half is not None and half.get("available") is False)

        # ---- board taxonomy and filters -------------------------------
        predict.ensure_tables(conn)
        board_columns = {r[1] for r in conn.execute(
            "PRAGMA table_info(board_picks)")}
        check("board stores market group, scope, model and rank",
              {"market_group", "scope", "model", "pick_rank"} <= board_columns)
        check("taxonomy covers every measured NBA market",
              set(predict.NBA_MARKETS) <= set(predict.NBA_TAXONOMY))
        check("every counting-stat group has a Board label",
              set(nba_stats.STAT_COLUMNS) <= set(predict.GROUP_LABELS))

        bad_taxonomy = 0
        for row in conn.execute(
                "SELECT sport, market, market_group, scope FROM board_picks"):
            group, scope, _ = predict.classify(row["sport"], row["market"])
            if row["market_group"] != group or row["scope"] != scope:
                bad_taxonomy += 1
        check("every pick carries its taxonomy", bad_taxonomy == 0,
              f"{bad_taxonomy} mismatches")

        sample_day = conn.execute(
            "SELECT date FROM board_picks WHERE sport = 'nba' "
            "ORDER BY date LIMIT 1").fetchone()
        if sample_day:
            day = sample_day["date"]
            page = predict.board(conn, day, "nba", 1)
            check("board response exposes filters and facets",
                  {"total", "unfiltered", "markets", "teams", "filters"}
                  <= set(page))
            check("board paginates with an honest total",
                  len(page["picks"]) <= 1
                  and page["has_more"] == (page["total"] > 1))
            rebounds = predict.board(conn, day, "nba", 50, market="rebounds")
            check("rebounds filter returns only rebounds",
                  all(p["market_group"] == "rebounds"
                      for p in rebounds["picks"]))
            home_rebounds = predict.board(
                conn, day, "nba", 50, market="rebounds", scope="home")
            check("home scope returns only home rebound picks",
                  all(p["market_group"] == "rebounds" and p["scope"] == "home"
                      for p in home_rebounds["picks"]))
            context = predict.board(conn, day, "nba", 50, source="board")
            check("context view is empty until it is measured",
                  context["total"] == 0 and bool(context["source_note"]))
            completed = predict.board(conn, day, "nba", 50, status="completed")
            check("completed filter returns only settled picks",
                  all(p["result"] is not None for p in completed["picks"]))

            market_columns = {r[1] for r in conn.execute(
                "PRAGMA table_info(board_markets)")}
            check("complete matchup table stores both teams and qualification",
                  {"home_team", "away_team", "market_group", "scope",
                   "qualified", "result", "actual"} <= market_columns)
            bad_market_taxonomy = 0
            for row in conn.execute(
                    "SELECT sport, market, market_group, scope FROM board_markets"):
                group, scope, _ = predict.classify(row["sport"], row["market"])
                if row["market_group"] != group or row["scope"] != scope:
                    bad_market_taxonomy += 1
            check("every matchup line carries its taxonomy",
                  bad_market_taxonomy == 0,
                  f"{bad_market_taxonomy} mismatches")

            games = conn.execute(
                "SELECT COUNT(DISTINCT game_key) FROM board_markets "
                "WHERE date = ? AND sport = 'nba'", (day,)).fetchone()[0]
            cards = predict.board_cards(conn, day, "nba", 50)
            check("grouped board returns one default card per game",
                  cards["total"] == games and cards["row_total"] > 0,
                  f"{cards['total']} cards for {games} games")
            check("default cards only show fixtures with a qualifying row",
                  all(card["qualified"] for card in cards["cards"]))
            check("every default card records its selected line and reason",
                  all(card.get("selected")
                      and card["selected"].get("reason")
                      and card["selected"]["qualified"]
                      for card in cards["cards"]))
            check("grouped board reports the complete stored row count",
                  cards["stored_rows"] >= cards["row_total"])
            check("the selected row is marked exactly once per card",
                  all(sum(1 for row in card["rows"] if row["selected"]) == 1
                      for card in cards["cards"]))
            rebounds_cards = predict.board_cards(
                conn, day, "nba", 50, market="rebounds")
            check("a rebound card holds home, away and combined lines",
                  rebounds_cards["total"] > 0
                  and all({r["scope"] for r in card["rows"]}
                          == {"home", "away", "combined"}
                          for card in rebounds_cards["cards"]))
            check("grouped market filter only returns that market",
                  all(card["market_group"] == "rebounds"
                      for card in rebounds_cards["cards"]))
            league_cards = predict.board_cards(
                conn, day, "nba", 50, league="NBA")
            check("grouped league filter only returns that league",
                  all(card["league"] == "NBA"
                      for card in league_cards["cards"]),
                  f"{league_cards['total']} cards")
            page = predict.board_cards(conn, day, "nba", 1)
            check("grouped board paginates by card",
                  len(page["cards"]) <= 1
                  and page["has_more"] == (page["total"] > 1))
        else:
            check("an NBA board day exists to test filters", False)
    finally:
        conn.close()
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
