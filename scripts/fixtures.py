"""Discover fixtures from football-data.org and decide which may be published.

Why this module exists
----------------------
The historical dataset comes from football-data.co.uk and only contains matches
that have been played. A dashboard therefore needs a second, forward-looking
source: a fixture the provider says exists but which has no result yet.

This module fetches that forward calendar, stores it in `available_fixtures`,
and then applies a mapping gate before anything can be published.

The three tables this touches mean three different things:

    available_fixtures   the provider says this fixture exists (a candidate)
    published_matches    the user is allowed to see it (a decision)
    matches              the engine has evidence for it (a result)

They are never merged. Auto-publishing writes only to `published_matches`.

The gate, and why it is strict
-----------------------------
An auto-published fixture with an unmapped team name would analyse as "no data"
while looking perfectly normal on the dashboard. That is the exact class of
silent failure this project keeps finding, so:

  * an alias must be exact, or
  * the provider name must equal a name already in `matches`, or
  * the normalised names must be identical, or
  * a curated exception must name it.

Fuzzy matching is used ONLY to suggest candidates to the admin. It is never
allowed to publish on its own. Anything unresolved lands in `needs_attention`
and stays off the dashboard until a human maps it once. The alias is then
remembered forever.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from seasons import normalize_season

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "sports_analyzer.db"

PROVIDER = "football-data.org"
API_BASE = "https://api.football-data.org/v4"
# Two weeks, not one: an international break can put the next matchday 10-13
# days away, and a 7-day window would quietly fall back to the *previous* round
# on the dashboard. Six calls either way, so the wider window is free.
LOOKAHEAD_DAYS = 14

# The free plan allows ten calls a minute. Six competitions, spaced out, keep
# the season sync comfortably inside it and leave room for the daily fetch.
PROVIDER_SPACING_SECONDS = 6.5

# A full season changes slowly (postponements, cup replays, TV moves), so a
# weekly refresh is enough. The 14-day window is refreshed every 12 hours.
CALENDAR_SYNC_DAYS = 7

# Provider competition code -> the league name the database already uses.
# Only these six: they are the free tier and the ones with historical data.
COMPETITIONS: dict[str, str] = {
    "PL": "Premier League",
    "BL1": "Bundesliga",
    "PD": "La Liga",
    "SA": "Serie A",
    "FL1": "Ligue 1",
    "DED": "Eredivisie",
}

# Provider status -> our smaller vocabulary. Anything not listed keeps its own
# lower-case value, which makes an unexpected status visible rather than guessed.
STATUS_MAP = {
    "scheduled": "scheduled",
    "timed": "scheduled",
    "in_play": "live",
    "paused": "live",
    "finished": "finished",
    "postponed": "postponed",
    "suspended": "postponed",
    "cancelled": "cancelled",
    "awarded": "finished",
}

# Names that differ so much that normalisation cannot bridge them, but which are
# unambiguous. Deliberately short: anything not here goes to needs_attention,
# which costs one click and can never publish the wrong team.
KNOWN_ALIASES: dict[str, str] = {
    # England
    "Manchester United FC": "Man United",
    "Manchester City FC": "Man City",
    "Newcastle United FC": "Newcastle",
    "Nottingham Forest FC": "Nott'm Forest",
    "Wolverhampton Wanderers FC": "Wolves",
    "Brighton & Hove Albion FC": "Brighton",
    "Tottenham Hotspur FC": "Tottenham",
    "West Ham United FC": "West Ham",
    "Leicester City FC": "Leicester",
    "Leeds United FC": "Leeds",
    "Sunderland AFC": "Sunderland",
    "AFC Bournemouth": "Bournemouth",
    "Sheffield United FC": "Sheffield United",
    "Sheffield Wednesday FC": "Sheffield Weds",
    "Queens Park Rangers FC": "QPR",
    "West Bromwich Albion FC": "West Brom",
    "Norwich City FC": "Norwich",
    "Ipswich Town FC": "Ipswich",
    "Hull City FC": "Hull",
    "Stoke City FC": "Stoke",
    "Swansea City AFC": "Swansea",
    "Cardiff City FC": "Cardiff",
    "Coventry City FC": "Coventry",
    "Preston North End FC": "Preston",
    "Blackburn Rovers FC": "Blackburn",
    "Bristol City FC": "Bristol City",
    "Middlesbrough FC": "Middlesbrough",
    "Watford FC": "Watford",
    "Burnley FC": "Burnley",
    "Everton FC": "Everton",
    # Spain (football-data.co.uk short forms in brackets)
    "Club Atlético de Madrid": "Ath Madrid",
    "Atlético de Madrid": "Ath Madrid",
    "Athletic Club": "Ath Bilbao",
    "Real Sociedad de Fútbol": "Sociedad",
    "Real Betis Balompié": "Betis",
    "RC Celta de Vigo": "Celta",
    "RCD Espanyol de Barcelona": "Espanol",
    "RC Deportivo La Coruña": "La Coruna",
    "Real Racing Club de Santander": "Santander",
    "Rayo Vallecano de Madrid": "Vallecano",
    "Deportivo Alavés": "Alaves",
    "CA Osasuna": "Osasuna",
    "Girona FC": "Girona",
    "CD Leganés": "Leganes",
    "Real Valladolid CF": "Valladolid",
    "UD Las Palmas": "Las Palmas",
    "Villarreal CF": "Villarreal",
    "Valencia CF": "Valencia",
    "Sevilla FC": "Sevilla",
    "Real Madrid CF": "Real Madrid",
    "FC Barcelona": "Barcelona",
    "Getafe CF": "Getafe",
    "RCD Mallorca": "Mallorca",
    # Germany
    "FC Bayern München": "Bayern Munich",
    "Bayern Munich": "Bayern Munich",
    "Borussia Dortmund": "Dortmund",
    "Bayer 04 Leverkusen": "Leverkusen",
    "Eintracht Frankfurt": "Ein Frankfurt",
    "FC Köln": "FC Koln",
    "1. FC Köln": "FC Koln",
    "VfB Stuttgart": "Stuttgart",
    "TSG 1899 Hoffenheim": "Hoffenheim",
    "1. FSV Mainz 05": "Mainz",
    "SV Werder Bremen": "Werder Bremen",
    "VfL Wolfsburg": "Wolfsburg",
    "1. FC Union Berlin": "Union Berlin",
    "FC St. Pauli": "St Pauli",
    "1. FC Heidenheim 1846": "Heidenheim",
    "VfL Bochum 1848": "Bochum",
    "FC Schalke 04": "Schalke 04",
    "Hamburger SV": "Hamburg",
    "Hertha BSC": "Hertha",
    "Borussia Mönchengladbach": "M'gladbach",
    "SC Freiburg": "Freiburg",
    "FC Augsburg": "Augsburg",
    "Holstein Kiel": "Holstein Kiel",
    # Italy
    "FC Internazionale Milano": "Inter",
    "AC Milan": "Milan",
    "Juventus FC": "Juventus",
    "SSC Napoli": "Napoli",
    "AS Roma": "Roma",
    "SS Lazio": "Lazio",
    "ACF Fiorentina": "Fiorentina",
    "Atalanta BC": "Atalanta",
    "Bologna FC 1909": "Bologna",
    "Torino FC": "Torino",
    "Udinese Calcio": "Udinese",
    "US Lecce": "Lecce",
    "Cagliari Calcio": "Cagliari",
    "Hellas Verona FC": "Verona",
    "Parma Calcio 1913": "Parma",
    "Como 1907": "Como",
    "Genoa CFC": "Genoa",
    "Empoli FC": "Empoli",
    "Venezia FC": "Venezia",
    "AC Monza": "Monza",
    # France
    "Paris Saint-Germain FC": "Paris SG",
    "Olympique de Marseille": "Marseille",
    "Olympique Lyonnais": "Lyon",
    "AS Monaco FC": "Monaco",
    "LOSC Lille": "Lille",
    "OGC Nice": "Nice",
    "Stade Rennais FC 1901": "Rennes",
    "RC Lens": "Lens",
    "Stade de Reims": "Reims",
    "RC Strasbourg Alsace": "Strasbourg",
    "Montpellier HSC": "Montpellier",
    "FC Nantes": "Nantes",
    "Stade Brestois 29": "Brest",
    "Toulouse FC": "Toulouse",
    "Le Havre AC": "Le Havre",
    "AJ Auxerre": "Auxerre",
    "Angers SCO": "Angers",
    "AS Saint-Étienne": "St Etienne",
    # Netherlands
    "AFC Ajax": "Ajax",
    # football-data.org sends these clubs under their short names, and "NEC"
    # and "AZ" are stripped as noise tokens — they can only match exactly.
    "PSV": "PSV Eindhoven",
    "PSV Eindhoven": "PSV Eindhoven",
    "AZ": "AZ Alkmaar",
    "AZ Alkmaar": "AZ Alkmaar",
    "NEC": "Nijmegen",
    "Feyenoord Rotterdam": "Feyenoord",
    "FC Twente": "Twente",
    "FC Utrecht": "Utrecht",
    "SC Heerenveen": "Heerenveen",
    "Sparta Rotterdam": "Sparta Rotterdam",
    "NEC Nijmegen": "Nijmegen",
    "Go Ahead Eagles": "Go Ahead Eagles",
    "Fortuna Sittard": "For Sittard",
    "PEC Zwolle": "Zwolle",
    "Heracles Almelo": "Heracles",
    "RKC Waalwijk": "Waalwijk",
    "Willem II": "Willem II",
    "Willem II Tilburg": "Willem II",
    "FC Groningen": "Groningen",
    "NAC Breda": "NAC Breda",
    "Almere City FC": "Almere City",
}

# Tokens that carry no identifying information in a club name.
_NOISE = {
    "fc", "afc", "cf", "sc", "ac", "sv", "vfl", "vfb", "tsg", "bsc", "ssc",
    "ss", "us", "rc", "rcd", "ca", "ud", "cd", "cfc", "as", "ogc", "losc",
    "aj", "az", "pec", "nec", "rkc", "nac", "hsc", "kv", "kvk", "sd", "club",
    "de", "futbol", "calcio", "the",
}


# --------------------------------------------------------------------------
# Token / HTTP
# --------------------------------------------------------------------------
def load_token() -> str | None:
    """Read FOOTBALL_DATA_TOKEN from the environment, then from .env.

    A tiny reader instead of a dependency: the project has no python-dotenv and
    one variable does not justify adding one. The key is never logged.
    """
    token = os.environ.get("FOOTBALL_DATA_TOKEN", "").strip()
    if token:
        return token
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return None
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "FOOTBALL_DATA_TOKEN":
            return value.strip().strip('"').strip("'") or None
    return None


def _get(path: str, token: str, timeout: int = 30) -> dict:
    request = urllib.request.Request(
        API_BASE + path,
        headers={"X-Auth-Token": token, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        if exc.code in (401, 403):
            raise PermissionError(
                "football-data.org rejected the token (HTTP "
                f"{exc.code}). Check FOOTBALL_DATA_TOKEN in .env."
            ) from exc
        if exc.code == 429:
            raise RuntimeError(
                "football-data.org rate limit hit. The free plan allows 10 "
                "calls per minute; wait a minute and retry."
            ) from exc
        raise RuntimeError(f"HTTP {exc.code} from football-data.org: {body}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"Could not reach football-data.org: {exc}") from exc


def fetch_competition(code: str, date_from: str, date_to: str, token: str,
                      timeout: int = 30) -> list[dict]:
    """One competition's fixtures in a date window, normalised to our fields."""
    payload = _get(
        f"/competitions/{code}/matches?dateFrom={date_from}&dateTo={date_to}",
        token, timeout)
    out: list[dict] = []
    for match in payload.get("matches", []):
        home = (match.get("homeTeam") or {}).get("name")
        away = (match.get("awayTeam") or {}).get("name")
        utc = match.get("utcDate")
        if not home or not away or not utc:
            continue
        moment = datetime.fromisoformat(utc.replace("Z", "+00:00"))
        out.append({
            "provider_id": str(match.get("id")),
            "competition": code,
            "league": COMPETITIONS[code],
            "date": moment.date().isoformat(),
            # Stored as UTC (Ghana is UTC+0). The provider's free tier may
            # delay kickoff times; the UI says "time may lag" rather than
            # pretending the minute is exact.
            "kickoff": moment.strftime("%H:%M"),
            "home_provider_name": home,
            "away_provider_name": away,
            "home_provider_short": (match.get("homeTeam") or {}).get("shortName"),
            "away_provider_short": (match.get("awayTeam") or {}).get("shortName"),
            "status": str(match.get("status") or "SCHEDULED").lower(),
            "raw": json.dumps(match, ensure_ascii=False),
        })
    return out


def fetch_season(code: str, token: str, timeout: int = 60) -> list[dict]:
    """The whole season for one competition: played and scheduled alike.

    `/competitions/{code}/matches` with no date filter returns the current
    season, which is exactly what a season browser needs. This is a different
    question from `fetch_competition` (which asks "what is on in the next two
    weeks"), so it hits a different endpoint and is stored in a different table.
    """
    payload = _get(f"/competitions/{code}/matches", token, timeout)
    out: list[dict] = []
    for match in payload.get("matches", []):
        home = (match.get("homeTeam") or {}).get("name")
        away = (match.get("awayTeam") or {}).get("name")
        utc = match.get("utcDate")
        if not home or not away or not utc:
            continue
        moment = datetime.fromisoformat(utc.replace("Z", "+00:00"))
        score = (match.get("score") or {}).get("fullTime") or {}
        out.append({
            "provider_id": str(match.get("id")),
            "competition": code,
            "league": COMPETITIONS[code],
            "season": _season_for(moment.date().isoformat()),
            "matchday": match.get("matchday"),
            "date": moment.date().isoformat(),
            "kickoff": moment.strftime("%H:%M"),
            "home_provider_name": home,
            "away_provider_name": away,
            "home_provider_short": (match.get("homeTeam") or {}).get("shortName"),
            "away_provider_short": (match.get("awayTeam") or {}).get("shortName"),
            "status": str(match.get("status") or "SCHEDULED").lower(),
            "home_goals": score.get("home"),
            "away_goals": score.get("away"),
            "raw": json.dumps(match, ensure_ascii=False),
        })
    return out


# --------------------------------------------------------------------------
# Team name normalisation and resolution
# --------------------------------------------------------------------------
def normalize_name(name: str) -> str:
    """Lower-case, ASCII, punctuation-free, suffix-free form for comparison.

    Only used for EXACT equality between normalised names. It is never used to
    decide that two similar names are the same team.
    """
    text = unicodedata.normalize("NFKD", name)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    tokens = [t for t in text.split() if t and t not in _NOISE and not t.isdigit()]
    return " ".join(tokens)


# The curated exceptions, keyed the same way as `matches` names are. This makes
# "FC Bayern München" and "FC Bayern Munchen" the same lookup, without letting
# a fuzzy score decide anything.
_NORMALIZED_ALIASES: dict[str, str] = {}
for _alias_name, _alias_team in KNOWN_ALIASES.items():
    _NORMALIZED_ALIASES.setdefault(normalize_name(_alias_name), _alias_team)



def canonical_index(conn: sqlite3.Connection) -> dict[str, str]:
    """normalised team name -> the database's own spelling.

    A key that would map to two different teams is dropped: an ambiguous name
    must not resolve, or it would silently pick one of them.
    """
    teams = [r[0] for r in conn.execute(
        "SELECT home_team FROM matches UNION SELECT away_team FROM matches")]
    grouped: dict[str, set[str]] = {}
    for team in teams:
        key = normalize_name(team)
        if key:
            grouped.setdefault(key, set()).add(team)
    return {key: next(iter(names)) for key, names in grouped.items()
            if len(names) == 1}


def canonical_team(conn: sqlite3.Connection, name: str) -> str | None:
    """The database's spelling of a team, case-insensitively, or None."""
    row = conn.execute(
        "SELECT t FROM (SELECT home_team AS t FROM matches "
        "UNION SELECT away_team AS t FROM matches) "
        "WHERE t = ? COLLATE NOCASE LIMIT 1", (name,)).fetchone()
    return row[0] if row else None


def suggest_teams(name: str, index: dict[str, str], limit: int = 3) -> list[dict]:
    """Closest canonical names, for the admin UI only."""
    key = normalize_name(name)
    scored = sorted(
        index.items(),
        key=lambda item: SequenceMatcher(None, key, item[0]).ratio(),
        reverse=True,
    )
    return [{"team": team, "score": round(SequenceMatcher(None, key, k).ratio(), 3)}
            for k, team in scored[:limit]]


def resolve_team(conn: sqlite3.Connection, provider_name: str,
                 provider_short: str | None = None,
                 index: dict[str, str] | None = None) -> tuple[str | None, str, list[dict]]:
    """Map a provider name to a team in `matches`.

    Returns (team or None, how, suggestions). `how` is one of: alias, exact,
    normalized, known, unmapped. Only the first four may auto-publish.
    """
    row = conn.execute(
        "SELECT team FROM team_aliases WHERE provider = ? AND provider_name = ?",
        (PROVIDER, provider_name)).fetchone()
    if row:
        return row[0], "alias", []

    for candidate in (provider_name, provider_short):
        if not candidate:
            continue
        exact = canonical_team(conn, candidate)
        if exact:
            return exact, "exact", []

    index = index if index is not None else canonical_index(conn)
    for candidate in (provider_name, provider_short):
        if not candidate:
            continue
        key = normalize_name(candidate)
        if key and key in index:
            return index[key], "normalized", []
        if key and key in _NORMALIZED_ALIASES:
            team = _NORMALIZED_ALIASES[key]
            exact = canonical_team(conn, team)
            if exact:
                return exact, "known", []

    for candidate in (provider_name, provider_short):
        if candidate and candidate in KNOWN_ALIASES:
            team = KNOWN_ALIASES[candidate]
            exact = canonical_team(conn, team)
            if exact:
                return exact, "known", []

    return None, "unmapped", suggest_teams(provider_short or provider_name, index)


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------
def _season_for(iso_date: str) -> str:
    season = normalize_season(None, iso_date)
    if season is None:
        # normalize_season should always answer for a real ISO date, but a
        # wrong season would silently split the fixture from its league, so
        # fail loudly rather than store a guess.
        raise ValueError(f"cannot derive a season for {iso_date!r}")
    return season


def ingest(conn: sqlite3.Connection, rows: list[dict], log=print) -> dict:
    """Upsert provider rows into `available_fixtures` and apply the gate."""
    conn.row_factory = sqlite3.Row
    index = canonical_index(conn)
    inserted = updated = mapped = needs = 0

    for row in rows:
        status = STATUS_MAP.get(row["status"], row["status"])
        home, how_home, sug_home = resolve_team(
            conn, row["home_provider_name"], row.get("home_provider_short"), index)
        away, how_away, sug_away = resolve_team(
            conn, row["away_provider_name"], row.get("away_provider_short"), index)

        problems: list[str] = []
        if not home:
            problems.append(f"home team '{row['home_provider_name']}' is not mapped")
        if not away:
            problems.append(f"away team '{row['away_provider_name']}' is not mapped")
        state = "ok" if not problems else "needs_attention"
        note = "; ".join(problems) if problems else None

        season = _season_for(row["date"])
        existing = conn.execute(
            "SELECT id FROM available_fixtures WHERE provider = ? AND provider_id = ?",
            (PROVIDER, row["provider_id"])).fetchone()

        if existing:
            # published_id is deliberately untouched: a provider refresh must
            # never un-publish or re-publish a fixture the admin decided on.
            conn.execute(
                "UPDATE available_fixtures SET competition=?, league=?, season=?, "
                "date=?, kickoff=?, home_provider_name=?, away_provider_name=?, "
                "home_provider_short=?, away_provider_short=?, home_team=?, "
                "away_team=?, status=?, mapping_state=?, mapping_note=?, raw=?, "
                "last_seen=datetime('now') WHERE id=?",
                (row["competition"], row["league"], season, row["date"],
                 row["kickoff"], row["home_provider_name"],
                 row["away_provider_name"], row.get("home_provider_short"),
                 row.get("away_provider_short"), home, away, status, state,
                 note, row.get("raw"), existing["id"]))
            updated += 1
        else:
            conn.execute(
                "INSERT INTO available_fixtures (provider, provider_id, "
                "competition, league, season, date, kickoff, home_provider_name, "
                "away_provider_name, home_provider_short, away_provider_short, "
                "home_team, away_team, status, mapping_state, mapping_note, raw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (PROVIDER, row["provider_id"], row["competition"], row["league"],
                 season, row["date"], row["kickoff"], row["home_provider_name"],
                 row["away_provider_name"], row.get("home_provider_short"),
                 row.get("away_provider_short"), home, away, status, state,
                 note, row.get("raw")))
            inserted += 1

        if state == "ok":
            mapped += 1
        else:
            needs += 1
            log(f"  NEEDS MAPPING  {row['date']} {row['home_provider_name']} v "
                f"{row['away_provider_name']}  ({note})")

    conn.commit()
    return {"inserted": inserted, "updated": updated,
            "mapped": mapped, "needs_attention": needs}


# --------------------------------------------------------------------------
# The season calendar
# --------------------------------------------------------------------------
def ingest_calendar(conn: sqlite3.Connection, rows: list[dict], log=print) -> dict:
    """Upsert provider rows into `season_fixtures`.

    Teams are mapped best-effort: an unmapped club still belongs on the
    calendar (the fixture exists and the score is real), it just cannot be
    opened for analysis until someone maps it once. `match_id` is never
    written here - only `link_calendar_results` touches it, so a refresh
    cannot quietly break a result that was already linked.
    """
    conn.row_factory = sqlite3.Row
    index = canonical_index(conn)
    inserted = updated = unmapped = 0

    for row in rows:
        status = STATUS_MAP.get(row["status"], row["status"])
        home, _, _ = resolve_team(
            conn, row["home_provider_name"], row.get("home_provider_short"), index)
        away, _, _ = resolve_team(
            conn, row["away_provider_name"], row.get("away_provider_short"), index)
        if not home or not away:
            unmapped += 1

        existing = conn.execute(
            "SELECT id FROM season_fixtures WHERE provider = ? AND provider_id = ?",
            (PROVIDER, row["provider_id"])).fetchone()

        values = (row["competition"], row["league"], row["season"],
                  row.get("matchday"), row["date"], row["kickoff"],
                  row["home_provider_name"], row["away_provider_name"],
                  home, away, status, row.get("home_goals"),
                  row.get("away_goals"), row.get("raw"))
        if existing:
            conn.execute(
                "UPDATE season_fixtures SET competition=?, league=?, season=?, "
                "matchday=?, date=?, kickoff=?, home_provider_name=?, "
                "away_provider_name=?, home_team=?, away_team=?, status=?, "
                "home_goals=?, away_goals=?, raw=?, last_seen=datetime('now') "
                "WHERE id=?",
                values + (existing["id"],))
            updated += 1
        else:
            conn.execute(
                "INSERT INTO season_fixtures (provider, provider_id, competition, "
                "league, season, matchday, date, kickoff, home_provider_name, "
                "away_provider_name, home_team, away_team, status, home_goals, "
                "away_goals, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (PROVIDER, row["provider_id"]) + values)
            inserted += 1

    conn.commit()
    return {"inserted": inserted, "updated": updated, "unmapped": unmapped}


def link_calendar_results(conn: sqlite3.Connection) -> int:
    """Attach each finished calendar fixture to its row in `matches`.

    The provider supplies a score, but only `matches` carries the evidence the
    engine counts. Linking the two means a played fixture on the season screen
    can be opened with full history behind it.
    """
    before = conn.execute(
        "SELECT COUNT(*) FROM season_fixtures WHERE match_id IS NOT NULL"
    ).fetchone()[0]
    conn.execute(
        "UPDATE season_fixtures SET match_id = ("
        "  SELECT m.id FROM matches m"
        "  WHERE m.league = season_fixtures.league"
        "    AND m.date = season_fixtures.date"
        "    AND lower(m.home_team) = lower(season_fixtures.home_team)"
        "    AND lower(m.away_team) = lower(season_fixtures.away_team)"
        "  LIMIT 1)"
        " WHERE match_id IS NULL"
        "   AND home_team IS NOT NULL AND away_team IS NOT NULL")
    conn.commit()
    after = conn.execute(
        "SELECT COUNT(*) FROM season_fixtures WHERE match_id IS NOT NULL"
    ).fetchone()[0]
    return after - before


def sync_calendar(db_path: Path | str = DB_PATH, token: str | None = None,
                  log=print, pause: float = PROVIDER_SPACING_SECONDS) -> dict:
    """Fetch all six competitions' whole seasons and store them.

    Six calls, spaced out: the free plan allows ten calls a minute and the
    football fixture fetch runs on the same key.
    """
    token = token or load_token()
    if not token:
        raise ValueError(
            "FOOTBALL_DATA_TOKEN is not set. Register free at "
            "https://www.football-data.org/client/register, then put "
            "FOOTBALL_DATA_TOKEN=your-key in the project's .env file.")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    competitions: list[dict] = []
    try:
        set_setting(conn, "calendar_sync_state", "running")
        for position, (code, league) in enumerate(COMPETITIONS.items()):
            if position:
                time.sleep(pause)
            try:
                rows = fetch_season(code, token)
                summary = ingest_calendar(conn, rows, log=log)
                linked = link_calendar_results(conn)
                competitions.append({
                    "code": code, "league": league,
                    "stored": summary["inserted"] + summary["updated"],
                    "inserted": summary["inserted"], "updated": summary["updated"],
                    "unmapped": summary["unmapped"], "linked": linked, "error": None})
                log(f"  CALENDAR  {league}: {len(rows)} fixtures "
                    f"({summary['inserted']} new, {linked} linked to results)")
            except PermissionError:
                raise
            except Exception as exc:  # noqa: BLE001 - one league must not kill the run
                competitions.append({"code": code, "league": league, "stored": 0,
                                     "inserted": 0, "updated": 0, "unmapped": 0,
                                     "linked": 0, "error": str(exc)})
                log(f"  CALENDAR  {league}: FAILED ({exc})")

        stored = conn.execute("SELECT COUNT(*) FROM season_fixtures").fetchone()[0]
        season = conn.execute(
            "SELECT season FROM season_fixtures ORDER BY date DESC LIMIT 1"
        ).fetchone()
        set_setting(conn, "last_calendar_sync",
                    datetime.now(timezone.utc).isoformat(timespec="seconds"))
        set_setting(conn, "calendar_sync_state", "ok")
        set_setting(conn, "calendar_sync_error", "")
    except Exception as exc:  # noqa: BLE001 - the state must record the failure
        set_setting(conn, "calendar_sync_state", "error")
        set_setting(conn, "calendar_sync_error", str(exc)[:300])
        raise
    finally:
        conn.close()

    return {"provider": PROVIDER, "competitions": competitions,
            "stored": stored, "season": season["season"] if season else None}


def season_view(conn: sqlite3.Connection, league: str, season: str,
                today: str | None = None,
                window_days: int = LOOKAHEAD_DAYS) -> dict:
    """One league-season, whole: results, the near window, and the far future.

    A fixture outside the window is returned with `state='outside'` and the date
    its analysis opens. It is deliberately NOT analysed: a projection for a
    match nine months away would be this season's form wearing a date it does
    not belong to.
    """
    conn.row_factory = sqlite3.Row
    today = today or _date.today().isoformat()
    window_end = (_date.fromisoformat(today)
                  + timedelta(days=window_days)).isoformat()

    published = {}
    for row in conn.execute(
            "SELECT id, date, home_team, away_team FROM published_matches"):
        published[(row["date"], (row["home_team"] or "").lower(),
                   (row["away_team"] or "").lower())] = row["id"]

    rows = conn.execute(
        "SELECT sf.*, "
        "  COALESCE(sf.home_goals, m.home_goals) AS home_goals_final, "
        "  COALESCE(sf.away_goals, m.away_goals) AS away_goals_final "
        "FROM season_fixtures sf "
        "LEFT JOIN matches m ON m.id = sf.match_id "
        "WHERE sf.league = ? AND sf.season = ? "
        "ORDER BY sf.date, sf.kickoff, sf.id", (league, season)).fetchall()
    source = "calendar"

    if not rows:
        # No calendar stored for this season. Past seasons do not need one:
        # the results are already in `matches`, and that is the honest source.
        #
        # `finished` fixtures still need a kickoff time that `matches` does not
        # carry, so time is left blank rather than invented.
        rows = conn.execute(
            "SELECT id AS provider_id, league, season, NULL AS matchday, "
            "date, NULL AS kickoff, home_team, away_team, 'finished' AS status, "
            "home_goals, away_goals, home_goals AS home_goals_final, "
            "away_goals AS away_goals_final, "
            "home_team AS home_team_resolved, "
            "away_team AS away_team_resolved, home_team AS home_provider_name, "
            "away_team AS away_provider_name, id AS match_id "
            "FROM matches WHERE league = ? AND season = ? "
            "ORDER BY date, id", (league, season)).fetchall()
        source = "history"

    days: list[dict] = []
    counts = {"played": 0, "published": 0, "ready": 0, "outside": 0, "unplayed": 0}
    for row in rows:
        keys = row.keys()
        status = row["status"]
        goals = (row["home_goals_final"] is not None
                 and row["away_goals_final"] is not None)
        home_team = row["home_team"] if "home_team_resolved" not in keys else row["home_team_resolved"]
        away_team = row["away_team"] if "away_team_resolved" not in keys else row["away_team_resolved"]
        published_id = published.get(
            (row["date"], (home_team or "").lower(), (away_team or "").lower()))

        # A linked match row means the result is already imported, even if the
        # provider's status has not caught up (it lags by a day or two).
        if status == "finished" or goals or row["match_id"]:
            state = "played"
        elif published_id:
            state = "published"
        elif row["date"] > window_end:
            state = "outside"
        elif row["date"] < today:
            state = "unplayed"
        else:
            state = "ready"

        window_opens = (_date.fromisoformat(row["date"])
                        - timedelta(days=window_days)).isoformat()
        entry = {
            "date": row["date"],
            "kickoff": row["kickoff"],
            "matchday": row["matchday"],
            "home": row["home_provider_name"],
            "away": row["away_provider_name"],
            "home_team": home_team,
            "away_team": away_team,
            "status": status,
            "home_goals": row["home_goals_final"],
            "away_goals": row["away_goals_final"],
            "match_id": row["match_id"],
            "published_id": published_id,
            "state": state,
            "window_opens": window_opens,
            "mapped": bool(home_team and away_team),
        }
        counts[state] = counts.get(state, 0) + 1

        if days and days[-1]["date"] == row["date"]:
            days[-1]["fixtures"].append(entry)
        else:
            days.append({"date": row["date"], "fixtures": [entry]})

    return {"league": league, "season": season, "source": source,
            "today": today, "window_days": window_days, "window_end": window_end,
            "counts": counts, "days": days}


# --------------------------------------------------------------------------
# Publishing
# --------------------------------------------------------------------------
def publish_fixture(conn: sqlite3.Connection, fixture_id: int) -> dict:
    """Publish one available fixture. Idempotent and duplicate-safe."""
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM available_fixtures WHERE id = ?",
                       (fixture_id,)).fetchone()
    if row is None:
        return {"ok": False, "reason": "fixture not found"}
    if row["published_id"]:
        return {"ok": True, "reason": "already published",
                "published_id": row["published_id"]}
    if row["mapping_state"] != "ok" or not row["home_team"] or not row["away_team"]:
        return {"ok": False, "reason": "teams are not mapped"}
    if row["status"] not in ("scheduled", "live"):
        return {"ok": False, "reason": f"status is {row['status']}"}

    duplicate = conn.execute(
        "SELECT id FROM published_matches WHERE date = ? AND home_team = ? "
        "AND away_team = ?", (row["date"], row["home_team"], row["away_team"])
    ).fetchone()
    if duplicate:
        conn.execute("UPDATE available_fixtures SET published_id = ? WHERE id = ?",
                     (duplicate[0], fixture_id))
        conn.commit()
        return {"ok": True, "reason": "already on the dashboard",
                "published_id": duplicate[0]}

    cur = conn.execute(
        "INSERT INTO published_matches (league, season, date, kickoff, home_team, "
        "away_team, markets, status, sort_order) VALUES (?,?,?,?,?,?,?,?,?)",
        (row["league"], row["season"], row["date"], row["kickoff"],
         row["home_team"], row["away_team"], None, "published", 0))
    published_id = cur.lastrowid
    conn.execute("UPDATE available_fixtures SET published_id = ? WHERE id = ?",
                 (published_id, fixture_id))
    conn.commit()
    return {"ok": True, "reason": "published", "published_id": published_id}


def team_match_counts(conn: sqlite3.Connection, before_date: str) -> dict[str, int]:
    """Matches per team strictly before a date — the evidence a fixture has."""
    counts: dict[str, int] = {}
    for team, n in conn.execute(
            "SELECT home_team, COUNT(*) FROM matches WHERE date < ? GROUP BY home_team "
            "UNION ALL "
            "SELECT away_team, COUNT(*) FROM matches WHERE date < ? GROUP BY away_team",
            (before_date, before_date)):
        counts[team] = counts.get(team, 0) + n
    return counts


def auto_publish(conn: sqlite3.Connection, lookahead_days: int = LOOKAHEAD_DAYS,
                 log=print) -> dict:
    """Publish every eligible fixture in leagues that have auto-publish on.

    Eligibility is deliberately boring: mapped teams, a future scheduled
    fixture, the league switched on, and at least one historical match for each
    team. A fixture that fails the last test is left for the admin, because a
    dashboard card that can only say "no usable data" is worse than no card.
    """
    conn.row_factory = sqlite3.Row
    today = _date.today().isoformat()
    end = (_date.today() + timedelta(days=lookahead_days)).isoformat()
    counts = team_match_counts(conn, end)

    rows = conn.execute(
        "SELECT a.* FROM available_fixtures a "
        "WHERE a.published_id IS NULL AND a.mapping_state = 'ok' "
        "  AND a.date >= ? AND a.date <= ? AND a.status = 'scheduled' "
        "  AND COALESCE((SELECT s.auto_publish FROM fixture_settings s "
        "                WHERE s.league = a.league), 1) = 1 "
        "ORDER BY a.date, a.kickoff", (today, end)).fetchall()

    published = duplicates = no_history = refused = 0
    for row in rows:
        if not counts.get(row["home_team"]) or not counts.get(row["away_team"]):
            no_history += 1
            continue
        result = publish_fixture(conn, row["id"])
        if result.get("ok") and result.get("reason") == "published":
            published += 1
            log(f"  AUTO-PUBLISHED  {row['date']} {row['league']}  "
                f"{row['home_team']} v {row['away_team']}")
        elif result.get("ok"):
            duplicates += 1
        else:
            refused += 1
            log(f"  NOT PUBLISHED   {row['date']} {row['home_team']} v "
                f"{row['away_team']}  ({result.get('reason')})")

    return {"published": published, "duplicates": duplicates,
            "no_history": no_history, "refused": refused}


# --------------------------------------------------------------------------
# Fetch orchestration
# --------------------------------------------------------------------------
def fetch_and_ingest(db_path: Path | str = DB_PATH, token: str | None = None,
                     lookahead_days: int = LOOKAHEAD_DAYS, publish: bool = True,
                     log=print) -> dict:
    """Fetch the six competitions, store them, and auto-publish what passes."""
    token = token or load_token()
    if not token:
        raise ValueError(
            "FOOTBALL_DATA_TOKEN is not set. Register free at "
            "https://www.football-data.org/client/register, then put "
            "FOOTBALL_DATA_TOKEN=your-key in the project's .env file.")

    today = _date.today()
    end = today + timedelta(days=lookahead_days)
    rows: list[dict] = []
    competitions: list[dict] = []

    for code, league in COMPETITIONS.items():
        try:
            got = fetch_competition(code, today.isoformat(), end.isoformat(), token)
            rows.extend(got)
            competitions.append({"code": code, "league": league,
                                 "fetched": len(got), "error": None})
        except PermissionError:
            # A bad token is not a per-league problem; stop and say so.
            raise
        except Exception as exc:  # noqa: BLE001 - one league must not kill the run
            competitions.append({"code": code, "league": league,
                                 "fetched": 0, "error": str(exc)})

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        summary = ingest(conn, rows, log=log)
        pub = auto_publish(conn, lookahead_days, log=log) if publish else {
            "published": 0, "duplicates": 0, "no_history": 0, "refused": 0}
        set_setting(conn, "last_fixture_fetch",
                    datetime.now(timezone.utc).isoformat(timespec="seconds"))
        set_setting(conn, "fixture_lookahead_days", str(lookahead_days))
        available = conn.execute(
            "SELECT COUNT(*) FROM available_fixtures WHERE date >= ? AND date <= ?",
            (today.isoformat(), end.isoformat())).fetchone()[0]
        needs = conn.execute(
            "SELECT COUNT(*) FROM available_fixtures WHERE date >= ? AND date <= ? "
            "AND mapping_state != 'ok'",
            (today.isoformat(), end.isoformat())).fetchone()[0]
    finally:
        conn.close()

    return {
        "provider": PROVIDER,
        "window": {"from": today.isoformat(), "to": end.isoformat()},
        "competitions": competitions,
        "available": available,
        "needs_attention": needs,
        **summary,
        **pub,
    }


# --------------------------------------------------------------------------
# Settings and aliases
# --------------------------------------------------------------------------
def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = datetime('now')", (key, value))
    conn.commit()


def get_settings(conn: sqlite3.Connection) -> list[dict]:
    """Every mapped league and whether auto-publish is on (default: on)."""
    stored = {r[0]: bool(r[1]) for r in conn.execute(
        "SELECT league, auto_publish FROM fixture_settings")}
    return [{"league": league, "auto_publish": stored.get(league, True)}
            for league in COMPETITIONS.values()]


def set_auto_publish(conn: sqlite3.Connection, league: str, enabled: bool) -> dict:
    if league not in COMPETITIONS.values():
        raise ValueError(f"unknown league: {league}")
    conn.execute(
        "INSERT INTO fixture_settings (league, auto_publish, updated_at) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(league) DO UPDATE SET auto_publish = excluded.auto_publish, "
        "updated_at = datetime('now')", (league, 1 if enabled else 0))
    conn.commit()
    return {"league": league, "auto_publish": bool(enabled)}


def upsert_alias(conn: sqlite3.Connection, provider_name: str, team: str) -> dict:
    """Remember a provider name -> canonical team, and re-resolve its fixtures."""
    provider_name = provider_name.strip()
    team = team.strip()
    if not provider_name or not team:
        raise ValueError("provider_name and team are required")
    canonical = canonical_team(conn, team)
    if not canonical:
        raise ValueError(f"'{team}' is not a team in the historical dataset. "
                         f"Use the exact spelling the database already has.")

    conn.execute(
        "INSERT INTO team_aliases (provider, provider_name, team) VALUES (?, ?, ?) "
        "ON CONFLICT(provider, provider_name) DO UPDATE SET team = excluded.team",
        (PROVIDER, provider_name, canonical))
    conn.commit()

    rows = conn.execute(
        "SELECT id, home_provider_name, away_provider_name, home_team, away_team "
        "FROM available_fixtures WHERE home_provider_name = ? OR away_provider_name = ?",
        (provider_name, provider_name)).fetchall()
    changed = 0
    for row in rows:
        home = canonical if row["home_provider_name"] == provider_name else row["home_team"]
        away = canonical if row["away_provider_name"] == provider_name else row["away_team"]
        state = "ok" if home and away else "needs_attention"
        note = None if state == "ok" else "one side is still unmapped"
        conn.execute(
            "UPDATE available_fixtures SET home_team=?, away_team=?, "
            "mapping_state=?, mapping_note=? WHERE id=?",
            (home, away, state, note, row["id"]))
        changed += 1
    conn.commit()

    # A name just got unmapped -> mapped, so the fixtures it was blocking are now
    # publishable. Publishing here means the admin fixes a spelling once and the
    # matches appear on the dashboard without a second "publish" step.
    lookahead = int(get_setting(conn, "fixture_lookahead_days", str(LOOKAHEAD_DAYS)))
    pub = auto_publish(conn, lookahead, log=lambda *_: None)

    return {"ok": True, "provider_name": provider_name, "team": canonical,
            "fixtures_updated": changed, "auto_published": pub["published"],
            "still_unmapped": pub["refused"] + pub["no_history"]}


def delete_alias(conn: sqlite3.Connection, alias_id: int) -> bool:
    cur = conn.execute("DELETE FROM team_aliases WHERE id = ?", (alias_id,))
    conn.commit()
    return cur.rowcount > 0
