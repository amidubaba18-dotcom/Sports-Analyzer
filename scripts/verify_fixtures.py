"""Offline checks for the fixture pipeline (scripts/fixtures.py).

Runs against a throwaway COPY of the live database, so it can publish, map and
re-publish freely without touching the real dashboard. No network: the provider
is replaced by hand-built rows that look exactly like fetch_competition output.

Run:
    .venv\\Scripts\\python.exe scripts\\verify_fixtures.py
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "scripts"))

import fixtures  # noqa: E402

passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  [{detail}]" if detail else ""))


def row(pid: str, home: str, away: str, day: str, status: str = "scheduled",
        league: str = "Premier League", code: str = "PL",
        home_short: str | None = None, away_short: str | None = None) -> dict:
    return {
        "provider_id": pid,
        "competition": code,
        "league": league,
        "date": day,
        "kickoff": "15:00",
        "home_provider_name": home,
        "away_provider_name": away,
        "home_provider_short": home_short,
        "away_provider_short": away_short,
        "status": status,
        "raw": "{}",
    }


def main() -> int:
    source = BASE / "data" / "sports_analyzer.db"
    if not source.exists():
        print(f"No database at {source}")
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="sa_fixtures_"))
    db = workdir / "test.db"
    shutil.copy2(source, db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    fixtures.set_setting(conn, "last_fixture_fetch", "")
    before_matches = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    today = date.today()
    day = (today + timedelta(days=2)).isoformat()

    try:
        print("\n1. Name resolution")
        team, how, _ = fixtures.resolve_team(conn, "Arsenal FC", None)
        check("'Arsenal FC' resolves to Arsenal", team == "Arsenal", f"{team}/{how}")
        team, how, _ = fixtures.resolve_team(conn, "Manchester United FC", None)
        check("'Manchester United FC' resolves to Man United",
              team == "Man United", f"{team}/{how}")
        team, how, _ = fixtures.resolve_team(conn, "FC Bayern München", None)
        check("accented 'FC Bayern München' resolves", team == "Bayern Munich",
              f"{team}/{how}")
        team, how, suggestions = fixtures.resolve_team(conn, "ZZ Nowhere FC", None)
        check("unknown name is NOT guessed", team is None and how == "unmapped",
              f"{team}/{how}")
        check("unknown name still gets suggestions", len(suggestions) == 3)

        print("\n2. Ingest and the mapping gate")
        rows = [
            row("t1", "Arsenal FC", "Chelsea FC", day),
            row("t2", "ZZ Nowhere FC", "Chelsea FC", day),
            row("t3", "Liverpool FC", "Everton FC", day, status="postponed"),
        ]
        summary = fixtures.ingest(conn, rows, log=lambda _: None)
        check("three rows inserted", summary["inserted"] == 3, str(summary))
        check("two rows mapped", summary["mapped"] == 2, str(summary))
        check("one row needs attention", summary["needs_attention"] == 1, str(summary))

        stored = {r["provider_id"]: r for r in conn.execute(
            "SELECT * FROM available_fixtures")}
        check("unmapped row is held back",
              stored["t2"]["mapping_state"] == "needs_attention")
        check("mapped row has canonical names",
              stored["t1"]["home_team"] == "Arsenal"
              and stored["t1"]["away_team"] == "Chelsea",
              f"{stored['t1']['home_team']} v {stored['t1']['away_team']}")
        check("season derived from the fixture date",
              stored["t1"]["season"] == fixtures.normalize_season(None, day),
              stored["t1"]["season"])

        print("\n3. Ingest never touches the historical table")
        after_matches = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        check("matches row count unchanged", before_matches == after_matches,
              f"{before_matches} -> {after_matches}")

        print("\n4. Auto-publish")
        report = fixtures.auto_publish(conn, lookahead_days=7, log=lambda _: None)
        check("only the eligible fixture was published",
              report["published"] == 1, str(report))
        check("postponed fixture was not published",
              stored["t3"]["published_id"] is None)
        check("unmapped fixture stays unpublished",
              stored["t2"]["published_id"] is None)

        published = conn.execute(
            "SELECT * FROM published_matches WHERE date = ?", (day,)).fetchone()
        check("published with canonical names, not provider names",
              published is not None
              and published["home_team"] == "Arsenal"
              and published["away_team"] == "Chelsea",
              str(dict(published)) if published else "missing")
        linked = conn.execute(
            "SELECT published_id FROM available_fixtures WHERE provider_id = 't1'"
        ).fetchone()
        check("published fixture is linked back",
              linked["published_id"] == published["id"],
              f"{linked['published_id']} vs {published['id']}")

        again = fixtures.auto_publish(conn, lookahead_days=7, log=lambda _: None)
        count = conn.execute("SELECT COUNT(*) FROM published_matches WHERE date = ?",
                             (day,)).fetchone()[0]
        check("running auto-publish twice creates no duplicate",
              again["published"] == 0 and count == 1, f"{again} / {count}")

        print("\n5. Manual publish is idempotent")
        result = fixtures.publish_fixture(conn, stored["t1"]["id"])
        check("already-published fixture is not inserted again",
              result["ok"] and result["reason"] == "already published", str(result))

        print("\n6. Mapping a name once fixes every fixture")
        alias = fixtures.upsert_alias(conn, "ZZ Nowhere FC", "Wolves")
        check("alias saved", alias["team"] == "Wolves", str(alias))
        refreshed = conn.execute(
            "SELECT * FROM available_fixtures WHERE provider_id = 't2'").fetchone()
        check("held fixture becomes publishable",
              refreshed["mapping_state"] == "ok"
              and refreshed["home_team"] == "Wolves",
              f"{refreshed['mapping_state']}/{refreshed['home_team']}")
        check("saving the alias auto-published it",
              refreshed["published_id"] is not None
              and alias["auto_published"] == 1,
              f"published_id={refreshed['published_id']} report={alias}")
        again = fixtures.publish_fixture(conn, refreshed["id"])
        check("publishing again stays idempotent",
              again["ok"] and again["reason"] == "already published", str(again))

        print("\n7. Alias validation")
        try:
            fixtures.upsert_alias(conn, "Another FC", "Not A Real Team")
            check("unknown canonical team is rejected", False)
        except ValueError:
            check("unknown canonical team is rejected", True)

        print("\n8. Re-ingest keeps decisions")
        fixtures.ingest(conn, rows, log=lambda _: None)
        kept = conn.execute(
            "SELECT published_id, home_team FROM available_fixtures "
            "WHERE provider_id = 't2'").fetchone()
        check("refresh does not un-publish a fixture",
              kept["published_id"] is not None, str(dict(kept)))
        check("refresh does not lose the saved mapping",
              kept["home_team"] == "Wolves", str(dict(kept)))

        print("\n9. The season calendar")
        cal_rows = conn.execute(
            "SELECT COUNT(*) FROM season_fixtures").fetchone()[0]
        check("the full season is stored", cal_rows > 0, f"{cal_rows} rows")
        check("no calendar fixture lacks a mapped team",
              conn.execute("SELECT COUNT(*) FROM season_fixtures "
                           "WHERE home_team IS NULL OR away_team IS NULL"
                           ).fetchone()[0] == 0)
        check("provider ids are unique",
              conn.execute("SELECT COUNT(*) FROM (SELECT provider_id "
                           "FROM season_fixtures GROUP BY provider_id "
                           "HAVING COUNT(*) > 1)").fetchone()[0] == 0)

        far = (today + timedelta(days=90)).isoformat()
        far_season = fixtures._season_for(far)
        cal_row = {
            "provider_id": "verify-cal-1", "competition": "PL",
            "league": "Premier League", "season": far_season, "matchday": 99,
            "date": far, "kickoff": "15:00",
            "home_provider_name": "Arsenal FC",
            "away_provider_name": "Chelsea FC",
            "home_provider_short": "Arsenal", "away_provider_short": "Chelsea",
            "status": "timed", "home_goals": None, "away_goals": None,
            "raw": "{}",
        }
        fixtures.ingest_calendar(conn, [cal_row], log=lambda _: None)
        fixtures.ingest_calendar(conn, [cal_row], log=lambda _: None)
        check("calendar ingest is idempotent",
              conn.execute("SELECT COUNT(*) FROM season_fixtures "
                           "WHERE provider_id = 'verify-cal-1'").fetchone()[0] == 1)

        published_before = conn.execute(
            "SELECT COUNT(*) FROM published_matches").fetchone()[0]
        fixtures.ingest_calendar(conn, [cal_row], log=lambda _: None)
        check("calendar ingest publishes nothing",
              conn.execute("SELECT COUNT(*) FROM published_matches"
                           ).fetchone()[0] == published_before)

        view = fixtures.season_view(conn, "Premier League", far_season)
        far_entry = next(f for d in view["days"] for f in d["fixtures"]
                         if f["date"] == far)
        check("a fixture beyond the window is not analysable",
              far_entry["state"] == "outside", far_entry["state"])
        check("the window opens exactly 14 days before kickoff",
              far_entry["window_opens"]
              == (date.fromisoformat(far)
                  - timedelta(days=fixtures.LOOKAHEAD_DAYS)).isoformat(),
              far_entry["window_opens"])

        played = [f for d in view["days"] for f in d["fixtures"]
                  if f["state"] == "played"]
        check("played fixtures carry a score",
              bool(played) and played[0]["home_goals"] is not None)
        linked_played = [f for f in played if f["match_id"]]
        check("played fixtures link to their match row",
              bool(linked_played)
              and conn.execute("SELECT 1 FROM matches WHERE id = ?",
                               (linked_played[0]["match_id"],)).fetchone()
              is not None)

        history = fixtures.season_view(conn, "Eredivisie", "2024-25")
        check("a season with no calendar falls back to history",
              history["source"] == "history"
              and history["counts"]["played"] == 306,
              f"{history['source']}/{history['counts']['played']}")
    finally:
        conn.close()
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
