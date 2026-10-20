"""End-to-end smoke test against a RUNNING backend.

Checks the whole path the user actually walks:
    publish a fixture -> see it on the dashboard -> analyse it -> read evidence
    -> type a team profile -> see it labelled as typed -> delete it again

Run the server first, then:
    python scripts/smoke_test.py
    python scripts/smoke_test.py --with-download   # also fetches the season CSVs

It cleans up after itself (deletes the fixture it published, the profiles it
typed, and anything else it created), and it restores any profile it found.
The download check is opt-in because it uses the network and rewrites data/raw/.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta

BASE = "http://localhost:8001"

failures: list[str] = []


def call(method: str, path: str, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def main(with_download: bool = False):
    print("=" * 62)
    print("Sports Analyzer - end to end smoke test")
    print("=" * 62)

    status, health = call("GET", "/health")
    check("GET /health", status == 200 and health.get("status") == "ok",
          f"historical={health.get('historical_matches') if health else '?'}")
    if status != 200:
        print("\nServer is not reachable. Start it first:")
        print("  cd backend")
        print(r"  ..\.venv\Scripts\python.exe -m uvicorn main:app --port 8001")
        return 1

    status, stats = call("GET", "/stats")
    check("GET /stats returns markets", status == 200 and len(stats["markets"]) >= 14,
          f"{len(stats['markets']) if status == 200 else '?'} markets")

    status, leagues = call("GET", "/leagues")
    check("GET /leagues", status == 200 and "Premier League" in leagues["leagues"],
          f"{len(leagues['leagues']) if status == 200 else '?'} leagues")

    status, teams = call("GET", "/teams/find?q=Bournemouth")
    check("GET /teams/find searches away teams too", status == 200 and len(teams["teams"]) > 0,
          f"{len(teams['teams']) if status == 200 else '?'} hits")

    # --- publish a fixture -------------------------------------------------
    fixture_date = (date.today() + timedelta(days=3)).isoformat()
    payload = {
        "league": "Premier League",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "date": fixture_date,
        "kickoff": "19:30",
        "markets": ["corners", "shots", "goals"],
    }
    status, created = call("POST", "/admin/published/matches", payload)
    check("POST /admin/published/matches", status in (200, 201),
          f"id={created.get('id') if isinstance(created, dict) else created}")
    if status not in (200, 201):
        print("  server said:", created)
        return 1
    new_id = created["id"]

    # --- duplicate protection ---------------------------------------------
    status, dup = call("POST", "/admin/published/matches", payload)
    check("duplicate publish is rejected (409)", status == 409,
          f"status={status}")

    # --- dashboard --------------------------------------------------------
    status, published = call("GET", f"/published/matches?date={fixture_date}")
    check("GET /published/matches shows the fixture",
          status == 200 and any(m["id"] == new_id for m in published),
          f"{len(published) if status == 200 else '?'} published that day")

    # The dashboard defaults to today; these two calls are what let it find the
    # nearest day that has something instead of showing an empty screen.
    status, days = call("GET", "/published/dates")
    ok_days = status == 200 and isinstance(days, dict)
    check("GET /published/dates lists the day just published",
          ok_days and any(d["date"] == fixture_date for d in days["dates"]),
          f"{len(days['dates']) if ok_days else '?'} day(s)")
    if ok_days:
        check("  and every listed day really has a published match",
              all(d["matches"] > 0 for d in days["dates"]),
              f"counts={[d['matches'] for d in days['dates']]}")

    status, empty_day = call("GET", "/published/matches?date=1999-01-01")
    check("a day with nothing published returns an empty list",
          status == 200 and empty_day == [],
          f"{len(empty_day) if status == 200 else '?'} rows")

    # --- analysis ---------------------------------------------------------
    status, analysis = call("GET", f"/published/matches/{new_id}/analysis?markets=corners")
    ok = status == 200 and analysis["analysis"]
    check("GET /published/matches/{id}/analysis", ok)
    if ok:
        a = analysis["analysis"][0]
        print(f"       corners -> weighted {a['weighted_avg']} "
              f"(line {a['line']}, {a['line_source']}), "
              f"supported={a['supported']}, evidence={a['evidence_points']}")
        check("  analysis has the three weighted sources",
              set(a["sources"].keys()) == {"home_away", "recent_form", "h2h"})
        check("  analysis reports a cutoff", analysis.get("cutoff") == fixture_date,
              f"cutoff={analysis.get('cutoff')}")
        has_value = a["weighted_avg"] is not None
        check("  analysis produced a number from real data", has_value)

    # --- line override ----------------------------------------------------
    status, custom = call("GET",
                          f"/published/matches/{new_id}/analysis?markets=corners&lines=corners=99.5")
    ok2 = status == 200 and custom["analysis"]
    if ok2:
        c = custom["analysis"][0]
        check("line override is honoured", c["line"] == 99.5, f"line={c['line']}")
        check("  and flips the verdict", c["supported"] is False, f"supported={c['supported']}")

    # --- fair line --------------------------------------------------------
    status, fair = call("GET", f"/published/matches/{new_id}/analysis?markets=corners")
    if status == 200 and fair["analysis"]:
        f = fair["analysis"][0]
        check("fair line is used when no line given", f["line_source"] == "fair",
              f"line={f['line']} source={f['line_source']}")

    # --- evidence (form / h2h tabs) ---------------------------------------
    status, evidence = call("GET", f"/published/matches/{new_id}/evidence?stat=corners")
    ok3 = status == 200
    check("GET /published/matches/{id}/evidence", ok3)
    if ok3:
        check("  evidence has home recent rows", isinstance(evidence["home_recent_rows"], list),
              f"{len(evidence['home_recent_rows'])} rows")
        check("  evidence has h2h rows", isinstance(evidence["h2h_rows"], list),
              f"{len(evidence['h2h_rows'])} meetings")

    # --- manual stats round trip ------------------------------------------
    status, matches = call("GET", "/matches/today")
    if status == 200 and matches:
        mid = matches[0]["id"]
        status, put = call("PUT", f"/admin/matches/{mid}/manual-stats",
                           {"home_tackles": 17, "away_tackles": 21})
        check("PUT manual stats", status == 200)
        status, got = call("GET", f"/admin/matches/{mid}/manual-stats")
        check("  manual stats read back", status == 200 and got.get("home_tackles") == 17,
              f"home_tackles={got.get('home_tackles') if status == 200 else '?'}")
        call("DELETE", f"/admin/matches/{mid}/manual-stats")

    # --- team profiles (typed per-game averages) ---------------------------
    # Arsenal and Chelsea are the fixture published above, so the engine will
    # actually use these numbers. Any profile the admin already has for them is
    # saved first and put back at the end: a test must never delete real work.
    status, prof = call("GET", "/admin/team-profiles?league=Premier%20League")
    ok4 = status == 200
    check("GET /admin/team-profiles", ok4 and prof.get("total", 0) > 0,
          f"{prof.get('total') if ok4 else '?'} teams, "
          f"{prof.get('with_profile') if ok4 else '?'} with a profile")
    if ok4:
        check("  it offers exactly the eight CSV-less markets",
              set(prof["markets"]) == {
                  "tackles", "throw_ins", "aerial_duels", "offsides",
                  "interceptions", "goal_kicks", "saves", "substitutions"},
              f"{len(prof['markets'])} markets")

        test_teams = ("Arsenal", "Chelsea")
        existing = {t["team"]: t["profile"] for t in prof["teams"]
                    if t["team"] in test_teams and t["profile"]}

        status, _ = call("PUT", "/admin/team-profiles",
                         {"league": "Premier League", "team": "Arsenal",
                          "season": prof["season"], "as_of": date.today().isoformat(),
                          "throw_ins": 400})
        check("a mistyped average is rejected (422)", status == 422,
              f"status={status}")

        made = []
        for team, value in (("Arsenal", 20.2), ("Chelsea", 19.0)):
            status, saved = call("PUT", "/admin/team-profiles",
                                 {"league": "Premier League", "team": team,
                                  "season": prof["season"],
                                  "as_of": date.today().isoformat(),
                                  "throw_ins": value})
            made.append(saved.get("profile") if status == 200 and saved else None)
        ids = [m["id"] for m in made if m and m.get("id")]
        check("PUT /admin/team-profiles", len(ids) == 2, f"ids={ids}")

        status, _ = call("PUT", "/admin/team-profiles",
                         {"league": "Premier League", "team": "Arsenal",
                          "season": prof["season"], "as_of": date.today().isoformat()})
        check("an empty profile is refused (400)", status == 400, f"status={status}")

        status, typed = call("GET",
                             f"/published/matches/{new_id}/analysis?markets=throw_ins")
        ta = typed["analysis"][0] if status == 200 and typed.get("analysis") else None
        if ta:
            check("  a typed average reaches the analysis",
                  ta["weighted_avg"] is not None
                  and abs(ta["weighted_avg"] - 39.2) < 0.05,
                  f"weighted={ta['weighted_avg']} (20.2 + 19.0)")
            check("  but it is not evidence and not confident",
                  ta["evidence_points"] == 0 and ta["low_confidence"] is True,
                  f"evidence={ta['evidence_points']} "
                  f"low_confidence={ta['low_confidence']}")
            check("  and it is labelled as typed, with no invented reference",
                  ta["profile_used"] is True and ta["has_league_reference"] is False,
                  f"profile_used={ta['profile_used']} "
                  f"has_league_reference={ta['has_league_reference']}")
        else:
            check("  a typed average reaches the analysis", False, f"status={status}")

        status, still = call("GET",
                             f"/published/matches/{new_id}/analysis?markets=corners")
        if status == 200 and still.get("analysis"):
            check("  a profile cannot touch a counted market",
                  still["analysis"][0]["profile_used"] is False,
                  f"corners profile_used={still['analysis'][0]['profile_used']}")

        for pid in ids:
            call("DELETE", f"/admin/team-profiles/{pid}")
        restored = []
        for team, old in existing.items():
            if not any(v is not None for v in old["values"].values()):
                # An empty row is not a profile, and the API no longer accepts
                # one, so it cannot be restored. Removing it is the honest result.
                call("DELETE", f"/admin/team-profiles/{old['id']}")
                continue
            status, _ = call("PUT", "/admin/team-profiles",
                             {"league": old["league"], "team": old["team"],
                              "season": old["season"], "as_of": old["as_of"],
                              **old["values"]})
            restored.append(status == 200)
        status, after = call("GET", "/admin/team-profiles?league=Premier%20League")
        left = [t for t in (after.get("teams") or [])
                if t["team"] in test_teams and t["profile"]]
        check("  cleanup leaves the database as it was found",
              status == 200 and len(left) == len(existing),
              f"{len(left)} profile(s) on the test teams, "
              f"{len(existing)} before")

    # --- downloading the season files (network, opt-in) --------------------
    if with_download:
        status, dl = call("POST", "/admin/download-csvs", {"import_after": False})
        ok5 = status == 200 and isinstance(dl, dict)
        report = dl.get("download", {}) if ok5 else {}
        check("POST /admin/download-csvs", ok5 and report.get("downloaded", 0) > 0,
              f"{report.get('downloaded', '?')} downloaded, "
              f"{report.get('refused', '?')} refused, "
              f"{report.get('failed', '?')} failed")
        check("  every league arrived and none was refused",
              ok5 and report.get("refused") == 0 and report.get("failed") == 0
              and dl.get("ok") is True, f"ok={dl.get('ok') if ok5 else '?'}")
        check("  and it did not import behind your back",
              ok5 and "import" not in dl,
              "import_after=False added no rows")
        status, bad = call("POST", "/admin/download-csvs",
                           {"season": "not-a-season", "import_after": False})
        check("  an unreadable season is rejected (400)", status == 400,
              f"status={status}")
    else:
        print("[SKIP] POST /admin/download-csvs  (pass --with-download: it uses the network)")

    # --- bad input --------------------------------------------------------
    status, _ = call("GET", "/published/matches/999999/analysis?markets=corners")
    check("unknown published id returns 404", status == 404, f"status={status}")

    status, _ = call("GET", f"/published/matches/{new_id}/analysis?markets=nonsense")
    check("unknown market returns 400", status == 400, f"status={status}")

    # --- automatic fixtures ----------------------------------------------
    # Read-only checks plus a self-cleaning alias round trip. Deliberately NO
    # fetch and NO publish-all: on a real installation those would publish live
    # fixtures as a side effect of running a test.
    status, fsettings = call("GET", "/admin/fixture-settings")
    ok_fs = status == 200 and len(fsettings.get("leagues", [])) == 6
    check("GET /admin/fixture-settings lists the six leagues", ok_fs,
          f"{len(fsettings.get('leagues', [])) if status == 200 else '?'} leagues")
    check("  and reports whether the provider key is configured",
          status == 200 and isinstance(fsettings.get("token_configured"), bool),
          f"token_configured={fsettings.get('token_configured') if status == 200 else '?'}")

    status, available = call("GET", "/admin/available-fixtures")
    check("GET /admin/available-fixtures", status == 200
          and isinstance(available.get("fixtures"), list),
          f"{available.get('total') if status == 200 else '?'} in the next 14 days")

    status, _ = call("GET", "/admin/available-fixtures?from=not-a-date")
    check("  a malformed date is rejected (400)", status == 400, f"status={status}")

    status, _ = call("GET", "/admin/team-aliases")
    check("GET /admin/team-aliases", status == 200 and "unmapped" in (_ or {}),
          f"status={status}")

    status, alias = call("PUT", "/admin/team-aliases",
                         {"provider_name": "Smoke Test FC", "team": "Arsenal"})
    check("PUT /admin/team-aliases saves an exact mapping",
          status == 200 and alias.get("team") == "Arsenal",
          f"{alias.get('team') if isinstance(alias, dict) else alias}")
    status, _ = call("PUT", "/admin/team-aliases",
                     {"provider_name": "Smoke Test FC", "team": "Not A Real Team"})
    check("  an unknown database team is rejected (400)", status == 400,
          f"status={status}")
    _, alias_list = call("GET", "/admin/team-aliases")
    for row in (alias_list or {}).get("aliases", []):
        if row.get("provider_name") == "Smoke Test FC":
            call("DELETE", f"/admin/team-aliases/{row['id']}")

    if not fsettings.get("token_configured"):
        status, err = call("POST", "/admin/fixtures/fetch", {})
        check("  fetch without a token explains what to do (400)",
              status == 400 and "FOOTBALL_DATA_TOKEN" in str(err),
              f"status={status}")
    else:
        print("[SKIP] POST /admin/fixtures/fetch  "
              "(a token is set; the daily scheduler owns the live fetch)")

    # --- NBA (read-only) --------------------------------------------------
    # The NBA is a separate sport with separate tables. These checks are
    # read-only on purpose: publishing or refreshing here would mutate the live
    # calendar as a side effect of running a test.
    status, nba_status = call("GET", "/nba/status")
    check("GET /nba/status", status == 200 and "counts" in (nba_status or {}),
          f"status={status}")
    if status == 200:
        status, nba_day = call("GET", "/nba/published")
        games = (nba_day or {}).get("games") or []
        check("GET /nba/published returns a date and a list",
              status == 200 and isinstance(games, list)
              and (nba_day or {}).get("date") is not None,
              f"status={status}")
        status, nba_dates = call("GET", "/nba/published/dates")
        check("GET /nba/published/dates lists matchdays",
              status == 200 and isinstance((nba_dates or {}).get("dates"), list),
              f"status={status}")
        status, nba_upcoming = call("GET", "/nba/fixtures?days=7")
        check("GET /nba/fixtures returns the window",
              status == 200
              and isinstance((nba_upcoming or {}).get("fixtures"), list),
              f"status={status}")
        if games:
            gid = games[0]["game_id"]
            status, analysis = call("GET", f"/nba/analysis/{gid}")
            markets = (analysis or {}).get("markets") or []
            available = [m for m in markets if m.get("available")]
            check("GET /nba/analysis returns a projection",
                  status == 200
                  and (analysis or {}).get("projection", {}).get("total", 0) > 0,
                  f"status={status}")
            check("  every NBA line is x.5",
                  bool(available)
                  and all((m["line"] * 2) % 2 == 1 for m in available),
                  f"{len(available)} available market(s)")
            check("  supported matches the comparison",
                  all(m["supported"] == (m["projection"] > m["line"])
                      for m in available),
                  f"{len(available)} market(s) checked")
        status, _ = call("GET", "/nba/analysis/0000000000")
        check("  an unknown NBA game is a clear 404", status == 404,
              f"status={status}")

    # --- the season calendar (read-only) ----------------------------------
    status, season = call("GET", "/season?league=Premier%20League")
    check("GET /season returns the whole season",
          status == 200 and len((season or {}).get("days") or []) > 0,
          f"status={status}")
    if status == 200:
        fixtures = [f for d in season["days"] for f in d["fixtures"]]
        played = [f for f in fixtures if f["state"] == "played"]
        outside = [f for f in fixtures if f["state"] == "outside"]
        check("  the season has both results and future fixtures",
              bool(played) and bool(outside),
              f"{len(played)} played, {len(outside)} future")
        check("  played fixtures carry a score",
              all(f["home_goals"] is not None for f in played),
              f"{len(played)} checked")
        check("  a future fixture opens its analysis 14 days before kickoff",
              bool(outside)
              and all(f["window_opens"] < f["date"] and
                      (date.fromisoformat(f["date"])
                       - date.fromisoformat(f["window_opens"])).days == 14
                      for f in outside),
              f"{len(outside)} future")
    status, past = call("GET", "/season?league=Eredivisie&season=2024-25")
    check("a past season is served from history",
          status == 200 and (past or {}).get("source") == "history",
          f"status={status} source={(past or {}).get('source')}")
    status, _ = call("GET", "/season?league=Not%20A%20League")
    check("  an unknown league is a clear 404", status == 404, f"status={status}")

    # --- the prediction board (read-only) ---------------------------------
    # Find a day the board has actually built for, rather than hard-coding one.
    board, board_date = None, None
    for offset in range(7):
        day = (date.today() + timedelta(days=offset)).isoformat()
        status, candidate = call("GET", f"/board?date={day}&sport=all&limit=10")
        if status == 200 and (candidate or {}).get("picks"):
            board, board_date = candidate, day
            break
    picks = (board or {}).get("picks") or []
    check("GET /board returns ranked picks",
          board is not None and len(picks) > 0,
          f"date={board_date} picks={len(picks)}")
    if picks:
        check("  every probability is inside the clamp",
              all(0.05 <= p["probability"] <= 0.95 for p in picks))
        check("  every pick names its line source",
              all(p["line_source"] for p in picks))
        check("  the board is ordered by probability",
              all(picks[i]["probability"] >= picks[i + 1]["probability"]
                  for i in range(len(picks) - 1)))
        check("  at most two picks per fixture",
              max(sum(1 for q in picks
                      if (q["date"], q["sport"], q["game_key"])
                      == (p["date"], p["sport"], p["game_key"]))
                  for p in picks) <= 2)
    status, nba_board = call(
        f"GET", f"/board?date={board_date or date.today().isoformat()}&sport=nba")
    check("  the sport filter is respected",
          status == 200
          and all(p["sport"] == "nba"
                  for p in (nba_board or {}).get("picks") or []),
          f"status={status}")
    status, _ = call("GET", "/board?sport=cricket")
    check("  an unknown sport is a clear 400", status == 400, f"status={status}")
    status, _ = call("GET", "/board?date=not-a-date")
    check("  an unknown date is a clear 400", status == 400, f"status={status}")
    status, predictions = call("GET", "/admin/predictions/status")
    check("GET /admin/predictions/status reports measured markets",
          status == 200 and (predictions or {}).get("markets_ready", 0) > 0,
          f"status={status} markets={(predictions or {}).get('markets_ready')}")

    # --- cleanup ----------------------------------------------------------
    status, _ = call("DELETE", f"/admin/published/matches/{new_id}")
    check("DELETE published match (cleanup)", status == 200, f"status={status}")

    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("All smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(with_download="--with-download" in sys.argv[1:]))
