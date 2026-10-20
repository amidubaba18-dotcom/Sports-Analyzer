"""Core statistical engine.

Everything here answers one question: "given the history stored before a
certain moment, what does the evidence say about a combined market total,
and how much evidence is there?"

Design rules that must not be broken:
  * Every average can be restricted with `before_date` (strictly BEFORE that
    date) and `exclude_match_id`. This is what stops a backtest from letting a
    match see its own result or the future.
  * SQL values always go through ? placeholders - never f-strings.
  * A stat is added once to STAT_COLUMNS and every averaging function picks
    it up automatically.
  * Nothing here recommends a bet. It reports evidence and a sample size.
"""
from __future__ import annotations

import datetime as _dt
import math
import sqlite3

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------
# Stat registry
# --------------------------------------------------------------------------
# Maps a market name -> (home column, away column) in the `matches` table.
# `goals` is the only market whose columns are already "the score"; the rest
# are per-team counts summed across the two teams to form a match total.
STAT_COLUMNS: dict[str, tuple[str, str]] = {
    "goals": ("home_goals", "away_goals"),
    "shots": ("home_shots", "away_shots"),
    "shots_on_target": ("home_shots_on_target", "away_shots_on_target"),
    "fouls": ("home_fouls", "away_fouls"),
    "corners": ("home_corners", "away_corners"),
    "yellow_cards": ("home_yellow_cards", "away_yellow_cards"),
    "red_cards": ("home_red_cards", "away_red_cards"),
    "tackles": ("home_tackles", "away_tackles"),
    "throw_ins": ("home_throw_ins", "away_throw_ins"),
    "aerial_duels": ("home_aerial_duels", "away_aerial_duels"),
    "offsides": ("home_offsides", "away_offsides"),
    "interceptions": ("home_interceptions", "away_interceptions"),
    "goal_kicks": ("home_goal_kicks", "away_goal_kicks"),
    "saves": ("home_saves", "away_saves"),
    "substitutions": ("home_substitutions", "away_substitutions"),
}

# Columns the admin can fill in by hand (not present in the source CSVs).
MANUAL_STAT_COLUMNS: list[str] = [
    "home_tackles", "away_tackles",
    "home_throw_ins", "away_throw_ins",
    "home_aerial_duels", "away_aerial_duels",
    "home_offsides", "away_offsides",
    "home_interceptions", "away_interceptions",
    "home_goal_kicks", "away_goal_kicks",
    "home_saves", "away_saves",
    "home_substitutions", "away_substitutions",
]

# The markets a team profile can supply a value for.
#
# Exactly the eight that no free CSV publishes. A profile must never be able to
# supply one of the CSV-backed markets: those are measured from real matches,
# cutoff-aware and reproducible, and a hand-typed average has no business
# displacing them. Keeping the list to the eight makes that structural rather
# than a rule someone has to remember.
PROFILE_STATS: tuple[str, ...] = (
    "tackles", "throw_ins", "aerial_duels", "offsides",
    "interceptions", "goal_kicks", "saves", "substitutions",
)

# How much a typed team average counts as, in matches of evidence.
#
# An external per-game average (a stats site's "per game" column) has no sample
# size we can verify, so it cannot be trusted the way a counted average is. But
# it cannot count as zero either: the shrunk estimator's weight IS the sample
# size, so n = 0 would make the league prior win outright and throw the number
# away. Three pseudo-matches keeps it a minority voice (n/(n+k) = 3/13 = 23% at
# k=10) while still producing a number, and is stated rather than hidden.
PROFILE_WEIGHT: float = 3.0

# Fallback lines used only when the caller supplies no line at all.
DEFAULT_MARKET_LINES: dict[str, float] = {
    "goals": 2.5,
    "shots": 24.5,
    "shots_on_target": 8.5,
    "fouls": 23.5,
    "corners": 9.5,
    "yellow_cards": 4.5,
    "red_cards": 0.5,
    "tackles": 35.5,
    "throw_ins": 40.5,
    "aerial_duels": 30.5,
    "offsides": 4.5,
    "interceptions": 18.5,
    "goal_kicks": 16.5,
    "saves": 6.5,
    "substitutions": 9.5,
}

# The legacy fixed weights. Kept because `method="blend"` still uses them and
# the backtester compares the two methods head to head. They are no longer the
# default: see SHRINK_K below.
DEFAULT_WEIGHTS: dict[str, float] = {
    "home_away": 0.45,
    "recent_form": 0.35,
    "h2h": 0.20,
}

# Shrinkage strength, in units of "pseudo-matches".
#
# The headline estimate is the two teams' season form pulled toward the league
# average in proportion to how little data stands behind it:
#
#     value = (n * team_total + k * league_average) / (n + k)
#
# so with n << k the league average dominates and with n >> k the teams' own
# form does. This replaced the fixed 45/35/20 blend because the pooled backtest
# (4,280 matches per market) found the blend predicted the ACTUAL total worse
# than this two-input estimator in four of six markets and better in none, and
# adding more evidence never improved its hit rate. See README, "Honest status
# of the model". k is swept by `backtester.py --shrink-k`.
SHRINK_K: float = 10.0


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def safe_avg(series: pd.Series) -> tuple[float | None, int]:
    """Mean that survives an all-NaN series.

    pandas .mean() returns NaN when every value is NaN, which then poisons any
    arithmetic downstream. Returns (average rounded to 2dp, count of non-null)
    or (None, 0) when there is nothing usable.
    """
    valid = series.notnull().sum()
    if valid == 0:
        return None, 0
    return round(float(series.mean()), 2), int(valid)


def _stat_ids(frame: pd.DataFrame, values: pd.Series) -> set:
    """The ids of the matches that actually carry a value for one stat.

    A team's recent-form window is its last five matches whatever they contain,
    so counting the window answers "how many matches did this team play", not
    "how many matches is this number built from". For a market the CSVs cover
    the two are the same; for a hand-entered market they are not, and the
    hand-entered one is the case that matters - two typed rows once reported
    nine matches of evidence because the other seven had simply been left blank.
    """
    if "id" not in frame.columns:
        return set()
    return set(frame.loc[values.notna(), "id"].tolist())


def _manual_merge(conn: sqlite3.Connection, df: pd.DataFrame) -> pd.DataFrame:
    """Overlay rows from `manual_stats` on top of `matches` rows.

    Manual values win over CSV values when both exist, but only for the columns
    the admin actually filled in. This is why a weekly CSV re-import does not
    destroy hand-entered numbers: the CSV table is rewritten, the overlay is
    not.
    """
    if df is None or df.empty or "id" not in df.columns:
        return df

    try:
        manual = pd.read_sql_query("SELECT * FROM manual_stats", conn)
    except (sqlite3.Error, pd.errors.DatabaseError):
        # Table missing -> nothing to overlay.
        return df

    if manual is None or manual.empty:
        return df

    overlap = [c for c in MANUAL_STAT_COLUMNS if c in df.columns and c in manual.columns]
    if not overlap:
        return df

    merged = df.merge(manual, left_on="id", right_on="match_id", how="left", suffixes=("", "_manual"))
    for col in overlap:
        manual_col = f"{col}_manual"
        if manual_col not in merged.columns:
            continue
        merged[col] = merged[manual_col].where(merged[manual_col].notna(), merged[col])
        merged = merged.drop(columns=[manual_col])
    for leftover in ("match_id_manual", "match_id"):
        if leftover in merged.columns and leftover != "match_id":
            merged = merged.drop(columns=[leftover])
    if "match_id" in merged.columns and "match_id" not in df.columns:
        merged = merged.drop(columns=["match_id"])
    return merged


def _date_bounds(before_date, clause: list[str], params: list) -> None:
    """Append the cutoff predicate if a cutoff was supplied.

    `date < ?` (strict) is deliberate: a match must never see itself.
    """
    if before_date is not None:
        clause.append("date < ?")
        params.append(before_date)


def _exclusion(exclude_match_id, clause: list[str], params: list) -> None:
    if exclude_match_id is not None:
        clause.append("id != ?")
        params.append(exclude_match_id)


# --------------------------------------------------------------------------
# Raw match readers (return DataFrames, manual stats already merged in)
# --------------------------------------------------------------------------
def get_team_home_matches(conn, team_name, before_date=None, exclude_match_id=None):
    clause = ["home_team = ?"]
    params = [team_name]
    _date_bounds(before_date, clause, params)
    _exclusion(exclude_match_id, clause, params)
    sql = f"SELECT * FROM matches WHERE {' AND '.join(clause)}"
    return _manual_merge(conn, pd.read_sql_query(sql, conn, params=params))


def get_team_away_matches(conn, team_name, before_date=None, exclude_match_id=None):
    clause = ["away_team = ?"]
    params = [team_name]
    _date_bounds(before_date, clause, params)
    _exclusion(exclude_match_id, clause, params)
    sql = f"SELECT * FROM matches WHERE {' AND '.join(clause)}"
    return _manual_merge(conn, pd.read_sql_query(sql, conn, params=params))


def get_team_recent_matches(conn, team_name, n=5, before_date=None, exclude_match_id=None):
    """The team's last `n` matches at either venue, newest first."""
    cols = ["id", "date", "home_team", "away_team"]
    for home_col, away_col in STAT_COLUMNS.values():
        for c in (home_col, away_col):
            if c not in cols:
                cols.append(c)

    clause = ["(home_team = ? OR away_team = ?)"]
    params = [team_name, team_name]
    _date_bounds(before_date, clause, params)
    _exclusion(exclude_match_id, clause, params)

    sql = (
        f"SELECT {', '.join(cols)} FROM matches "
        f"WHERE {' AND '.join(clause)} ORDER BY date DESC LIMIT ?"
    )
    params.append(n)
    return _manual_merge(conn, pd.read_sql_query(sql, conn, params=params))


def get_h2h_matches(conn, team_a, team_b, before_date=None, exclude_match_id=None):
    """Every previous meeting between the two teams, both venues, newest first."""
    cols = ["id", "date", "home_team", "away_team"]
    for home_col, away_col in STAT_COLUMNS.values():
        for c in (home_col, away_col):
            if c not in cols:
                cols.append(c)

    clause = [
        "((home_team = ? AND away_team = ?) OR (home_team = ? AND away_team = ?))"
    ]
    params = [team_a, team_b, team_b, team_a]
    _date_bounds(before_date, clause, params)
    _exclusion(exclude_match_id, clause, params)

    sql = (
        f"SELECT {', '.join(cols)} FROM matches "
        f"WHERE {' AND '.join(clause)} ORDER BY date DESC"
    )
    return _manual_merge(conn, pd.read_sql_query(sql, conn, params=params))


# --------------------------------------------------------------------------
# Averages
# --------------------------------------------------------------------------
def _empty_result() -> dict:
    return {"matches_played": 0, "match_ids": set()}


def get_team_home_averages(conn, team_name, before_date=None, exclude_match_id=None):
    """Averages of each stat while this team was the HOME side."""
    cols = ["id"] + [home_col for home_col, _ in STAT_COLUMNS.values()]
    clause = ["home_team = ?"]
    params = [team_name]
    _date_bounds(before_date, clause, params)
    _exclusion(exclude_match_id, clause, params)

    sql = f"SELECT {', '.join(cols)} FROM matches WHERE {' AND '.join(clause)}"
    df = _manual_merge(conn, pd.read_sql_query(sql, conn, params=params))

    result = _empty_result()
    result["matches_played"] = len(df)
    if "id" in df.columns:
        result["match_ids"] = set(df["id"].tolist())

    for stat, (home_col, _) in STAT_COLUMNS.items():
        if home_col in df.columns:
            avg, count = safe_avg(df[home_col])
            result[f"ids_{stat}"] = _stat_ids(df, df[home_col])
        else:
            avg, count = None, 0
            result[f"ids_{stat}"] = set()
        result[f"avg_{stat}"] = avg
        result[f"n_{stat}"] = count
    return result


def get_team_away_averages(conn, team_name, before_date=None, exclude_match_id=None):
    """Averages of each stat while this team was the AWAY side."""
    cols = ["id"] + [away_col for _, away_col in STAT_COLUMNS.values()]
    clause = ["away_team = ?"]
    params = [team_name]
    _date_bounds(before_date, clause, params)
    _exclusion(exclude_match_id, clause, params)

    sql = f"SELECT {', '.join(cols)} FROM matches WHERE {' AND '.join(clause)}"
    df = _manual_merge(conn, pd.read_sql_query(sql, conn, params=params))

    result = _empty_result()
    result["matches_played"] = len(df)
    if "id" in df.columns:
        result["match_ids"] = set(df["id"].tolist())

    for stat, (_, away_col) in STAT_COLUMNS.items():
        if away_col in df.columns:
            avg, count = safe_avg(df[away_col])
            result[f"ids_{stat}"] = _stat_ids(df, df[away_col])
        else:
            avg, count = None, 0
            result[f"ids_{stat}"] = set()
        result[f"avg_{stat}"] = avg
        result[f"n_{stat}"] = count
    return result


def get_team_recent_averages(conn, team_name, n=5, before_date=None, exclude_match_id=None):
    """Averages over the team's last `n` matches, picking the correct column
    depending on whether they were home or away in each one."""
    df = get_team_recent_matches(
        conn, team_name, n, before_date=before_date, exclude_match_id=exclude_match_id
    )

    result = _empty_result()
    result["matches_played"] = len(df)
    if "id" in df.columns:
        result["match_ids"] = set(df["id"].tolist())

    if df.empty:
        for stat in STAT_COLUMNS:
            result[f"avg_{stat}"] = None
            result[f"n_{stat}"] = 0
            result[f"ids_{stat}"] = set()
        return result

    # is_home is a boolean Series used to choose which column to read per row.
    is_home = df["home_team"] == team_name

    for stat, (home_col, away_col) in STAT_COLUMNS.items():
        if home_col not in df.columns or away_col not in df.columns:
            result[f"avg_{stat}"] = None
            result[f"n_{stat}"] = 0
            result[f"ids_{stat}"] = set()
            continue
        # np.where picks element-wise: home value if home, else away value.
        team_values = pd.Series(np.where(is_home, df[home_col], df[away_col]),
                                index=df.index)
        avg, count = safe_avg(team_values)
        result[f"avg_{stat}"] = avg
        result[f"n_{stat}"] = count
        result[f"ids_{stat}"] = _stat_ids(df, team_values)
    return result


def get_h2h_summary(conn, team_a, team_b, before_date=None, exclude_match_id=None):
    """Average COMBINED match total per stat across previous meetings."""
    df = get_h2h_matches(
        conn, team_a, team_b, before_date=before_date, exclude_match_id=exclude_match_id
    )

    result = _empty_result()
    result["matches_played"] = len(df)
    if "id" in df.columns:
        result["match_ids"] = set(df["id"].tolist())

    if df.empty:
        for stat in STAT_COLUMNS:
            result[f"avg_{stat}"] = None
            result[f"n_{stat}"] = 0
            result[f"ids_{stat}"] = set()
        return result

    for stat, (home_col, away_col) in STAT_COLUMNS.items():
        if home_col not in df.columns or away_col not in df.columns:
            result[f"avg_{stat}"] = None
            result[f"n_{stat}"] = 0
            result[f"ids_{stat}"] = set()
            continue
        combined = df[home_col] + df[away_col]
        avg, count = safe_avg(combined)
        result[f"avg_{stat}"] = avg
        result[f"n_{stat}"] = count
        result[f"ids_{stat}"] = _stat_ids(df, combined)
    return result


def get_team_profile(conn, team, league=None, season=None, before_date=None):
    """The admin's typed per-game averages for one team, if one applies.

    Why this exists
    ---------------
    The eight markets in PROFILE_STATS are published by no free CSV, so the only
    way they ever carry data is by hand. Typing two counts per fixture is
    accurate but brutal: nine markets x two teams x a weekend of fixtures. What
    the admin already has, free, is a stats site's *per game* column for each
    team.

    That number is not a rival to the engine - it is the same quantity the engine
    would otherwise derive from match rows. The "home / away average" source is
    literally "the home team's expected total plus the away team's expected
    total", and a per-game average is exactly that expected total. So a profile
    is a substitute for the *derivation*, not for the model.

    What it is NOT
    --------------
    It is not measured, so it can never masquerade as evidence. A profile
    contributes a value and a stated weight (PROFILE_WEIGHT) but no match ids,
    which is what the UI counts as evidence points, and it is only consulted
    where the measured average is None. Turn a profile on and no counted number
    anywhere changes.

    `as_of` is enforced against `before_date` so the cutoff rule holds: an
    average dated after the fixture cannot be used to analyse it. That matters
    for honesty about the past, and not at all for a fixture still to be played.
    """
    clause = ["team = ?"]
    params: list = [team]
    if league:
        clause.append("league = ?")
        params.append(league)
    if season:
        clause.append("season = ?")
        params.append(season)
    if before_date:
        clause.append("as_of <= ?")
        params.append(before_date)

    sql = (f"SELECT * FROM team_profiles WHERE {' AND '.join(clause)} "
           f"ORDER BY as_of DESC LIMIT 1")
    try:
        cur = conn.execute(sql, params)
        row = cur.fetchone()
    except sqlite3.Error:
        # An older database without the table still works; it simply has no
        # profiles. That is not an error worth failing an analysis over.
        return None
    if row is None:
        return None
    return dict(zip([d[0] for d in cur.description], row))


def get_league_average(conn, league, season=None, stat_key="avg_goals",
                       before_date=None, exclude_match_id=None):
    """Average combined match total for a stat across a league.

    This is the basis for both the "league average" baseline and the derived
    fair line. It is cutoff-aware on purpose: using the whole season would
    leak the future into a backtest.
    """
    stat = stat_key.replace("avg_", "", 1)
    if stat not in STAT_COLUMNS:
        return {"stat": stat, "average": None, "matches": 0, "match_ids": set()}

    home_col, away_col = STAT_COLUMNS[stat]
    clause = [f"{home_col} IS NOT NULL", f"{away_col} IS NOT NULL"]
    params: list = []
    if league is not None:
        clause.append("league = ?")
        params.append(league)
    if season is not None:
        clause.append("season = ?")
        params.append(season)
    _date_bounds(before_date, clause, params)
    _exclusion(exclude_match_id, clause, params)

    sql = (
        f"SELECT ({home_col} + {away_col}) AS total, id FROM matches "
        f"WHERE {' AND '.join(clause)}"
    )
    df = pd.read_sql_query(sql, conn, params=params)
    if df.empty:
        return {"stat": stat, "average": None, "matches": 0, "match_ids": set()}

    return {
        "stat": stat,
        "average": round(float(df["total"].mean()), 2),
        "matches": int(len(df)),
        "match_ids": set(df["id"].tolist()),
    }


def snap_to_half_line(value: float) -> float:
    """Snap a value to the nearest valid bookmaker total line: always x.5.

    This MUST never return a whole number. At a line of 3.0 a match with exactly
    3 goals is a PUSH - it is neither over nor under. A scorer that tests
    `actual > line` silently books every push as an UNDER, which inflates the
    under rate and makes "always back under" look far better than it is.

    Real totals lines are always x.5 precisely so that pushes cannot happen, so
    the fair line has to respect the same rule.
    """
    doubled = float(value) * 2.0
    lower = math.floor(doubled)
    # Only ODD doubled values correspond to x.5 lines.
    candidates = [d for d in range(lower - 1, lower + 3) if d % 2 == 1]
    # Nearest wins; on an exact tie prefer the HIGHER line, so the choice cannot
    # systematically pull the line below the average and manufacture OVERs.
    best = min(candidates, key=lambda d: (abs(d - doubled), -d))
    return best / 2.0


def get_league_reference(conn, league, season, stat_key, before_date=None,
                         shrink_k=SHRINK_K, prior_min=50):
    """A stable league total for this market, shrunk toward a long-run prior.

    A season-only average is far too noisy early on: a handful of matches is
    enough to produce a "fair" line of 6.5 goals or 42.5 shots, which is not a
    line anyone would post and which silently decides every early-season
    fixture. So the season average is shrunk toward, in order of preference:

      1. the same league's average over ALL seasons strictly before the date,
         once that pool has at least `prior_min` matches;
      2. the market's conventional line, when there is no long run yet.

    Everything is cutoff-aware, so this can never see the future.

    Returns the value, how many season matches back it, and which prior was
    used, so the caller can say where the number came from.
    """
    stat = stat_key.replace("avg_", "", 1)
    recent = get_league_average(conn, league, season, stat_key, before_date=before_date)
    long_run = get_league_average(conn, league, None, stat_key, before_date=before_date)

    long_ok = long_run["average"] is not None and long_run["matches"] >= prior_min
    if long_ok:
        prior, prior_name = float(long_run["average"]), "league_long_run"
    else:
        fallback = DEFAULT_MARKET_LINES.get(stat, 2.5)
        prior, prior_name = float(fallback), "market_default"

    if recent["average"] is None:
        # No season matches before this date: either the season label matched
        # nothing in the table or the season has not started. The league's own
        # history is still the best reference available, so say that plainly
        # rather than dressing a real average up as a generic default.
        if long_ok:
            return {
                "stat": stat,
                "average": round(prior, 2),
                "matches": int(long_run["matches"]),
                "basis": "league_long_run",
                "prior": prior_name,
                "prior_value": round(prior, 2),
                "season_average": None,
                "season_matches": 0,
            }
        return {
            "stat": stat,
            "average": round(prior, 2),
            "matches": 0,
            "basis": "market_default",
            "prior": prior_name,
            "prior_value": round(prior, 2),
            "season_average": None,
            "season_matches": 0,
        }

    n = recent["matches"]
    value = (n * recent["average"] + shrink_k * prior) / (n + shrink_k)
    return {
        "stat": stat,
        "average": round(float(value), 2),
        "matches": n,
        "basis": "season_shrunk",
        "prior": prior_name,
        "prior_value": round(prior, 2),
        "season_average": recent["average"],
        "season_matches": n,
    }


def suggest_line(conn, home_team, away_team, stat_key, league=None, season=None,
                 before_date=None, shrink_k=SHRINK_K):
    """A principled default line when the user has not supplied one.

    Uses the shrunk league reference for that stat, computed strictly before the
    match. This is far better than one hardcoded line for every fixture: a
    Liverpool v Arsenal bout and a relegation six-pointer get different
    baselines. Returns (line, source, sample_size).
    """
    stat = stat_key.replace("avg_", "", 1)
    reference = get_league_reference(conn, league, season, stat_key,
                                     before_date=before_date, shrink_k=shrink_k)
    if reference["average"] is None:
        fallback = DEFAULT_MARKET_LINES.get(stat, 2.5)
        return float(fallback), "default", 0
    # Bookmaker totals sit on x.5 boundaries, and so must the fair line, or a
    # match landing exactly on the line becomes an unscorable push.
    snapped = snap_to_half_line(reference["average"])
    source = "fair" if reference.get("basis") != "market_default" else "default"
    return float(snapped), source, reference["season_matches"]


# --------------------------------------------------------------------------
# The weighted combination
# --------------------------------------------------------------------------
def _no_data(base: dict) -> dict:
    """Shared "nothing usable" result, so every caller gets the same keys."""
    base.update({
        "weighted_avg": None,
        "supported": None,
        "evidence_points": 0,
        "low_confidence": True,
        "note": "Not enough data: no source had usable values for this stat.",
    })
    return base


def _shrunk_estimate(home_stats, away_stats, home_away_value, recent_value,
                     h2h_value, league_reference, n_key, shrink_k):
    """Season form shrunk toward the league average, with honest fallbacks.

    value = (n * team_total + k * league_reference) / (n + k)

    n is the SMALLER of the two teams' sample sizes, so one well-covered team
    cannot mask a barely-covered opponent. When season form is missing entirely
    this falls back in order of how much each remaining source is trusted, and
    the returned note always says which route was taken - a number is never
    presented without saying what produced it.
    """
    if home_away_value is not None and league_reference is not None:
        n = min(home_stats.get(n_key, 0) or 0, away_stats.get(n_key, 0) or 0)
        value = (n * home_away_value + shrink_k * league_reference) / (n + shrink_k)
        return value, (f"season home/away shrunk toward the league reference "
                       f"{league_reference} (k={shrink_k:g}, n={n})")
    if home_away_value is not None:
        return home_away_value, "season home/away only (no league reference)"
    if recent_value is not None:
        return recent_value, "recent form only (no season home/away data yet)"
    if league_reference is not None:
        return league_reference, "league average only (no team data yet)"
    if h2h_value is not None:
        return h2h_value, "head-to-head only (no other data)"
    return None, ""


def calculate_weighted_market(home_stats, away_stats, home_recent, away_recent,
                              h2h_stats, stat_key, line, weights=None,
                              method="shrunk", shrink_k=SHRINK_K,
                              league_reference=None,
                              league_reference_basis=None):
    """Combine the evidence sources into one number.

    Two methods exist, and the backtester compares them head to head:

      * "shrunk" (default) - the two teams' season form pulled toward the league
        average by sample size: (n * team_total + k * league_reference) / (n + k).

      * "blend" - the original fixed 45/35/20 average of season form, recent
        form and head-to-head, rescaled across whatever sources have data.

    "shrunk" is the default because it was MEASURED to be better, not preferred
    by taste: over 4,280 matches per market it had lower mean absolute error
    than the blend in four of six markets, was statistically tied in the other
    two, and was never clearly worse. Recent form and head-to-head are still
    returned in full so the UI can show them as evidence, but they do not vote,
    because on that sample they added variance rather than accuracy.

    Every source also gets a `reliability` - n / (n + k) - saying how much of it
    survives shrinkage. A source with 5 matches at k=10 is one-third itself and
    two-thirds league average, and the UI is expected to say so.
    """
    stat = stat_key.replace("avg_", "", 1)
    n_key = f"n_{stat}"

    def combined(a, b):
        if a.get(stat_key) is None or b.get(stat_key) is None:
            return None, 0
        return round(a[stat_key] + b[stat_key], 2), a.get(n_key, 0) + b.get(n_key, 0)

    home_away_value, home_away_sample = combined(home_stats, away_stats)
    recent_value, recent_sample = combined(home_recent, away_recent)

    h2h_value = None
    h2h_sample = 0
    if h2h_stats.get("matches_played", 0) > 0 and h2h_stats.get(stat_key) is not None:
        h2h_value = h2h_stats[stat_key]
        h2h_sample = h2h_stats.get(n_key, 0)

    values = {
        "home_away": home_away_value,
        "recent_form": recent_value,
        "h2h": h2h_value,
    }
    available = {k: v for k, v in values.items() if v is not None}

    samples = {
        "home_away": home_away_sample,
        "recent_form": recent_sample,
        "h2h": h2h_sample,
    }
    reliability = {
        key: (round(n / (n + shrink_k), 3) if n else 0.0)
        for key, n in samples.items()
    }

    base = {
        "stat": stat,
        "line": line,
        "home_away_combined": home_away_value,
        "recent_form_combined": recent_value,
        "h2h_combined": h2h_value,
        "home_away_sample": home_away_sample,
        "recent_form_sample": recent_sample,
        "h2h_sample": h2h_sample,
        "sample_sizes": samples,
        "reliability": reliability,
        "shrink_k": shrink_k,
        "league_reference": league_reference,
        "method": method,
        "sources_used": sorted(available.keys()),
    }

    # Say so when a typed team average is part of the number, and when it was
    # dated. A value that came from outside the data must never be presented as
    # though the database had counted it.
    profile_home = home_stats.get(f"profile_{stat}")
    profile_away = away_stats.get(f"profile_{stat}")
    if profile_home or profile_away:
        stamp = (profile_home or profile_away).get("as_of")
        who = "both teams" if (profile_home and profile_away) else (
            "the home team" if profile_home else "the away team")
        base["profile_used"] = True
        base["profile_as_of"] = stamp
        base["profile_teams"] = who
        base["profile_note"] = (
            f"includes an admin-typed per-game average for {who} "
            f"(as of {stamp}); not measured from matches")

    if not available:
        return _no_data(base)

    # A typed team average must never be shrunk toward a hardcoded guess.
    #
    # The shrink target is meant to be the league's own measured average. When a
    # market has no league data at all - which is the normal state of the eight
    # manual markets - `get_league_reference` still returns a number, but it is
    # just DEFAULT_MARKET_LINES dressed up with basis="market_default". Shrinking
    # a real typed average toward it discards the only actual information in the
    # room: a 39.2 profile pulled to 40.2 by a 40.5 guess, at n=3 keeping barely
    # a fifth of the input. So in that one case the reference is dropped and the
    # profile stands on its own. A measured market is untouched, because this
    # only fires when a profile contributed AND the reference is the default.
    if (profile_home or profile_away) and league_reference_basis == "market_default":
        league_reference = None
        base["league_reference"] = None
        base["profile_reference_dropped"] = "market_default"

    if method == "blend":
        use = dict(weights or DEFAULT_WEIGHTS)
        total_weight = sum(use[k] for k in available)
        # Weights rescaled so the surviving sources still sum to 1.
        rescaled = {k: use[k] / total_weight for k in available}
        weighted_avg = sum(v * rescaled[k] for k, v in available.items())
        base["weights_used"] = {k: round(v, 4) for k, v in rescaled.items()}
    else:
        weighted_avg, method_note = _shrunk_estimate(
            home_stats, away_stats, home_away_value, recent_value, h2h_value,
            league_reference, n_key, shrink_k)
        if weighted_avg is None:
            return _no_data(base)
        base["method_note"] = method_note

    # Evidence = number of DISTINCT matches that carry a value for THIS stat
    # behind the sources actually used. A match appearing in both the form set
    # and the H2H set counts once. Counting the whole window instead would
    # flatter a sparse market badly: two hand-typed rows in ten matches would
    # report ten.
    unique_ids: set = set()
    for source, result in (
        ("home_away", (home_stats, away_stats)),
        ("recent_form", (home_recent, away_recent)),
    ):
        if source in available:
            for r in result:
                unique_ids.update(r.get(f"ids_{stat}", set()))
    if "h2h" in available:
        unique_ids.update(h2h_stats.get(f"ids_{stat}", set()))

    evidence_points = len(unique_ids)

    base.update({
        "weighted_avg": round(weighted_avg, 2),
        "supported": weighted_avg > line,
        "evidence_points": evidence_points,
        "low_confidence": evidence_points < 3,
    })
    return base


def _apply_profile(result: dict, profile: dict | None) -> None:
    """Fill in a typed per-game average where nothing was measured.

    Measured data always wins: a stat whose counted average is not None is left
    exactly as it was, so a profile can only ever ADD a number that was missing,
    never overwrite one that was counted. That ordering is the whole safety
    argument for letting an unverified number near the engine at all.

    What it writes, per filled stat:
        avg_<stat>      the typed average
        n_<stat>        PROFILE_WEIGHT, so shrinkage treats it as a few matches
        profile_<stat>  where it came from, so the UI can say so out loud

    It deliberately writes no `ids_<stat>`. Evidence points are counted from
    match ids, and a typed average stands behind no match - claiming otherwise
    would be exactly the invented confidence the UI is supposed to refuse.
    """
    if not profile:
        return
    for stat in PROFILE_STATS:
        value = profile.get(stat)
        if value is None:
            continue
        if result.get(f"avg_{stat}") is not None:
            continue                      # counted data wins
        result[f"avg_{stat}"] = round(float(value), 2)
        result[f"n_{stat}"] = int(PROFILE_WEIGHT)
        result[f"profile_{stat}"] = {
            "as_of": profile.get("as_of"),
            "league": profile.get("league"),
            "season": profile.get("season"),
        }


def analyze_market(conn, home_team, away_team, stat_key, line=None, recent_n=5,
                   before_date=None, exclude_match_id=None, weights=None,
                   league=None, season=None, method="shrunk",
                   shrink_k=SHRINK_K):
    """Full evidence bundle for one market of one match.

    If `line` is None a fair line is derived from the shrunk league reference
    (strictly before the match), and `line_source` says where it came from.
    `method` selects the estimator - see `calculate_weighted_market`.
    """
    home_stats = get_team_home_averages(
        conn, home_team, before_date=before_date, exclude_match_id=exclude_match_id)
    away_stats = get_team_away_averages(
        conn, away_team, before_date=before_date, exclude_match_id=exclude_match_id)
    home_recent = get_team_recent_averages(
        conn, home_team, n=recent_n, before_date=before_date, exclude_match_id=exclude_match_id)
    away_recent = get_team_recent_averages(
        conn, away_team, n=recent_n, before_date=before_date, exclude_match_id=exclude_match_id)
    h2h_stats = get_h2h_summary(
        conn, home_team, away_team, before_date=before_date, exclude_match_id=exclude_match_id)

    # Admin-typed per-team averages, used only where the counted data is empty.
    # Guarded on the cutoff through their own `as_of` date, so a profile entered
    # after a match cannot be used to analyse that match.
    _apply_profile(home_stats, get_team_profile(
        conn, home_team, league=league, season=season, before_date=before_date))
    _apply_profile(away_stats, get_team_profile(
        conn, away_team, league=league, season=season, before_date=before_date))

    reference = get_league_reference(conn, league, season, stat_key,
                                     before_date=before_date, shrink_k=shrink_k)

    line_source = "user"
    if line is None:
        line, line_source, _ = suggest_line(
            conn, home_team, away_team, stat_key, league=league, season=season,
            before_date=before_date, shrink_k=shrink_k)

    result = calculate_weighted_market(
        home_stats, away_stats, home_recent, away_recent, h2h_stats,
        stat_key, line, weights=weights, method=method, shrink_k=shrink_k,
        league_reference=reference["average"],
        league_reference_basis=reference.get("basis"))

    result["line_source"] = line_source
    result["line_reference"] = reference
    result["home_team"] = home_team
    result["away_team"] = away_team
    result["season_home_matches"] = home_stats.get("matches_played", 0)
    result["season_away_matches"] = away_stats.get("matches_played", 0)
    result["h2h_matches"] = h2h_stats.get("matches_played", 0)
    return result


# --------------------------------------------------------------------------
# Fixtures / lists
# --------------------------------------------------------------------------
def get_matches_by_date(conn, date=None):
    if date is None:
        date = _dt.date.today().isoformat()
    sql = (
        "SELECT id, date, league, season, status, home_team, away_team, "
        "home_goals, away_goals, home_ht_goals, away_ht_goals, referee, "
        "odds_home_win, odds_draw, odds_away_win, odds_over_2_5, odds_under_2_5 "
        "FROM matches WHERE date = ? ORDER BY league, home_team"
    )
    return _manual_merge(conn, pd.read_sql_query(sql, conn, params=(date,)))


def get_upcoming_matches(conn, from_date=None, limit=50):
    """Matches with no score yet (status = scheduled)."""
    if from_date is None:
        from_date = _dt.date.today().isoformat()
    sql = (
        "SELECT id, date, league, season, status, home_team, away_team "
        "FROM matches WHERE status = 'scheduled' AND date >= ? "
        "ORDER BY date ASC, league ASC LIMIT ?"
    )
    return pd.read_sql_query(sql, conn, params=(from_date, limit))


def find_teams(conn, search_text, league=None):
    """Search team names across BOTH home and away columns.

    The original version only looked at home_team, so a team that happened to
    be away in every stored match was invisible.
    """
    sql = (
        "SELECT DISTINCT team, league FROM ("
        "  SELECT home_team AS team, league FROM matches "
        "  UNION SELECT away_team AS team, league FROM matches"
        ") WHERE team LIKE ?"
    )
    params: list = [f"%{search_text}%"]
    if league is not None:
        sql += " AND league = ?"
        params.append(league)
    sql += " ORDER BY team"
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchall()


def list_leagues(conn):
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT league FROM matches ORDER BY league")
    return [r[0] for r in cur.fetchall()]


def list_markets():
    """All markets the engine can analyse, with their default lines."""
    return [
        {"market": name, "default_line": DEFAULT_MARKET_LINES.get(name)}
        for name in STAT_COLUMNS
    ]
