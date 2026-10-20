"""Engine regression checks.

Runs a handful of analyses whose answers were verified earlier in the project
and asserts they still match. The point is to catch a refactor that silently
changes a number - those bugs do not crash, they just quietly make the model
wrong.

Section 8 covers a different kind of silent failure: the CSV refresh deleting
hand-entered work. It runs on a throwaway copy of the database.

Sections 8, 9 and 10 all write, so they run on throwaway copies of the real
database and never touch it.

Usage:
    python scripts/verify_engine.py
"""
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backtester  # noqa: E402
import import_data  # noqa: E402
import stats  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "sports_analyzer.db"

failures = []


def check(label, actual, expected, tolerance=0.01):
    if expected is None:
        ok = actual is None
    elif actual is None:
        ok = False
    elif isinstance(expected, (int, float)):
        ok = abs(actual - expected) <= tolerance
    else:
        ok = actual == expected
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label}: got {actual!r}, expected {expected!r}")
    if not ok:
        failures.append(label)


def find_match(conn, home_like, away_like, date=None):
    sql = ("SELECT id, date, league, season, home_team, away_team "
           "FROM matches WHERE home_team LIKE ? AND away_team LIKE ?")
    params = [f"%{home_like}%", f"%{away_like}%"]
    if date:
        sql += " AND date = ?"
        params.append(date)
    sql += " ORDER BY date DESC LIMIT 1"
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchone()


def temp_db_copy(name):
    """A throwaway copy of the real database, for checks that have to write."""
    tmp_dir = Path(tempfile.gettempdir()) / "sports_analyzer_verify"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    target = tmp_dir / name
    shutil.copy2(DB_PATH, target)
    return target


def main():
    conn = sqlite3.connect(DB_PATH)

    print("=== 1. Leakage guard: the cutoff must exclude the match and all after it ===")
    row = find_match(conn, "Marseille", "Paris SG")
    if row is None:
        print("  (Marseille v PSG not found - skipping)")
    else:
        mid, date, league, season, home, away = row
        print(f"  match: {home} v {away} on {date} (id={mid})")

        leaky = stats.analyze_market(conn, home, away, "avg_goals", 2.5, recent_n=5)
        clean = stats.analyze_market(conn, home, away, "avg_goals", 2.5, recent_n=5,
                                     before_date=date, exclude_match_id=mid)
        print(f"  no cutoff      -> {leaky['weighted_avg']}")
        print(f"  cutoff applied -> {clean['weighted_avg']}")

        # Comparing two numbers would not prove anything - both are just numbers.
        # The real test is whether any match on or after the cutoff sits behind
        # the number, so look at the evidence sets themselves.
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM matches WHERE (home_team = ? OR away_team = ?) AND date >= ?",
            (home, home, date))
        future_ids = {r[0] for r in cur.fetchall()}

        clean_form = stats.get_team_home_averages(
            conn, home, before_date=date, exclude_match_id=mid)
        leaked = future_ids & clean_form["match_ids"]
        print(f"  matches of {home} on/after the cutoff: {len(future_ids)}; "
              f"of those in the cutoff evidence: {len(leaked)}")
        check("no match on/after the cutoff leaks into the home form", leaked, set())

        # Meta-check, or a pass means nothing: the same test must FAIL to find a
        # leak only because the cutoff worked, not because it cannot find one.
        unfiltered = stats.get_team_home_averages(conn, home)
        would_leak = future_ids & unfiltered["match_ids"]
        check("the same check does find those matches without a cutoff",
              len(would_leak) > 0, True)

    print()
    print("=== 2. Regression pin: the cutoff value is stable ===")
    print("     (This does NOT prove the number is right - Test 1 is the real")
    print("      leakage guard. This only catches a refactor silently changing it.)")
    row2 = find_match(conn, "Liverpool", "Arsenal")
    if row2 is not None:
        mid2, date2, _, _, home2, away2 = row2
        a = stats.analyze_market(conn, home2, away2, "avg_corners", 9.5, recent_n=5)
        b = stats.analyze_market(conn, home2, away2, "avg_corners", 9.5, recent_n=5,
                                 before_date=date2, exclude_match_id=mid2)
        print(f"  {home2} v {away2} on {date2}: all-time={a['weighted_avg']} "
              f"cutoff={b['weighted_avg']}")
        # The all-time value moves whenever either club plays a new match (recent
        # form is part of the blend), so it is pinned to the current dataset, not
        # to eternity. The cutoff value below is the one that must not drift.
        check("all-time (no cutoff) value matches the current dataset",
              a["weighted_avg"], 11.35)
        check("cutoff value unchanged", b["weighted_avg"], 11.36)

    print()
    print("=== 3. Missing data must not crash and must report no support ===")
    res = stats.analyze_market(conn, "Nonexistent FC", "Also Fake", "avg_goals", 2.5)
    check("weighted_avg is None", res["weighted_avg"], None)
    check("supported is None", res["supported"], None)
    check("evidence_points is 0", res["evidence_points"], 0)
    check("low_confidence is True", res["low_confidence"], True)

    print()
    print("=== 4. find_teams must search away teams too ===")
    teams = [t[0] for t in stats.find_teams(conn, "Bournemouth")]
    check("Bournemouth found via search", "Bournemouth" in teams, True)

    print()
    print("=== 5. League average + fair line are cutoff-aware ===")
    early = stats.get_league_average(conn, "Premier League", "2025-26", "avg_goals",
                                     before_date="2025-09-01")
    late = stats.get_league_average(conn, "Premier League", "2025-26", "avg_goals",
                                    before_date="2025-12-01")
    print(f"  avg goals before 2025-09-01: {early['average']} (n={early['matches']})")
    print(f"  avg goals before 2025-12-01: {late['average']} (n={late['matches']})")
    check("more matches before the later cutoff", late["matches"] > early["matches"], True)

    line, source, n = stats.suggest_line(conn, "Arsenal", "Chelsea", "avg_goals",
                                         league="Premier League", season="2025-26",
                                         before_date="2025-12-01")
    print(f"  suggested line: {line} (source={source}, n={n})")
    check("fair line is on an x.5 boundary (never a whole number)",
          line % 1, 0.5)

    print()
    print("=== 6. Push prevention: snap_to_half_line never returns a whole number ===")
    print("     A whole-number line creates pushes, and a push is neither over")
    print("     nor under. Booking it as UNDER is what flattered 'always under'.")
    cases = [
        # (league average seen so far, expected x.5 line)
        (2.77, 2.5),   # nearest valid x.5 (3.0 is a whole number, not allowed)
        (2.8, 2.5),    # 3.0 would be nearer but is not a valid line
        (3.0, 3.5),    # exact tie between 2.5 and 3.5 -> higher wins
        (2.6, 2.5),
        (3.2, 3.5),
        (0.01, 0.5),   # red cards: "over 0.5" means at least one red
        (9.92, 9.5),
        (2.25, 2.5),
    ]
    for raw, expected in cases:
        snapped = stats.snap_to_half_line(raw)
        check(f"snap {raw} -> {snapped}", snapped, expected)
        if snapped % 1 != 0.5:
            failures.append(f"snap {raw} returned a whole number: {snapped}")

    # Every market's real fair line must be x.5, checked against actual data.
    print()
    for market in ("goals", "corners", "shots", "fouls", "yellow_cards", "red_cards"):
        line, source, n = stats.suggest_line(
            conn, "Arsenal", "Chelsea", f"avg_{market}",
            league="Premier League", season="2025-26", before_date="2025-12-01")
        ok = line % 1 == 0.5
        print(f"  {market:14} fair line {line} (source={source})")
        if not ok:
            failures.append(f"{market} fair line is a whole number: {line}")

    print()
    print("=== 7. Backtest statistics: Wilson interval and McNemar z ===")
    print("     These decide whether a gap is real. If they are wrong the whole")
    print("     report lies, so they are pinned against hand-computed values.")

    # Wilson: 0/10 and 5/10 at 95%. Published values are (0, .2775) and
    # (.2366, .7634).
    lo, hi = backtester.wilson_interval(0, 10)
    check("wilson 0/10 lower", lo, 0.0)
    check("wilson 0/10 upper", hi, 0.2775)
    lo, hi = backtester.wilson_interval(5, 10)
    check("wilson 5/10 lower", lo, 0.2366)
    check("wilson 5/10 upper", hi, 0.7634)
    check("wilson 0/0 has no interval", backtester.wilson_interval(0, 0), (None, None))

    # McNemar z: 20 discordant wins for A and 5 for B -> 15/sqrt(25) = 3.0.
    def _m(over, a_sup, b_sup):
        return {"over": over,
                "predictions": {"a": {"supported": a_sup},
                                "b": {"supported": b_sup}}}

    matches = [_m(True, True, False) for _ in range(20)]
    matches += [_m(False, True, False) for _ in range(5)]
    p = backtester.paired_hits(matches, "a", "b")
    check("mcnemar n", p["n"], 25)
    check("mcnemar a_only", p["a_only"], 20)
    check("mcnemar b_only", p["b_only"], 5)
    check("mcnemar z = 15/sqrt(25)", p["z"], 3.0)
    check("verdict for z=3", backtester.hit_verdict(3.0), "AHEAD (clear)")
    check("verdict for z=0", backtester.hit_verdict(0.0), "no clear difference")

    # A tie must give z = 0 and not divide by zero, and so must a sample where
    # the two predictors never disagree.
    tied = [_m(True, True, False), _m(True, False, True)]
    check("mcnemar on a perfect tie", backtester.paired_hits(tied, "a", "b")["z"], 0.0)
    same = [_m(True, True, True), _m(False, False, False)]
    check("mcnemar with no disputes", backtester.paired_hits(same, "a", "b")["z"], 0.0)

    conn.close()

    # ------------------------------------------------------------------
    print("\n=== 8. A CSV refresh must not delete hand-entered matches ===")
    print("     Refreshing a season CSV is the normal way to bring in newly")
    print("     played matches, and it removes rows the file no longer lists.")
    print("     A row the admin typed in is not in that file at all, so it")
    print("     must be recognised by `source` and spared. Runs on a copy;")
    print("     the real database is never touched.")
    csv_path = BASE_DIR / "data" / "raw" / "premier-league" / "2026-27.csv"
    league, season = "Premier League", "2026-27"
    tmp_db = temp_db_copy("verify_copy.db")

    probe = sqlite3.connect(tmp_db)
    try:
        cur = probe.cursor()
        cur.execute("DELETE FROM matches WHERE home_team = ?", ("Verify Manual FC",))
        cur.execute(
            "INSERT INTO matches (date, league, season, status, source, home_team,"
            " away_team, home_goals, away_goals) VALUES (?,?,?,?,?,?,?,?,?)",
            ("2026-10-11", league, season, "completed", "manual",
             "Verify Manual FC", "Verify Ghost United", 1, 0))
        manual_id = cur.lastrowid
        cur.execute("INSERT INTO manual_stats (match_id, home_throw_ins) VALUES (?,?)",
                    (manual_id, 21))
        probe.commit()

        print(f"  hand-entered match id={manual_id} + 1 manual stat, then re-import")
        import_data.import_season(csv_path, league, season, tmp_db)

        cur.execute("SELECT source FROM matches WHERE id = ?", (manual_id,))
        row = cur.fetchone()
        check("hand-entered match survived the refresh", row is not None, True)
        check("  and is still marked source='manual'", row[0] if row else None, "manual")

        cur.execute("SELECT home_throw_ins FROM manual_stats WHERE match_id = ?",
                    (manual_id,))
        stat_row = cur.fetchone()
        check("  its manual stats are still linked", stat_row is not None, True)
        check("  and unchanged", stat_row[0] if stat_row else None, 21)

        cur.execute("SELECT COUNT(*) FROM manual_stats ms LEFT JOIN matches m"
                    " ON m.id = ms.match_id WHERE m.id IS NULL")
        check("no orphaned manual stats", cur.fetchone()[0], 0)
    finally:
        probe.close()
        tmp_db.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    print("\n=== 9. Hand-entered counts must reach the engine, and only that stat ===")
    print("     The eight manual-only markets have no CSV source, so typing")
    print("     them in is the ONLY way they ever carry data. Also checks that")
    print("     typing one stat cannot disturb a CSV-backed one.")
    tmp_db = temp_db_copy("verify_overlay.db")
    probe = sqlite3.connect(tmp_db)
    try:
        cur = probe.cursor()
        cur.execute("SELECT home_team FROM matches WHERE date < ? AND league = ?"
                    " GROUP BY home_team ORDER BY COUNT(*) DESC LIMIT 1",
                    ("2026-01-01", "Premier League"))
        team = cur.fetchone()[0]

        before = stats.get_team_home_averages(probe, team, before_date="2026-01-01")
        check("offsides carries no data before anything is typed",
              (before["avg_offsides"], before["n_offsides"]), (None, 0))

        ids = [r[0] for r in cur.execute(
            "SELECT id FROM matches WHERE home_team = ? AND date < ?"
            " ORDER BY date DESC LIMIT 5", (team, "2026-01-01"))]
        for mid in ids:
            cur.execute(
                "INSERT INTO manual_stats (match_id, home_offsides, away_offsides)"
                " VALUES (?,?,?) ON CONFLICT(match_id) DO UPDATE SET"
                " home_offsides=excluded.home_offsides,"
                " away_offsides=excluded.away_offsides", (mid, 9, 7))
        probe.commit()

        after = stats.get_team_home_averages(probe, team, before_date="2026-01-01")
        print(f"  {team}: typed offsides 9 into {len(ids)} matches")
        check("  the offsides average is now built from them",
              (after["avg_offsides"], after["n_offsides"]), (9.0, 5))
        check("  and no CSV market moved",
              after["avg_corners"], before["avg_corners"])
    finally:
        probe.close()
        tmp_db.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    print("\n=== 10. Typed team averages: useful, but never disguised as evidence ===")
    print("     The eight manual-only markets have no CSV source. A per-team")
    print("     average is the same quantity the engine would derive from match")
    print("     rows, so it can stand in for the derivation - but it is not")
    print("     measured, so it must not become evidence, must not overwrite")
    print("     counted data, and must not be used for a fixture it postdates.")
    tmp_db = temp_db_copy("verify_profiles.db")
    probe = sqlite3.connect(tmp_db)
    try:
        cur = probe.cursor()
        home, away, date, league, season = cur.execute(
            "SELECT home_team, away_team, date, league, season FROM matches "
            "WHERE league = 'Premier League' AND status = 'completed' "
            "ORDER BY date DESC LIMIT 1").fetchone()
        print(f"  fixture: {home} v {away}  ({league} {season}, {date})")

        def analyze(market):
            return stats.analyze_market(probe, home, away, f"avg_{market}",
                                        before_date=date, league=league, season=season)

        check("no profile, no counted data -> still no number",
              analyze("throw_ins")["weighted_avg"], None)
        corners_before = analyze("corners")

        cur.execute("INSERT INTO team_profiles (league, season, team, as_of, throw_ins)"
                    " VALUES (?,?,?,?,?)", (league, season, home, date, 20.2))
        probe.commit()
        check("one side alone is not a match total",
              analyze("throw_ins")["weighted_avg"], None)

        cur.execute("INSERT INTO team_profiles (league, season, team, as_of, throw_ins)"
                    " VALUES (?,?,?,?,?)", (league, season, away, date, 19.0))
        probe.commit()
        both = analyze("throw_ins")
        check("both sides give the exact sum of what was typed (not shrunk to a default)",
              both["weighted_avg"], 20.2 + 19.0)
        check("  the result says the number is external", both.get("profile_used"), True)
        check("  and dates it", both.get("profile_as_of"), date)
        check("  and claims no evidence from it", both["evidence_points"], 0)
        check("  and stays flagged low confidence", both["low_confidence"], True)

        cur.execute("UPDATE team_profiles SET as_of = ?", ("2099-01-01",))
        probe.commit()
        check("a profile dated after the fixture is ignored",
              analyze("throw_ins")["weighted_avg"], None)

        cur.execute("UPDATE team_profiles SET as_of = ?", (date,))
        probe.commit()
        check("a CSV-backed market is untouched by profiles",
              analyze("corners")["weighted_avg"], corners_before["weighted_avg"])

        csv_backed = {"goals", "shots", "shots_on_target", "fouls", "corners",
                      "yellow_cards", "red_cards"}
        check("a profile cannot supply a CSV-backed market",
              sorted(set(stats.PROFILE_STATS) & csv_backed), [])

        # A profile must never overwrite a counted average. Tested directly on
        # the guard, so the check does not also pin the shrinkage arithmetic.
        counted = stats.get_team_home_averages(probe, home, before_date=date)
        counted["avg_throw_ins"] = 31.0
        counted["n_throw_ins"] = 7
        stats._apply_profile(counted, {"as_of": date, "league": league,
                                       "season": season, "throw_ins": 99.0})
        check("  counted data is not overwritten by a profile",
              (counted["avg_throw_ins"], counted["n_throw_ins"]), (31.0, 7))
    finally:
        probe.close()
        tmp_db.unlink(missing_ok=True)

    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {failures}")
        return 1
    print("All engine checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
