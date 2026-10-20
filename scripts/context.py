"""Context intelligence for the board — measured, gated, and optional.

The core models stay exactly where they are. This module adds a *second*,
clearly separate assessment that starts from the core projection and may shift
it, but only when a factor's effect is:

  1. large enough to matter relative to that market's own error;
  2. statistically distinguishable from noise on the training slice;
  3. an improvement on a validation slice the selection never saw.

If any of those fails, the factor is recorded with its reason and the board
keeps the core number. A derby, a rivalry or a "must-win" is never a reason to
move a probability on its own — it is a *label* until the history says
otherwise.

Everything here is cutoff-aware: rest days, standings and travel are computed
from matches strictly before the fixture, so a backtest cannot see the future.

Public API
----------
ensure_tables(conn)                         create context_stats/context_markets
football_index(conn) / nba_index(conn)      precomputed lookups, no leakage
football_factors(index, row)                {factor: bool}
nba_factors(index, game)                    {factor: bool}
measure_from_samples(conn, sport, samples)  fit + gate, store, return report
coefficients(conn, sport)                   {market: [enabled factor rows]}
adjust(sport, market, projection, line, factors, coeffs)
                                            {delta, applied, considered, capped}
factor_catalogue(sport)                     labels and descriptions
"""

from __future__ import annotations

import sqlite3
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date, timedelta
from math import asin, cos, radians, sin, sqrt

# --------------------------------------------------------------------------
# Gates. These are deliberately conservative: a small, real effect that cannot
# be separated from noise is not used, and a factor that is significant in
# training but does not improve the validation slice is not used either.
# --------------------------------------------------------------------------
MIN_FACTOR_PRESENT = 60
MIN_FACTOR_ABSENT = 60
MIN_T = 2.0
MIN_EFFECT_SIGMA = 0.10
TRAIN_SHARE = 0.60
VALIDATE_SHARE = 0.20
# A validation gain smaller than this is noise with a decimal point.
MIN_BRIER_GAIN = 0.005          # relative to the core Brier score
# Even if several factors pass, the total shift is capped so the context track
# can never become a second, louder model.
MAX_ADJUSTMENT_SIGMA = 0.50


# --------------------------------------------------------------------------
# Curated data. Both lists are labels, not evidence: the measurement decides
# whether the label moves a number. Every name was checked against the live
# database (see verify_context.py).
# --------------------------------------------------------------------------
FOOTBALL_DERBIES: dict[str, tuple[tuple[str, str], ...]] = {
    "Premier League": (
        ("Arsenal", "Tottenham"),
        ("Arsenal", "Chelsea"),
        ("Liverpool", "Everton"),
        ("Liverpool", "Man United"),
        ("Man City", "Man United"),
        ("Newcastle", "Sunderland"),
        ("Aston Villa", "Wolves"),
        ("Brighton", "Crystal Palace"),
        ("Leeds", "Man United"),
        ("Nott'm Forest", "Leicester"),
    ),
    "La Liga": (
        ("Real Madrid", "Barcelona"),
        ("Real Madrid", "Ath Madrid"),
        ("Sevilla", "Betis"),
        ("Barcelona", "Espanol"),
        ("Valencia", "Levante"),
        ("Ath Bilbao", "Sociedad"),
        ("Celta", "La Coruna"),
        ("Sevilla", "Malaga"),
    ),
    "Bundesliga": (
        ("Dortmund", "Schalke 04"),
        ("Bayern Munich", "Dortmund"),
        ("Hamburg", "St Pauli"),
        ("FC Koln", "M'gladbach"),
        ("Werder Bremen", "Hamburg"),
        ("Schalke 04", "Bochum"),
    ),
    "Serie A": (
        ("Inter", "Milan"),
        ("Roma", "Lazio"),
        ("Juventus", "Inter"),
        ("Juventus", "Torino"),
        ("Napoli", "Roma"),
        ("Genoa", "Sampdoria"),
    ),
    "Ligue 1": (
        ("Paris SG", "Marseille"),
        ("Lyon", "St Etienne"),
        ("Lille", "Lens"),
        ("Nice", "Monaco"),
        ("Nantes", "Rennes"),
    ),
    "Eredivisie": (
        ("Ajax", "Feyenoord"),
        ("PSV Eindhoven", "Ajax"),
        ("Feyenoord", "Sparta Rotterdam"),
    ),
}

# NBA rivalries, by franchise id. Names would drift; ids do not.
NBA_RIVALRIES: tuple[tuple[int, int], ...] = (
    (1610612747, 1610612738),   # Lakers - Celtics
    (1610612747, 1610612746),   # Lakers - Clippers
    (1610612752, 1610612751),   # Knicks - Nets
    (1610612752, 1610612755),   # Knicks - 76ers
    (1610612744, 1610612739),   # Warriors - Cavaliers
    (1610612744, 1610612747),   # Warriors - Lakers
    (1610612741, 1610612765),   # Bulls - Pistons
    (1610612748, 1610612752),   # Heat - Knicks
    (1610612759, 1610612742),   # Spurs - Mavericks
    (1610612759, 1610612745),   # Spurs - Rockets
    (1610612742, 1610612745),   # Mavericks - Rockets
    (1610612738, 1610612755),   # Celtics - 76ers
    (1610612749, 1610612741),   # Bucks - Bulls
)

# Arena coordinates, used only to measure travel. Approximate city centres are
# enough: the factor asks "did this team change time zone and fly far?", not
# for a runway measurement.
NBA_ARENAS: dict[int, tuple[float, float]] = {
    1610612737: (33.757, -84.396),   # Atlanta
    1610612738: (42.366, -71.062),   # Boston
    1610612739: (41.497, -81.688),   # Cleveland
    1610612740: (29.949, -90.082),   # New Orleans
    1610612741: (41.881, -87.674),   # Chicago
    1610612742: (32.790, -96.810),   # Dallas
    1610612743: (39.749, -105.008),  # Denver
    1610612744: (37.768, -122.388),  # Golden State
    1610612745: (29.751, -95.362),   # Houston
    1610612746: (34.043, -118.267),  # LA Clippers
    1610612747: (34.043, -118.267),  # LA Lakers
    1610612748: (25.781, -80.187),   # Miami
    1610612749: (43.045, -87.917),   # Milwaukee
    1610612750: (44.980, -93.276),   # Minnesota
    1610612751: (40.683, -73.975),   # Brooklyn
    1610612752: (40.751, -73.994),   # New York
    1610612753: (28.539, -81.384),   # Orlando
    1610612754: (39.764, -86.155),   # Indiana
    1610612755: (39.901, -75.172),   # Philadelphia
    1610612756: (33.446, -112.071),  # Phoenix
    1610612757: (45.532, -122.667),  # Portland
    1610612758: (38.580, -121.500),  # Sacramento
    1610612759: (29.427, -98.438),   # San Antonio
    1610612760: (35.463, -97.515),   # Oklahoma City
    1610612761: (43.643, -79.379),   # Toronto
    1610612762: (40.768, -111.901),  # Utah
    1610612763: (35.138, -90.050),   # Memphis
    1610612764: (38.898, -77.021),   # Washington
    1610612765: (42.341, -83.055),   # Detroit
    1610612766: (35.225, -80.839),   # Charlotte
}


def factor_catalogue(sport: str) -> dict[str, str]:
    """Label for every factor the sport can measure."""
    if sport == "football":
        return {
            "rest_short": "Either team played 3 or fewer days ago",
            "congested": "Either team played 3+ matches in 14 days",
            "derby": "Derby / historic rivalry",
            "stakes_top": "Either team in the title or European race",
            "stakes_bottom": "Either team in the relegation fight",
            "long_break": "Both teams had 10+ days off",
        }
    return {
        "b2b": "Either team on a back-to-back",
        "rest_short": "Either team had one day or less of rest",
        "long_rest": "Both teams had 3+ days of rest",
        "travel_high": "Either team travelled 1,500+ miles since its last game",
        "rivalry": "Historic rivalry",
        "stakes_bubble": "Both teams within three games of .500 after 25 games",
        "tanking": "Either team under .350 after 55 games",
    }


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS context_stats (
    sport        TEXT NOT NULL,
    market       TEXT NOT NULL,
    factor       TEXT NOT NULL,
    label        TEXT,
    n_present    INTEGER NOT NULL DEFAULT 0,
    n_absent     INTEGER NOT NULL DEFAULT 0,
    effect       REAL,
    se           REAL,
    t            REAL,
    sigma_ratio  REAL,
    selected     INTEGER NOT NULL DEFAULT 0,
    reason       TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (sport, market, factor)
);

CREATE TABLE IF NOT EXISTS context_markets (
    sport           TEXT NOT NULL,
    market          TEXT NOT NULL,
    core_mae        REAL,
    context_mae     REAL,
    core_brier      REAL,
    context_brier   REAL,
    validate_n      INTEGER,
    test_n          INTEGER,
    test_core_brier REAL,
    test_context_brier REAL,
    selected        TEXT,
    enabled         INTEGER NOT NULL DEFAULT 0,
    reason          TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (sport, market)
);
"""

BOARD_COLUMNS = (
    ("core_probability", "REAL"),
    ("core_side", "TEXT"),
    ("context_probability", "REAL"),
    ("adjustment", "REAL"),
    ("context_json", "TEXT"),
    ("model", "TEXT"),
)


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _ensure_board_columns(conn)
    conn.commit()


def _ensure_board_columns(conn: sqlite3.Connection) -> None:
    """Add the context columns to board_picks on an existing database."""
    have = {row[1] for row in conn.execute("PRAGMA table_info(board_picks)")}
    for name, kind in BOARD_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE board_picks ADD COLUMN {name} {kind}")


# --------------------------------------------------------------------------
# Football: rest, congestion, standings, derbies
# --------------------------------------------------------------------------
def _rank_table(table: dict[str, list]) -> dict[str, dict]:
    ordered = sorted(table.items(),
                     key=lambda kv: (-kv[1][0], -(kv[1][1] - kv[1][2]),
                                     -kv[1][1], kv[0]))
    ranked = {}
    for pos, (team, (pts, gf, ga, played)) in enumerate(ordered, start=1):
        ranked[team] = {"pos": pos, "pts": pts, "gd": gf - ga, "gf": gf,
                        "played": played, "n": len(ordered)}
    return ranked


class FootballIndex:
    """One pass over the finished matches; cutoff-aware lookups afterwards."""

    def __init__(self, conn: sqlite3.Connection):
        conn.row_factory = sqlite3.Row
        self.team_dates: dict[str, list[str]] = defaultdict(list)
        self.snapshots: dict[str, dict[tuple[str, str], dict[str, dict]]] = {}
        self.snapshot_dates: list[str] = []
        self.derbies = {
            league: {frozenset(pair) for pair in pairs}
            for league, pairs in FOOTBALL_DERBIES.items()
        }

        rows = conn.execute(
            "SELECT id, date, league, season, home_team, away_team, "
            "       home_goals, away_goals FROM matches "
            "WHERE home_goals IS NOT NULL AND away_goals IS NOT NULL "
            "ORDER BY date, id").fetchall()

        tables: dict[tuple[str, str], dict[str, list]] = defaultdict(dict)
        current = None
        for row in rows:
            day = row["date"]
            if day != current:
                if current is not None:
                    self.snapshots[current] = {
                        key: _rank_table(t) for key, t in tables.items() if t
                    }
                    self.snapshot_dates.append(current)
                current = day
            self.team_dates[row["home_team"]].append(day)
            self.team_dates[row["away_team"]].append(day)

            key = (row["league"], row["season"])
            table = tables[key]
            home = table.setdefault(row["home_team"], [0, 0, 0, 0])
            away = table.setdefault(row["away_team"], [0, 0, 0, 0])
            home[1] += row["home_goals"] or 0
            home[2] += row["away_goals"] or 0
            away[1] += row["away_goals"] or 0
            away[2] += row["home_goals"] or 0
            home[3] += 1
            away[3] += 1
            if row["home_goals"] > row["away_goals"]:
                home[0] += 3
            elif row["home_goals"] < row["away_goals"]:
                away[0] += 3
            else:
                home[0] += 1
                away[0] += 1
        if current is not None:
            self.snapshots[current] = {
                key: _rank_table(t) for key, t in tables.items() if t
            }
            self.snapshot_dates.append(current)

        for team in self.team_dates:
            self.team_dates[team].sort()

    def _previous(self, team: str, day: str) -> str | None:
        dates = self.team_dates.get(team)
        if not dates:
            return None
        i = bisect_left(dates, day)
        return dates[i - 1] if i else None

    def _played_in(self, team: str, day: str, days: int) -> int:
        dates = self.team_dates.get(team)
        if not dates:
            return 0
        i = bisect_left(dates, day)
        cutoff = (date.fromisoformat(day) - timedelta(days=days)).isoformat()
        j = bisect_left(dates, cutoff)
        return i - j

    def _table(self, league: str, season: str, day: str):
        i = bisect_left(self.snapshot_dates, day)
        if i == 0:
            return None
        return self.snapshots[self.snapshot_dates[i - 1]].get((league, season))

    def factors(self, home: str, away: str, day: str,
                league: str, season: str) -> dict[str, bool]:
        f = {"rest_short": False, "congested": False, "derby": False,
             "stakes_top": False, "stakes_bottom": False, "long_break": False}

        rest_home = self._previous(home, day)
        rest_away = self._previous(away, day)
        if rest_home or rest_away:
            gaps = []
            for prev in (rest_home, rest_away):
                if prev:
                    gaps.append((date.fromisoformat(day)
                                 - date.fromisoformat(prev)).days)
            if gaps and min(gaps) <= 3:
                f["rest_short"] = True
            if gaps and len(gaps) == 2 and min(gaps) >= 10:
                f["long_break"] = True

        if (self._played_in(home, day, 14) >= 3
                or self._played_in(away, day, 14) >= 3):
            f["congested"] = True

        f["derby"] = frozenset((home, away)) in self.derbies.get(league, set())

        table = self._table(league, season, day)
        if table:
            for team in (home, away):
                row = table.get(team)
                if not row or row["played"] < 8:
                    continue
                n = row["n"]
                leader = min(table.values(), key=lambda r: r["pos"])
                if row["pos"] <= 6 and leader["pts"] - row["pts"] <= 4:
                    f["stakes_top"] = True
                safe_pos = n - 2
                safe = next((r for r in table.values()
                             if r["pos"] == safe_pos), None)
                if (row["pos"] >= n - 4 and safe is not None
                        and row["pts"] - safe["pts"] <= 4):
                    f["stakes_bottom"] = True
        return f


# --------------------------------------------------------------------------
# NBA: back-to-backs, rest, travel, standings, rivalries
# --------------------------------------------------------------------------
def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * 6371 * asin(sqrt(h))


KM_PER_MILE = 1.609344


class NbaIndex:
    def __init__(self, conn: sqlite3.Connection):
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT game_id, game_date, home_team_id, away_team_id, "
            "       home_score, away_score FROM nba_games "
            "WHERE home_score IS NOT NULL AND away_score IS NOT NULL "
            "ORDER BY game_date, game_id").fetchall()

        self.games: dict[str, dict] = {}
        self.by_team: dict[int, list[dict]] = defaultdict(list)
        self.results: dict[int, list[tuple[str, int]]] = defaultdict(list)
        self.rivalries = {frozenset(pair) for pair in NBA_RIVALRIES}

        for row in rows:
            home_won = 1 if row["home_score"] > row["away_score"] else 0
            for team, opp, won, own_venue in (
                    (row["home_team_id"], row["away_team_id"], home_won,
                     row["home_team_id"]),
                    (row["away_team_id"], row["home_team_id"], 1 - home_won,
                     row["home_team_id"])):
                self.by_team[team].append({
                    "game_id": row["game_id"], "date": row["game_date"],
                    "opp": opp, "home": own_venue == team,
                    "venue": own_venue, "won": won,
                })
                self.results[team].append((row["game_date"], won))

    def _previous(self, team: int, day: str) -> dict | None:
        games = self.by_team.get(team)
        if not games:
            return None
        i = bisect_left([g["date"] for g in games], day)
        return games[i - 1] if i else None

    def _record(self, team: int, day: str) -> tuple[int, int]:
        rows = self.results.get(team, [])
        i = bisect_left([r[0] for r in rows], day)
        wins = sum(w for _, w in rows[:i])
        return wins, i - wins

    def _travel(self, team: int, day: str, venue: int) -> float:
        prev = self._previous(team, day)
        if prev is None:
            return 0.0
        start = NBA_ARENAS.get(prev["venue"])
        end = NBA_ARENAS.get(venue)
        if not start or not end:
            return 0.0
        return _haversine(start, end) / KM_PER_MILE

    def factors(self, home: int, away: int, day: str) -> dict[str, bool]:
        f = {"b2b": False, "rest_short": False, "long_rest": False,
             "travel_high": False, "rivalry": False, "stakes_bubble": False,
             "tanking": False}

        gaps = []
        for team in (home, away):
            prev = self._previous(team, day)
            if prev:
                gaps.append((date.fromisoformat(day)
                             - date.fromisoformat(prev["date"])).days)
        if gaps:
            if min(gaps) <= 1:
                f["rest_short"] = True
            if min(gaps) == 1:
                f["b2b"] = True
            if len(gaps) == 2 and min(gaps) >= 3:
                f["long_rest"] = True

        if (self._travel(away, day, home) >= 1500
                or self._travel(home, day, home) >= 1500):
            f["travel_high"] = True

        f["rivalry"] = frozenset((home, away)) in self.rivalries

        hw, hl = self._record(home, day)
        aw, al = self._record(away, day)
        if (hw + hl >= 25 and aw + al >= 25
                and abs(hw - hl) <= 3 and abs(aw - al) <= 3):
            f["stakes_bubble"] = True

        for w, l in ((hw, hl), (aw, al)):
            if w + l >= 55 and w / max(1, w + l) < 0.35:
                f["tanking"] = True
        return f


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------
def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _variance(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return sum((v - m) ** 2 for v in values) / (len(values) - 1)


def _p_over(sorted_residuals: list[float], edge: float) -> float:
    """Empirical P(actual > line) given edge = projection - line."""
    if not sorted_residuals:
        return 0.5
    i = bisect_right(sorted_residuals, -edge)
    return (len(sorted_residuals) - i) / len(sorted_residuals)


def _evaluate(samples: list[dict], residuals: list[float],
              effects: dict[str, float]) -> dict:
    """MAE, Brier and pick accuracy for one track on a slice."""
    mae = 0.0
    brier = 0.0
    hits = 0
    picks = 0
    for s in samples:
        delta = sum(e for key, e in effects.items() if s["factors"].get(key))
        projection = s["projection"] + delta
        mae += abs(s["actual"] - projection)
        p_over = _p_over(residuals, projection - s["line"])
        outcome = 1.0 if s["actual"] > s["line"] else 0.0
        brier += (p_over - outcome) ** 2
        if max(p_over, 1 - p_over) >= 0.55:
            picks += 1
            if (p_over >= 0.5) == bool(outcome):
                hits += 1
    n = max(1, len(samples))
    return {"n": len(samples), "mae": mae / n, "brier": brier / n,
            "accuracy": (hits / picks) if picks else None, "picks": picks}


def measure_from_samples(conn: sqlite3.Connection, sport: str,
                         samples: dict[str, list[dict]], log=print) -> dict:
    """Fit and gate every factor for every market, then store the result.

    `samples[market]` is a chronological list of dicts with
    `projection`, `line`, `actual`, `factors`.
    """
    ensure_tables(conn)
    catalogue = factor_catalogue(sport)
    markets: dict[str, dict] = {}
    stored = 0

    for market, rows in samples.items():
        rows = sorted(rows, key=lambda r: r.get("date") or "")
        if len(rows) < 200:
            continue

        split_a = int(len(rows) * TRAIN_SHARE)
        split_b = int(len(rows) * (TRAIN_SHARE + VALIDATE_SHARE))
        train, validate, test = rows[:split_a], rows[split_a:split_b], rows[split_b:]

        train_res = sorted(s["actual"] - s["projection"] for s in train)
        sd = sqrt(_variance([s["actual"] - s["projection"] for s in train]))
        if sd <= 0:
            continue

        effects: dict[str, dict] = {}
        for factor, label in catalogue.items():
            present = [s["actual"] - s["projection"]
                       for s in train if s["factors"].get(factor)]
            absent = [s["actual"] - s["projection"]
                      for s in train if not s["factors"].get(factor)]
            entry = {"factor": factor, "label": label,
                     "n_present": len(present), "n_absent": len(absent),
                     "effect": None, "se": None, "t": None,
                     "sigma_ratio": None, "selected": 0, "reason": ""}
            if (len(present) < MIN_FACTOR_PRESENT
                    or len(absent) < MIN_FACTOR_ABSENT):
                entry["reason"] = (f"not enough matches "
                                   f"({len(present)} with, {len(absent)} without)")
            else:
                effect = _mean(present) - _mean(absent)
                se = sqrt(_variance(present) / len(present)
                          + _variance(absent) / len(absent))
                t = effect / se if se > 0 else 0.0
                entry.update(effect=effect, se=se, t=t,
                             sigma_ratio=abs(effect) / sd)
                if abs(t) < MIN_T:
                    entry["reason"] = f"inside noise (t={t:.2f})"
                elif abs(effect) < MIN_EFFECT_SIGMA * sd:
                    entry["reason"] = f"too small to matter ({abs(effect)/sd:.2f}σ)"
            effects[factor] = entry

        # Forward selection on the validation slice, largest effect first.
        candidates = [e for e in effects.values()
                      if e["effect"] is not None and not e["reason"]]
        candidates.sort(key=lambda e: abs(e["effect"]), reverse=True)
        chosen: list[str] = []
        base = _evaluate(validate, train_res, {})
        best = base["brier"]
        for cand in candidates:
            trial = _evaluate(validate, train_res,
                              {**{k: effects[k]["effect"] for k in chosen},
                               cand["factor"]: cand["effect"]})
            if trial["brier"] < best - 1e-12:
                chosen.append(cand["factor"])
                best = trial["brier"]

        gain = (base["brier"] - best) / base["brier"] if base["brier"] else 0.0
        enabled = bool(chosen) and gain >= MIN_BRIER_GAIN
        if chosen and not enabled:
            for key in chosen:
                effects[key]["reason"] = (
                    f"no out-of-sample gain (+{gain*100:.2f}% Brier)")
            chosen = []

        chosen_effects = {k: effects[k]["effect"] for k in chosen}
        for key in chosen:
            effects[key]["selected"] = 1
            effects[key]["reason"] = "applied"

        core_v = _evaluate(validate, train_res, {})
        ctx_v = _evaluate(validate, train_res, chosen_effects)
        core_t = _evaluate(test, train_res, {})
        ctx_t = _evaluate(test, train_res, chosen_effects)

        reason = ("applied" if enabled
                  else "no factor survived the gates"
                  if not candidates else "did not improve out of sample")

        for entry in effects.values():
            conn.execute(
                "INSERT INTO context_stats (sport, market, factor, label, "
                "  n_present, n_absent, effect, se, t, sigma_ratio, selected, "
                "  reason, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(sport, market, factor) DO UPDATE SET "
                "  label=excluded.label, n_present=excluded.n_present, "
                "  n_absent=excluded.n_absent, effect=excluded.effect, "
                "  se=excluded.se, t=excluded.t, "
                "  sigma_ratio=excluded.sigma_ratio, selected=excluded.selected, "
                "  reason=excluded.reason, updated_at=datetime('now')",
                (sport, market, entry["factor"], entry["label"],
                 entry["n_present"], entry["n_absent"], entry["effect"],
                 entry["se"], entry["t"], entry["sigma_ratio"],
                 entry["selected"], entry["reason"]))

        conn.execute(
            "INSERT INTO context_markets (sport, market, core_mae, context_mae, "
            "  core_brier, context_brier, validate_n, test_n, test_core_brier, "
            "  test_context_brier, selected, enabled, reason, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(sport, market) DO UPDATE SET "
            "  core_mae=excluded.core_mae, context_mae=excluded.context_mae, "
            "  core_brier=excluded.core_brier, "
            "  context_brier=excluded.context_brier, "
            "  validate_n=excluded.validate_n, test_n=excluded.test_n, "
            "  test_core_brier=excluded.test_core_brier, "
            "  test_context_brier=excluded.test_context_brier, "
            "  selected=excluded.selected, enabled=excluded.enabled, "
            "  reason=excluded.reason, updated_at=datetime('now')",
            (sport, market, core_t["mae"], ctx_t["mae"], core_v["brier"],
             ctx_v["brier"], core_v["n"], core_t["n"], core_t["brier"],
             ctx_t["brier"], ",".join(chosen), int(enabled), reason))

        stored += 1
        markets[market] = {
            "enabled": enabled, "selected": chosen, "reason": reason,
            "gain": gain, "validate": {"core": core_v, "context": ctx_v},
            "test": {"core": core_t, "context": ctx_t},
            "factors": effects,
        }
        log(f"  CONTEXT  {sport}/{market:<18} "
            f"{'on ' if enabled else 'off'} {','.join(chosen) or '-'} "
            f"brier {core_t['brier']:.4f} -> {ctx_t['brier']:.4f} "
            f"(validate gain {gain*100:+.2f}%)")

    conn.commit()
    return {"markets": stored, "detail": markets}


# --------------------------------------------------------------------------
# Applying it
# --------------------------------------------------------------------------
def coefficients(conn: sqlite3.Connection, sport: str) -> dict[str, dict]:
    """Enabled factors per market, with their measured effect."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM context_stats WHERE sport = ? AND selected = 1 "
        "ORDER BY market, ABS(effect) DESC", (sport,)).fetchall()
    out: dict[str, dict] = defaultdict(dict)
    for row in rows:
        out[row["market"]][row["factor"]] = dict(row)
    return dict(out)


def adjust(sport: str, market: str, projection: float, line: float,
           factors: dict[str, bool], coeffs: dict[str, dict],
           sigma: float | None = None) -> dict:
    """Shift the core projection by the measured, enabled effects.

    Returns the delta, the factors that caused it, and every factor that was
    considered but not applied, with the reason. The caller recomputes the
    probability from the unchanged residual distribution.
    """
    enabled = coeffs.get(market) or {}
    applied, considered = [], []
    delta = 0.0
    for factor, active in factors.items():
        row = enabled.get(factor)
        if row:
            applied.append({
                "key": factor, "label": row["label"],
                "effect": round(row["effect"], 3),
                "t": round(row["t"], 2), "n": row["n_present"],
            })
            if active:
                delta += row["effect"]
        else:
            considered.append(factor)

    capped = False
    if sigma and sigma > 0 and abs(delta) > MAX_ADJUSTMENT_SIGMA * sigma:
        delta = MAX_ADJUSTMENT_SIGMA * sigma * (1 if delta > 0 else -1)
        capped = True

    return {"delta": round(delta, 3), "applied": applied,
            "considered": considered, "capped": capped,
            "context_projection": round(projection + delta, 2)}


def market_report(conn: sqlite3.Connection, sport: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(
        "SELECT * FROM context_markets WHERE sport = ? ORDER BY market",
        (sport,))]
