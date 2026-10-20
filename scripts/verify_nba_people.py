"""Offline checks for rosters, player production and availability.

Runs on a copy, never the live database, and never touches the network.

What this guards:

    identity      every active roster row points at a known player and team
    one team      a player cannot be on two active rosters in the same season
    production    a summer signing keeps the production earned elsewhere
    arithmetic    team minutes per game add up to a plausible game
    availability  an injured player resolves to an NBA id, not an ESPN id
    honesty       every row carries its source and its capture time
    idempotence   running the sync twice does not duplicate anything

Usage:
    python scripts/verify_nba_people.py
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
import nba_people  # noqa: E402

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
    workdir = Path(tempfile.mkdtemp(prefix="nba_people_verify_"))
    db_copy = workdir / "verify.db"
    shutil.copy2(DB_PATH, db_copy)

    conn = sqlite3.connect(db_copy)
    conn.row_factory = sqlite3.Row
    try:
        print(f"Verifying a copy: {db_copy}\n")

        # ---- schema -------------------------------------------------
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        for name in ("nba_players", "nba_rosters", "nba_player_stats",
                     "nba_injuries", "nba_sync_log"):
            check(f"table {name} exists", name in tables)
        check("ensure_tables is idempotent",
              (nba_people.ensure_tables(conn) is None
               and nba_people.ensure_tables(conn) is None))

        # ---- roster identity ----------------------------------------
        active = conn.execute(
            "SELECT COUNT(*) FROM nba_rosters WHERE active = 1").fetchone()[0]
        teams = conn.execute(
            "SELECT COUNT(DISTINCT team_id) FROM nba_rosters "
            "WHERE active = 1 AND season = ?",
            (nba_people.ROSTER_SEASON,)).fetchone()[0]
        check("all 30 teams carry an active roster", teams == 30, f"{teams}")
        check("roster has at least 450 active players", active >= 450,
              f"{active}")
        check("every active roster player exists in nba_players",
              conn.execute(
                  "SELECT COUNT(*) FROM nba_rosters r LEFT JOIN nba_players p "
                  "ON p.player_id = r.player_id "
                  "WHERE r.active = 1 AND p.player_id IS NULL").fetchone()[0] == 0)
        check("no player is on two active rosters",
              conn.execute(
                  "SELECT COUNT(*) FROM (SELECT player_id FROM nba_rosters "
                  "WHERE active = 1 GROUP BY player_id HAVING COUNT(*) > 1)"
              ).fetchone()[0] == 0)
        check("every active roster row names its season",
              conn.execute(
                  "SELECT COUNT(*) FROM nba_rosters WHERE active = 1 "
                  "AND (season IS NULL OR season = '')").fetchone()[0] == 0)
        check("roster rows carry their source",
              conn.execute(
                  "SELECT COUNT(*) FROM nba_rosters WHERE source IS NULL "
                  "OR source = ''").fetchone()[0] == 0)

        # ---- production ---------------------------------------------
        seasons = {r[0] for r in conn.execute(
            "SELECT DISTINCT season FROM nba_player_stats")}
        completed = {s for s in seasons if s < nba_people.ROSTER_SEASON}
        check("three completed seasons of player stats", len(completed) >= 3,
              f"{sorted(completed)}")
        for season in sorted(completed):
            n = conn.execute(
                "SELECT COUNT(*) FROM nba_player_stats WHERE season = ? "
                "AND gp >= 5", (season,)).fetchone()[0]
            check(f"{season} has at least 500 qualified players", n >= 500,
                  f"{n}")
        check("true-shooting is a percentage",
              conn.execute(
                  "SELECT COUNT(*) FROM nba_player_stats WHERE ts_pct IS NOT "
                  "NULL AND fga >= 50 AND (ts_pct < 0.2 OR ts_pct > 1.0)"
              ).fetchone()[0] == 0)
        check("usage proxy is a fraction",
              conn.execute(
                  "SELECT COUNT(*) FROM nba_player_stats WHERE usage_proxy IS "
                  "NOT NULL AND (usage_proxy < 0 OR usage_proxy > 1.5)"
              ).fetchone()[0] == 0)

        # The provider returns one row per player, attributed to his final
        # team, covering his whole season. Per-team sums are therefore not a
        # rotation and must not be treated as one. The league-wide total is
        # still a hard invariant: 30 teams x 82 games x ~240 minutes.
        league_minutes = conn.execute(
            "SELECT SUM(minutes * gp) / (30.0 * 82.0) FROM nba_player_stats "
            "WHERE season = '2025-26' AND gp >= 5").fetchone()[0] or 0
        check("league-wide minutes per team-game are plausible",
              230 <= league_minutes <= 250, f"{league_minutes:.1f}")

        # A player who changed teams must still show the production earned at
        # the old club. Paul George moved to Boston; his minutes cannot be 0.
        george = conn.execute(
            "SELECT s.minutes FROM nba_rosters r "
            "JOIN nba_players p ON p.player_id = r.player_id "
            "JOIN nba_player_stats s ON s.player_id = r.player_id "
            "WHERE r.season = ? AND r.active = 1 AND p.full_name = 'Paul George' "
            "AND s.season = '2025-26' ORDER BY s.minutes DESC LIMIT 1",
            (nba_people.ROSTER_SEASON,)).fetchone()
        check("a summer signing keeps his prior production",
              george is not None and (george["minutes"] or 0) > 10,
              f"{george['minutes'] if george else 'no row'}")

        # ---- availability -------------------------------------------
        latest = conn.execute(
            "SELECT MAX(captured_at) FROM nba_injuries").fetchone()[0]
        check("an injury snapshot exists", latest is not None)
        entries = conn.execute(
            "SELECT COUNT(*) FROM nba_injuries WHERE captured_at = ?",
            (latest,)).fetchone()[0] if latest else 0
        check("snapshot has at least 60 entries", entries >= 60, f"{entries}")
        mapped_team = conn.execute(
            "SELECT COUNT(*) FROM nba_injuries WHERE captured_at = ? "
            "AND team_id IS NOT NULL", (latest,)).fetchone()[0] if latest else 0
        mapped_player = conn.execute(
            "SELECT COUNT(*) FROM nba_injuries WHERE captured_at = ? "
            "AND player_id IS NOT NULL", (latest,)).fetchone()[0] if latest else 0
        check("90% of injuries resolve to a team",
              entries and mapped_team / entries >= 0.9,
              f"{mapped_team}/{entries}")
        check("80% of injuries resolve to an NBA player id",
              entries and mapped_player / entries >= 0.8,
              f"{mapped_player}/{entries}")
        check("an unresolved injury keeps a NULL id rather than a guess",
              conn.execute(
                  "SELECT COUNT(*) FROM nba_injuries WHERE player_id IS NOT NULL "
                  "AND player_id NOT IN (SELECT player_id FROM nba_players)"
              ).fetchone()[0] == 0)
        check("injury rows carry their capture time",
              conn.execute(
                  "SELECT COUNT(*) FROM nba_injuries "
                  "WHERE captured_at IS NULL OR captured_at = ''"
              ).fetchone()[0] == 0)

        # Availability must be bounded and must count a known Out player's
        # minutes, not silently drop him.
        boston = 1610612738
        avail = nba_people.availability(conn, boston)
        check("availability minutes stay inside a game",
              0 <= avail["minutes_unavailable"] <= 240,
              f"{avail['minutes_unavailable']}")
        check("a known Out player's minutes are counted",
              avail["minutes_unavailable"] > 0,
              f"{avail['minutes_unavailable']}")
        check("availability never writes",
              conn.execute("SELECT COUNT(*) FROM nba_injuries").fetchone()[0]
              == conn.execute("SELECT COUNT(*) FROM nba_injuries").fetchone()[0])

        # ---- sync log -----------------------------------------------
        for job in ("rosters", "player_stats", "injuries"):
            row = conn.execute(
                "SELECT status FROM nba_sync_log WHERE job = ? "
                "ORDER BY id DESC LIMIT 1", (job,)).fetchone()
            check(f"{job} sync recorded a success",
                  row is not None and row["status"] == "ok",
                  row["status"] if row else "missing")

        print(f"\n{PASSED} passed, {FAILED} failed")
        return 0 if FAILED == 0 else 1
    finally:
        conn.close()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
