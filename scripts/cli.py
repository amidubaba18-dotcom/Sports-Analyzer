"""Interactive command line analysis.

This was never meant to be the product - the web UI is. It exists because the
fastest way to sanity-check the engine is from a terminal, with no browser, no
server and no cache involved.

Usage
-----
    python scripts/cli.py

Commands inside the prompt:
    find <text>              search team names (use before guessing a spelling)
    list                     show today's matches with their ids
    list 2025-10-04          show matches on a date
    pick <id>                analyse a stored match, cutoff-aware
    markets                  list available markets and default lines
    quit

Direct (non-interactive) usage:
    python scripts/cli.py --date 2025-10-04
    python scripts/cli.py --match-id 1234 --markets corners,shots
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stats  # noqa: E402
from stats import STAT_COLUMNS  # noqa: E402
from matches_engine import analyze_match  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "sports_analyzer.db"


def fmt(value, width=7):
    if value is None:
        return "n/a".rjust(width)
    return f"{value:.2f}".rjust(width)


def print_match_list(conn, date=None):
    df = stats.get_matches_by_date(conn, date)
    if df.empty:
        print(f"  No matches for {date or 'today'}.")
        return df
    print(f"  {'id':>7}  {'date':10}  {'league':16}  match")
    for _, row in df.iterrows():
        print(f"  {row['id']:>7}  {row['date']:10}  {str(row['league'])[:16]:16}  "
              f"{row['home_team']} v {row['away_team']}")
    return df


def print_result(match, results):
    print()
    print(f"  {match['home_team']} v {match['away_team']}   "
          f"({match['league']} {match['season']}, {match['date']})")
    print(f"  cutoff: only matches strictly before {match['date']} are used")
    print()
    print(f"  {'market':17} {'weighted':>9} {'line':>7} {'source':>8} "
          f"{'evid':>5}  verdict")
    print("  " + "-" * 68)

    for entry in results:
        market = entry.get("market", "?")
        if entry.get("error"):
            print(f"  {market:17} {'ERROR':>9}  {entry['error']}")
            continue
        if entry.get("weighted_avg") is None:
            print(f"  {market:17} {'n/a':>9} {'-':>7} {'-':>8} {0:>5}  no data")
            continue

        verdict = "OVER" if entry["supported"] else "UNDER"
        warn = "  (!) low confidence" if entry.get("low_confidence") else ""
        print(f"  {market:17} {fmt(entry['weighted_avg']):>9} "
              f"{fmt(entry.get('line')):>7} {str(entry.get('line_source', '')):>8} "
              f"{entry.get('evidence_points', 0):>5}  {verdict}{warn}")

        sources = entry.get("sources_used") or []
        parts = []
        if entry.get("home_away_combined") is not None:
            parts.append(f"home/away {entry['home_away_combined']} ({entry.get('home_away_sample', 0)}m)")
        if entry.get("recent_form_combined") is not None:
            parts.append(f"recent {entry['recent_form_combined']} ({entry.get('recent_form_sample', 0)}m)")
        if entry.get("h2h_combined") is not None:
            parts.append(f"h2h {entry['h2h_combined']} ({entry.get('h2h_sample', 0)}m)")
        if parts:
            print(f"  {'':17} " + " | ".join(parts))

    print()
    print("  This is a record of what the evidence said. It is not betting advice.")


def analyse_id(conn, match_id, markets=None, lines=None):
    from matches_engine import select_match
    match = select_match(conn, match_id)
    if match is None:
        print(f"  No match with id {match_id}.")
        return
    results = analyze_match(
        conn, match["home_team"], match["away_team"],
        markets=markets, lines=lines or {},
        before_date=match["date"], exclude_match_id=match_id,
        league=match.get("league"), season=match.get("season"))
    print_result(match, results)


def interactive(conn):
    print("Sports Analyzer CLI. Type 'help' for commands, 'quit' to exit.")
    while True:
        try:
            raw = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not raw:
            continue
        parts = raw.split()
        command = parts[0].lower()

        if command in ("quit", "exit", "q"):
            break

        if command == "help":
            print("  find <text>      search team names")
            print("  list [date]      list matches (default today)")
            print("  pick <id>        analyse a stored match")
            print("  markets          available markets")
            print("  quit")

        elif command == "find":
            if len(parts) < 2:
                print("  usage: find <text>")
                continue
            rows = stats.find_teams(conn, " ".join(parts[1:]))
            if not rows:
                print("  nothing found")
            for team, league in rows:
                print(f"    {team:24} {league}")

        elif command == "list":
            print_match_list(conn, parts[1] if len(parts) > 1 else None)

        elif command == "pick":
            if len(parts) < 2 or not parts[1].isdigit():
                print("  usage: pick <id>")
                continue
            analyse_id(conn, int(parts[1]))

        elif command == "markets":
            for item in stats.list_markets():
                print(f"    {item['market']:18} default line {item['default_line']}")

        else:
            print(f"  unknown command '{command}' (try help)")


def main():
    parser = argparse.ArgumentParser(description="Interactive analysis")
    parser.add_argument("--match-id", type=int)
    parser.add_argument("--date", help="list matches on this date and exit")
    parser.add_argument("--markets", help="comma separated, e.g. corners,shots")
    parser.add_argument("--lines", help="market=value pairs, e.g. corners=10.5")
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    markets = [m.strip() for m in args.markets.split(",")] if args.markets else None
    lines = {}
    if args.lines:
        for part in args.lines.split(","):
            if "=" in part:
                key, value = part.split("=", 1)
                lines[key.strip()] = float(value)

    conn = sqlite3.connect(Path(args.db))
    try:
        if args.match_id is not None:
            analyse_id(conn, args.match_id, markets, lines)
        elif args.date:
            print_match_list(conn, args.date)
        else:
            interactive(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
