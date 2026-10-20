"""Football season labels, defined once.

Why this is its own module
--------------------------
A season label is a JOIN KEY. It appears in `matches`, in `published_matches`,
and in every query the engine makes for a league reference. A label in any shape
other than the canonical `YYYY-YY` therefore matches nothing - and nothing
crashes. The league comparison just quietly disappears and the fair line drops
to a generic prior, which looks like an answer.

It also decides whether a CSV import UPDATES a season or DUPLICATES it, because
`import_data` takes the label from the file name. A file called `2026-2027.csv`
used to be a different season from `2026-27`, so importing it inserted a second
copy of every match in that season instead of refreshing the first. Measured on
the real database: the Premier League's 50 matches became 100, under two labels,
and a third file name produced 150.

Both the API and the importer call these functions, so the two cannot disagree.
"""
from __future__ import annotations


def season_for_date(iso_date: str | None) -> str:
    """Map a date to its season label, e.g. '2025-09-01' -> '2025-26'.

    Seasons run July -> June, so a July-December date belongs to the season
    starting that year and a January-June date to the one that began the year
    before.
    """
    try:
        year, month = int(str(iso_date)[:4]), int(str(iso_date)[5:7])
    except (ValueError, TypeError):
        return ""
    start = year if month >= 7 else year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def normalize_season(value: str | None, iso_date: str | None) -> str | None:
    """Force a season label into the canonical 'YYYY-YY' form.

    Accepts the shapes a human or a file name actually produces - '2026/2027',
    '2026-2027', '2026_27' - and rejects anything that is not genuinely a
    season, rather than inventing one. When a date is supplied, an unusable
    label falls back to the season that date belongs to.
    """
    if value:
        cleaned = str(value).strip().replace("/", "-").replace("_", "-")
        parts = cleaned.split("-")
        if (len(parts) == 2 and len(parts[0]) == 4 and parts[0].isdigit()
                and parts[1].isdigit()):
            start = int(parts[0])
            end = parts[1][-2:]
            # The end year must genuinely be the next one, so a nonsense label
            # cannot be "normalised" into a real season.
            if int(end) == (start + 1) % 100:
                return f"{start}-{end}"
    return season_for_date(iso_date) if iso_date else None


def season_from_filename(stem: str) -> str | None:
    """The season a CSV file name claims, or None if it claims nothing.

    `import_data` labels a season by the file name, because that is the only
    thing tying a downloaded file to a season. That makes the file name part of
    the data, so it is validated rather than trusted: a stem that is not a
    season returns None, and the caller skips the file with an explanation
    instead of importing a duplicate season under a name like 'E0'.

    No date fallback here on purpose - there is no date yet, and guessing one
    would be exactly the silent invention this module exists to prevent.
    """
    return normalize_season(stem, None)
