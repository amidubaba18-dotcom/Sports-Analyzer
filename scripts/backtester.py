"""Cutoff-aware backtesting: does the 45/35/20 model actually beat simple
baselines?

What this version adds over the previous one
--------------------------------------------
  1. Every hit rate has a 95% Wilson confidence interval. A gap smaller than
     the interval is noise, and the report now says so instead of printing
     "LOSES TO by -2.5%".
  2. Model vs each baseline is compared PAIRED, on the same matches, with a
     McNemar-style z score. That is far more sensitive than comparing two
     separate hit rates.
  3. An ERROR metric (mean absolute error and bias of the predicted total
     against the actual total) sits beside the hit rate. A 0/1 hit rate throws
     away how close each prediction was; error does not.
  4. Reference predictors (always over / always under) and any predictor that
     answers the same way >95% of the time are excluded from "best baseline".
     They are the base rate in disguise, not skill.
  5. A shrinkage EXPERIMENT: team average blended toward the league average,
     weighted by sample size. If small samples are why the model loses, this
     is the cheapest fix to test.
  6. Failures are counted and printed per predictor, never hidden in a low
     coverage number.
  7. A loud warning if any fair line is a whole number (pushes become
     possible, and the x.5 snap in stats.suggest_line is wrong).

Usage
-----
    python scripts/backtester.py --market goals
    python scripts/backtester.py --markets goals,corners,shots --league "Premier League"
    python scripts/backtester.py --market corners --line 9.5 --season 2025-26
    python scripts/backtester.py --market goals --min-date 2025-12-01   (test period)
    python scripts/backtester.py --all-markets --shrink-k 15

Leave --league and --season off to pool every league and season in the DB,
which is what gives you enough matches to tell signal from noise.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402

import stats  # noqa: E402
from stats import STAT_COLUMNS  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "sports_analyzer.db"

EMPTY = {"value": None, "supported": None, "evidence": 0}
SHRINK_K = 10.0
Z95 = 1.96

# Predictors that do not forecast anything. They define the base rate.
REFERENCE = {"baseline_always_over", "baseline_always_under"}


# --------------------------------------------------------------------------
# Small statistics helpers
# --------------------------------------------------------------------------
def wilson_interval(hits, n, z=Z95):
    """95% Wilson score interval for a proportion. (None, None) if n == 0."""
    if n <= 0:
        return (None, None)
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def paired_hits(matches, a_name, b_name):
    """Paired comparison of two predictors' right/wrong calls on shared matches.

    Only matches where BOTH answered are used. `a_only` = a right and b wrong,
    `b_only` = b right and a wrong. z is the McNemar normal approximation.
    diff > 0 means `a` is ahead.
    """
    a_only = b_only = n = 0
    for m in matches:
        pa = m["predictions"][a_name]["supported"]
        pb = m["predictions"][b_name]["supported"]
        if pa is None or pb is None:
            continue
        n += 1
        right_a = pa == m["over"]
        right_b = pb == m["over"]
        if right_a and not right_b:
            a_only += 1
        elif right_b and not right_a:
            b_only += 1
    disputed = a_only + b_only
    z = (a_only - b_only) / math.sqrt(disputed) if disputed else 0.0
    return {"n": n, "a_only": a_only, "b_only": b_only,
            "diff": ((a_only - b_only) / n) if n else None, "z": z}


def hit_verdict(z):
    if z >= Z95:
        return "AHEAD (clear)"
    if z <= -Z95:
        return "BEHIND (clear)"
    return "no clear difference"


def paired_error(matches, a_name, b_name):
    """Paired difference in absolute error of the predicted total.

    mean_diff < 0 means `a` has the SMALLER error (is more accurate).
    """
    diffs = []
    for m in matches:
        va = m["predictions"][a_name]["value"]
        vb = m["predictions"][b_name]["value"]
        if va is None or vb is None:
            continue
        diffs.append(abs(va - m["actual_total"]) - abs(vb - m["actual_total"]))
    n = len(diffs)
    if n < 2:
        return None
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    se = math.sqrt(var / n)
    z = mean / se if se > 0 else 0.0
    return {"n": n, "mean_diff": mean, "z": z}


def error_verdict(z):
    if z <= -Z95:
        return "MORE accurate (clear)"
    if z >= Z95:
        return "LESS accurate (clear)"
    return "no clear difference"


# --------------------------------------------------------------------------
# Outcome
# --------------------------------------------------------------------------
def actual_total(row, market):
    """The real combined match total for a market, or None if not recorded."""
    if market not in STAT_COLUMNS:
        raise ValueError(f"unknown market: {market}")
    home_col, away_col = STAT_COLUMNS[market]
    home_val = row.get(home_col)
    away_val = row.get(away_col)
    if home_val is None or away_val is None or pd.isna(home_val) or pd.isna(away_val):
        return None
    return float(home_val) + float(away_val)


# --------------------------------------------------------------------------
# Predictors - all cutoff-aware, all the same shape
# --------------------------------------------------------------------------
def predict_model(conn, row, market, line, weights=None, recent_n=5,
                  shrink_k=SHRINK_K, method="shrunk", **_):
    """The engine's own estimator. Default method: sample-size shrunk home/away."""
    result = stats.analyze_market(
        conn, row["home_team"], row["away_team"], f"avg_{market}", line,
        recent_n=recent_n, before_date=row["date"], exclude_match_id=row["id"],
        weights=weights, league=row.get("league"), season=row.get("season"),
        method=method, shrink_k=shrink_k)
    if result.get("weighted_avg") is None:
        return dict(EMPTY)
    return {
        "value": result["weighted_avg"],
        "supported": result["supported"],
        "evidence": result.get("evidence_points", 0),
    }


def predict_blend(conn, row, market, line, weights=None, recent_n=5, **_):
    """The LEGACY fixed 45/35/20 blend, kept only so the two can be compared.

    The engine no longer uses this. It is scored on exactly the same matches and
    the same lines as `model_engine`, which is the only honest way to show what
    changing the estimator actually did.
    """
    return predict_model(conn, row, market, line, weights=weights,
                         recent_n=recent_n, method="blend")


def predict_homeaway(conn, row, market, line, **_):
    """Baseline: only season home-form + season away-form."""
    home = stats.get_team_home_averages(
        conn, row["home_team"], before_date=row["date"], exclude_match_id=row["id"])
    away = stats.get_team_away_averages(
        conn, row["away_team"], before_date=row["date"], exclude_match_id=row["id"])
    key = f"avg_{market}"
    if home.get(key) is None or away.get(key) is None:
        return dict(EMPTY)
    value = round(home[key] + away[key], 2)
    return {
        "value": value,
        "supported": value > line,
        "evidence": home.get(f"n_{market}", 0) + away.get(f"n_{market}", 0),
    }


def predict_recent(conn, row, market, line, recent_n=5, **_):
    """Baseline: only last-N form for both teams."""
    home = stats.get_team_recent_averages(
        conn, row["home_team"], n=recent_n, before_date=row["date"], exclude_match_id=row["id"])
    away = stats.get_team_recent_averages(
        conn, row["away_team"], n=recent_n, before_date=row["date"], exclude_match_id=row["id"])
    key = f"avg_{market}"
    if home.get(key) is None or away.get(key) is None:
        return dict(EMPTY)
    value = round(home[key] + away[key], 2)
    return {
        "value": value,
        "supported": value > line,
        "evidence": home.get(f"n_{market}", 0) + away.get(f"n_{market}", 0),
    }


def predict_h2h(conn, row, market, line, **_):
    """Baseline: only previous meetings between the two sides."""
    h2h = stats.get_h2h_summary(
        conn, row["home_team"], row["away_team"],
        before_date=row["date"], exclude_match_id=row["id"])
    key = f"avg_{market}"
    if h2h.get(key) is None:
        return dict(EMPTY)
    value = h2h[key]
    return {
        "value": value,
        "supported": value > line,
        "evidence": h2h.get(f"n_{market}", 0),
    }


def predict_league(conn, row, market, line, **_):
    """Baseline: the league's own average for this market, before this match.

    NOTE: at a per-match fair line this is circular, because the fair line is
    this same league average snapped to a half-goal. Its hit rate then depends
    on rounding, not skill, and it is usually flagged "degenerate". Its ERROR
    (how far the league average is from the real total) is the honest way to
    use it, and is what the error table reports.
    """
    league_avg = stats.get_league_average(
        conn, row.get("league"), row.get("season"), f"avg_{market}",
        before_date=row["date"], exclude_match_id=row["id"])
    if league_avg["average"] is None:
        return dict(EMPTY)
    value = league_avg["average"]
    return {"value": value, "supported": value > line, "evidence": league_avg["matches"]}


def predict_shrunk(conn, row, market, line, shrink_k=SHRINK_K, **_):
    """EXPERIMENT: season home/away form shrunk toward the league average.

    value = (n * team_total + k * league_avg) / (n + k)

    n is the smaller of the two teams' sample sizes. With few matches the
    league average dominates; with many the team's own form does. If the model
    loses because small samples are noisy, this should close the gap.
    """
    key = f"avg_{market}"
    home = stats.get_team_home_averages(
        conn, row["home_team"], before_date=row["date"], exclude_match_id=row["id"])
    away = stats.get_team_away_averages(
        conn, row["away_team"], before_date=row["date"], exclude_match_id=row["id"])
    league_avg = stats.get_league_average(
        conn, row.get("league"), row.get("season"), key,
        before_date=row["date"], exclude_match_id=row["id"])
    if home.get(key) is None or away.get(key) is None or league_avg["average"] is None:
        return dict(EMPTY)
    n = min(home.get(f"n_{market}", 0), away.get(f"n_{market}", 0))
    team_total = home[key] + away[key]
    value = round((n * team_total + shrink_k * league_avg["average"]) / (n + shrink_k), 2)
    return {"value": value, "supported": value > line, "evidence": n}


def predict_always_over(conn, row, market, line, **_):
    """Reference: always OVER. Not a model; defines the base rate."""
    return {"value": None, "supported": True, "evidence": 0}


def predict_always_under(conn, row, market, line, **_):
    """Reference: always UNDER."""
    return {"value": None, "supported": False, "evidence": 0}


PREDICTORS = {
    "model_engine": predict_model,
    "legacy_blend_45_35_20": predict_blend,
    "baseline_homeaway": predict_homeaway,
    "baseline_recent": predict_recent,
    "baseline_h2h": predict_h2h,
    "baseline_league_avg": predict_league,
    "experiment_shrunk": predict_shrunk,
    "baseline_always_over": predict_always_over,
    "baseline_always_under": predict_always_under,
}
MODEL = "model_engine"


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
def load_sample(conn, market, league=None, season=None, min_date=None, max_date=None,
                limit=None):
    """Completed matches that actually have the outcome recorded for `market`."""
    home_col, away_col = STAT_COLUMNS[market]
    clause = [
        f"{home_col} IS NOT NULL",
        f"{away_col} IS NOT NULL",
        "home_goals IS NOT NULL",
    ]
    params: list = []
    if league:
        clause.append("league = ?")
        params.append(league)
    if season:
        clause.append("season = ?")
        params.append(season)
    if min_date:
        clause.append("date >= ?")
        params.append(min_date)
    if max_date:
        clause.append("date <= ?")
        params.append(max_date)

    sql = f"SELECT * FROM matches WHERE {' AND '.join(clause)} ORDER BY date ASC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return pd.read_sql_query(sql, conn, params=params)


def run(conn, market="goals", line=None, league=None, season=None, min_date=None,
        max_date=None, limit=None, min_evidence=3, weights=None, recent_n=5,
        require_model_data=True, line_mode=None, shrink_k=SHRINK_K, progress=True):
    """Run the model and every baseline over one match sample.

    line_mode:
      * "fixed"     - one line for every match (a supplied --line).
      * "per_match" - each match judged against a fair line derived from the
                      league average strictly before that match.

    Default: "fixed" if a line was supplied, otherwise "per_match".
    """
    if line_mode is None:
        line_mode = "fixed" if line is not None else "per_match"

    sample = load_sample(conn, market, league, season, min_date, max_date, limit)

    fixed_line = None
    if line_mode == "fixed":
        if line is not None:
            fixed_line = line
            line_source = "user"
        else:
            reference = sample.iloc[0] if len(sample) else None
            fixed_line, line_source, _ = stats.suggest_line(
                conn,
                reference["home_team"] if reference is not None else "",
                reference["away_team"] if reference is not None else "",
                f"avg_{market}", league=league, season=season, before_date=min_date)
    else:
        line_source = "fair_per_match"

    per_match = []
    whole_line_count = 0
    total_rows = len(sample)
    for i, (_, row) in enumerate(sample.iterrows(), start=1):
        if progress and i % 250 == 0:
            print(f"    ...{market}: {i}/{total_rows}", file=sys.stderr)

        actual = actual_total(row, market)
        if actual is None:
            continue

        if line_mode == "per_match":
            match_line, _, _ = stats.suggest_line(
                conn, row["home_team"], row["away_team"], f"avg_{market}",
                league=row.get("league"), season=row.get("season"),
                before_date=row["date"])
        else:
            match_line = fixed_line

        if float(match_line) % 1 == 0:
            whole_line_count += 1

        # A PUSH lands exactly on the line. It is neither over nor under, so it
        # is removed for every predictor rather than booked as an UNDER.
        is_push = abs(actual - match_line) < 1e-9
        over = None if is_push else (actual > match_line)

        record = {
            "id": int(row["id"]),
            "date": row["date"],
            "league": row.get("league"),
            "season": row.get("season"),
            "home_team": row["home_team"],
            "away_team": row["away_team"],
            "line": match_line,
            "actual_total": actual,
            "over": over,
            "push": is_push,
            "predictions": {},
        }
        for name, fn in PREDICTORS.items():
            try:
                record["predictions"][name] = fn(
                    conn, row, market, match_line, weights=weights,
                    recent_n=recent_n, shrink_k=shrink_k)
            except Exception as exc:  # surfaced in the report, never hidden
                record["predictions"][name] = {
                    "value": None, "supported": None, "evidence": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        per_match.append(record)

    pushes = [m for m in per_match if m["push"]]
    per_match = [m for m in per_match if not m["push"]]

    # Failures are counted BEFORE the model-data filter so they cannot hide.
    errors: dict[str, dict] = {}
    for m in per_match:
        for name, p in m["predictions"].items():
            if "error" in p:
                e = errors.setdefault(name, {"count": 0, "first": p["error"]})
                e["count"] += 1

    if require_model_data:
        per_match = [
            r for r in per_match
            if r["predictions"][MODEL]["supported"] is not None
            and r["predictions"][MODEL]["evidence"] >= min_evidence
        ]

    lines_used = [m["line"] for m in per_match]
    return {
        "market": market,
        "line_mode": line_mode,
        "line": (fixed_line if line_mode == "fixed" else
                 (round(sum(lines_used) / len(lines_used), 3) if lines_used else None)),
        "line_source": line_source,
        "sample_size": len(per_match),
        "rows_loaded": total_rows,
        "whole_line_matches": whole_line_count,
        "pushes_excluded": len(pushes),
        "push_details": [{"date": m["date"], "home_team": m["home_team"],
                          "away_team": m["away_team"], "line": m["line"],
                          "actual_total": m["actual_total"]} for m in pushes[:20]],
        "errors": errors,
        "shrink_k": shrink_k,
        "matches": per_match,
    }


def summarize(backtest):
    """Hit rates with intervals, paired comparisons, and error metrics."""
    matches = backtest["matches"]
    total = len(matches)
    base = {
        "market": backtest["market"],
        "line": backtest["line"],
        "line_source": backtest["line_source"],
        "line_mode": backtest.get("line_mode"),
        "rows_loaded": backtest.get("rows_loaded", 0),
        "whole_line_matches": backtest.get("whole_line_matches", 0),
        "pushes_excluded": backtest.get("pushes_excluded", 0),
        "push_details": backtest.get("push_details", []),
        "errors": backtest.get("errors", {}),
        "shrink_k": backtest.get("shrink_k", SHRINK_K),
    }
    if total == 0:
        base.update({"sample_size": 0, "base_rate_over": None, "predictors": {}})
        return base

    over_count = sum(1 for m in matches if m["over"])
    base_rate = over_count / total
    majority_rate = max(base_rate, 1 - base_rate)

    out = {}
    for name in PREDICTORS:
        answered = [m for m in matches if m["predictions"][name]["supported"] is not None]
        hits = sum(1 for m in answered if m["predictions"][name]["supported"] == m["over"])
        over_answers = sum(1 for m in answered if m["predictions"][name]["supported"])
        answer_rate = (over_answers / len(answered)) if answered else None
        lo, hi = wilson_interval(hits, len(answered))

        valued = [m for m in matches if m["predictions"][name]["value"] is not None]
        mae = bias = None
        if valued and name not in REFERENCE:
            errs = [m["predictions"][name]["value"] - m["actual_total"] for m in valued]
            mae = sum(abs(e) for e in errs) / len(errs)
            bias = sum(errs) / len(errs)

        out[name] = {
            "answered": len(answered),
            "coverage": round(len(answered) / total, 3),
            "hits": hits,
            "hit_rate": round(hits / len(answered), 4) if answered else None,
            "ci_low": round(lo, 4) if lo is not None else None,
            "ci_high": round(hi, 4) if hi is not None else None,
            "answer_rate_over": round(answer_rate, 4) if answer_rate is not None else None,
            "degenerate": (name not in REFERENCE and answer_rate is not None
                           and (answer_rate > 0.95 or answer_rate < 0.05)),
            "mae": round(mae, 3) if mae is not None else None,
            "bias": round(bias, 3) if bias is not None else None,
            "error_n": len(valued) if name not in REFERENCE else 0,
        }

    # Best baseline: real, non-degenerate baselines only.
    best_name, best_rate = None, None
    for name, s in out.items():
        if (name.startswith("baseline") and name not in REFERENCE
                and not s["degenerate"] and s["hit_rate"] is not None):
            if best_rate is None or s["hit_rate"] > best_rate:
                best_name, best_rate = name, s["hit_rate"]

    # Paired comparisons of the model against everything else.
    hit_pairs, err_pairs = {}, {}
    for name in PREDICTORS:
        if name == MODEL:
            continue
        hit_pairs[name] = paired_hits(matches, MODEL, name)
        if name not in REFERENCE:
            pe = paired_error(matches, MODEL, name)
            if pe is not None:
                err_pairs[name] = pe

    line_counts: dict[float, int] = {}
    for m in matches:
        line_counts[m["line"]] = line_counts.get(m["line"], 0) + 1

    high = [m for m in matches
            if m["predictions"][MODEL]["supported"] is not None
            and m["predictions"][MODEL]["evidence"] >= 10]
    high_hits = sum(1 for m in high if m["predictions"][MODEL]["supported"] == m["over"])
    hlo, hhi = wilson_interval(high_hits, len(high))

    base.update({
        "line_distribution": sorted(line_counts.items()),
        "sample_size": total,
        "base_rate_over": round(base_rate, 4),
        "majority_class_rate": round(majority_rate, 4),
        "best_baseline": {"name": best_name, "rate": best_rate},
        "predictors": out,
        "paired_hits": hit_pairs,
        "paired_error": err_pairs,
        "model_high_evidence": {
            "threshold": 10, "answered": len(high), "hits": high_hits,
            "hit_rate": round(high_hits / len(high), 4) if high else None,
            "ci_low": round(hlo, 4) if hlo is not None else None,
            "ci_high": round(hhi, 4) if hhi is not None else None,
        },
    })
    return base


def _pct(x):
    return "n/a" if x is None else f"{x:.1%}"


def print_summary(summary):
    if summary.get("line_source") == "user":
        line_txt = f"fixed {summary['line']}"
    else:
        line_txt = f"fair per-match (mean {summary['line']})"
    print(f"\n{'=' * 78}")
    print(f"Market: {summary['market']}   Line: {line_txt}   Sample: {summary['sample_size']}"
          f"   (loaded {summary.get('rows_loaded', 0)})")

    # ---- loud problems first
    for name, e in summary.get("errors", {}).items():
        print(f"  !! {name} FAILED on {e['count']} match(es). First error: {e['first']}")

    whole = summary.get("whole_line_matches", 0)
    if whole and summary.get("line_mode") == "per_match":
        print(f"  !! {whole} match(es) used a WHOLE-NUMBER fair line. Fair lines must be "
              f"x.5 (e.g. 2.5, 3.5), so fix the snap in stats.snap_to_half_line. "
              f"Until then pushes distort this test.")

    pushes = summary.get("pushes_excluded", 0)
    if pushes:
        print(f"  PUSHES EXCLUDED: {pushes} match(es) landed exactly on the line and "
              f"were dropped for every predictor.")
        for p in summary.get("push_details", [])[:3]:
            print(f"    {p['date']}  {p['home_team']} v {p['away_team']}  "
                  f"total {p['actual_total']} = line {p['line']}")

    if summary["sample_size"] == 0:
        print("  No qualifying matches.")
        return

    n = summary["sample_size"]
    print(f"  Base rate (actual OVER): {_pct(summary['base_rate_over'])}"
          f"   Majority class: {_pct(summary['majority_class_rate'])}"
          f"   (a rate needs to differ by more than ~{Z95 * math.sqrt(0.25 / n):.1%} to mean anything)")

    dist = summary.get("line_distribution") or []
    if dist:
        print("  Lines used: " + ", ".join(f"{ln}: {c}" for ln, c in dist))

    # ---- hit rates with intervals
    print()
    print(f"  {'predictor':24} {'answered':>8} {'hits':>6} {'rate':>7}  {'95% interval':>15}")
    print("  " + "-" * 66)
    degenerate = []
    for name, s in summary["predictors"].items():
        iv = (f"{_pct(s['ci_low'])} to {_pct(s['ci_high'])}"
              if s["ci_low"] is not None else "n/a")
        flag = ""
        if s.get("degenerate"):
            side = "OVER" if s["answer_rate_over"] > 0.5 else "UNDER"
            flag = f"  <- always says {side}"
            degenerate.append(name)
        print(f"  {name:24} {s['answered']:>8} {s['hits']:>6} {_pct(s['hit_rate']):>7}  "
              f"{iv:>15}{flag}")
    if degenerate:
        print(f"\n  WARNING: {', '.join(degenerate)} answered the same way >95% of the time.")
        print("  That score is the base rate in disguise. It is excluded from 'best baseline'.")

    # ---- paired hit comparison
    print("\n  Model vs each predictor, PAIRED (same matches, hit/miss):")
    print(f"  {'vs':24} {'n':>6} {'model only':>11} {'other only':>11}  verdict")
    for name, p in summary["paired_hits"].items():
        print(f"  {name:24} {p['n']:>6} {p['a_only']:>11} {p['b_only']:>11}  "
              f"{hit_verdict(p['z'])}")

    # ---- error metric
    print("\n  Predicted total vs actual total (lower error = better):")
    print(f"  {'predictor':24} {'n':>6} {'MAE':>8} {'bias':>8}")
    for name, s in summary["predictors"].items():
        if s["mae"] is None:
            continue
        print(f"  {name:24} {s['error_n']:>6} {s['mae']:>8.3f} {s['bias']:>+8.3f}")
    if summary["paired_error"]:
        print("\n  Model error vs each predictor, PAIRED (negative = model closer):")
        for name, p in summary["paired_error"].items():
            print(f"  {name:24} n={p['n']:<6} mean diff {p['mean_diff']:+.3f}  "
                  f"{error_verdict(p['z'])}")

    # ---- plain-English reading
    model = summary["predictors"][MODEL]
    print(f"\n  Model: {_pct(model['hit_rate'])} "
          f"(interval {_pct(model['ci_low'])} to {_pct(model['ci_high'])}) "
          f"vs majority class {_pct(summary['majority_class_rate'])}")
    if model["ci_low"] is not None:
        if model["ci_low"] > summary["majority_class_rate"]:
            print("  -> Model is clearly above the majority class.")
        elif model["ci_high"] < summary["majority_class_rate"]:
            print("  -> Model is clearly below the majority class.")
        else:
            print("  -> The majority-class rate sits inside the model's interval: "
                  "no evidence of skill on hit rate alone.")
    best = summary["best_baseline"]
    if best["name"]:
        p = summary["paired_hits"][best["name"]]
        print(f"  Best real baseline: {best['name']} ({_pct(best['rate'])}). "
              f"Model vs that baseline, paired: {hit_verdict(p['z'])}.")
        print("  (Picking the best of several baselines after the fact flatters it. "
              "Treat this as the ceiling, not the fair comparison.)")

    legacy = summary["predictors"].get("legacy_blend_45_35_20")
    if legacy and legacy["hit_rate"] is not None:
        lp = summary["paired_hits"].get("legacy_blend_45_35_20")
        le = summary["paired_error"].get("legacy_blend_45_35_20")
        print(f"  Legacy 45/35/20 blend: {_pct(legacy['hit_rate'])} "
              f"(MAE {legacy['mae']}), engine {_pct(model['hit_rate'])} "
              f"(MAE {model['mae']}).")
        if lp:
            print(f"    Paired calls, engine vs legacy blend: {hit_verdict(lp['z'])} "
                  f"({lp['a_only']} vs {lp['b_only']} of {lp['n']})")
        if le:
            print(f"    Paired error, engine vs legacy blend: {error_verdict(le['z'])} "
                  f"(mean diff {le['mean_diff']:+.3f})")

    hi_ev = summary["model_high_evidence"]
    if hi_ev["answered"]:
        print(f"  Model on high evidence (>={hi_ev['threshold']}): "
              f"{hi_ev['hits']}/{hi_ev['answered']} = {_pct(hi_ev['hit_rate'])} "
              f"(interval {_pct(hi_ev['ci_low'])} to {_pct(hi_ev['ci_high'])})")
    print(f"  Shrinkage k = {summary.get('shrink_k')} "
          f"(0 would disable shrinkage and reproduce raw team form)")


def main():
    parser = argparse.ArgumentParser(description="Cutoff-aware backtest")
    parser.add_argument("--market", default="goals", choices=list(STAT_COLUMNS))
    parser.add_argument("--markets", default=None,
                        help="comma separated subset, e.g. goals,corners,shots")
    parser.add_argument("--line", type=float, default=None,
                        help="A fixed line. Omit to use a per-match fair line.")
    parser.add_argument("--line-mode", default="auto", choices=["auto", "fixed", "per_match"],
                        help="auto = fixed when --line is given, else per-match fair line")
    parser.add_argument("--weights", default=None,
                        help="legacy blend only, e.g. home_away=0.5,recent_form=0.3,h2h=0.2 "
                             "(ignored by the default shrunk method)")
    parser.add_argument("--league", default=None)
    parser.add_argument("--season", default=None)
    parser.add_argument("--min-date", default=None)
    parser.add_argument("--max-date", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--min-evidence", type=int, default=3)
    parser.add_argument("--shrink-k", type=float, default=SHRINK_K,
                        help="strength of shrinkage toward the league average")
    parser.add_argument("--all-markets", action="store_true")
    args = parser.parse_args()

    weights = None
    if args.weights:
        weights = {}
        for part in args.weights.split(","):
            key, value = part.split("=", 1)
            weights[key.strip()] = float(value)

    line_mode = None if args.line_mode == "auto" else args.line_mode

    if args.markets:
        markets = [m.strip() for m in args.markets.split(",") if m.strip()]
        unknown = [m for m in markets if m not in STAT_COLUMNS]
        if unknown:
            print(f"unknown market(s): {unknown}")
            return 1
    elif args.all_markets:
        markets = list(STAT_COLUMNS)
    else:
        markets = [args.market]

    manual_markets = {"tackles", "throw_ins", "aerial_duels", "offsides",
                      "interceptions", "goal_kicks", "saves", "substitutions"}

    conn = sqlite3.connect(DB_PATH)
    try:
        for market in markets:
            if market in manual_markets:
                home_col = STAT_COLUMNS[market][0]
                have = conn.execute(
                    f"SELECT COUNT(*) FROM matches WHERE {home_col} IS NOT NULL").fetchone()[0]
                if have == 0:
                    print(f"\nMarket: {market} - no manual data recorded, skipped.")
                    continue

            backtest = run(
                conn, market=market, line=args.line, league=args.league,
                season=args.season, min_date=args.min_date, max_date=args.max_date,
                limit=args.limit, min_evidence=args.min_evidence,
                weights=weights, line_mode=line_mode, shrink_k=args.shrink_k)
            print_summary(summarize(backtest))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
