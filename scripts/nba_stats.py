"""The NBA model: possessions -> ratings -> expected points, with evidence.

What is deliberately different from football
--------------------------------------------
Football's engine is a weighted average of goals, corners and cards, shrunk
toward a league reference. Basketball is not that shape. A basketball game is
a fixed number of possessions, and every point is a rate per possession, so the
model works in the same units the sport does:

    possessions = FGA + 0.44 * FTA - OREB + TOV
    off_rating  = 100 * points / possessions
    pace        = possessions * 48 / game_minutes

A team's expected offensive rating against a given opponent is

    expected_off = own_off * opponent_def / league_off

which says: a good offence against a bad defence scores more, and both effects
are measured against the league, not invented. The expected points are then

    points = expected_off * expected_possessions / 100

where expected possessions is the average of the two teams' pace. That is why
pace is a first-class input and not a footnote: the same two offences produce a
different total in a fast game.

The three estimates and their shrinkage
---------------------------------------
Each team has three nested estimates, and each is shrunk toward the one above it:

    prior  = every stored game before the fixture (the long-run team rating)
    season = this season's games, shrunk toward `prior`
    recent = the last 10 games, shrunk toward `season`

Shrinking toward a prior rather than toward zero is what stops a 3-game sample
from producing a 130-point projection. It also solves the opening-night problem
without a special case: with zero games this season, `season` collapses to
`prior`, so the model uses last season and says so.

At season start the prior is last season's 82 games, not an empty table. That is
the whole reason history is ingested before the schedule.

No bet recommendation
---------------------
`supported` means only "the model's number is on the over side of this line".
It is not a pick, and the UI must not present it as one.
"""
from __future__ import annotations

import math
import sqlite3

# How many games a new observation needs before it outweighs its prior. These
# are NBA values, not football's SHRINK_K = 10: a team's offensive rating
# stabilises faster than a corner average because each game carries ~100
# possessions, but a 3-game sample is still noise.
SHRINK_K_PRIOR = 12.0
SHRINK_K_SEASON = 8.0
SHRINK_K_RECENT = 5.0
SHRINK_K_PACE = 10.0

# The blend of the three estimates. Season-to-date is the backbone, recent form
# is a tilt, and the long-run prior is a small anchor. These are a starting
# point to be tested by the backtester, exactly as football's 45/35/20 were.
WEIGHT_SEASON = 0.65
WEIGHT_RECENT = 0.25
WEIGHT_PRIOR = 0.10

# A league average built from fewer than this many games is blended with the
# previous season rather than trusted on its own.
LEAGUE_SHRINK_GAMES = 200.0

# The team counting stats. Two-pointers are derived rather than stored, so a
# two and a three can never be counted twice. Each stat is projected in the
# same units as points: a rate per 100 possessions, multiplied by the expected
# possessions of this game.
STAT_COLUMNS = {
    "rebounds": "reb",
    "two_pm": "(fgm - fg3m)",
    "three_pm": "fg3m",
    "assists": "ast",
    "turnovers": "tov",
    "steals": "stl",
    "blocks": "blk",
    "fouls": "pf",
}

STAT_LABELS = {
    "rebounds": "Rebounds",
    "two_pm": "2-pointers",
    "three_pm": "3-pointers",
    "assists": "Assists",
    "turnovers": "Turnovers",
    "steals": "Steals",
    "blocks": "Blocks",
    "fouls": "Fouls",
}

# The three Board keys for each stat, in scope order. Keeping the keys explicit
# makes a migration additive: an old board row keeps its engine key.
STAT_MARKETS = {
    "rebounds": ("rebounds_total", "home_rebounds", "away_rebounds"),
    "two_pm": ("two_pm_total", "home_two_pm", "away_two_pm"),
    "three_pm": ("three_pm_total", "home_three_pm", "away_three_pm"),
    "assists": ("assists_total", "home_assists", "away_assists"),
    "turnovers": ("turnovers_total", "home_turnovers", "away_turnovers"),
    "steals": ("steals_total", "home_steals", "away_steals"),
    "blocks": ("blocks_total", "home_blocks", "away_blocks"),
    "fouls": ("fouls_total", "home_fouls", "away_fouls"),
}

# A flat lookup used by grading and the Board line override.
STAT_BY_MARKET = {
    key: (stat, scope)
    for stat, keys in STAT_MARKETS.items()
    for key, scope in zip(keys, ("combined", "home", "away"))
}

REBOUND_MARKETS = STAT_MARKETS["rebounds"]

# Used only when the database is empty; never presented as measured.
FALLBACK_LEAGUE = {
    "off": 113.5,
    "def": 113.5,
    "pace": 99.0,
    "home_edge": 2.4,
    "reb": 43.8,
    "reb_home_edge": 0.0,
    "stats": {
        "rebounds": 43.8,
        "two_pm": 28.7,
        "three_pm": 13.2,
        "assists": 26.7,
        "turnovers": 14.1,
        "steals": 8.0,
        "blocks": 5.0,
        "fouls": 19.1,
    },
    "stat_edges": {stat: 0.0 for stat in STAT_COLUMNS},
}

# Quarter and half markets need enough line scores to measure a share and a
# baseline. Below this they are reported as unavailable, not guessed.
MIN_QUARTER_GAMES = 100


def season_for_date(iso_date: str) -> str:
    """The NBA season label for a date: October starts a new season."""
    year = int(str(iso_date)[:4])
    month = int(str(iso_date)[5:7])
    start = year if month >= 10 else year - 1
    return f"{start}-{str(start + 1)[2:]}"


def snap_to_half_line(value: float) -> float:
    """Snap to the nearest x.5 line, never a whole number.

    Same rule as football, for the same reason: a whole-number line admits a
    push, and a push silently becomes an under in any `actual > line` test.
    """
    doubled = float(value) * 2.0
    lower = math.floor(doubled)
    candidates = [d for d in range(lower - 1, lower + 3) if d % 2 == 1]
    best = min(candidates, key=lambda d: (abs(d - doubled), -d))
    return best / 2.0


def _shrink(mean, n, prior, k):
    """Weighted mean of a sample and its prior, by sample size."""
    if mean is None:
        return prior
    n = float(n or 0)
    if prior is None:
        return mean
    return (n * mean + k * prior) / (n + k)


def _mean(conn: sqlite3.Connection, sql: str, params=()):
    row = conn.execute(sql, params).fetchone()
    if not row:
        return None, 0
    mean, n = row[0], row[1] or 0
    return (float(mean) if mean is not None else None), int(n)


def _rate_sum(conn: sqlite3.Connection, where: str, params=(),
              expr: str = "reb"):
    """A per-100-possession rate from SUM/SUM, plus the row count.

    A mean of per-game rates would be right only if every game had the same
    number of possessions. Dividing two sums keeps fast and slow games weighted
    by how much basketball they actually contained.
    """
    row = conn.execute(
        f"SELECT SUM({expr}), SUM(possessions), COUNT(*) FROM nba_team_games "
        f"WHERE {where} AND {expr} IS NOT NULL AND possessions > 0",
        params,
    ).fetchone()
    if not row or not row[1]:
        return None, 0
    return 100.0 * float(row[0]) / float(row[1]), int(row[2] or 0)


def _aggregate(conn: sqlite3.Connection, where: str, params=()):
    """Every team counting stat per 100 possessions from one scan.

    Returns ({stat: rate}, team_games). One query is enough because all eight
    stats share the same `possessions` denominator. That matters: the NBA
    measurement walks 2,500 games, and eight separate scans per team would make
    the walk multiply instead of grow linearly.
    """
    sums = ", ".join(
        f"SUM(COALESCE({expr}, 0)) AS {stat}"
        for stat, expr in STAT_COLUMNS.items())
    # A game missing one stat column is not a zero; it is an incomplete row.
    # Requiring every column keeps a future data gap from silently lowering a
    # team's rate instead of reducing the sample.
    valid = " AND ".join(f"{expr} IS NOT NULL" for expr in STAT_COLUMNS.values())
    row = conn.execute(
        f"SELECT COUNT(*), SUM(possessions), {sums} FROM nba_team_games "
        f"WHERE {where} AND possessions > 0 AND {valid}", params).fetchone()
    if not row or not row[1]:
        return {}, 0
    possessions = float(row[1])
    rates = {
        stat: 100.0 * float(row[2 + i] or 0) / possessions
        for i, stat in enumerate(STAT_COLUMNS)
    }
    return rates, int(row[0] or 0)


def _aggregate_recent(conn: sqlite3.Connection, column: str, team_id: int,
                      before_date: str, limit: int = 10):
    """The last `limit` games for one side, with all eight stat sums."""
    inner_cols = ", ".join(
        f"{expr} AS {stat}" for stat, expr in STAT_COLUMNS.items())
    sums = ", ".join(f"SUM({stat}) AS {stat}" for stat in STAT_COLUMNS)
    valid = " AND ".join(f"{expr} IS NOT NULL" for expr in STAT_COLUMNS.values())
    row = conn.execute(
        f"SELECT COUNT(*), SUM(possessions), {sums} FROM ("
        f"  SELECT possessions, {inner_cols} FROM nba_team_games "
        f"  WHERE {column} = ? AND game_date < ? AND possessions > 0 "
        f"  AND {valid} ORDER BY game_date DESC LIMIT {int(limit)})",
        (team_id, before_date)).fetchone()
    if not row or not row[1]:
        return {}, 0
    possessions = float(row[1])
    rates = {
        stat: 100.0 * float(row[2 + i] or 0) / possessions
        for i, stat in enumerate(STAT_COLUMNS)
    }
    return rates, int(row[0] or 0)


# ---------------------------------------------------------------------------
# League and team estimates
# ---------------------------------------------------------------------------

def league_averages(conn: sqlite3.Connection, before_date: str,
                    season: str) -> dict:
    """League offence, defence, pace and home edge, strictly before the game."""
    season_off, season_n = _mean(
        conn,
        "SELECT AVG(off_rating), COUNT(*) FROM nba_team_games "
        "WHERE game_date < ? AND season = ? AND off_rating IS NOT NULL",
        (before_date, season),
    )
    season_def, _ = _mean(
        conn,
        "SELECT AVG(def_rating), COUNT(*) FROM nba_team_games "
        "WHERE game_date < ? AND season = ? AND def_rating IS NOT NULL",
        (before_date, season),
    )
    season_pace, _ = _mean(
        conn,
        "SELECT AVG(pace), COUNT(*) FROM nba_team_games "
        "WHERE game_date < ? AND season = ? AND pace IS NOT NULL",
        (before_date, season),
    )
    prior_off, prior_n = _mean(
        conn,
        "SELECT AVG(off_rating), COUNT(*) FROM nba_team_games "
        "WHERE game_date < ? AND season < ? AND off_rating IS NOT NULL",
        (before_date, season),
    )
    prior_def, _ = _mean(
        conn,
        "SELECT AVG(def_rating), COUNT(*) FROM nba_team_games "
        "WHERE game_date < ? AND season < ? AND def_rating IS NOT NULL",
        (before_date, season),
    )
    prior_pace, _ = _mean(
        conn,
        "SELECT AVG(pace), COUNT(*) FROM nba_team_games "
        "WHERE game_date < ? AND season < ? AND pace IS NOT NULL",
        (before_date, season),
    )

    season_games = season_n / 2.0
    prior_games = prior_n / 2.0
    # With no prior at all, fall back to the constants, but report that.
    basis = "measured"
    if prior_off is None:
        basis = "league constants"
        prior_off = FALLBACK_LEAGUE["off"]
        prior_def = FALLBACK_LEAGUE["def"]
        prior_pace = FALLBACK_LEAGUE["pace"]

    off = _shrink(season_off, season_games, prior_off, LEAGUE_SHRINK_GAMES)
    deff = _shrink(season_def, season_games, prior_def, LEAGUE_SHRINK_GAMES)
    pace = _shrink(season_pace, season_games, prior_pace, LEAGUE_SHRINK_GAMES)

    home_pts, home_n = _mean(
        conn,
        "SELECT AVG(points), COUNT(*) FROM nba_team_games "
        "WHERE game_date < ? AND is_home = 1 AND points IS NOT NULL",
        (before_date,),
    )
    away_pts, away_n = _mean(
        conn,
        "SELECT AVG(points), COUNT(*) FROM nba_team_games "
        "WHERE game_date < ? AND is_home = 0 AND points IS NOT NULL",
        (before_date,),
    )
    edge = (home_pts - away_pts
            if home_pts is not None and away_pts is not None else None)
    if edge is None or home_n < 50 or away_n < 50:
        edge = FALLBACK_LEAGUE["home_edge"]

    season_stats, season_stat_n = _aggregate(
        conn, "game_date < ? AND season = ?", (before_date, season))
    prior_stats, prior_stat_n = _aggregate(
        conn, "game_date < ? AND season < ?", (before_date, season))
    home_stats, home_stat_n = _aggregate(
        conn, "game_date < ? AND is_home = 1", (before_date,))
    away_stats, away_stat_n = _aggregate(
        conn, "game_date < ? AND is_home = 0", (before_date,))

    if not prior_stats:
        basis = "league constants"
    stat_rates = {}
    stat_edges = {}
    for stat in STAT_COLUMNS:
        prior = prior_stats.get(stat, FALLBACK_LEAGUE["stats"][stat])
        stat_rates[stat] = _shrink(
            season_stats.get(stat), season_stat_n / 2.0, prior,
            LEAGUE_SHRINK_GAMES)
        measured_edge = None
        if (home_stats.get(stat) is not None
                and away_stats.get(stat) is not None
                and home_stat_n >= 50 and away_stat_n >= 50):
            measured_edge = home_stats[stat] - away_stats[stat]
        stat_edges[stat] = (
            measured_edge if measured_edge is not None
            else FALLBACK_LEAGUE["stat_edges"][stat])

    reb = stat_rates["rebounds"]
    reb_edge = stat_edges["rebounds"]

    return {
        "off": off,
        "def": deff,
        "pace": pace,
        "home_edge": edge,
        "reb": reb,
        "reb_home_edge": reb_edge,
        "stats": stat_rates,
        "stat_edges": stat_edges,
        "season_games": season_games,
        "prior_games": prior_games,
        "basis": basis,
    }


def _component(conn, team_id: int, before_date: str, column: str,
               where_extra: str = "", params=()):
    return _mean(
        conn,
        f"SELECT AVG({column}), COUNT(*) FROM nba_team_games "
        f"WHERE team_id = ? AND game_date < ? AND {column} IS NOT NULL "
        f"{where_extra}",
        (team_id, before_date, *params),
    )


def team_form(conn: sqlite3.Connection, team_id: int, before_date: str,
              season: str, league: dict | None = None) -> dict:
    """A team's offence, defence and pace, with the sample behind each."""
    if league is None:
        league = league_averages(conn, before_date, season)

    prior_off, prior_n = _component(conn, team_id, before_date, "off_rating",
                                    "AND season < ?", (season,))
    prior_def, _ = _component(conn, team_id, before_date, "def_rating",
                              "AND season < ?", (season,))
    prior_pace, _ = _component(conn, team_id, before_date, "pace",
                               "AND season < ?", (season,))

    season_off, season_n = _component(conn, team_id, before_date, "off_rating",
                                      "AND season = ?", (season,))
    season_def, _ = _component(conn, team_id, before_date, "def_rating",
                               "AND season = ?", (season,))
    season_pace, _ = _component(conn, team_id, before_date, "pace",
                                "AND season = ?", (season,))

    recent_off, recent_n = _mean(
        conn,
        "SELECT AVG(off_rating), COUNT(*) FROM ("
        "  SELECT off_rating FROM nba_team_games "
        "  WHERE team_id = ? AND game_date < ? AND off_rating IS NOT NULL "
        "  ORDER BY game_date DESC LIMIT 10)",
        (team_id, before_date),
    )
    recent_def, _ = _mean(
        conn,
        "SELECT AVG(def_rating), COUNT(*) FROM ("
        "  SELECT def_rating FROM nba_team_games "
        "  WHERE team_id = ? AND game_date < ? AND def_rating IS NOT NULL "
        "  ORDER BY game_date DESC LIMIT 10)",
        (team_id, before_date),
    )
    recent_pace, _ = _mean(
        conn,
        "SELECT AVG(pace), COUNT(*) FROM ("
        "  SELECT pace FROM nba_team_games "
        "  WHERE team_id = ? AND game_date < ? AND pace IS NOT NULL "
        "  ORDER BY game_date DESC LIMIT 10)",
        (team_id, before_date),
    )

    base_off = _shrink(prior_off, prior_n, league["off"], SHRINK_K_PRIOR)
    base_def = _shrink(prior_def, prior_n, league["def"], SHRINK_K_PRIOR)
    base_pace = _shrink(prior_pace, prior_n, league["pace"], SHRINK_K_PACE)

    season_off_s = _shrink(season_off, season_n, base_off, SHRINK_K_SEASON)
    season_def_s = _shrink(season_def, season_n, base_def, SHRINK_K_SEASON)
    season_pace_s = _shrink(season_pace, season_n, base_pace, SHRINK_K_PACE)

    recent_off_s = _shrink(recent_off, recent_n, season_off_s, SHRINK_K_RECENT)
    recent_def_s = _shrink(recent_def, recent_n, season_def_s, SHRINK_K_RECENT)
    recent_pace_s = _shrink(recent_pace, recent_n, season_pace_s, SHRINK_K_RECENT)

    def blend(prior, season_value, recent):
        return (WEIGHT_SEASON * season_value + WEIGHT_RECENT * recent
                + WEIGHT_PRIOR * prior)

    return {
        "team_id": team_id,
        "off": blend(base_off, season_off_s, recent_off_s),
        "def": blend(base_def, season_def_s, recent_def_s),
        "pace": blend(base_pace, season_pace_s, recent_pace_s),
        "base_off": base_off,
        "base_def": base_def,
        "base_pace": base_pace,
        "season_off": season_off_s,
        "season_def": season_def_s,
        "season_pace": season_pace_s,
        "recent_off": recent_off_s,
        "recent_def": recent_def_s,
        "recent_pace": recent_pace_s,
        "n_prior": prior_n,
        "n_season": season_n,
        "n_recent": recent_n,
        "off_reliability": _reliability(season_n, SHRINK_K_SEASON),
        "def_reliability": _reliability(season_n, SHRINK_K_SEASON),
        "pace_reliability": _reliability(season_n, SHRINK_K_PACE),
    }


def _reliability(n: int, k: float) -> float:
    """n / (n + k): how much of a source survives shrinkage."""
    return float(n) / (float(n) + k)


# ---------------------------------------------------------------------------
# Team counting stats: a rate per 100 possessions, projected like points
# ---------------------------------------------------------------------------

def _stat_profile(conn: sqlite3.Connection, column: str, team_id: int,
                  before_date: str, season: str, league: dict) -> dict:
    """One side of a stat matchup: own rate or rate allowed.

    `column` is `team_id` for a team's own stats and `opponent_id` for what its
    opponents collected. Each row is still one team-game, so the rate is per 100
    possessions of the team that collected the stat.
    """
    prior, prior_n = _aggregate(
        conn, f"{column} = ? AND game_date < ? AND season < ?",
        (team_id, before_date, season))
    current, current_n = _aggregate(
        conn, f"{column} = ? AND game_date < ? AND season = ?",
        (team_id, before_date, season))
    recent, recent_n = _aggregate_recent(conn, column, team_id, before_date)

    out = {}
    for stat in STAT_COLUMNS:
        base = _shrink(prior.get(stat), prior_n, league["stats"][stat],
                       SHRINK_K_PRIOR)
        season_s = _shrink(current.get(stat), current_n, base,
                           SHRINK_K_SEASON)
        recent_s = _shrink(recent.get(stat), recent_n, season_s,
                           SHRINK_K_RECENT)
        blended = (WEIGHT_SEASON * season_s + WEIGHT_RECENT * recent_s
                   + WEIGHT_PRIOR * base)
        out[stat] = {
            "rate": blended,
            "prior": base,
            "season": season_s,
            "recent": recent_s,
            "n_prior": prior_n,
            "n_season": current_n,
            "n_recent": recent_n,
        }
    return out


def team_stat_form(conn: sqlite3.Connection, team_id: int, before_date: str,
                   season: str, league: dict | None = None) -> dict:
    """Every counting stat for and against, per 100 possessions."""
    if league is None:
        league = league_averages(conn, before_date, season)
    own = _stat_profile(conn, "team_id", team_id, before_date, season, league)
    allowed = _stat_profile(conn, "opponent_id", team_id, before_date, season,
                            league)
    return {
        stat: {"own": own[stat]["rate"], "allowed": allowed[stat]["rate"],
               "own_detail": own[stat], "allowed_detail": allowed[stat]}
        for stat in STAT_COLUMNS
    }


def rebound_form(conn: sqlite3.Connection, team_id: int, before_date: str,
                 season: str, league: dict | None = None) -> dict:
    """Compatibility wrapper: the rebound block of `team_stat_form`."""
    return team_stat_form(conn, team_id, before_date, season, league)["rebounds"]


def team_stat_baseline(box: dict, stat: str) -> float | None:
    """This team's own per-game average for a counting stat.

    Season-to-date once five games exist, otherwise the prior season. It is the
    line the Board tests for a team stat: "more or less than this team usually
    gets", not "more or less than the league average".
    """
    season = (box or {}).get("season") or {}
    prior = (box or {}).get("prior") or {}
    if season.get("games", 0) >= 5 and season.get(stat) is not None:
        return float(season[stat])
    if prior.get(stat) is not None:
        return float(prior[stat])
    return None


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def project(conn: sqlite3.Connection, home_team_id: int, away_team_id: int,
            game_date: str, season: str | None = None) -> dict:
    """Expected points for both teams, the total, and the sample behind it."""
    season = season or season_for_date(game_date)
    league = league_averages(conn, game_date, season)
    home = team_form(conn, home_team_id, game_date, season, league)
    away = team_form(conn, away_team_id, game_date, season, league)

    possessions = (home["pace"] + away["pace"]) / 2.0
    home_off = home["off"] * away["def"] / league["off"]
    away_off = away["off"] * home["def"] / league["def"]

    home_points = home_off * possessions / 100.0 + league["home_edge"] / 2.0
    away_points = away_off * possessions / 100.0 - league["home_edge"] / 2.0

    home_stats = team_stat_form(conn, home_team_id, game_date, season, league)
    away_stats = team_stat_form(conn, away_team_id, game_date, season, league)
    stat_projections = {}
    for stat in STAT_COLUMNS:
        league_rate = league["stats"][stat]
        home_per100 = (home_stats[stat]["own"]
                       * away_stats[stat]["allowed"] / league_rate)
        away_per100 = (away_stats[stat]["own"]
                       * home_stats[stat]["allowed"] / league_rate)
        edge = league["stat_edges"][stat]
        home_value = (home_per100 + edge / 2.0) * possessions / 100.0
        away_value = (away_per100 - edge / 2.0) * possessions / 100.0
        fair_home_stat = (
            league_rate + edge / 2.0) * league["pace"] / 100.0
        fair_away_stat = (
            league_rate - edge / 2.0) * league["pace"] / 100.0
        stat_projections[stat] = {
            "home": home_value,
            "away": away_value,
            "total": home_value + away_value,
            "fair_home": fair_home_stat,
            "fair_away": fair_away_stat,
            "fair_total": fair_home_stat + fair_away_stat,
            "home_per100": home_per100,
            "away_per100": away_per100,
            "home_form": home_stats[stat],
            "away_form": away_stats[stat],
        }
    rebounds = stat_projections["rebounds"]

    sigma = _total_sigma(conn, game_date)
    total = home_points + away_points
    # The fair line is what a league-average matchup at this pace would score,
    # NOT the projection rounded. Rounding the projection would make
    # `projection > line` true almost by construction and turn the verdict into
    # a tautology.
    fair_home = league["off"] * league["pace"] / 100.0 + league["home_edge"] / 2.0
    fair_away = league["off"] * league["pace"] / 100.0 - league["home_edge"] / 2.0
    fair_total = fair_home + fair_away

    return {
        "season": season,
        "game_date": game_date,
        "home_team_id": home_team_id,
        "away_team_id": away_team_id,
        "home_points": home_points,
        "away_points": away_points,
        "total": total,
        "fair_total": fair_total,
        "fair_home": fair_home,
        "fair_away": fair_away,
        "possessions": possessions,
        "home_off_rating": home_off,
        "away_off_rating": away_off,
        "home": home,
        "away": away,
        "league": league,
        "rebounds": rebounds,
        "team_stats": stat_projections,
        "sigma": sigma,
        "range_low": total - 1.645 * sigma,
        "range_high": total + 1.645 * sigma,
        "range_low_68": total - sigma,
        "range_high_68": total + sigma,
        "range_label": ("central 90% of NBA game totals historically; "
                        "a spread, not a calibrated prediction interval"),
    }


def _total_sigma(conn: sqlite3.Connection, before_date: str) -> float:
    """Standard deviation of completed game totals before the fixture."""
    row = conn.execute(
        "SELECT AVG(t), AVG(t * t), COUNT(*) FROM ("
        "  SELECT (home_score + away_score) AS t FROM nba_games "
        "  WHERE game_date < ? AND home_score IS NOT NULL "
        "    AND away_score IS NOT NULL)",
        (before_date,),
    ).fetchone()
    if not row or not row[2]:
        return 18.0
    mean, mean_sq, n = row
    variance = max(mean_sq - mean * mean, 0.0)
    return math.sqrt(variance) if n > 1 else 18.0


# ---------------------------------------------------------------------------
# Baselines: measured frequencies, never calibrated probabilities
# ---------------------------------------------------------------------------

def baseline(conn: sqlite3.Connection, before_date: str, line: float,
             team_id: int | None = None, part: str = "game",
             stat: str | None = None, scope: str = "combined") -> dict:
    """How often the relevant population finished over a line, before the game.

    `part` is 'game', 'team', 'half' or 'quarter'. `stat` + `scope` are used for
    the eight counting stats; they are separate from `part` so a new stat does
    not need a new branch. Half and quarter counts need line scores and return
    `available=False` when there are too few.
    """
    if stat:
        if stat not in STAT_COLUMNS:
            return {"line": line, "over": 0, "total": 0, "rate": None,
                    "label": "unknown stat", "available": False}
        expr = STAT_COLUMNS[stat]
        if scope == "combined":
            row = conn.execute(
                f"SELECT COUNT(*), SUM(CASE WHEN total_stat > ? "
                f"THEN 1 ELSE 0 END) FROM ("
                f"  SELECT game_id, SUM({expr}) AS total_stat "
                f"  FROM nba_team_games WHERE game_date < ? "
                f"  AND {expr} IS NOT NULL GROUP BY game_id "
                f"  HAVING COUNT(*) = 2)",
                (line, before_date),
            ).fetchone()
            label = "all NBA games before this one"
        else:
            row = conn.execute(
                f"SELECT COUNT(*), SUM(CASE WHEN {expr} > ? THEN 1 ELSE 0 END) "
                f"FROM nba_team_games WHERE game_date < ? AND team_id = ? "
                f"AND {expr} IS NOT NULL",
                (line, before_date, team_id),
            ).fetchone()
            label = "this team's games before this one"
    elif part == "game":
        row = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN home_score + away_score > ? "
            "THEN 1 ELSE 0 END) FROM nba_games "
            "WHERE game_date < ? AND home_score IS NOT NULL "
            "AND away_score IS NOT NULL",
            (line, before_date),
        ).fetchone()
        label = "all NBA games before this one"
    elif part == "team":
        row = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN points > ? THEN 1 ELSE 0 END) "
            "FROM nba_team_games WHERE game_date < ? AND team_id = ? "
            "AND points IS NOT NULL",
            (line, before_date, team_id),
        ).fetchone()
        label = "this team's games before this one"
    elif part in REBOUND_MARKETS:
        if part == "rebounds_total":
            row = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN total_reb > ? THEN 1 ELSE 0 END) "
                "FROM ("
                "  SELECT game_id, SUM(reb) AS total_reb FROM nba_team_games "
                "  WHERE game_date < ? AND reb IS NOT NULL GROUP BY game_id "
                "  HAVING COUNT(*) = 2)",
                (line, before_date),
            ).fetchone()
            label = "all NBA games before this one"
        else:
            row = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN reb > ? THEN 1 ELSE 0 END) "
                "FROM nba_team_games WHERE game_date < ? AND team_id = ? "
                "AND reb IS NOT NULL",
                (line, before_date, team_id),
            ).fetchone()
            label = "this team's games before this one"
    else:
        if part == "half":
            expr = "home_q1 + home_q2 + away_q1 + away_q2"
        else:
            expr = "home_q1 + away_q1"
        row = conn.execute(
            f"SELECT COUNT(*), SUM(CASE WHEN {expr} > ? THEN 1 ELSE 0 END) "
            "FROM nba_games WHERE game_date < ? AND has_quarters = 1",
            (line, before_date),
        ).fetchone()
        label = ("games with a first-half line score before this one"
                 if part == "half" else
                 "games with a first-quarter line score before this one")

    total, over = (row[0] or 0, row[1] or 0) if row else (0, 0)
    return {
        "line": line,
        "over": int(over),
        "total": int(total),
        "rate": (over / total) if total else None,
        "label": label,
        "available": total > 0,
    }


def share_of_game(conn: sqlite3.Connection, before_date: str) -> dict:
    """Average share of regulation points scored in the first half / first quarter.

    The denominator is the four-quarter total, not the final score: an overtime
    period adds points that belong to no quarter, and dividing by the final
    score would make OT games look like they score less in the first half.
    """
    row = conn.execute(
        "SELECT AVG((home_q1 + home_q2 + away_q1 + away_q2) * 1.0 "
        "           / (home_q1 + home_q2 + home_q3 + home_q4 "
        "              + away_q1 + away_q2 + away_q3 + away_q4)), "
        "       AVG((home_q1 + away_q1) * 1.0 "
        "           / (home_q1 + home_q2 + home_q3 + home_q4 "
        "              + away_q1 + away_q2 + away_q3 + away_q4)), "
        "       COUNT(*) "
        "FROM nba_games WHERE game_date < ? AND has_quarters = 1 "
        "AND (home_q1 + home_q2 + home_q3 + home_q4 "
        "     + away_q1 + away_q2 + away_q3 + away_q4) > 0",
        (before_date,),
    ).fetchone()
    if not row or not row[2]:
        return {"half": None, "quarter": None, "games": 0}
    return {"half": float(row[0]), "quarter": float(row[1]), "games": int(row[2])}


# ---------------------------------------------------------------------------
# Team display statistics
# ---------------------------------------------------------------------------

def _box(conn, team_id: int, before_date: str, season: str, where: str,
         params=()) -> dict:
    row = conn.execute(
        "SELECT COUNT(*), AVG(points), AVG(reb), AVG(ast), AVG(tov), AVG(stl), "
        "       AVG(blk), AVG(pf), AVG(fgm), AVG(fga), AVG(fg3m), AVG(fg3a), "
        "       AVG(ftm), AVG(fta) "
        "FROM nba_team_games WHERE team_id = ? AND game_date < ? "
        f"AND points IS NOT NULL {where}",
        (team_id, before_date, *params),
    ).fetchone()
    if not row or not row[0]:
        return {"games": 0}
    n = row[0]
    fga, fg3a, fta = row[9] or 0, row[11] or 0, row[13] or 0
    fgm, fg3m = row[8], row[10]
    two_pm = (fgm - fg3m) if fgm is not None and fg3m is not None else None
    return {
        "games": int(n),
        "points": row[1],
        "rebounds": row[2],
        "assists": row[3],
        "turnovers": row[4],
        "steals": row[5],
        "blocks": row[6],
        "fouls": row[7],
        "fgm": fgm,
        "fg3m": fg3m,
        "two_pm": two_pm,
        "three_pm": fg3m,
        "fg_pct": (row[8] / fga) if fga else None,
        "fg3_pct": (row[10] / fg3a) if fg3a else None,
        "ft_pct": (row[12] / fta) if fta else None,
    }


def team_display(conn: sqlite3.Connection, team_id: int, before_date: str,
                 season: str) -> dict:
    """Season-to-date counting stats, with last-10 and venue splits as context."""
    team = conn.execute("SELECT name, city, tricode FROM nba_teams "
                        "WHERE team_id = ?", (team_id,)).fetchone()
    season_box = _box(conn, team_id, before_date, season, "AND season = ?",
                      (season,))
    prior_box = _box(conn, team_id, before_date, season, "AND season < ?",
                     (season,))
    # Last 10 counting stats, regardless of season (opening night has no
    # current-season games, and last season's finish is the honest context).
    last10 = conn.execute(
        "SELECT COUNT(*), AVG(points), AVG(reb), AVG(ast), AVG(tov), "
        "       AVG(stl), AVG(blk), AVG(pf), AVG(fgm), AVG(fg3m) FROM ("
        "  SELECT points, reb, ast, tov, stl, blk, pf, fgm, fg3m, game_date "
        "  FROM nba_team_games WHERE team_id = ? AND game_date < ? "
        "  AND points IS NOT NULL ORDER BY game_date DESC LIMIT 10)",
        (team_id, before_date),
    ).fetchone()
    last10_fgm, last10_fg3m = last10[8], last10[9]
    last10_two_pm = (
        last10_fgm - last10_fg3m
        if last10_fgm is not None and last10_fg3m is not None else None)
    home_box = _box(conn, team_id, before_date, season, "AND season = ? AND is_home = 1",
                    (season,))
    away_box = _box(conn, team_id, before_date, season, "AND season = ? AND is_home = 0",
                    (season,))
    return {
        "team_id": team_id,
        "name": team[0] if team else str(team_id),
        "city": team[1] if team else None,
        "tricode": team[2] if team else None,
        "season": season_box,
        "prior": prior_box,
        "last10": {
            "games": int(last10[0] or 0),
            "points": last10[1],
            "rebounds": last10[2],
            "assists": last10[3],
            "turnovers": last10[4],
            "steals": last10[5],
            "blocks": last10[6],
            "fouls": last10[7],
            "fgm": last10_fgm,
            "fg3m": last10_fg3m,
            "two_pm": last10_two_pm,
            "three_pm": last10_fg3m,
        },
        "home": home_box,
        "away": away_box,
    }


# ---------------------------------------------------------------------------
# The full evidence bundle
# ---------------------------------------------------------------------------

def analyze(conn: sqlite3.Connection, home_team_id: int, away_team_id: int,
            game_date: str, season: str | None = None,
            lines: dict | None = None) -> dict:
    """Every number the dashboard needs for one game, with its sample."""
    lines = lines or {}
    projection = project(conn, home_team_id, away_team_id, game_date, season)
    season = projection["season"]
    home_id, away_id = home_team_id, away_team_id

    home_box = team_display(conn, home_id, game_date, season)
    away_box = team_display(conn, away_id, game_date, season)

    def pick(key: str, fair: float):
        """Use the admin's market line if given, else the league-reference line."""
        given = lines.get(key)
        if given is None:
            return snap_to_half_line(fair), "league_average", fair
        return float(given), "market", float(given)

    total_line, total_source, total_fair = pick(
        "game_total", projection["fair_total"])
    home_line, home_source, home_fair = pick(
        "home_team_total", projection["fair_home"])
    away_line, away_source, away_fair = pick(
        "away_team_total", projection["fair_away"])

    markets = [
        _market("game_total", "Game total", projection["total"], total_line,
                total_source, total_fair,
                baseline(conn, game_date, total_line, part="game"),
                projection),
        _market("away_team_total", f"{_tricode(away_box)} team total",
                projection["away_points"], away_line, away_source, away_fair,
                baseline(conn, game_date, away_line, team_id=away_id,
                         part="team"),
                projection, scope="away"),
        _market("home_team_total", f"{_tricode(home_box)} team total",
                projection["home_points"], home_line, home_source, home_fair,
                baseline(conn, game_date, home_line, team_id=home_id,
                         part="team"),
                projection, scope="home"),
    ]

    # Every counting stat is projected as one block; combined and both team
    # scopes come from the same block, so they cannot disagree about the game.
    for stat, (total_key, home_key, away_key) in STAT_MARKETS.items():
        block = projection["team_stats"][stat]
        label = STAT_LABELS[stat]

        total_line, total_source, total_fair = pick(
            total_key, block["fair_total"])
        home_line, home_source, home_fair = pick(
            home_key, block["fair_home"])
        away_line, away_source, away_fair = pick(
            away_key, block["fair_away"])
        if home_source != "market":
            own = team_stat_baseline(home_box, stat)
            if own is not None:
                home_line = snap_to_half_line(own)
                home_source = "team_baseline"
        if away_source != "market":
            own = team_stat_baseline(away_box, stat)
            if own is not None:
                away_line = snap_to_half_line(own)
                away_source = "team_baseline"

        markets.extend([
            _market(total_key, f"Combined {label.lower()}", block["total"],
                    total_line, total_source, total_fair,
                    baseline(conn, game_date, total_line, stat=stat,
                             scope="combined"),
                    projection),
            _market(home_key, f"{_tricode(home_box)} {label.lower()}",
                    block["home"], home_line, home_source, home_fair,
                    baseline(conn, game_date, home_line, team_id=home_id,
                             stat=stat, scope="home"),
                    projection, scope="home"),
            _market(away_key, f"{_tricode(away_box)} {label.lower()}",
                    block["away"], away_line, away_source, away_fair,
                    baseline(conn, game_date, away_line, team_id=away_id,
                             stat=stat, scope="away"),
                    projection, scope="away"),
        ])

    markets.extend(_period_markets(conn, game_date, projection,
                                   projection["fair_total"]))

    season_games = int(min(projection["home"]["n_season"],
                           projection["away"]["n_season"]))
    prior_games = int(min(projection["home"]["n_prior"],
                          projection["away"]["n_prior"]))
    low = season_games < 10 and prior_games < 10

    note = ""
    if projection["league"]["basis"] == "league constants":
        note = ("No completed games are stored yet, so the league reference is "
                "a constant. Import history first.")
    elif season_games == 0:
        note = (f"No {season} games have been played yet, so the ratings come "
                f"from prior seasons ({prior_games} games).")
    elif season_games < 10:
        note = (f"Only {season_games} game(s) this season, so the ratings lean "
                f"on prior seasons ({prior_games} games).")

    rebounds = projection["rebounds"]
    team_stats = {}
    for stat, block in projection["team_stats"].items():
        team_stats[stat] = {
            "home": block["home"],
            "away": block["away"],
            "total": block["total"],
            "fair_home": block["fair_home"],
            "fair_away": block["fair_away"],
            "fair_total": block["fair_total"],
            "league_rate": projection["league"]["stats"][stat],
            "home_edge": projection["league"]["stat_edges"][stat],
            "form": {"home": block["home_form"], "away": block["away_form"]},
        }

    return {
        "game": {
            "game_date": game_date,
            "season": season,
            "home": home_box,
            "away": away_box,
        },
        "projection": {
            "home_points": projection["home_points"],
            "away_points": projection["away_points"],
            "total": projection["total"],
            "fair_total": projection["fair_total"],
            "fair_home": projection["fair_home"],
            "fair_away": projection["fair_away"],
            "possessions": projection["possessions"],
            "home_off_rating": projection["home_off_rating"],
            "away_off_rating": projection["away_off_rating"],
            "home_edge": projection["league"]["home_edge"],
            "home_rebounds": rebounds["home"],
            "away_rebounds": rebounds["away"],
            "total_rebounds": rebounds["total"],
            "fair_total_rebounds": rebounds["fair_total"],
            "basis": "regulation (48 minutes); overtime, when it happens, "
                     "is included in the actual result but not projected",
        },
        "rebounds": {
            "home": rebounds["home"],
            "away": rebounds["away"],
            "total": rebounds["total"],
            "fair_home": rebounds["fair_home"],
            "fair_away": rebounds["fair_away"],
            "fair_total": rebounds["fair_total"],
            "home_per100": rebounds["home_per100"],
            "away_per100": rebounds["away_per100"],
            "league_rate": projection["league"]["reb"],
            "home_edge": projection["league"]["reb_home_edge"],
            "form": {
                "home": rebounds["home_form"],
                "away": rebounds["away_form"],
            },
        },
        "team_stats": team_stats,
        "range": {
            "low": projection["range_low"],
            "high": projection["range_high"],
            "low_68": projection["range_low_68"],
            "high_68": projection["range_high_68"],
            "sigma": projection["sigma"],
            "label": projection["range_label"],
        },
        "ratings": {
            "home": _rating_block(projection["home"]),
            "away": _rating_block(projection["away"]),
        },
        "league": projection["league"],
        "markets": markets,
        "evidence": {
            "season_games": season_games,
            "prior_games": prior_games,
            "low_confidence": low,
            "note": note,
        },
        "head_to_head": _head_to_head(conn, home_id, away_id, game_date),
    }


def _tricode(team: dict) -> str:
    return team.get("tricode") or team.get("name") or "Team"


def _rating_block(form: dict) -> dict:
    return {
        "off_rating": form["off"],
        "def_rating": form["def"],
        "pace": form["pace"],
        "season_off": form["season_off"],
        "season_def": form["season_def"],
        "season_pace": form["season_pace"],
        "recent_off": form["recent_off"],
        "recent_def": form["recent_def"],
        "recent_pace": form["recent_pace"],
        "prior_off": form["base_off"],
        "prior_def": form["base_def"],
        "prior_pace": form["base_pace"],
        "season_games": form["n_season"],
        "prior_games": form["n_prior"],
        "recent_games": form["n_recent"],
    }


def _market(key: str, label: str, projection_value: float, line: float,
            line_source: str, fair_line: float, base: dict,
            projection: dict, scope: str = "combined") -> dict:
    difference = projection_value - line
    return {
        "key": key,
        "label": label,
        "scope": scope,
        "projection": projection_value,
        "line": line,
        "line_source": line_source,
        "fair_line": fair_line,
        "difference": difference,
        "supported": difference > 0,
        "baseline": base,
        "evidence": {
            "season_games": int(min(projection["home"]["n_season"],
                                    projection["away"]["n_season"])),
            "prior_games": int(min(projection["home"]["n_prior"],
                                   projection["away"]["n_prior"])),
        },
        "available": True,
    }


def _period_markets(conn: sqlite3.Connection, game_date: str,
                    projection: dict, fair_total: float) -> list[dict]:
    """First-half and first-quarter totals, only where line scores exist."""
    shares = share_of_game(conn, game_date)
    markets = []
    for key, part, share_key, label in (
        ("first_half_total", "half", "half", "First-half total"),
        ("first_quarter_total", "quarter", "quarter", "First-quarter total"),
    ):
        share = shares.get(share_key)
        if shares["games"] < MIN_QUARTER_GAMES or not share:
            markets.append({
                "key": key,
                "label": label,
                "available": False,
                "reason": (f"Only {shares['games']} game(s) have a line score "
                           f"before this one; {MIN_QUARTER_GAMES} are needed "
                           f"to measure a share and a baseline."),
            })
            continue
        value = projection["total"] * share
        fair = fair_total * share
        line = snap_to_half_line(fair)
        market = _market(key, label, value, line, "league_average", fair,
                         baseline(conn, game_date, line, part=part),
                         projection)
        market["share_of_game"] = share
        market["share_games"] = shares["games"]
        market["share_label"] = (
            f"{share * 100:.1f}% of a game's points, measured over "
            f"{shares['games']} games with a line score")
        markets.append(market)
    return markets


def _head_to_head(conn: sqlite3.Connection, home_id: int, away_id: int,
                  before_date: str, limit: int = 5) -> dict:
    """Prior meetings, strictly before the fixture. Context, not a weight."""
    rows = conn.execute(
        "SELECT game_id, game_date, home_team_id, away_team_id, home_score, "
        "       away_score FROM nba_games "
        "WHERE game_date < ? AND home_score IS NOT NULL "
        "  AND ((home_team_id = ? AND away_team_id = ?) "
        "    OR (home_team_id = ? AND away_team_id = ?)) "
        "ORDER BY game_date DESC LIMIT ?",
        (before_date, home_id, away_id, away_id, home_id, limit),
    ).fetchall()
    games = [{
        "game_id": r[0], "game_date": r[1],
        "home_team_id": r[2], "away_team_id": r[3],
        "home_score": r[4], "away_score": r[5],
    } for r in rows]
    return {"games": games, "count": len(games),
            "label": "prior meetings, context only"}


def get_team(conn: sqlite3.Connection, team_id: int) -> dict | None:
    row = conn.execute("SELECT team_id, name, city, tricode, slug "
                       "FROM nba_teams WHERE team_id = ?",
                       (team_id,)).fetchone()
    if not row:
        return None
    return {"team_id": row[0], "name": row[1], "city": row[2],
            "tricode": row[3], "slug": row[4]}


def list_teams(conn: sqlite3.Connection) -> list[dict]:
    return [{"team_id": r[0], "name": r[1], "city": r[2], "tricode": r[3]}
            for r in conn.execute(
                "SELECT team_id, name, city, tricode FROM nba_teams "
                "ORDER BY name")]
