"""Download a season's CSV files from football-data.co.uk into data/raw/.

Why this is a script and not three lines inside the API
------------------------------------------------------
The API must not grow its own idea of where a file goes or what a season code
is. `import_data` already owns the folder -> league map and the division codes,
and `seasons` owns the season label. This module reuses both, so the download
half and the import half cannot disagree about what `data/raw/premier-league/
2026-27.csv` means.

The URL scheme
--------------
    https://www.football-data.co.uk/mmz4281/<season>/<division>.csv

where <season> is the provider's own four digits, e.g. 2026/27 -> `2627`, and
<division> comes from `import_data.LEAGUE_DIV_CODES` (E0, D1, SP1, ...). The
file is written as `<season>.csv` in the league's folder, because the importer
takes the season label from the file name.

Nothing is written until the bytes have been checked
----------------------------------------------------
A provider outage, a captcha or a moved URL returns an HTML error page, and
writing that over a good 380-row CSV would silently empty a season - the sweep
in `import_season` would then delete every match the file no longer lists. So
each response is parsed just far enough to confirm it looks like a season CSV
before it is allowed to replace an existing file.

Usage:
    python scripts/fetch_csvs.py --season 2026-27
    python scripts/fetch_csvs.py --season 2026-27 --import
    python scripts/fetch_csvs.py --league premier-league
"""
from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path

from import_data import DB_PATH, LEAGUE_DIV_CODES, RAW_DIR
from seasons import normalize_season

PROVIDER_URL = "https://www.football-data.co.uk/mmz4281/{code}/{div}.csv"

# The provider republishes the whole season each time, so a normal refresh grows
# the file. A response that is far smaller than what is already on disk is
# treated as suspicious rather than accepted, unless it is still a complete,
# parseable CSV - the check below is what actually decides.
MIN_PLAUSIBLE_BYTES = 200


def season_code(season: str) -> str:
    """The provider's season code: '2026-27' -> '2627'."""
    start, end = season.split("-")
    return f"{start[-2:]}{end[-2:]}"


def looks_like_season_csv(raw: bytes) -> tuple[bool, str]:
    """Is this actually a football-data.co.uk season file?

    Returns (ok, reason). Only the header is inspected: that is enough to tell a
    real file from an error page, and it does not care how many rows the season
    currently has (a season three weeks old has very few, and that is normal).
    """
    if len(raw) < MIN_PLAUSIBLE_BYTES:
        return False, f"only {len(raw)} bytes"
    text = raw.decode("utf-8-sig", "replace")
    lines = text.splitlines()
    if not lines:
        return False, "empty response"
    header = lines[0]
    missing = [c for c in ("Div", "Date", "HomeTeam", "AwayTeam") if c not in header]
    if missing:
        return False, f"header is missing {', '.join(missing)} (got {header[:60]!r})"
    if len(lines) < 2:
        return False, "header only, no matches in the file"
    return True, f"{len(lines) - 1} match row(s)"


def download_season(season: str, root: Path = RAW_DIR, log=print,
                    timeout: int = 60, only: list[str] | None = None) -> dict:
    """Fetch every mapped league's file for one season.

    Returns a per-file report rather than raising, so a single unreachable
    league cannot stop the other five from being refreshed. `status` is one of
    'downloaded', 'failed' (never got usable bytes) or 'refused' (got bytes that
    were not a season CSV, so the existing file was left alone).
    """
    canonical = normalize_season(season, None)
    if canonical is None:
        raise ValueError(
            f"'{season}' is not a season label I can read. Use e.g. 2026-27.")

    code = season_code(canonical)
    files: list[dict] = []

    for folder in sorted(LEAGUE_DIV_CODES):
        if only and folder not in only:
            continue
        div = LEAGUE_DIV_CODES[folder]
        url = PROVIDER_URL.format(code=code, div=div)
        target = root / folder / f"{canonical}.csv"
        entry = {"league_folder": folder, "division": div, "url": url,
                 "path": str(target), "status": "failed", "detail": ""}

        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                raw = response.read()
        except (urllib.error.URLError, OSError) as exc:
            entry["detail"] = f"{type(exc).__name__}: {exc}"
            log(f"  FAILED  {folder:15} {entry['detail']}")
            files.append(entry)
            continue

        ok, detail = looks_like_season_csv(raw)
        if not ok:
            entry["status"] = "refused"
            entry["detail"] = detail
            log(f"  REFUSED {folder:15} {detail} - existing file left alone")
            files.append(entry)
            continue

        previous = target.stat().st_size if target.exists() else None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)

        entry.update(status="downloaded", detail=detail,
                     bytes=len(raw), previous_bytes=previous)
        change = "new" if previous is None else f"{previous} -> {len(raw)} bytes"
        log(f"  OK      {folder:15} {detail}  [{change}]")
        files.append(entry)

    return {
        "season": canonical,
        "code": code,
        "files": files,
        "downloaded": sum(1 for f in files if f["status"] == "downloaded"),
        "refused": sum(1 for f in files if f["status"] == "refused"),
        "failed": sum(1 for f in files if f["status"] == "failed"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download a season's CSVs from football-data.co.uk into data/raw/")
    parser.add_argument("--season", required=True,
                        help="season label, e.g. 2026-27")
    parser.add_argument("--league", action="append", default=None,
                        help="only this folder (repeatable); default is all mapped leagues")
    parser.add_argument("--db", default=str(DB_PATH), help="database path for --import")
    parser.add_argument("--import", dest="do_import", action="store_true",
                        help="import the downloaded files into the database afterwards")
    args = parser.parse_args()

    print(f"Downloading season {args.season} from football-data.co.uk")
    try:
        report = download_season(args.season, only=args.league)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"\n{report['downloaded']} downloaded, {report['refused']} refused, "
          f"{report['failed']} failed")

    if args.do_import and report["downloaded"]:
        import import_data
        print("\nImporting:")
        summary = import_data.import_tree(log=lambda line: print(f"  {line}"))
        print(f"\n+{summary['inserted']} new, {summary['updated']} refreshed, "
              f"{summary['removed']} removed")

    # A refused file means data that should have arrived did not, which is the
    # one outcome a scheduled run must not report as success. A failed league is
    # usually the provider being briefly unavailable and is reported, not fatal.
    return 1 if report["refused"] else 0


if __name__ == "__main__":
    sys.exit(main())
