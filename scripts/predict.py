"""The prediction board: which stat is most likely to land on the model's side?

What this module is NOT
-----------------------
It is not a second prediction engine. `stats.py` (football) and `nba_stats.py`
(NBA) remain the only places a projection is made. This module measures how
wrong those projections have been, turns that error distribution into a
probability, and ranks the day's fixtures by it.

Why a probability must be measured
----------------------------------
"Projection 11.8, line 9.5" is not a probability. A projection is 2.3 above the
line; whether that is a strong signal depends on how wide that market's errors
are. Corners swing by three or four a game; goals do not. Dividing the edge by
the measured error spread puts every market on one comparable scale, which is
what makes a board possible at all - a 0.4-corner edge and a 0.4-goal edge are
not the same thing.

Three numbers per pick, and what each one means
-----------------------------------------------
    projection   what the model says the stat will be
    line         the line being tested: the league reference unless a market
                 line was entered
    probability  the share of past matches, at the same edge, where the actual
                 landed on the model's side

The probability is empirical: the residual (actual - projection) distribution is
measured match by match, then a normal-free CDF is read at the edge. It is
shrunk toward 50% on thin samples (n / (n + 50)), so 120 matches can never look
as certain as 900. Nothing here is trained on the match being shown: the
measurement is walk-forward, using the engine exactly as the dashboard does.

What is deliberately absent
---------------------------
No stake, no stake sizing, no "confidence score", no word "bet". The board ranks
what is likely to happen relative to a line. It never claims a line is beatable.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import nba_stats
import stats
from nba_schema import ensure_schema as ensure_nba_schema

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "sports_analyzer.db"

# The markets with a CSV column behind them. The manual-only markets have no
# walk-forward history, so they cannot be calibrated and must not appear on a
# board that promises a measured probability.
FOOTBALL_MARKETS = (
    "goals", "shots", "shots_on_target", "fouls", "corners", "yellow_cards",
)

NBA_MARKETS = (
    "game_total", "home_team_total", "away_team_total",
    "first_half_total", "first_quarter_total",
) + tuple(key for keys in nba_stats.STAT_MARKETS.values() for key in keys)

SCOPES = ("combined", "home", "away")

# Market taxonomy: a market (what is being counted) and a scope (whose count).
# They are separate fields on purpose. "Assists" + "combined" and "Assists" +
# "home" are different questions, but they belong to the same filter family;
# storing only a flat key would force the Board to know every combination.
NBA_TAXONOMY = {
    "game_total": ("points", "combined", "Points"),
    "home_team_total": ("points", "home", "Points"),
    "away_team_total": ("points", "away", "Points"),
    "first_half_total": ("points_1h", "combined", "1st-half points"),
    "first_quarter_total": ("points_1q", "combined", "1st-quarter points"),
}
for _stat, _keys in nba_stats.STAT_MARKETS.items():
    for _key, _scope in zip(_keys, SCOPES):
        NBA_TAXONOMY[_key] = (_stat, _scope, nba_stats.STAT_LABELS[_stat])

GROUP_LABELS = {
    "points": "Points",
    "points_1h": "1st-half points",
    "points_1q": "1st-quarter points",
    **nba_stats.STAT_LABELS,
}

NBA_MARKET_LABELS = {
    "game_total": "Game total",
    "home_team_total": "Home team total",
    "away_team_total": "Away team total",
    "first_half_total": "First-half total",
    "first_quarter_total": "First-quarter total",
}
for _stat, _keys in nba_stats.STAT_MARKETS.items():
    _label = nba_stats.STAT_LABELS[_stat].lower()
    NBA_MARKET_LABELS[_keys[0]] = f"Combined {_label}"
    NBA_MARKET_LABELS[_keys[1]] = f"Home {_label}"
    NBA_MARKET_LABELS[_keys[2]] = f"Away {_label}"


def classify(sport: str, market: str) -> tuple[str, str, str]:
    """(market group, scope, label) for any stored market key.

    Football markets are all combined team totals, so the group is the market
    itself. NBA keys carry the distinction explicitly.
    """
    if sport == "nba":
        return NBA_TAXONOMY.get(
            market, (market, "combined", market.replace("_", " ").title()))
    return (market, "combined",
            GROUP_LABELS.get(market, market.replace("_", " ").capitalize()))

# How many completed matches each market's error distribution is measured on.
# 400 brings the standard error of a quantile to about two points and takes
# roughly a minute and a half in total; the full history adds precision, not
# shape, and would take half an hour.
FOOTBALL_SAMPLE = 400
# How many completed games the NBA walk covers. 2,500 rather than a season,
# because only 2024-25 carries line scores: the half and quarter markets have
# no residuals at all inside the most recent 1,200 games.
NBA_SAMPLE = 2500

# Below this there is no board entry at all. A 60% claim from 40 matches is
# noise with a percentage sign on it.
MIN_SAMPLE = 120
MIN_CONFIDENCE = 0.55
# The default board is deliberately focused: the top two picks per fixture. The
# snapshot still stores every qualifying market, so a market/scope filter can
# show the full set without recalculating anything.
DEFAULT_TOP_PER_GAME = 2
MAX_PICKS_PER_GAME = DEFAULT_TOP_PER_GAME  # legacy name

# Display bands, not new claims. The measured hit rate for each band is in
# prediction_stats.calibration; these cut-offs only group picks that already
# cleared MIN_CONFIDENCE.
CONFIDENCE_BANDS = (
    ("high", "High", 0.65),
    ("medium", "Medium", 0.58),
    ("low", "Low", 0.55),
)

# Thin-sample shrinkage: n / (n + K). With n=400 the model keeps 89% of its
# distance from 50%; with n=120 it keeps 71%.
SHRINK_K = 50.0

QUANTILE_POINTS = (0.01, 0.025, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35,
                   0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80,
                   0.85, 0.90, 0.95, 0.975, 0.99)

SCHEMA = """
CREATE TABLE IF NOT EXISTS prediction_stats (
    sport        TEXT NOT NULL,
    market       TEXT NOT NULL,
    label        TEXT,
    n            INTEGER NOT NULL DEFAULT 0,
    sigma        REAL,
    mae          REAL,
    naive_mae    REAL,
    bias         REAL,
    calibration  TEXT,
    quantiles    TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (sport, market)
);

CREATE TABLE IF NOT EXISTS board_picks (
    id           INTEGER PRIMARY KEY,
    date         TEXT NOT NULL,
    sport        TEXT NOT NULL,
    game_key     TEXT NOT NULL,
    game_label   TEXT NOT NULL,
    league       TEXT,
    market       TEXT NOT NULL,
    market_label TEXT,
    market_group TEXT,
    scope        TEXT NOT NULL DEFAULT 'combined',
    model        TEXT NOT NULL DEFAULT 'core',
    side         TEXT NOT NULL,
    line         REAL NOT NULL,
    projection   REAL NOT NULL,
    probability  REAL NOT NULL,
    sample       INTEGER NOT NULL,
    line_source  TEXT,
    start_time   TEXT,
    pick_rank    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    result       TEXT,
    actual       REAL,
    graded_at    TEXT,
    UNIQUE (date, sport, game_key, market)
);

CREATE INDEX IF NOT EXISTS idx_board_date
    ON board_picks(date, sport, probability DESC);
CREATE INDEX IF NOT EXISTS idx_board_result
    ON board_picks(result, date);

-- Every available market for a published fixture, not only the rows that
-- cleared the confidence bar. The Board groups these by fixture + market so a
-- supporter can read the home, away and combined lines together. `board_picks`
-- remains the qualifying subset used for the ranked record.
CREATE TABLE IF NOT EXISTS board_markets (
    id           INTEGER PRIMARY KEY,
    date         TEXT NOT NULL,
    sport        TEXT NOT NULL,
    game_key     TEXT NOT NULL,
    game_label   TEXT NOT NULL,
    home_team    TEXT,
    away_team    TEXT,
    league       TEXT,
    market       TEXT NOT NULL,
    market_label TEXT,
    market_group TEXT NOT NULL,
    scope        TEXT NOT NULL,
    model        TEXT NOT NULL DEFAULT 'core',
    side         TEXT NOT NULL,
    line         REAL NOT NULL,
    projection   REAL NOT NULL,
    probability  REAL NOT NULL,
    sample       INTEGER NOT NULL,
    line_source  TEXT,
    start_time   TEXT,
    qualified    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    result       TEXT,
    actual       REAL,
    graded_at    TEXT,
    UNIQUE (date, sport, game_key, market)
);

CREATE INDEX IF NOT EXISTS idx_markets_date
    ON board_markets(date, sport, market_group);
CREATE INDEX IF NOT EXISTS idx_markets_game
    ON board_markets(date, sport, game_key, market_group);
CREATE INDEX IF NOT EXISTS idx_markets_result
    ON board_markets(result, date);
"""


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate_board(conn)
    conn.commit()


def _migrate_board(conn: sqlite3.Connection) -> None:
    """Add the filter columns to a board built before they existed.

    The board is a snapshot, so the migration must never rewrite a pick's
    numbers. It only labels each row with the market family and scope it always
    belonged to.
    """
    columns = {r[1] for r in conn.execute("PRAGMA table_info(board_picks)")}
    additions = (
        ("market_group", "market_group TEXT"),
        ("scope", "scope TEXT NOT NULL DEFAULT 'combined'"),
        ("model", "model TEXT NOT NULL DEFAULT 'core'"),
        ("pick_rank", "pick_rank INTEGER NOT NULL DEFAULT 1"),
    )
    for name, ddl in additions:
        if name not in columns:
            conn.execute(f"ALTER TABLE board_picks ADD COLUMN {ddl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_board_filters "
        "ON board_picks(date, sport, market_group, scope)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_board_rank "
        "ON board_picks(date, sport, pick_rank)")
    rows = conn.execute(
        "SELECT id, sport, market FROM board_picks "
        "WHERE market_group IS NULL").fetchall()
    for row in rows:
        group, scope, _ = classify(row[1], row[2])
        conn.execute(
            "UPDATE board_picks SET market_group = ?, scope = ? WHERE id = ?",
            (group, scope, row[0]))
    if "pick_rank" not in columns:
        # Every qualifying pick is stored now, but the default view still shows
        # the top two per fixture. Backfill a rank for boards built before the
        # column existed, without touching any projection or probability.
        conn.execute(
            "UPDATE board_picks SET pick_rank = ("
            "  SELECT COUNT(*) + 1 FROM board_picks p2 "
            "  WHERE p2.date = board_picks.date "
            "    AND p2.sport = board_picks.sport "
            "    AND p2.game_key = board_picks.game_key "
            "    AND (p2.probability > board_picks.probability "
            "         OR (p2.probability = board_picks.probability "
            "             AND p2.id < board_picks.id)))")
    conn.commit()


# --------------------------------------------------------------------------
# Measuring error
# --------------------------------------------------------------------------
def _quantile(sorted_values: list[float], p: float) -> float:
    """Linear-interpolated quantile of an already sorted list."""
    if not sorted_values:
        return 0.0
    if p <= 0:
        return sorted_values[0]
    if p >= 1:
        return sorted_values[-1]
    pos = p * (len(sorted_values) - 1)
    low = int(pos)
    high = min(low + 1, len(sorted_values) - 1)
    frac = pos - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


def _cdf(grid: list[list[float]], value: float) -> float:
    """Read an empirical CDF built as [[residual, cumulative], ...]."""
    if not grid:
        return 0.5
    if value <= grid[0][0]:
        return 0.0
    if value >= grid[-1][0]:
        return 1.0
    for i in range(1, len(grid)):
        x0, p0 = grid[i - 1]
        x1, p1 = grid[i]
        if value <= x1:
            if x1 == x0:
                return p1
            frac = (value - x0) / (x1 - x0)
            return p0 + frac * (p1 - p0)
    return 1.0


def _build_grid(values: list[float]) -> list[list[float]]:
    ordered = sorted(values)
    return [[round(_quantile(ordered, p), 4), p] for p in QUANTILE_POINTS]


def _calibration(residuals: list[float]) -> list[dict]:
    """Holdout reliability: does a 60% claim actually land 60% of the time?

    The first 70% of matches, in date order, fit the distribution; the last 30%
    are scored against it. The claim is a quantile threshold, so this measures
    the distribution itself rather than one particular line.
    """
    if len(residuals) < 120:
        return []
    cut = int(len(residuals) * 0.7)
    fit = sorted(residuals[:cut])
    holdout = residuals[cut:]
    if len(holdout) < 40:
        return []

    out = []
    for claim in (0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90):
        threshold = _quantile(fit, 1.0 - claim)
        observed = sum(1 for r in holdout if r > threshold) / len(holdout)
        out.append({"claim": claim, "observed": round(observed, 4),
                    "n": len(holdout)})
    return out


def _summarise(residuals: list[float],
               naive_residuals: list[float] | None = None) -> dict:
    if len(residuals) < 2:
        return {}
    return {
        "n": len(residuals),
        "sigma": round(statistics.pstdev(residuals), 3),
        "mae": round(statistics.fmean(abs(r) for r in residuals), 3),
        "naive_mae": (round(statistics.fmean(abs(r) for r in naive_residuals), 3)
                      if naive_residuals else None),
        "bias": round(statistics.fmean(residuals), 3),
        "quantiles": json.dumps(_build_grid(residuals)),
        "calibration": json.dumps(_calibration(residuals)),
    }


def measure_football(conn: sqlite3.Connection, per_market: int = FOOTBALL_SAMPLE,
                     log=print) -> dict:
    """Walk the engine forward over recent matches and record its residuals."""
    ensure_tables(conn)
    conn.row_factory = sqlite3.Row
    done: dict[str, dict] = {}

    for market in FOOTBALL_MARKETS:
        home_col, away_col = stats.STAT_COLUMNS[market]
        rows = conn.execute(
            f"SELECT * FROM matches WHERE {home_col} IS NOT NULL "
            f"  AND {away_col} IS NOT NULL AND home_goals IS NOT NULL "
            f"ORDER BY date DESC LIMIT ?", (per_market,)).fetchall()
        rows = list(reversed(rows))
        residuals: list[float] = []
        naive_residuals: list[float] = []

        for row in rows:
            actual = (row[home_col] or 0) + (row[away_col] or 0)
            try:
                result = stats.analyze_market(
                    conn, row["home_team"], row["away_team"],
                    f"avg_{market}", before_date=row["date"],
                    exclude_match_id=row["id"], league=row["league"],
                    season=row["season"])
            except Exception:  # noqa: BLE001 - one bad row must not stop the run
                continue
            value = result.get("weighted_avg")
            reference = result.get("league_reference")
            if value is None:
                continue
            residuals.append(actual - value)
            if reference is not None:
                naive_residuals.append(actual - reference)

        summary = _summarise(residuals, naive_residuals)
        if summary:
            summary.update(sport="football", market=market,
                           label=market.replace("_", " "))
            done[market] = summary
            log(f"  PREDICT  football/{market:<16} n={summary['n']:<4} "
                f"sigma={summary['sigma']:<6} MAE={summary['mae']} "
                f"(naive {summary['naive_mae']})")
        else:
            log(f"  PREDICT  football/{market:<16} no usable history")

    _store_stats(conn, "football", done)
    return done


def _nba_actual(game: sqlite3.Row, market: str,
                team_stats: dict | None = None) -> float | None:
    group, scope, _ = classify("nba", market)
    if group in nba_stats.STAT_COLUMNS:
        if not team_stats:
            return None
        home = (team_stats.get(game["home_team_id"]) or {}).get(group)
        away = (team_stats.get(game["away_team_id"]) or {}).get(group)
        if home is None or away is None:
            return None
        if scope == "combined":
            return float(home + away)
        return float(home if scope == "home" else away)
    if market == "game_total":
        return (game["home_score"] or 0) + (game["away_score"] or 0)
    if market == "home_team_total":
        return game["home_score"]
    if market == "away_team_total":
        return game["away_score"]
    if market == "first_quarter_total":
        if game["home_q1"] is None or game["away_q1"] is None:
            return None
        return game["home_q1"] + game["away_q1"]
    if market == "first_half_total":
        if None in (game["home_q1"], game["home_q2"],
                    game["away_q1"], game["away_q2"]):
            return None
        return (game["home_q1"] + game["home_q2"]
                + game["away_q1"] + game["away_q2"])
    return None


def measure_nba(conn: sqlite3.Connection, limit: int = NBA_SAMPLE,
                log=print) -> dict:
    """Walk the NBA engine forward and record one residual per market per game."""
    ensure_tables(conn)
    conn.row_factory = sqlite3.Row
    ensure_nba_schema(conn)
    rows = conn.execute(
        "SELECT * FROM nba_games WHERE home_score IS NOT NULL "
        "  AND away_score IS NOT NULL AND season_type = 'Regular Season' "
        "ORDER BY game_date DESC, game_id DESC LIMIT ?", (limit,)).fetchall()
    rows = list(reversed(rows))

    residuals: dict[str, list[float]] = {k: [] for k in NBA_MARKETS}
    naive_residuals: dict[str, list[float]] = {k: [] for k in NBA_MARKETS}
    stat_rows = conn.execute(
        "SELECT game_id, team_id, reb, fgm, fg3m, ast, tov, stl, blk, pf "
        "FROM nba_team_games").fetchall()
    team_stats: dict[str, dict[int, dict]] = {}
    for row in stat_rows:
        two_pm = (row["fgm"] - row["fg3m"]
                  if row["fgm"] is not None and row["fg3m"] is not None
                  else None)
        team_stats.setdefault(row["game_id"], {})[row["team_id"]] = {
            "rebounds": row["reb"],
            "two_pm": two_pm,
            "three_pm": row["fg3m"],
            "assists": row["ast"],
            "turnovers": row["tov"],
            "steals": row["stl"],
            "blocks": row["blk"],
            "fouls": row["pf"],
        }
    for game in rows:
        try:
            bundle = nba_stats.analyze(
                conn, game["home_team_id"], game["away_team_id"],
                game["game_date"], game["season"])
        except Exception:  # noqa: BLE001 - one bad game must not stop the run
            continue
        for market in bundle.get("markets", []):
            key = market.get("key")
            if key not in residuals or not market.get("available"):
                continue
            actual = _nba_actual(
                game, key, team_stats.get(game["game_id"]))
            if actual is None:
                continue
            residuals[key].append(actual - market["projection"])
            # The naive line must be the line the Board actually tests. For a
            # team total or team counting stat that is the team's own baseline,
            # not the league reference used for the combined market. Measuring
            # against a different line would make `beats_naive` easier than the
            # claim it appears to support.
            fair = market.get("fair_line")
            group, scope, _ = classify("nba", key)
            if key in ("home_team_total", "away_team_total"):
                side = "home" if key.startswith("home") else "away"
                own = _team_baseline(bundle, side)
                if own is not None:
                    fair = nba_stats.snap_to_half_line(own)
            elif (group in nba_stats.STAT_COLUMNS
                  and scope in ("home", "away")):
                own = nba_stats.team_stat_baseline(
                    bundle["game"][scope], group)
                if own is not None:
                    fair = nba_stats.snap_to_half_line(own)
            if fair is not None:
                naive_residuals[key].append(actual - float(fair))

    done: dict[str, dict] = {}
    for market, values in residuals.items():
        summary = _summarise(values, naive_residuals[market])
        if summary:
            summary.update(sport="nba", market=market,
                           label=NBA_MARKET_LABELS.get(
                               market, market.replace("_", " ")))
            done[market] = summary
            log(f"  PREDICT  nba/{market:<22} n={summary['n']:<4} "
                f"sigma={summary['sigma']} MAE={summary['mae']} "
                f"(naive {summary['naive_mae']})")
    _store_stats(conn, "nba", done)
    return done


def _store_stats(conn: sqlite3.Connection, sport: str,
                 done: dict[str, dict]) -> None:
    for market, s in done.items():
        conn.execute(
            "INSERT INTO prediction_stats (sport, market, label, n, sigma, mae, "
            "  naive_mae, bias, calibration, quantiles, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(sport, market) DO UPDATE SET "
            "  label=excluded.label, n=excluded.n, sigma=excluded.sigma, "
            "  mae=excluded.mae, naive_mae=excluded.naive_mae, "
            "  bias=excluded.bias, calibration=excluded.calibration, "
            "  quantiles=excluded.quantiles, updated_at=datetime('now')",
            (sport, market, s.get("label"), s["n"], s["sigma"], s["mae"],
             s.get("naive_mae"), s["bias"], s["calibration"], s["quantiles"]))
    conn.commit()


def measure_all(db_path: Path | str = DB_PATH, log=print,
                sport: str = "all") -> dict:
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_tables(conn)
        football = {}
        nba = {}
        if sport in ("all", "football"):
            log("Measuring football residuals (walk-forward) ...")
            football = measure_football(conn, log=log)
        if sport in ("all", "nba"):
            log("Measuring NBA residuals (walk-forward) ...")
            nba = measure_nba(conn, log=log)
        return {"football": len(football), "nba": len(nba)}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# From error to probability
# --------------------------------------------------------------------------
def probability_over(stats_row: sqlite3.Row | dict, edge: float) -> float:
    """P(actual > line) when the model is `edge` above it.

    residual = actual - projection, so actual > line iff residual > -edge.
    The empirical CDF is read at that point, then pulled toward 50% for small
    samples. The result is clamped to [0.05, 0.95]: a measured distribution
    cannot honestly claim more than that about a single match.
    """
    grid = stats_row["quantiles"]
    grid = json.loads(grid) if isinstance(grid, str) else grid
    n = int(stats_row["n"] or 0)
    if not grid or n < 2:
        return 0.5
    raw = 1.0 - _cdf(grid, -float(edge))
    shrink = n / (n + SHRINK_K)
    p = 0.5 + (raw - 0.5) * shrink
    return min(0.95, max(0.05, p))


def _load_stats(conn: sqlite3.Connection, sport: str) -> dict[str, sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return {r["market"]: r for r in conn.execute(
        "SELECT * FROM prediction_stats WHERE sport = ? AND n >= ?",
        (sport, MIN_SAMPLE))}


def _side_probability(stats_row, projection: float,
                      line: float) -> tuple[str, float, float]:
    """The side with the measured edge, its probability, and the raw edge.

    The side is chosen by the higher probability, not by a separate rule, so
    the card and the probability cannot disagree about which way the model
    leans. A market below MIN_CONFIDENCE still gets a side and a probability;
    it simply is not a qualifying pick.
    """
    edge = projection - line
    p_over = probability_over(stats_row, edge)
    if p_over >= 0.5:
        return "over", p_over, edge
    return "under", 1.0 - p_over, edge


def _snapshot(market: str, label: str, line: float, projection: float,
              stats_row, line_source: str) -> dict:
    side, probability, edge = _side_probability(
        stats_row, projection, line)
    return {
        "market": market, "market_label": label, "side": side,
        "line": round(line, 2), "projection": round(projection, 2),
        "probability": round(probability, 4), "edge": round(edge, 2),
        "sample": int(stats_row["n"]), "line_source": line_source,
        "qualified": probability >= MIN_CONFIDENCE,
    }


def _pick(market: str, label: str, line: float, projection: float,
          stats_row, line_source: str):
    row = _snapshot(market, label, line, projection, stats_row, line_source)
    return row if row["qualified"] else None


# --------------------------------------------------------------------------
# Building the board
# --------------------------------------------------------------------------
def _build_football(conn: sqlite3.Connection, start: str, end: str,
                    now: str, log=print) -> int:
    conn.row_factory = sqlite3.Row
    table = _load_stats(conn, "football")
    if not table:
        log("  BOARD    football: no measured markets yet")
        return 0

    matches = conn.execute(
        "SELECT * FROM published_matches WHERE date BETWEEN ? AND ? "
        "ORDER BY date, kickoff", (start, end)).fetchall()
    stored = 0
    skipped = 0
    for match in matches:
        start_time = match["date"] + "T" + (match["kickoff"] or "00:00") + ":00"
        if start_time <= now:
            # Already under way: a pre-match probability shown now would be a
            # claim made after the fact.
            skipped += 1
            continue
        rows = []
        for market, s in table.items():
            try:
                result = stats.analyze_market(
                    conn, match["home_team"], match["away_team"],
                    f"avg_{market}", before_date=match["date"],
                    league=match["league"], season=match["season"])
            except Exception:  # noqa: BLE001
                continue
            projection = result.get("weighted_avg")
            line = result.get("line")
            if projection is None or line is None or not result.get("sources_used"):
                continue
            row = _snapshot(market, s["label"] or market.replace("_", " "),
                            float(line), float(projection), s, "league")
            rows.append(row)
        rows.sort(key=lambda r: r["probability"], reverse=True)
        label = f"{match['home_team']} v {match['away_team']}"
        for row in rows:
            _insert_market(
                conn, match["date"], "football", str(match["id"]), label,
                match["home_team"], match["away_team"], match["league"],
                row, start_time)
        picks = [row for row in rows if row["qualified"]]
        for rank, pick in enumerate(picks, start=1):
            stored += _insert_pick(
                conn, match["date"], "football", str(match["id"]),
                label, match["league"], pick, start_time, rank)
    log(f"  BOARD    football: {stored} picks for "
        f"{len(matches) - skipped} fixtures"
        + (f" ({skipped} already under way)" if skipped else ""))
    return stored


def _build_nba(conn: sqlite3.Connection, start: str, end: str,
               now: str, log=print) -> int:
    conn.row_factory = sqlite3.Row
    table = _load_stats(conn, "nba")
    if not table:
        log("  BOARD    nba: no measured markets yet")
        return 0
    ensure_nba_schema(conn)

    games = conn.execute(
        "SELECT * FROM nba_published WHERE game_date BETWEEN ? AND ? "
        "ORDER BY game_date", (start, end)).fetchall()
    stored = 0
    skipped = 0
    for game in games:
        start_time = game["game_datetime_utc"]
        if start_time and start_time[:19] <= now:
            # Tip-off already happened; a pre-match pick now would be hindsight.
            skipped += 1
            continue
        lines = {}
        if game["market_total"] is not None:
            lines["game_total"] = game["market_total"]
        if game["market_home_total"] is not None:
            lines["home_team_total"] = game["market_home_total"]
        if game["market_away_total"] is not None:
            lines["away_team_total"] = game["market_away_total"]
        try:
            bundle = nba_stats.analyze(
                conn, game["home_team_id"], game["away_team_id"],
                game["game_date"], game["season"], lines=lines)
        except Exception:  # noqa: BLE001
            continue

        rows = []
        for market in bundle.get("markets", []):
            key = market.get("key")
            s = table.get(key)
            if not s or not market.get("available"):
                continue
            if market.get("line") is None or market.get("projection") is None:
                continue
            line = float(market["line"])
            source = market.get("line_source") or "league_average"
            group, scope, _ = classify("nba", key)
            if key in ("home_team_total", "away_team_total") and source != "market":
                # A team total compared to the league's average team total is a
                # statement about how good the team is, not about tonight. On
                # the board it is compared to the team's own baseline instead,
                # so the pick says "more or less than this team usually scores".
                baseline = _team_baseline(bundle, "home" if key.startswith(
                    "home") else "away")
                if baseline is not None:
                    line = nba_stats.snap_to_half_line(baseline)
                    source = "team_baseline"
            if (group in nba_stats.STAT_COLUMNS
                    and scope in ("home", "away")
                    and source != "market"):
                baseline = _team_stat_baseline(bundle, scope, group)
                if baseline is not None:
                    line = nba_stats.snap_to_half_line(baseline)
                    source = "team_baseline"
            rows.append(_snapshot(
                key, market.get("label") or key, line,
                float(market["projection"]), s, source))
        home_team = bundle["game"]["home"]
        away_team = bundle["game"]["away"]

        def full_name(team):
            return " ".join(part for part in (team.get("city"), team.get("name"))
                            if part) or str(team.get("team_id"))

        home = full_name(home_team)
        away = full_name(away_team)
        label = f"{home} v {away}"
        for row in rows:
            _insert_market(
                conn, game["game_date"], "nba", str(game["game_id"]), label,
                home, away, "NBA", row, game["game_datetime_utc"])
        picks = [row for row in rows if row["qualified"]]
        picks.sort(key=lambda p: p["probability"], reverse=True)
        for rank, pick in enumerate(picks, start=1):
            stored += _insert_pick(
                conn, game["game_date"], "nba", str(game["game_id"]),
                label, "NBA", pick, game["game_datetime_utc"], rank)
    log(f"  BOARD    nba: {stored} picks for {len(games) - skipped} games"
        + (f" ({skipped} already under way)" if skipped else ""))
    return stored


def _team_baseline(bundle: dict, side: str) -> float | None:
    """What this team usually scores, for a team-total line.

    Season points once there are enough games to mean something, otherwise the
    prior season's per-game average (246 team-games for a full year). This is
    the same information the engine starts from, not a second model.
    """
    form = (bundle.get("game") or {}).get(side) or {}
    season = form.get("season") or {}
    prior = form.get("prior") or {}
    if season.get("games", 0) >= 5 and season.get("points"):
        return float(season["points"])
    if prior.get("points"):
        return float(prior["points"])
    return None


def _team_stat_baseline(bundle: dict, side: str, stat: str) -> float | None:
    """What this team usually gets for one counting stat.

    Same rule as the team scoring baseline: this season once five games exist,
    otherwise the prior season. It is the team's own average, not a matchup
    projection, so the pick answers "more or less than this team usually gets".
    """
    form = (bundle.get("game") or {}).get(side) or {}
    return nba_stats.team_stat_baseline(form, stat)


def _insert_market(conn, day, sport, game_key, game_label, home_team,
                   away_team, league, row, start_time) -> int:
    """Store one complete matchup line, qualifying or not.

    The card view reads this table so a supporter can see the home, away and
    combined answer together. `qualified` records whether the row also made
    the ranked board, without changing the numbers shown here.
    """
    group, scope, _ = classify(sport, row["market"])
    cur = conn.execute(
        "INSERT INTO board_markets (date, sport, game_key, game_label, "
        "  home_team, away_team, league, market, market_label, market_group, "
        "  scope, model, side, line, projection, probability, sample, "
        "  line_source, start_time, qualified) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(date, sport, game_key, market) DO NOTHING",
        (day, sport, game_key, game_label, home_team, away_team, league,
         row["market"], row["market_label"], group, scope, "core", row["side"],
         row["line"], row["projection"], row["probability"], row["sample"],
         row["line_source"], start_time, 1 if row["qualified"] else 0))
    return cur.rowcount or 0


def _insert_pick(conn, day, sport, game_key, game_label, league, pick,
                 start_time, pick_rank: int = 1) -> int:
    group, scope, _ = classify(sport, pick["market"])
    cur = conn.execute(
        "INSERT INTO board_picks (date, sport, game_key, game_label, league, "
        "  market, market_label, market_group, scope, model, side, line, "
        "  projection, probability, sample, line_source, start_time, pick_rank) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(date, sport, game_key, market) DO NOTHING",
        (day, sport, game_key, game_label, league, pick["market"],
         pick["market_label"], group, scope, "core", pick["side"],
         pick["line"], pick["projection"], pick["probability"], pick["sample"],
         pick["line_source"], start_time, int(pick_rank)))
    return cur.rowcount or 0


def build_board(db_path: Path | str = DB_PATH, days: int = 14,
                replace: bool = False, log=print) -> dict:
    """Fill the board for the published window. Existing picks are frozen.

    A pick is only replaced when it is ungraded AND its kickoff has not passed,
    so a rebuild can never rewrite history it already committed to.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_tables(conn)
        today = date.today().isoformat()
        end = (date.today() + timedelta(days=days)).isoformat()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        if replace:
            conn.execute(
                "DELETE FROM board_picks WHERE result IS NULL AND date >= ? "
                "AND (start_time IS NULL OR start_time > ?)", (today, now))
            conn.execute(
                "DELETE FROM board_markets WHERE result IS NULL AND date >= ? "
                "AND (start_time IS NULL OR start_time > ?)", (today, now))
            conn.commit()
        football = _build_football(conn, today, end, now, log=log)
        nba = _build_nba(conn, today, end, now, log=log)
        # Existing picks are frozen, but their display rank is not a
        # prediction. When a later fill adds a new qualifying market, an old
        # rank can collide (two rank 1s, or three rows at rank <= 2). Renumber
        # the whole window by probability so the legacy top-two view means what
        # it says. Lines, probabilities and timestamps are untouched.
        conn.execute(
            "WITH ranked AS ("
            "  SELECT id, ROW_NUMBER() OVER ("
            "    PARTITION BY date, sport, game_key "
            "    ORDER BY probability DESC, id"
            "  ) AS rn FROM board_picks"
            ") "
            "UPDATE board_picks SET pick_rank = "
            "  (SELECT rn FROM ranked WHERE ranked.id = board_picks.id)"
        )
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM board_picks WHERE date >= ?",
                             (today,)).fetchone()[0]
        return {"football": football, "nba": nba, "stored": total,
                "from": today, "to": end}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Reading and grading
# --------------------------------------------------------------------------
def _stats_map(conn: sqlite3.Connection) -> dict[tuple[str, str], sqlite3.Row]:
    return {(r["sport"], r["market"]): r
            for r in conn.execute("SELECT * FROM prediction_stats")}


def _like_term(term: str) -> str:
    """Escape a user's team text for a LIKE, so `%` is a character, not a wildcard."""
    escaped = (term.replace("\\", "\\\\").replace("%", "\\%")
               .replace("_", "\\_"))
    return f"%{escaped}%"


def _filter_clause(day: str, sport: str, market: str | None = None,
                   scope: str | None = None, team: str | None = None,
                   league: str | None = None, source: str = "all",
                   confidence: str = "any", status: str = "all",
                   top_only: bool = True,
                   now: str | None = None) -> tuple[str, list]:
    """The Board's one WHERE builder, shared by the page and the count."""
    clause = "date = ?"
    params: list = [day]
    if sport in ("football", "nba"):
        clause += " AND sport = ?"
        params.append(sport)
    if league:
        clause += " AND league = ?"
        params.append(league)
    if market:
        clause += " AND market_group = ?"
        params.append(market)
    if scope in SCOPES:
        clause += " AND scope = ?"
        params.append(scope)
    if top_only:
        clause += " AND pick_rank <= ?"
        params.append(DEFAULT_TOP_PER_GAME)
    if team:
        clause += " AND game_label LIKE ? ESCAPE '\\'"
        params.append(_like_term(team))
    if source == "core":
        clause += " AND COALESCE(model, 'core') = 'core'"
    elif source == "board":
        clause += " AND COALESCE(model, 'core') IN ('context', 'board')"
    elif source == "agree":
        clause += " AND COALESCE(model, 'core') = 'agree'"
    if confidence in ("high", "medium", "low"):
        minimum = next(
            m for key, _, m in CONFIDENCE_BANDS if key == confidence)
        clause += " AND probability >= ?"
        params.append(minimum)
        if confidence != "high":
            clause += " AND probability < ?"
            params.append(0.65 if confidence == "medium" else 0.58)
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    if status == "completed":
        clause += " AND result IS NOT NULL"
    elif status == "upcoming":
        clause += (" AND result IS NULL AND (start_time IS NULL "
                   "OR substr(start_time, 1, 19) >= ?)")
        params.append(now)
    elif status == "live":
        clause += (" AND result IS NULL AND start_time IS NOT NULL "
                   "AND substr(start_time, 1, 19) < ?")
        params.append(now)
    return clause, params


def _market_options(conn: sqlite3.Connection, sport: str, day: str) -> list[dict]:
    """Measured markets, with the scopes each one supports and today's count.

    Only markets with a measured error distribution appear. A market that has
    no sample is omitted rather than offered as an empty promise.
    """
    sports = ("football", "nba") if sport == "all" else (sport,)
    rows = conn.execute(
        "SELECT sport, market, label, n FROM prediction_stats WHERE n >= ?",
        (MIN_SAMPLE,)).fetchall()
    groups: dict[str, dict] = {}
    for r in rows:
        if r["sport"] not in sports:
            continue
        group, scope, label = classify(r["sport"], r["market"])
        item = groups.setdefault(group, {
            "market": group, "label": GROUP_LABELS.get(group, label),
            "scopes": set(), "sports": set(), "n": 0, "count": 0,
        })
        item["scopes"].add(scope)
        item["sports"].add(r["sport"])
        item["n"] = max(item["n"], int(r["n"] or 0))

    count_clause = "date = ?"
    count_params: list = [day]
    if sport in ("football", "nba"):
        count_clause += " AND sport = ?"
        count_params.append(sport)
    for r in conn.execute(
            f"SELECT market_group, COUNT(*) AS n FROM board_picks "
            f"WHERE {count_clause} AND market_group IS NOT NULL "
            f"GROUP BY market_group", count_params):
        if r["market_group"] in groups:
            groups[r["market_group"]]["count"] = int(r["n"])

    out = []
    for item in groups.values():
        item["scopes"] = [s for s in SCOPES if s in item["scopes"]]
        item["sports"] = [s for s in ("football", "nba") if s in item["sports"]]
        out.append(item)
    return sorted(out, key=lambda i: (i["label"], i["market"]))


def _accuracy(conn: sqlite3.Connection, sport: str, market: str | None,
              scope: str | None) -> list[dict]:
    """Measured error for the market being filtered, for the summary line.

    This is the walk-forward measurement, not a record of board picks. It is
    shown beside the filters so a market can never look stronger than its own
    measured error.
    """
    if not market:
        return []
    out = []
    for r in conn.execute("SELECT * FROM prediction_stats").fetchall():
        if sport in ("football", "nba") and r["sport"] != sport:
            continue
        group, row_scope, label = classify(r["sport"], r["market"])
        if group != market:
            continue
        if scope in SCOPES and row_scope != scope:
            continue
        calibration = []
        if r["calibration"]:
            try:
                calibration = json.loads(r["calibration"])
            except (ValueError, TypeError):
                calibration = []
        out.append({
            "sport": r["sport"],
            "market": r["market"],
            "market_group": group,
            "scope": row_scope,
            "label": label,
            "n": int(r["n"] or 0),
            "mae": r["mae"],
            "naive_mae": r["naive_mae"],
            "beats_naive": (
                None if r["naive_mae"] is None or r["mae"] is None
                else bool(r["mae"] < r["naive_mae"])),
            "bias": r["bias"],
            "calibration": calibration,
        })
    return sorted(out, key=lambda item: SCOPES.index(item["scope"]))


def board(conn: sqlite3.Connection, day: str, sport: str = "all",
          limit: int = 10, offset: int = 0, market: str | None = None,
          scope: str = "all", team: str | None = None,
          league: str | None = None, source: str = "all",
          confidence: str = "any", status: str = "all",
          sort: str = "probability") -> dict:
    """The ranked board for one day, read from the frozen snapshot.

    Filtering never calls the model. It reads `board_picks`, which was written
    before kick-off and never rewritten after the match started.
    """
    conn.row_factory = sqlite3.Row
    limit = max(1, min(50, int(limit)))
    offset = max(0, int(offset))
    filters = {"market": market, "scope": scope, "team": team,
               "league": league, "source": source, "confidence": confidence,
               "status": status, "sort": sort}
    base = {
        "date": day, "sport": sport, "picks": [], "candidates": 0,
        "total": 0, "unfiltered": 0, "market_total": 0,
        "league_total": 0,
        "offset": offset, "limit": limit,
        "has_more": False,
        "record": {"graded": 0, "won": 0, "lost": 0, "pushed": 0,
                   "void": 0, "hit_rate": None},
        "history": {"graded": 0, "won": 0, "lost": 0, "pushed": 0,
                    "void": 0, "hit_rate": None},
        "measured": 0, "fixtures": 0, "filters": filters,
        "markets": [], "teams": [], "scopes": list(SCOPES),
        "leagues": [], "accuracy": [],
        "confidence_bands": [
            {"key": key, "label": label, "min": minimum}
            for key, label, minimum in CONFIDENCE_BANDS],
        "source_available": source in ("all", "core"),
        "context_available": False,
        "source_note": None,
    }
    if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'board_picks'").fetchone() is None:
        return base

    clause, params = _filter_clause(
        day, sport, market=market, scope=scope, team=team, league=league,
        source=source, confidence=confidence, status=status,
        top_only=not (market or (scope and scope != "all")))
    total = conn.execute(
        f"SELECT COUNT(*) FROM board_picks WHERE {clause}", params).fetchone()[0]
    market_clause = "date = ?"
    market_params: list = [day]
    if sport in ("football", "nba"):
        market_clause += " AND sport = ?"
        market_params.append(sport)
    if market:
        market_clause += " AND market_group = ?"
        market_params.append(market)
    market_total = conn.execute(
        f"SELECT COUNT(*) FROM board_picks WHERE {market_clause}",
        market_params).fetchone()[0]
    league_clause = market_clause
    league_params = list(market_params)
    if league:
        league_clause += " AND league = ?"
        league_params.append(league)
    if not market:
        league_clause += " AND pick_rank <= ?"
        league_params.append(DEFAULT_TOP_PER_GAME)
    league_total = conn.execute(
        f"SELECT COUNT(*) FROM board_picks WHERE {league_clause}",
        league_params).fetchone()[0]
    # The denominator for "showing X of Y": market/scope/league decide the
    # universe, while team/confidence/status only narrow it. A market filter
    # shows every rank in that market, not just the default top two per game.
    base_clause, base_params = _filter_clause(
        day, sport, market=market, scope=scope, league=league,
        top_only=not (market or (scope and scope != "all")))
    order = {
        "probability": "probability DESC, sport, id",
        "time": "start_time IS NULL, start_time ASC, probability DESC, sport, id",
        "edge": "ABS(projection - line) DESC, probability DESC, sport, id",
    }.get(sort, "probability DESC, sport, id")
    rows = conn.execute(
        f"SELECT * FROM board_picks WHERE {clause} ORDER BY {order} "
        f"LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()

    table = _stats_map(conn)
    picks = []
    for r in rows:
        s = table.get((r["sport"], r["market"]))
        calibration = []
        if s and s["calibration"]:
            try:
                calibration = json.loads(s["calibration"])
            except (ValueError, TypeError):
                calibration = []
        bucket = None
        for c in calibration:
            if c["claim"] <= r["probability"] + 1e-9:
                bucket = c
        group, row_scope, _ = classify(r["sport"], r["market"])
        part = r["game_label"].split(" v ")
        home_name = part[0] if part else r["game_label"]
        away_name = part[-1] if len(part) > 1 else ""
        scope_label = ("both teams" if row_scope == "combined"
                       else home_name if row_scope == "home" else away_name)
        picks.append({
            "rank": offset + len(picks) + 1,
            "date": r["date"],
            "sport": r["sport"],
            "game_key": r["game_key"],
            "game_label": r["game_label"],
            "league": r["league"],
            "market": r["market"],
            "market_group": group,
            "market_label": r["market_label"],
            "scope": row_scope,
            "scope_label": scope_label,
            "model": (r["model"] or "core") if "model" in r.keys() else "core",
            "side": r["side"],
            "line": r["line"],
            "projection": r["projection"],
            "probability": r["probability"],
            "edge": round(r["projection"] - r["line"], 2),
            "sample": r["sample"],
            "line_source": r["line_source"],
            "start_time": r["start_time"],
            "result": r["result"],
            "actual": r["actual"] if "actual" in r.keys() else None,
            "measurement": {
                "n": s["n"] if s else None,
                "sigma": s["sigma"] if s else None,
                "mae": s["mae"] if s else None,
                "naive_mae": s["naive_mae"] if s else None,
                "beats_naive": (
                    None if not s or s["naive_mae"] is None or s["mae"] is None
                    else bool(s["mae"] < s["naive_mae"])),
            },
            "calibration": bucket,
        })

    unfiltered_clause = "date = ?"
    unfiltered_params: list = [day]
    if sport in ("football", "nba"):
        unfiltered_clause += " AND sport = ?"
        unfiltered_params.append(sport)

    labels = conn.execute(
        f"SELECT DISTINCT game_label FROM board_picks "
        f"WHERE {unfiltered_clause}", unfiltered_params).fetchall()
    teams = sorted({
        name.strip() for row in labels for name in row["game_label"].split(" v ")
        if name.strip()})
    leagues = [
        row["league"] for row in conn.execute(
            f"SELECT DISTINCT league FROM board_picks "
            f"WHERE {unfiltered_clause} AND league IS NOT NULL "
            f"ORDER BY league", unfiltered_params)
    ]

    context_rows = conn.execute(
        "SELECT COUNT(*) FROM board_picks "
        "WHERE COALESCE(model, 'core') != 'core'").fetchone()[0]
    source_note = None
    if source in ("board", "agree") and not context_rows:
        source_note = (
            "The context-enhanced model is not measured yet. Only core-model "
            "predictions exist, so this view is empty on purpose rather than "
            "filled with the core model under another name.")

    return {
        **base,
        "picks": picks,
        "candidates": total,
        "total": total,
        "market_total": market_total,
        "league_total": league_total,
        "base_total": conn.execute(
            f"SELECT COUNT(*) FROM board_picks WHERE {base_clause}",
            base_params).fetchone()[0],
        "unfiltered": conn.execute(
            f"SELECT COUNT(*) FROM board_picks WHERE {unfiltered_clause} "
            f"AND pick_rank <= ?",
            (*unfiltered_params, DEFAULT_TOP_PER_GAME)).fetchone()[0],
        "has_more": offset + len(picks) < total,
        "record": _record(conn, sport, day, market, scope, team, league),
        "history": _record(conn, sport, None, market, scope, team, league),
        "measured": sum(1 for key in table
                        if sport == "all" or key[0] == sport),
        "fixtures": _fixtures_on(conn, day, sport),
        "markets": _market_options(conn, sport, day),
        "teams": teams,
        "leagues": leagues,
        "accuracy": _accuracy(conn, sport, market, scope),
        "source_available": source in ("all", "core") or bool(context_rows),
        "context_available": bool(context_rows),
        "source_note": source_note,
    }


def _market_options_grouped(conn: sqlite3.Connection, sport: str,
                            day: str,
                            league: str | None = None) -> list[dict]:
    """Markets stored for this day, with a card count rather than a pick count.

    The Board now reads the complete matchup snapshot, so a market is useful
    as soon as any published fixture has a stored line for it. Counting games
    (not qualifying picks) keeps the dropdown honest when every row sits below
    the old confidence bar.
    """
    stats = _stats_map(conn)
    groups: dict[str, dict] = {}
    clause = "date = ?"
    params: list = [day]
    if sport in ("football", "nba"):
        clause += " AND sport = ?"
        params.append(sport)
    if league:
        clause += " AND league = ?"
        params.append(league)
    for r in conn.execute(
            f"SELECT sport, game_key, market, market_group, market_label "
            f"FROM board_markets WHERE {clause}", params):
        group = r["market_group"]
        item = groups.setdefault(group, {
            "market": group,
            "label": GROUP_LABELS.get(group) or r["market_label"] or group,
            "sports": set(), "games": set(), "scopes": set(), "n": 0,
        })
        item["sports"].add(r["sport"])
        item["games"].add((r["sport"], r["game_key"]))
        item["scopes"].add(classify(r["sport"], r["market"])[1])
        s = stats.get((r["sport"], r["market"]))
        if s:
            item["n"] = max(item["n"], int(s["n"] or 0))
    out = []
    for item in groups.values():
        item["sports"] = [s for s in ("football", "nba")
                          if s in item["sports"]]
        item["scopes"] = [s for s in SCOPES if s in item["scopes"]]
        item["count"] = len(item.pop("games"))
        out.append(item)
    return sorted(out, key=lambda item: (item["label"], item["market"]))


def _market_record(conn: sqlite3.Connection, sport: str, before: str | None,
                   market: str | None = None,
                   league: str | None = None) -> dict:
    """Settled rows in the complete matchup snapshot."""
    clause = "result IS NOT NULL"
    params: list = []
    if before:
        clause += " AND date < ?"
        params.append(before)
    if sport in ("football", "nba"):
        clause += " AND sport = ?"
        params.append(sport)
    if market:
        clause += " AND market_group = ?"
        params.append(market)
    if league:
        clause += " AND league = ?"
        params.append(league)
    rows = conn.execute(
        f"SELECT result, COUNT(*) AS n FROM board_markets WHERE {clause} "
        f"GROUP BY result", params).fetchall()
    counts = {r["result"]: r["n"] for r in rows}
    decided = counts.get("win", 0) + counts.get("loss", 0)
    return {
        "graded": decided + counts.get("push", 0),
        "won": counts.get("win", 0),
        "lost": counts.get("loss", 0),
        "pushed": counts.get("push", 0),
        "void": counts.get("void", 0),
        "hit_rate": round(counts.get("win", 0) / decided, 4) if decided else None,
    }


def _card_row(conn: sqlite3.Connection, r: sqlite3.Row,
              stats: dict, home: str, away: str) -> dict:
    group, scope, _ = classify(r["sport"], r["market"])
    s = stats.get((r["sport"], r["market"]))
    result = r["result"]
    return {
        "market": r["market"],
        "market_label": r["market_label"],
        "market_group": group,
        "scope": scope,
        "scope_label": ("Combined" if scope == "combined"
                        else home if scope == "home" else away),
        "team": home if scope == "home" else away if scope == "away" else None,
        "side": r["side"],
        "line": r["line"],
        "projection": r["projection"],
        "probability": r["probability"],
        "edge": round(r["projection"] - r["line"], 2),
        "sample": r["sample"],
        "line_source": r["line_source"],
        "qualified": bool(r["qualified"]),
        "selected": False,
        "actual": r["actual"],
        "result": result,
        "hit": result == "win",
        "miss": result == "loss",
        "status": {
            "win": "HIT", "loss": "MISS", "push": "PUSH", "void": "VOID",
        }.get(result),
        "measurement": {
            "n": s["n"] if s else None,
            "sigma": s["sigma"] if s else None,
            "mae": s["mae"] if s else None,
            "naive_mae": s["naive_mae"] if s else None,
            "beats_naive": (
                None if not s or s["naive_mae"] is None or s["mae"] is None
                else bool(s["mae"] < s["naive_mae"])),
        },
    }


def _selection_score(r: sqlite3.Row, stats: dict) -> float:
    """Rank a stored line without inventing a second probability.

    The measured probability already says how often comparable lines landed.
    It does not say how much to trust that measurement: a team baseline with
    246 games is noisier than a league baseline with 2,500. Shrink the edge
    toward 50% for small samples, then give a tiny nudge to a market whose
    model error is better than its naive baseline. The nudge is a tie-breaker,
    never a new confidence number.
    """
    probability = float(r["probability"] or 0.5)
    sample = float(r["sample"] or 0)
    reliability = sample / (sample + 250.0) if sample > 0 else 0.0
    score = 0.5 + (probability - 0.5) * reliability
    measured = stats.get((r["sport"], r["market"]))
    if (measured and measured["mae"] is not None
            and measured["naive_mae"] is not None
            and measured["mae"] < measured["naive_mae"]):
        score += 0.01
    return score


def _selection_reason(r: sqlite3.Row, stats: dict) -> str:
    """The evidence behind a selected card, written from stored fields only."""
    probability = round(float(r["probability"] or 0) * 100)
    line = float(r["line"])
    projection = float(r["projection"])
    difference = projection - line
    direction = "above" if difference >= 0 else "below"
    side = "Over" if r["side"] == "over" else "Under"
    sample = int(r["sample"] or 0)
    source = {
        "league": "league line",
        "league_average": "league average",
        "team_baseline": "team's own baseline",
        "market": "saved market line",
    }.get(r["line_source"] or "", (r["line_source"] or "reference line")
           .replace("_", " "))
    parts = [f"{probability}% measured for {side} {line:g} across "
             f"{sample:,} games",
             f"model {abs(difference):.2f} {direction} the line",
             source]
    measured = stats.get((r["sport"], r["market"]))
    if (measured and measured["mae"] is not None
            and measured["naive_mae"] is not None
            and measured["mae"] < measured["naive_mae"]):
        parts.append("model error below its baseline")
    return " · ".join(parts)


def board_cards(conn: sqlite3.Connection, day: str, sport: str = "all",
                limit: int = 10, offset: int = 0, market: str | None = None,
                league: str | None = None) -> dict:
    """A fixture-first board read from the complete stored matchup snapshot.

    One card is one fixture + one market family. A selected market returns
    every published fixture's home, away and combined lines. The default view
    is the curated layer: only fixtures with at least one row above the
    measured bar, using the strongest family and marking the strongest row.
    Filtering only reads `board_markets`; it never calls the model or changes a
    stored line.
    """
    conn.row_factory = sqlite3.Row
    ensure_tables(conn)
    limit = max(1, min(50, int(limit)))
    offset = max(0, int(offset))
    stats = _stats_map(conn)
    clause = "date = ?"
    params: list = [day]
    if sport in ("football", "nba"):
        clause += " AND sport = ?"
        params.append(sport)
    if league:
        clause += " AND league = ?"
        params.append(league)
    if market:
        clause += " AND market_group = ?"
        params.append(market)
    rows = conn.execute(
        f"SELECT * FROM board_markets WHERE {clause} "
        f"ORDER BY start_time IS NULL, start_time, sport, game_key, "
        f"  CASE scope WHEN 'home' THEN 0 WHEN 'away' THEN 1 ELSE 2 END, id",
        params).fetchall()

    stored_rows = len(rows)
    grouped: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for r in rows:
        key = (r["sport"], r["game_key"], r["market_group"])
        grouped.setdefault(key, []).append(r)

    if not market:
        # Default: the Board is a selection layer, not a dump of every stored
        # market. A fixture appears only when at least one row cleared the
        # measured bar, and only its strongest family is shown. Choosing a
        # market explicitly below returns the complete stored set for it.
        best: dict[tuple[str, str], tuple[float, tuple[str, str, str]]] = {}
        for key, group_rows in grouped.items():
            eligible = [r for r in group_rows if r["qualified"]]
            if not eligible:
                continue
            score = max(_selection_score(r, stats) for r in eligible)
            game = (key[0], key[1])
            if game not in best or score > best[game][0]:
                best[game] = (score, key)
        grouped = {key: grouped[key] for _, key in best.values()}

    cards = []
    for key in sorted(grouped, key=lambda k: (
            min((r["start_time"] or "9999") for r in grouped[k]),
            k[0], k[2], k[1])):
        group_rows = grouped[key]
        first = group_rows[0]
        home = first["home_team"] or (first["game_label"].split(" v ")[0])
        away = first["away_team"] or (
            first["game_label"].split(" v ")[-1]
            if " v " in first["game_label"] else "")
        card_rows = [_card_row(conn, r, stats, home, away)
                     for r in group_rows]
        paired = list(zip(group_rows, card_rows))
        qualified_pairs = [(raw, card) for raw, card in paired
                           if card["qualified"]]
        selected_raw, selected_row = max(
            qualified_pairs or paired,
            key=lambda pair: _selection_score(pair[0], stats))
        selected_row["selected"] = True
        selected = {**selected_row,
                    "reason": _selection_reason(selected_raw, stats)}
        settled = [r for r in card_rows if r["result"] is not None]
        wins = sum(1 for r in settled if r["result"] == "win")
        losses = sum(1 for r in settled if r["result"] == "loss")
        pushes = sum(1 for r in settled if r["result"] == "push")
        voids = sum(1 for r in settled if r["result"] == "void")
        measurement = max(
            (r["measurement"]["n"] or 0 for r in card_rows), default=0)
        cards.append({
            "sport": first["sport"],
            "date": first["date"],
            "game_key": first["game_key"],
            "game_label": first["game_label"],
            "home_team": home,
            "away_team": away,
            "league": first["league"],
            "start_time": first["start_time"],
            "market_group": first["market_group"],
            "market_label": GROUP_LABELS.get(
                first["market_group"], first["market_label"] or
                first["market_group"]),
            "rows": card_rows,
            "selected": selected,
            "qualified": any(r["qualified"] for r in card_rows),
            "best_probability": max(
                (r["probability"] for r in card_rows), default=None),
            "measurement": {"n": measurement},
            "settled_lines": len(settled),
            "won_lines": wins,
            "lost_lines": losses,
            "pushed_lines": pushes,
            "void_lines": voids,
        })

    total = len(cards)
    page = cards[offset:offset + limit]
    page_rows = [row for card in page for row in card["rows"]]
    all_rows = [row for card in cards for row in card["rows"]]
    settled_all = [r for r in all_rows if r["result"] is not None]
    wins_all = sum(1 for r in settled_all if r["result"] == "win")
    losses_all = sum(1 for r in settled_all if r["result"] == "loss")
    won_or_lost = wins_all + losses_all

    labels = conn.execute(
        f"SELECT DISTINCT game_label FROM board_markets WHERE date = ?"
        + (" AND sport = ?" if sport in ("football", "nba") else ""),
        (day, sport) if sport in ("football", "nba") else (day,)).fetchall()
    teams = sorted({name.strip() for row in labels
                    for name in row["game_label"].split(" v ") if name.strip()})
    leagues = [r["league"] for r in conn.execute(
        "SELECT DISTINCT league FROM board_markets WHERE date = ? "
        "AND league IS NOT NULL ORDER BY league", (day,))]
    options = _market_options_grouped(conn, sport, day, league)
    if market and not any(o["market"] == market for o in options):
        # Keep a saved market visible even when this date has no stored rows,
        # so the UI shows an empty state instead of silently resetting it.
        for (market_sport, market_key), s in stats.items():
            group, _, label = classify(market_sport, market_key)
            if group != market or (sport in ("football", "nba")
                                   and market_sport != sport):
                continue
            options.append({
                "market": market,
                "label": GROUP_LABELS.get(market, label),
                "sports": [market_sport],
                "scopes": [],
                "n": int(s["n"] or 0),
                "count": 0,
            })
            break
        options.sort(key=lambda item: (item["label"], item["market"]))
    return {
        "date": day,
        "sport": sport,
        "cards": page,
        "total": total,
        "base_total": total,
        "stored_rows": stored_rows,
        "row_total": len(all_rows),
        "page_row_total": len(page_rows),
        "settled_lines": len(settled_all),
        "won_lines": wins_all,
        "lost_lines": losses_all,
        "hit_rate": round(wins_all / won_or_lost, 4) if won_or_lost else None,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(page) < total,
        "filters": {"market": market, "league": league},
        "markets": options,
        "teams": teams,
        "leagues": leagues,
        "accuracy": _accuracy(conn, sport, market, "all"),
        "record": _market_record(conn, sport, day, market, league),
        "history": _market_record(conn, sport, None, market, league),
        "measured": sum(1 for key in stats
                        if sport == "all" or key[0] == sport),
        "fixtures": _fixtures_on(conn, day, sport),
    }


def _fixtures_on(conn: sqlite3.Connection, day: str, sport: str) -> int:
    """How many fixtures are published for that day, so an empty board can say
    whether the day is outside the window or simply cleared nothing."""
    total = 0
    if sport in ("all", "football"):
        total += conn.execute(
            "SELECT COUNT(*) FROM published_matches WHERE date = ?",
            (day,)).fetchone()[0]
    if sport in ("all", "nba"):
        total += conn.execute(
            "SELECT COUNT(*) FROM nba_published WHERE game_date = ?",
            (day,)).fetchone()[0]
    return total


def _record(conn: sqlite3.Connection, sport: str, before: str | None,
            market: str | None = None, scope: str | None = None,
            team: str | None = None, league: str | None = None) -> dict:
    clause = "result IS NOT NULL"
    params: list = []
    if before:
        clause += " AND date < ?"
        params.append(before)
    if sport in ("football", "nba"):
        clause += " AND sport = ?"
        params.append(sport)
    if league:
        clause += " AND league = ?"
        params.append(league)
    if market:
        clause += " AND market_group = ?"
        params.append(market)
    if scope in SCOPES:
        clause += " AND scope = ?"
        params.append(scope)
    if team:
        clause += " AND game_label LIKE ? ESCAPE '\\'"
        params.append(_like_term(team))
    rows = conn.execute(
        f"SELECT result, COUNT(*) AS n FROM board_picks WHERE {clause} "
        f"GROUP BY result", params).fetchall()
    counts = {r["result"]: r["n"] for r in rows}
    decided = counts.get("win", 0) + counts.get("loss", 0)
    return {
        "graded": decided + counts.get("push", 0),
        "won": counts.get("win", 0),
        "lost": counts.get("loss", 0),
        "pushed": counts.get("push", 0),
        "void": counts.get("void", 0),
        "hit_rate": round(counts.get("win", 0) / decided, 4) if decided else None,
    }


def _actual_for(conn: sqlite3.Connection, pick: sqlite3.Row) -> float | None:
    if pick["sport"] == "football":
        home_col, away_col = stats.STAT_COLUMNS[pick["market"]]
        row = conn.execute(
            f"SELECT {home_col} AS h, {away_col} AS a FROM matches "
            f"WHERE date = ? AND lower(home_team) = lower(?) "
            f"  AND lower(away_team) = lower(?) LIMIT 1",
            (pick["date"], pick["game_label"].split(" v ")[0],
             pick["game_label"].split(" v ")[-1])).fetchone()
        if row is None or row["h"] is None or row["a"] is None:
            return None
        return float(row["h"] + row["a"])

    game = conn.execute(
        "SELECT * FROM nba_games WHERE game_id = ?", (pick["game_key"],)
    ).fetchone()
    if game is None and str(pick["game_key"]).isdigit():
        # Snapshots built before the key switched to the NBA game id.
        game = conn.execute(
            "SELECT g.* FROM nba_published p JOIN nba_games g "
            "  ON g.game_id = p.game_id WHERE p.id = ?",
            (int(pick["game_key"]),)).fetchone()
    if game is None:
        return None
    group, _, _ = classify("nba", pick["market"])
    team_stats = None
    if group in nba_stats.STAT_COLUMNS:
        team_stats = {}
        for row in conn.execute(
                "SELECT team_id, reb, fgm, fg3m, ast, tov, stl, blk, pf "
                "FROM nba_team_games WHERE game_id = ?",
                (game["game_id"],)):
            two_pm = (row["fgm"] - row["fg3m"]
                      if row["fgm"] is not None and row["fg3m"] is not None
                      else None)
            team_stats[row["team_id"]] = {
                "rebounds": row["reb"],
                "two_pm": two_pm,
                "three_pm": row["fg3m"],
                "assists": row["ast"],
                "turnovers": row["tov"],
                "steals": row["stl"],
                "blocks": row["blk"],
                "fouls": row["pf"],
            }
    return _nba_actual(game, pick["market"], team_stats)


def _grade_table(conn: sqlite3.Connection, table: str, before: str,
                 stale: str) -> dict:
    """Settle every ungraded row in one snapshot table.

    The query includes today, so a game that finishes during the day can be
    updated on the next scheduler pass. It never rewrites a line, projection,
    probability or timestamp; only result/actual/graded_at change.
    """
    if table not in ("board_picks", "board_markets"):
        raise ValueError(table)
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE result IS NULL AND date <= ? "
        f"ORDER BY date", (before,)).fetchall()
    graded = {"win": 0, "loss": 0, "push": 0, "pending": 0, "void": 0}
    for pick in rows:
        actual = _actual_for(conn, pick)
        if actual is None:
            # A pick that still cannot be settled a week later never will be:
            # the result was never recorded. Mark it void rather than leaving
            # it pending forever, which would quietly flatter the record.
            if pick["date"] <= stale:
                conn.execute(
                    f"UPDATE {table} SET result = 'void', "
                    f"  graded_at = datetime('now') WHERE id = ?",
                    (pick["id"],))
                graded["void"] += 1
            else:
                graded["pending"] += 1
            continue
        if abs(actual - pick["line"]) < 1e-9:
            result = "push"
        elif (actual > pick["line"]) == (pick["side"] == "over"):
            result = "win"
        else:
            result = "loss"
        conn.execute(
            f"UPDATE {table} SET result = ?, actual = ?, "
            f"  graded_at = datetime('now') WHERE id = ?",
            (result, actual, pick["id"]))
        graded[result] += 1
    return graded


def grade(conn: sqlite3.Connection, before: str | None = None,
          log=print) -> dict:
    """Settle every complete matchup line and qualifying pick.

    `before` is inclusive: a game played earlier today is settled as soon as
    its final box score exists. The default scheduler calls this every half
    hour, so the Board's HIT/MISS marks do not wait for tomorrow's build.
    """
    conn.row_factory = sqlite3.Row
    ensure_tables(conn)
    before = before or date.today().isoformat()
    stale = (date.fromisoformat(before) - timedelta(days=7)).isoformat()
    markets = _grade_table(conn, "board_markets", before, stale)
    picks = _grade_table(conn, "board_picks", before, stale)
    conn.commit()
    graded = {key: markets[key] + picks[key] for key in markets}
    log(f"  GRADE    {markets['win'] + picks['win']} won, "
        f"{markets['loss'] + picks['loss']} lost, "
        f"{markets['push'] + picks['push']} pushed, "
        f"{markets['void'] + picks['void']} void, "
        f"{markets['pending'] + picks['pending']} pending")
    return graded


def rebuild(db_path: Path | str = DB_PATH, log=print) -> dict:
    """Measure, then build. The one call an admin button needs."""
    measured = measure_all(db_path, log=log)
    built = build_board(db_path, replace=True, log=log)
    return {"measured": measured, "built": built}


def daily(db_path: Path | str = DB_PATH, log=print) -> dict:
    """Grade finished games, then fill missing board lines. Safe to run often."""
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_tables(conn)
        graded = grade(conn, log=log)
    finally:
        conn.close()
    built = build_board(db_path, replace=False, log=log)
    return {"graded": graded, "built": built}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Measure model error and build the board")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--sport", choices=("all", "football", "nba"),
                        default="all", help="which engine to measure")
    parser.add_argument("--measure-only", action="store_true")
    parser.add_argument("--board-only", action="store_true")
    parser.add_argument("--grade", action="store_true")
    args = parser.parse_args()

    if args.grade:
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        grade(conn)
    elif args.measure_only:
        measure_all(args.db, sport=args.sport)
    elif args.board_only:
        build_board(args.db, days=args.days, replace=True)
    else:
        rebuild(args.db)
