# Sports Analyzer — the complete system

This is the full description of what exists in this repository, from the first
line of the engine to the dashboard fallback added last. It is written to be read
before the basketball work starts, so it records not just *what* was built but
*why* each rule exists and which bug it prevents.

Nothing here is aspirational. If something is not built, it is listed under
**Known limits**.

---

## 1. What the product is

A private football statistical research tool for one admin and a small group of
users.

For a chosen fixture, stat and line it shows:

- the season home/away averages of both teams,
- their recent form and head-to-head record,
- a combined expected **match total**,
- the sample size behind that number,
- and whether the evidence points **over** or **under** the line.

It does not recommend a bet. It does not show a "confidence score". It reports a
number, the evidence behind it, and how thin that evidence is. When there is not
enough data it says so instead of inventing a figure.

The tool is deliberately **not** sportsbook-shaped: no odds tiles, no casino
visuals, no animation, no fake percentages.

---

## 2. The two audiences

| | Admin | Normal user |
|---|---|---|
| Sees | everything | only fixtures the admin published |
| Can | import, publish, enter stats, fill profiles, export, analyse anything | open a published fixture and read its analysis |
| Entry point | `Admin` button on the dashboard | the dashboard |

The split is enforced by data and by admin authentication: normal users are
served from `published_matches`, never from `matches`. In production every
`/admin/*` route and the NBA publish/refresh writes require `ADMIN_TOKEN`; the
Admin screen asks for it and keeps it only in the browser session. The app
refuses to start with `APP_ENV=production` and no token. A local development
instance without the variable remains open, so it must stay bound to
`127.0.0.1`.

---

## 3. The four data concepts — the core design rule

These are deliberately separate tables, and mixing them up is the mistake the
whole design exists to prevent.

| Concept | Table | What it is | Can it be deleted by a CSV refresh? |
|---|---|---|---|
| **Historical matches** | `matches` | what the engine learns from | yes, if `source='csv'` |
| **Published matches** | `published_matches` | what users may see | no |
| **Manual stats** | `manual_stats` | counts the free CSVs do not carry | no (attached to a match) |
| **Team profiles** | `team_profiles` | a typed per-game average | no |

The rules that follow from this:

- Publishing a fixture does **not** add it to the historical dataset.
- Importing history does **not** publish anything.
- A published fixture is analysed from **team names + date**, not from a match id.
  It may have no row in `matches` at all.
- A team profile is the weakest concept: it fills a gap where nothing has been
  counted, it is labelled as typed wherever it is used, and it can never outrank
  counted data.

---

## 4. Stack and how it runs

| Layer | Technology |
|---|---|
| API | Python 3 + FastAPI + uvicorn |
| Storage | SQLite (`data/sports_analyzer.db`) |
| Data work | pandas + numpy |
| UI | one plain `index.html` (no framework, no build step) |
| Serving | FastAPI serves both the API and `/app/` from one process |

Start it:

```powershell
start.bat
```

or:

```powershell
.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8001 --app-dir backend
```

Open `http://localhost:8001/app/`. API docs: `http://localhost:8001/docs`.

`start.bat` checks that `.venv\Scripts\python.exe` exists, opens the browser after
three seconds, then runs uvicorn in the foreground.

`backend/requirements.txt` holds the dependencies (fastapi, uvicorn, pandas,
numpy, pydantic).

---

## 5. Repository map

```
Sports-Analyzer/
  start.bat                   one-click server + browser
  README.md                   the working guide (workflow, model status, limits)
  SYSTEM.md                   this document

  backend/
    main.py                   FastAPI: validation, routing, JSON shaping, static mount
    requirements.txt

  scripts/
    stats.py                  the engine: averages, shrinkage, fair lines
    matches_engine.py         batch layer over stats.py
    backtester.py             cutoff-aware model vs baseline evaluation
    import_data.py            CSV -> SQLite (id-preserving upsert, source-aware)
    fetch_csvs.py             download the current season file for every league
    seasons.py                season labels ('2026-27') — one definition, shared
    export_data.py            SQLite -> CSV and back (admin work survives a rebuild)
    manual_stats.py           enter the stats the CSVs lack, from the terminal
    cli.py                    terminal analysis (fastest sanity check)
    migrate_schema.py         create/upgrade the schema (idempotent)
    verify_engine.py          engine regression checks (known-correct values)
    smoke_test.py             end-to-end HTTP test (server must be running)
    db_info.py                print the schema and row counts
    explore.py                what is actually in the DB, per league and season

  frontend/
    index.html                the whole PWA (dashboard, match centre, admin)
    manifest.json             PWA metadata
    icon.svg                  icon
    sw.js                     minimal service worker (skipWaiting/claim)

  data/
    raw/<league>/<season>.csv   source CSVs, replaced as the season runs
    export/                     the database written out as CSV (the backup)
    sports_analyzer.db          the live database
```

The project is versioned in Git at
`https://github.com/amidubaba18-dotcom/Sports-Analyzer`. `.env` is ignored; the
database and `data/export/` are tracked so a fresh clone can start with the
current evidence and predictions.

---

## 6. Database schema

Created and upgraded by `scripts/migrate_schema.py`, which is **idempotent** — it
can run on an empty folder or on a database months old.

### `matches` — the historical dataset

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | preserved across re-imports |
| `date` | TEXT NOT NULL | ISO `YYYY-MM-DD` |
| `home_team`, `away_team` | TEXT NOT NULL | provider names (`Man United`, `Paris SG`, …) |
| `league` | TEXT NOT NULL | comes from the **folder**, never the CSV `Div` |
| `season` | TEXT NOT NULL | canonical `YYYY-YY` |
| `status` | TEXT NOT NULL DEFAULT `'completed'` | `completed` / `scheduled` |
| `source` | TEXT NOT NULL DEFAULT `'csv'` | `'csv'` or `'manual'` |
| `referee` | TEXT | |
| `home_goals`, `away_goals` | INTEGER | nullable: a fixture may have no score yet |
| `home_ht_goals`, `away_ht_goals`, `ht_result` | INTEGER/TEXT | half-time |
| `home_shots`/`away_shots` | INTEGER | |
| `home_shots_on_target`/`away_…` | INTEGER | |
| `home_fouls`/`away_fouls` | INTEGER | |
| `home_corners`/`away_corners` | INTEGER | |
| `home_yellow_cards`/`away_…` | INTEGER | |
| `home_red_cards`/`away_…` | INTEGER | |
| `home_tackles` … `away_substitutions` | INTEGER | 8 markets × 2, normally NULL |
| `odds_home_win`, `odds_draw`, `odds_away_win` | REAL | stored, not used by the model |
| `odds_over_2_5`, `odds_under_2_5` | REAL | stored, not used |
| `handicap_line`, `odds_handicap_home`, `odds_handicap_away` | REAL | stored, not used |

Indexes: `date`, `home_team`, `away_team`, `(home_team, date)`,
`(away_team, date)`, `(league, season)`, and the unique identity index
`idx_matches_identity` on **`(league, season, date, home_team, away_team)`**.

The identity index is what makes duplicate protection and the id-preserving
upsert possible. It is the same key the API checks before publishing.

### `manual_stats` — hand-entered counts, one row per match

`match_id` INTEGER PRIMARY KEY (one row per match, not per team), then:

```
home_tackles, away_tackles,
home_throw_ins, away_throw_ins,
home_aerial_duels, away_aerial_duels,
home_offsides, away_offsides,
home_interceptions, away_interceptions,
home_goal_kicks, away_goal_kicks,
home_saves, away_saves,
home_substitutions, away_substitutions,
updated_at, updated_by
```

All values are **counts for that match**, not percentages and not averages.

### `published_matches` — what the dashboard may show

| Column | Notes |
|---|---|
| `id` | |
| `league` | free text |
| `season` | canonical, filled from the date if omitted |
| `date` | ISO |
| `kickoff` | e.g. `19:30`, may be null |
| `home_team`, `away_team` | free text, but the team-search endpoint exists to avoid name splits |
| `markets` | JSON array or CSV of market names; null = all |
| `status` | `'published'` (default) or `'hidden'` |
| `sort_order` | integer, default 0 |
| `created_at`, `updated_at` | |

### `team_profiles` — typed per-team per-game averages

| Column | Notes |
|---|---|
| `id` | |
| `league`, `season`, `team` | NOT NULL |
| `as_of` | NOT NULL, ISO date the figures were read |
| `tackles`, `throw_ins`, `aerial_duels`, `offsides`, `interceptions`, `goal_kicks`, `saves`, `substitutions` | REAL, per game, **one team** |
| `note`, `updated_at`, `updated_by` | |

Unique index `idx_team_profiles_identity` on **`(league, season, team)`**.
`as_of` is deliberately *not* part of the identity: the admin keeps one current
picture of a team and moves its date forward.

### `available_fixtures` — candidates from the provider

A fixture the provider says exists. It is **not** a published match and **not** a
historical match; it is a candidate with a mapping verdict.

| Column | Notes |
|---|---|
| `id` | |
| `provider`, `provider_id` | UNIQUE together; `provider_id` is the provider's match id |
| `competition`, `league`, `season` | canonical league name, never the provider's |
| `date`, `kickoff` | ISO date; kickoff is `HH:MM` **UTC** |
| `home_provider_name`, `away_provider_name` | exactly as the provider sent them |
| `home_provider_short`, `away_provider_short` | the provider's short name, used for matching |
| `home_team`, `away_team` | canonical names, NULL until mapped |
| `status` | `scheduled` / `live` / `finished` / `postponed` / `cancelled` |
| `mapping_state` | `ok` or `needs_attention` |
| `mapping_note` | why it is held back |
| `published_id` | the `published_matches` row it became, if any |
| `first_seen`, `last_seen`, `raw` | provenance; `published_id` is never reset by a refresh |

### `season_fixtures` — the whole season, for browsing only

The provider's full calendar: played and scheduled alike. This is a **calendar**,
not a candidate. It is never published from; publishing still flows through
`available_fixtures` and the 14-day window, so a fixture nine months away cannot
reach the dashboard wearing today's form.

| Column | Notes |
|---|---|
| `id` | |
| `provider`, `provider_id` | UNIQUE together; the provider's match id |
| `competition`, `league`, `season`, `matchday` | canonical names; season `YYYY-YY` |
| `date`, `kickoff` | ISO date; kickoff `HH:MM` UTC |
| `home_provider_name`, `away_provider_name` | exactly as the provider sent them |
| `home_team`, `away_team` | canonical names, NULL until mapped |
| `status` | `scheduled` / `live` / `finished` / `postponed` / `cancelled` |
| `home_goals`, `away_goals` | the provider's score once played |
| `match_id` | the `matches` row this result became, rebuilt from the natural key |
| `first_seen`, `last_seen`, `raw` | provenance |

Measured: **2,058 fixtures** for 2026-27 across the six leagues (380/306 per
league), every team mapped, 369 results linked to `matches`.

The season screen returns one of three states per fixture, and the distinction is
the whole point:

- **played** — has a result (from `matches` when linked, else the provider);
- **published / ready** — inside the 14-day window, analyzable;
- **outside** — beyond the window, with `window_opens = date − 14 days` and a
  note. It is never given a projection.

A past season needs no calendar: `GET /season` falls back to `matches`, which is
already complete for 2024-25 and 2025-26.

### `prediction_stats` — how wrong each market has been

One row per sport × market. Written by `scripts/predict.py`, never by hand.

| Column | Notes |
|---|---|
| `sport`, `market` | PRIMARY KEY together |
| `label` | display label |
| `n` | matches in the walk-forward measurement |
| `sigma` | standard deviation of `actual − projection` |
| `mae` | mean absolute error of the model |
| `naive_mae` | the same error for the reference line: league average for football and combined NBA markets, the team's own baseline for NBA team markets |
| `bias` | mean residual; positive means the model under-projects |
| `quantiles` | JSON `[[residual, cumulative probability], ...]` |
| `calibration` | JSON holdout reliability: claim vs observed, per bucket |
| `updated_at` | when it was measured |

### `board_picks` — the qualifying frozen picks

One row per date × sport × fixture × market. Every qualifying market pick is
written before kickoff and never rewritten afterwards. The legacy ranked
`GET /board` view serves ranks 1–2 per fixture; a market or scope filter serves
the full stored set. The user-facing Board reads `board_markets` instead.

| Column | Notes |
|---|---|
| `date`, `sport`, `game_key`, `market` | UNIQUE together |
| `game_label`, `league` | display |
| `market_group` | filter family: `points`, `rebounds`, `goals`, `corners`, … |
| `scope` | `combined`, `home` or `away` (whose count the market is) |
| `model` | `core`; the context layer gets its own value once it is measured |
| `side`, `line`, `projection` | what the pick is |
| `probability` | measured, shrunk, clamped to 0.05–0.95 |
| `sample` | matches behind the probability |
| `line_source` | `league`, `league_average`, `team_baseline`, or `market` |
| `start_time` | when it stops being replaceable |
| `pick_rank` | 1 = strongest in that fixture; the legacy default reads 1–2, a market/scope filter reads all ranks |
| `result`, `actual`, `graded_at` | `win` / `loss` / `push` / `void` after the match |

`market` is the exact engine key (`home_rebounds`, `first_half_total`); the
legacy filters on `market_group` + `scope`, not on the key. That separation is
what lets “rebounds” contain a combined question and two team questions without
the frontend learning every combination. The migration labels old rows; it
never changes their numbers.

### `board_markets` — the complete matchup snapshot

One row per date × sport × fixture × exact market, including rows below the
qualifying bar. This is what the card view reads: a selected market can show
home, away and combined together even when one of them was not strong enough
to become a ranked pick. It is written before kickoff and never backfilled.

| Column | Notes |
|---|---|
| `date`, `sport`, `game_key`, `market` | UNIQUE together |
| `game_label`, `home_team`, `away_team`, `league` | card header |
| `market_group`, `scope`, `market_label` | grouping and row label |
| `side`, `line`, `projection` | the answer the card displays |
| `probability`, `sample`, `line_source` | measured support and line provenance |
| `qualified` | 1 when the row also exists in `board_picks` |
| `start_time` | when the snapshot stops being replaceable |
| `result`, `actual`, `graded_at` | `win` / `loss` / `push` / `void` after the match |

The card view groups by fixture + `market_group` and shows the rows in
home → away → combined order (period and football markets have a combined row
only). The default view is the curated Board: a fixture appears only when at
least one row cleared the measured bar, and only its strongest family is shown.
The strongest row is marked `selected` and carries a `reason` built from the
stored line, projection, sample, line source and measured baseline comparison.
Selecting a market returns every stored fixture for that family, including rows
below the bar, marked as below-bar rather than silently promoted.

### `team_aliases` — one exact name mapping, learned once

| Column | Notes |
|---|---|
| `id` | |
| `provider`, `provider_name` | UNIQUE together; the provider's exact spelling |
| `team` | NOT NULL; must equal a team already in `matches` |
| `created_at` | |

Saving a mapping does two things in one request: it re-resolves every held
fixture with that provider name, then runs the normal auto-publish gate over the
window. A spelling fix therefore puts the match on the dashboard immediately —
there is no second "publish" step.

### `fixture_settings` and `app_settings`

`fixture_settings` is one row per league (`league` PK, `auto_publish`, `updated_at`);
a missing row means **on**. `app_settings` is a small key/value store for
`last_fixture_fetch` and `fixture_lookahead_days`.

### Why the migration exists

The original `matches` table had `home_goals`/`away_goals` as `NOT NULL`, so a
scheduled fixture could not be stored and there was no way to tell scheduled from
played. It also had no `source`, and `manual_stats` / `published_matches` were
never created by any script. The migration adds all of it and rebuilds the table
with the identity index when needed.

A full rebuild is lossless:

```powershell
.venv\Scripts\python.exe scripts\migrate_schema.py
.venv\Scripts\python.exe scripts\import_data.py
.venv\Scripts\python.exe scripts\export_data.py --restore
```

---

## 7. Markets and lines

Markets live in one place: `STAT_COLUMNS` in `scripts/stats.py`. Add a stat there
and every averaging function, the API and the UI pick it up. There are **15**.

| Market | Source | Default line |
|---|---|---|
| `goals` | CSV | 2.5 |
| `shots` | CSV | 24.5 |
| `shots_on_target` | CSV | 8.5 |
| `fouls` | CSV | 23.5 |
| `corners` | CSV | 9.5 |
| `yellow_cards` | CSV | 4.5 |
| `red_cards` | CSV (degenerate) | 0.5 |
| `tackles` | manual only | 35.5 |
| `throw_ins` | manual only | 40.5 |
| `aerial_duels` | manual only | 30.5 |
| `offsides` | manual only | 4.5 |
| `interceptions` | manual only | 18.5 |
| `goal_kicks` | manual only | 16.5 |
| `saves` | manual only | 6.5 |
| `substitutions` | manual only | 9.5 |

`MANUAL_STAT_COLUMNS` is the 16 columns (8 markets × home/away) the admin can
type. `PROFILE_STATS` is exactly the eight manual-only markets — so a profile
*structurally cannot* supply a CSV-backed market.

The eight manual markets start completely empty and hold only what is typed. An
empty market returns `weighted_avg: null` with "No usable data" and the note that
the engine found no evidence before kickoff. Only the **line** falls back to
`DEFAULT_MARKET_LINES`; no number is invented from a league prior.

The engine needs **both** teams to have a value for a market. One side alone still
reports nothing, because the estimator works on a match total (home expected +
away expected).

### Lines are always x.5

A totals line of `3.0` is invalid: a match with exactly 3 goals is a **push**.
A scorer that tests `actual > line` silently books every push as an UNDER.

Two safeguards, both tested:

1. `snap_to_half_line` lands the fair line on an x.5 boundary, ties going to the
   **higher** line.
2. Any match still exactly on the line is detected and dropped from **every**
   predictor, model and baselines alike, with the count reported.

Measured effect on Premier League 2025-26 goals O2.5: the base rate moved from a
reported **43.5% OVER** to **54.8%**, and "always under" from 56.5% to 45.2%. The
bug had inverted the finding.

---

## 8. The engine, step by step

`scripts/stats.py`. Every query accepts `before_date` and `exclude_match_id`, and
uses `date < ?` — strictly before. That is what stops a match seeing its own
result or the future.

### 8.1 Inputs the engine gathers

For one market of one fixture:

- the home team's **season home** matches before the cutoff,
- the away team's **season away** matches before the cutoff,
- both teams' **last 5** matches before the cutoff,
- the **head-to-head** meetings before the cutoff,
- the **league reference** for that market, before the cutoff,
- the two teams' **profiles**, if any, dated on or before the cutoff.

### 8.2 The default estimator — `method="shrunk"`

```
team_total = home team's home average + away team's away average
value      = (n * team_total + k * league_reference) / (n + k)
             k = 10, n = the SMALLER of the two teams' sample sizes
```

`n` is the smaller sample on purpose: one well-covered team must not mask a
barely-covered opponent.

Each source reports `reliability = n / (n + k)` — how much of it survives
shrinkage. A source with 5 matches at k=10 is one third itself and two thirds
league average. It is deliberately **not** a set of percentages adding to 100,
because under shrinkage the sources are not competing for a fixed share.

Recent form and head-to-head are still computed and returned, but under `shrunk`
they do **not vote**: on the pooled backtest they added variance rather than
accuracy. They are shown as evidence instead of silently moving the number.

If season form is missing the estimator falls back in a fixed order (recent form,
then league reference, then H2H), and `method_note` always says which route was
taken. A number is never returned without saying what produced it.

### 8.3 The league reference

`get_league_reference` shrinks the season average toward a long-run prior:

```
value = (n * season_average + k * prior) / (n + k)      k = 10
```

The prior is the same league's average over **all earlier seasons** once that pool
reaches 50 matches, and the market's conventional line before then.

Every response reports the basis:

- `season_shrunk` — the season's own data, shrunk toward the prior;
- `league_long_run` — the long-run pool was used as the prior;
- `market_default` — only `DEFAULT_MARKET_LINES` was available.

Without this, the 2025-26 opener produced a goals fair line of **6.5** from almost
no data.

### 8.4 Lines

- `line` supplied by the caller → `line_source = "user"`.
- no line → `suggest_line` derives a **fair line** from the shrunk league
  reference, snapped to x.5 → `line_source = "fair"`.
- no reference at all → `DEFAULT_MARKET_LINES` → `line_source = "default"`.

The UI shows which one it used.

### 8.5 Evidence and confidence

- `evidence_points` counts **distinct matches** behind the number; a match in both
  the form set and the H2H set is counted once.
- `low_confidence` is set when fewer than **3** distinct matches back the figure.
- `match_ids` (the whole window) are retained for leakage checks in
  `verify_engine.py` sections 1 and 2.

### 8.6 The legacy blend — `method="blend"`

Still present, still scored head-to-head:

```
season home/away form (45%) + last-5 recent form (35%) + head-to-head (20%)
```

`calculate_weighted_market` drops any source with no data and rescales the
remaining weights so they still sum to 1 — a missing H2H does not drag the number
toward zero. It is no longer the default: `shrunk` replaced it because it measured
better, not because it reads better.

### 8.7 Team profiles

Typed per-team per-game averages for the eight CSV-less markets.

- **Measured data always wins.** A profile only fills a stat whose counted average
  is `None`, so it can never overwrite a counted number.
- **It is never evidence.** It contributes `PROFILE_WEIGHT = 3.0` pseudo-matches
  but **no match ids**, and evidence points are counted from match ids. With two
  profiles and nothing counted, the app says "Based on 0 distinct match(es)".
- **The date is enforced.** A profile is used only for a fixture on or after its
  `as_of`; a typed average can never rewrite the past.
- **No league average, no shrink.** If a profile contributed *and* the reference
  basis is `market_default`, the reference is dropped (`league_reference = None`)
  — a real typed number is not shrunk toward a hardcoded guess, and the UI says
  "the team data alone — no league average to shrink toward" instead of inventing
  a reliability percentage.

### 8.8 The analysis object the API returns

`shape_analysis` emits:

```
market, line, line_source, line_reference,
method, method_note, shrink_k, league_reference,
weighted_avg, supported, evidence_points, low_confidence,
sources: {
  home_away:   {label, value, reliability, sample},
  recent_form: {label, value, reliability, sample},
  h2h:         {label, value, reliability, sample}
},
sources_used, note,
has_league_reference,          # was a league average actually available?
profile_used, profile_as_of, profile_teams, profile_note,
season_home_matches, season_away_matches, h2h_matches
```

`has_league_reference` exists so the UI never prints "rest league average" when no
league average exists. `profile_used` relabels the home/away row as
"Home / Away average (partly typed, not measured)".

### 8.9 Red cards

`red_cards` is degenerate: over 95% of matches fall on one side of 0.5. The engine
reports the measured frequency and the number, but no skill claim is made and the
backtester excludes it from `best_baseline`.

---

## 9. Backtesting: how the model was judged

`scripts/backtester.py` is cutoff-aware: every prediction uses only matches
strictly before the fixture, at a **fair per-match line**, and drops pushes for
every predictor.

Predictors scored side by side:

| Predictor | What it is |
|---|---|
| `model_engine` | the live engine (`shrunk`, k=10) |
| `legacy_blend_45_35_20` | the old 45/35/20 blend |
| `experiment_shrunk` | a separate shrinkage experiment |
| `baseline_homeaway` | simple home/away season averages, no shrinkage |
| `baseline_recent` | last-5 recent form only |
| `baseline_h2h` | head-to-head only |
| `baseline_league_avg` | the league average (degenerate in fair-line mode) |
| `baseline_always_over` | reference: always over |
| `baseline_always_under` | reference: always under |

Method:

- **Wilson 95% confidence intervals** on every hit rate. A gap smaller than the
  interval is noise, and the report says so.
- **Paired McNemar z** against each baseline on the same matches — far more
  sensitive than comparing two separate hit rates.
- **MAE and bias** of the predicted total against the actual total, because a 0/1
  hit rate throws away how close the prediction was.
- `baseline_always_over` / `always_under` are **reference** predictors: excluded
  from `best_baseline` and from MAE.
- Any predictor that answers the same way **>95%** of the time is flagged
  degenerate and excluded from `best_baseline` — it is the base rate in disguise.

### The results (all six leagues, ~4,280 scored matches per market)

| market | engine | legacy 45/35/20 | calls vs legacy | error vs legacy | engine MAE |
|---|---|---|---|---|---|
| goals | 56.8% (55.3–58.3) | 55.8% | no clear difference | **more accurate** | 1.328 |
| shots | 53.0% (51.5–54.5) | 53.8% | no clear difference | **more accurate** | 4.776 |
| shots on target | 55.8% (54.3–57.3) | 56.0% | no clear difference | **more accurate** | 2.478 |
| corners | 52.0% (50.5–53.5) | 52.3% | no clear difference | **more accurate** | 2.761 |
| fouls | 58.1% (56.6–59.5) | **59.4%** | **behind (clear)** | no clear difference | 4.292 |
| yellow cards | 56.8% (55.3–58.3) | 55.6% | no clear difference | **more accurate** | — |
| red cards | 83.9% | 81.5% | **ahead (clear)** | no clear difference | 0.311 |

Against a plain baseline the picture is less flattering, and that is the honest
reading:

- **goals** — level with `baseline_homeaway` (56.1%).
- **shots on target** — level with `baseline_homeaway` (55.6%), clearly ahead of a
  plain league average.
- **yellow cards** — clearly ahead of `baseline_homeaway` (55.3%). The only market
  where the engine beats every simple baseline on calls.
- **corners** — the majority class (51.9%) sits inside the interval: no measurable
  skill. Its predicted total is also **less** accurate than a league average
  (MAE 2.761 vs 2.717).
- **shots** — same weakness on error: 4.776 vs 4.668 (clear, +0.107).
- **fouls** — ahead of a league average on calls, but **clearly behind the legacy
  blend** (345 calls vs 404). This is the one hit-rate regression, and the reason
  `--shrink-k` is on the work list: k=10 was inherited, never tuned.

Conclusion recorded in the README: the engine is better at predicting the
**total** (clearly closer than the legacy blend in five of six non-degenerate
markets), not at beating a trivial rule on the **over/under call** (only yellow
cards clears its best simple baseline). Do not treat the verdict as a selection
tool.

---

## 10. The API - 64 application routes

All defined in `backend/main.py`. JSON in, JSON out. Validation through Pydantic.

In production every `/admin/*` route and the NBA publish/refresh writes require
`X-Admin-Token` (or `Authorization: Bearer ...`). Public reads and `/health` stay
open. `/docs` and `/openapi.json` are disabled when `APP_ENV=production`.

### Reference / public

| Method | Path | What it does |
|---|---|---|
| GET | `/api` | service banner |
| GET | `/favicon.ico` | icon |
| GET | `/health` | status + `historical_matches` + `published_matches` |
| GET | `/stats` | the 15 markets and their default lines |
| GET | `/leagues` | the 6 leagues actually present |
| GET | `/markets/{market}/base-rate` | measured frequency and sample size for a market (optionally `league`, `season`, `line`, `before_date`) |
| GET | `/teams/find?q=` | team search across home **and** away names |
| GET | `/matches/today` | today's historical matches |
| GET | `/matches/date/{date}` | a day's historical matches |
| GET | `/fixtures?from=` | upcoming fixtures |
| GET | `/matches/{id}` | one historical match |
| GET | `/matches/{id}/analysis` | analysis for a played match (cutoff = its date) |
| POST | `/analyze` | ad-hoc analysis from team names, date, markets, lines, weights |

### Published (the user view)

| Method | Path | What it does |
|---|---|---|
| GET | `/published/matches?date=` | published fixtures for a day (default today), `status != 'hidden'` |
| GET | `/published/dates` | every day that has a published fixture, with counts |
| GET | `/published/matches/{id}` | one published fixture |
| GET | `/published/matches/{id}/analysis` | analysis from team names + date, cutoff = fixture date |
| GET | `/published/matches/{id}/evidence?stat=` | the raw season/recent/H2H rows behind an analysis |

### The board

| Method | Path | What it does |
|---|---|---|
| GET | `/board` | the day's cards, read from the frozen snapshot |

The user-facing view uses `grouped=1` with `date` (`YYYY-MM-DD`), `sport`
(`all` / `football` / `nba`), `limit` (1–50), `offset`, `market` (a
`market_group`) and `league` (an exact league label). A grouped response
returns `cards`, `total` (curated card count), `stored_rows` (every stored
row for the date, before the default selection), `row_total` (lines across the
shown cards),
`settled_lines`, `won_lines`, `lost_lines`, `hit_rate`, `has_more`, `markets`
(market families stored for the date, with a game count), `leagues`,
`accuracy` and `history`.

Each card is one fixture + one market family. It carries `home_team`,
`away_team`, `league`, `start_time`, `market_group`, `market_label`, `rows`
(home → away → combined, or combined only for period/football markets),
`qualified`, `selected` (the chosen row and its recorded `reason`),
`settled_lines`, `won_lines` and `lost_lines`. Each row carries
`side`, `line`, `projection`, `probability`, `line_source`, `qualified`,
`actual`, `result`, `status` (`HIT` / `MISS` / `PUSH` / `VOID`) and its
measured `n` / MAE / naive baseline.

The default card view is the selection layer: a fixture appears only when at
least one row cleared the measured bar, and only its strongest family is shown.
The chosen row is marked `selected` and carries a `reason` derived from the
frozen line, projection, sample, line source and baseline comparison. Selecting
a market returns every stored fixture for that family, including rows below
the bar, so the card can show the complete home/away/combined answer. This is a
read: it never calls the engine, recalculates a line or backfills a date after
the result is known.

The legacy response (without `grouped=1`) still accepts `scope`, `team`,
`source`, `confidence`, `status` and `sort`, and still exposes the
`board_picks` rank contract. The default legacy view reads only
`pick_rank <= 2`; selecting a market or scope reads every stored rank.
`unfiltered` is the default day count (ranks 1–2) and `market_total` is the
all-rank count for that market.

Filtering is a read: it never calls the engine and never writes a pick.
`source=board` and `source=agree` return an empty page with a `source_note` until
a context model has been measured — the core model is not relabelled to fill
them.

A pick carries its projection, its line, `line_source` (`league` / `market` /
`league_average` / `team_baseline`), the market group and scope, the measured
probability, the sample it was measured on, and the holdout calibration bucket.
It does not carry a stake, an odds figure, or a recommendation to bet.

### Admin — predictions

| Method | Path | What it does |
|---|---|---|
| GET | `/admin/predictions/status` | measured markets, sample sizes, MAE vs naive, upcoming picks, matchup lines, settled record |
| POST | `/admin/predictions/rebuild` | re-measure every market, then rebuild the board (background, about 10 minutes) |

### Admin — published fixtures

| Method | Path | What it does |
|---|---|---|
| GET | `/admin/published/matches?date=` | all published rows for a day, including hidden |
| POST | `/admin/published/matches` | publish; **409** on the same date + both teams |
| PUT | `/admin/published/matches/{id}` | edit / hide / reorder |
| DELETE | `/admin/published/matches/{id}` | remove |

### Admin — automatic fixtures

| Method | Path | What it does |
|---|---|---|
| GET | `/admin/fixture-settings` | provider, whether a token is set, lookahead, last fetch, per-league auto-publish |
| PUT | `/admin/fixture-settings` | switch one league's auto-publish on/off |
| POST | `/admin/fixtures/fetch` | fetch the next N days, ingest, then auto-publish what passes the gate; **400** without a token |
| GET | `/admin/available-fixtures?from=&to=` | candidates in a window, with mapping state and per-date evidence |
| POST | `/admin/available-fixtures/publish` | publish selected ids (idempotent, duplicate-safe) |
| POST | `/admin/available-fixtures/publish-all` | publish every eligible fixture in the window |
| GET | `/admin/team-aliases` | saved provider→team mappings plus every name still blocking a fixture |
| PUT | `/admin/team-aliases` | save one exact mapping, re-resolve its fixtures, then auto-publish them |
| DELETE | `/admin/team-aliases/{id}` | forget one mapping |

### Admin — historical matches and manual stats

| Method | Path | What it does |
|---|---|---|
| POST | `/admin/matches` | hand-add a match (`source='manual'`) |
| DELETE | `/admin/matches/{id}` | delete a hand-added match |
| GET | `/admin/matches/missing-manual?league=&since=` | completed matches with no manual entry (the weekly worklist) |
| GET | `/admin/matches/{id}/manual-stats` | one match's typed counts |
| PUT | `/admin/matches/{id}/manual-stats` | save typed counts |
| DELETE | `/admin/matches/{id}/manual-stats` | clear them |

### Admin — team profiles

| Method | Path | What it does |
|---|---|---|
| GET | `/admin/team-profiles?league=&season=&as_of=&q=` | teams, their match counts, and the current profile |
| PUT | `/admin/team-profiles` | upsert one `(league, season, team)`; **400** if every market is blank |
| DELETE | `/admin/team-profiles/{id}` | clear one team's typed averages |

### Admin — data operations

| Method | Path | What it does |
|---|---|---|
| GET | `/admin/dashboard?date=` | counts for the admin landing view |
| GET | `/admin/data` | freshness, per-league/season counts, market coverage, hand-entered work, last export |
| POST | `/admin/download-csvs` | fetch the current season for all mapped leagues, then import |
| POST | `/admin/import-csvs` | re-read `data/raw/` and refresh the dataset |
| POST | `/admin/export` | write the database to `data/export/` |

### NBA

Twelve operations, all under `/nba` and `/admin/nba`. They read only `nba_*`
tables; no football table is touched.

| Method | Path | What it does |
|---|---|---|
| GET | `/nba/status` | counts, games with line scores, last fetch/publish, current season |
| GET | `/nba/teams` | the 30 teams |
| GET | `/nba/teams/{team_id}/roster` | official roster, latest production per player, availability snapshot |
| GET | `/nba/published?date=` | published games for a day; with no date, today → next matchday → last |
| GET | `/nba/published/dates` | every day with a published game, with counts |
| GET | `/nba/fixtures?days=14` | upcoming fixtures with a `published_id` when shown |
| GET | `/nba/analysis/{game_id}` | the full bundle: projection, range, ratings, markets, baselines, evidence |
| POST | `/nba/publish/{game_id}` | publish (idempotent); optional body `{game_total, home_team_total, away_team_total}` |
| DELETE | `/nba/publish/{game_id}` | un-publish |
| POST | `/nba/refresh` | background thread: current-season history + schedule + auto-publish |
| GET | `/admin/nba/people/status` | roster/availability counts, recent sync jobs, last game pulls |
| POST | `/admin/nba/people/refresh` | background: rosters, player production, availability |

`/nba/analysis` accepts the same three lines as query parameters; a stored line
wins over the league-reference line and is labelled `"market"` in the response.

`/nba/teams/{team_id}/roster` joins production on the **player**, not the team,
so a summer signing is shown with the evidence he earned elsewhere.
`availability.minutes_unavailable` is descriptive: the injury feed has no
history, so it cannot be measured and is never an input to a projection.

### Request models

- `ManualStatsIn` — 16 nullable floats.
- `PublishedMatchIn` — `league`, `home_team`, `away_team`, `date`, `kickoff?`,
  `season?`, `markets?`, `status='published'`; date validated as ISO, teams not
  blank.
- `PublishedMatchUpdate` — all fields optional.
- `MatchIn` — date, league, teams, season?, status?, goals?.
- `TeamProfileIn` — league, team, season?, as_of?, eight values `ge=0, le=100`
  (the `le` is a typo guard, not a real limit), note?.
- `DownloadIn` — `season?`, `import_after=True`.
- `AnalyzeIn` — teams, date?, league?, season?, markets?, lines?, weights?,
  `recent_n=5`.
- `FetchFixturesIn` — `lookahead_days=14` (1–30), `publish=True`.
- `PublishFixturesIn` — `ids` (at least one).
- `FixtureSettingsIn` — `league`, `auto_publish`.
- `TeamAliasIn` — `provider_name`, `team` (both 1–160 chars).

### Error conventions

- **409** — publishing a duplicate (same date + both teams).
- **404** — unknown published id / match id / alias id.
- **400** — unknown stat or market; weights that do not sum to 1.0; an unreadable
  season label; an all-blank team profile; a missing or rejected
  `FOOTBALL_DATA_TOKEN`; an alias pointing at a team that is not in `matches`;
  a malformed `from`/`to` date.
- **502** — the fixture provider itself failed (network, HTTP error, rate limit)
  after the request was understood.
- **422** — Pydantic validation (bad date, blank team, out-of-range profile value).

---

## 11. The frontend

One file, `frontend/index.html`, served at `/app/`. No framework, no build step.

### Views

- **Dashboard** (`view-dashboard`) — published fixtures for one day, league chips,
  and a date bar.
- **Board** (`view-board`) - the curated selection layer over the complete stored
  matchup snapshot. The default shows one strongest market family per fixture and
  only when a row cleared the measured bar; a market filter shows every stored
  fixture for that family, including below-bar rows. The chosen row is marked
  **Board pick** and a **Why this pick** block records the sample, projection,
  line source and baseline comparison. The only filters are sport, date, league
  and market; home, away and combined lines sit together on the card. Each row
  shows the side and line, the model projection, the final actual, the measured
  probability and an automatic `HIT` / `MISS` (or `PUSH` / `VOID`) once the game
  is settled. Football cards are combined-only because the frozen football
  engine has no separate home/away model. Each sport keeps its own saved filter
  set on the device; a saved market is never silently reset when a date has no
  stored lines.
- **Season** (`view-season`) — the whole stored calendar for one league-season:
  results, the near window, and "analysis opens" beyond it.
- **Match centre** (`view-match`) — the fixture header and three tabs: **Markets**
  (default), **Form**, **H2H**.
- **NBA dashboard** (`view-nba`) and **NBA game** (`view-nba-match`). The game
  page has its own market filters (Points, Rebounds, Assists, Turnovers, Steals,
  Blocks, 2-pointers, 3-pointers, Fouls, plus All) over the complete 29-market
  analysis; filtering is local and does not call the model again.
- **Admin** (`view-admin`) — seven tabs: **Fixtures**, **Today**, **Publish**,
  **Stats**, **Profiles**, **Data**, **Overview**.

### Design rules encoded in the CSS

- Dark, muted palette (`#0D1210` background); one accent colour; no gradients.
- No animation beyond a subtle skeleton shimmer.
- Stat names are uppercase via `text-transform`.
- Numbers are shown with their sample size, never alone.
- No odds tiles, no stake, no betting language.
- The board's inline percentage is the one number allowed to look like a score:
  it is a measured frequency with its sample size, not a confidence score.
- Loading shows a skeleton (`.sk`), not a spinner.

### Dashboard

`loadDashboard(iso?)`:

1. With no argument: takes today's ISO date, fetches
   `/published/matches?date=today`, and if empty reads `/published/dates` to fall
   back to the **nearest day that has something** — the next published day if one
   is ahead, otherwise the most recent one behind. Only this start-at-today path
   falls back; an explicit date is shown as asked, empty or not.
2. With an argument: loads exactly that date.
3. Fetches `/published/dates` alongside the matches and renders a **date strip** of
   nearby published matchdays plus today; tapping a chip calls
   `loadDashboard(date)` and tapping the date in the header calls it with no
   argument (home).
4. Renders the cards, the league chips, and — when the automatic path had to move
   off today — a note: *"Nothing is published for today. Showing the next
   matchday, Wed 21 Oct."* The header shows the day actually on screen.

The header arrows step one calendar day from `dashShown`. Two quick taps chain
correctly because the handler advances `dashShown` before fetching, and
`dashSeq` makes any older in-flight response a no-op so it cannot overwrite the
newer day.

This exists because a Saturday fixture used to become invisible on Sunday: an
empty screen is indistinguishable from "there is nothing", which is a lie by
omission.

### Match centre

- `renderMarketChips()` lists the markets allowed for the fixture.
- `renderMarket()` shows, for one market:
  - the expected total and the line, with `line_source`;
  - the three source rows, each with value, reliability and sample;
  - the evidence line: *"Based on N distinct match(es)"*;
  - a low-confidence warning under 3 matches;
  - the profile disclosure when a typed average contributed.
- The source rows say exactly what they are:
  - counted: `"N% own data, rest league average"`;
  - typed: `"typed average, no match count"`;
  - no reference: `"the team data alone — no league average to shrink toward"`.
- No data: *"No usable data — The engine found no evidence for this market before
  kickoff."*
- The back button clears the selected match, the market chips and the panel; the
  chip handler also guards against a null selection (a real crash fixed).

### Admin tabs

| Tab | What it does |
|---|---|
| **Today** | today's published fixtures with Edit / Hide / Delete |
| **Publish** | form (league, teams, date, kickoff, markets), duplicate warning, and the list |
| **Stats** | completed matches with no manual entry, filtered by league and 7/14/30 days; the form shows home and away side by side for all eight markets |
| **Profiles** | teams × eight markets, filters (league, season, as_of, search), per-row Clear, "Save all filled rows", an honest "N of M teams filled in" counter |
| **Data** | Last download / Last refresh cards, freshness, market coverage, hand-entered work, export |
| **Overview** | dataset counts, the league/season breakdown, and the board's measured markets, pick count, settled record and **Rebuild** button |

`renderAdminData()` is where **Download latest CSVs** and **Refresh from CSVs**
live. A download shows how many files arrived, how many were refused, and the log
of every league.

### PWA

- `manifest.json`: standalone, portrait, `#0D1210`, the icon, a description that
  says it is a research tool and not betting advice.
- `sw.js`: a two-line service worker that takes control immediately. There is no
  offline cache — deliberately, so a stale UI can never show stale data.

---

## 12. The data pipeline

```
provider CSV ──download──> data/raw/<league>/<season>.csv
                              │
                              │ import (upsert on natural key)
                              ▼
                        data/sports_analyzer.db
                              │                    │
                    ┌─────────┘                    └──────────┐
                    ▼                                         ▼
             published_matches                        data/export/*.csv
             (the user view)                          (the backup)
```

**Download is not import. Import is not publish. Publish is not evidence.**

### 12.1 Download — `scripts/fetch_csvs.py`

```
GET https://www.football-data.co.uk/mmz4281/<code>/<div>.csv
```

- season code: `2026-27` → `2627`.
- divisions: `premier-league`=E0, `bundesliga`=D1, `la-liga`=SP1, `serie-a`=I1,
  `ligue-1`=F1, `eredivisie`=N1.
- target: `data/raw/<league>/<season>.csv`.
- validation before writing: at least 200 bytes, header must contain `Div`,
  `Date`, `HomeTeam`, `AwayTeam`, and at least one match row.
- a response that fails validation is **refused** and the existing file is left
  alone. This matters: an outage returns an HTML error page, and writing that over
  a 380-row season would make the next import delete the whole season.
- only the **current season** is fetched by the button. Older seasons are never
  touched (use `--season` explicitly).

CLI:

```powershell
.venv\Scripts\python.exe scripts\fetch_csvs.py --season 2026-27
.venv\Scripts\python.exe scripts\fetch_csvs.py --season 2026-27 --import
```

### 12.2 Import — `scripts/import_data.py`

- Files are discovered as `data/raw/<folder>/<file>.csv`.
- **The season is the file name**, validated and canonicalised:
  `2026_27.csv` / `2026/2027.csv` → `2026-27`; a non-season stem such as `E0.csv`
  is skipped and the run exits 1.
- **The league is the folder**, never the `Div` column. A folder not in
  `LEAGUE_NAMES` imports under a derived name and warns. A `Div` that disagrees
  with `LEAGUE_DIV_CODES` is warned about but the folder still wins.
- CSV columns map explicitly (`FTHG`→`home_goals`, `HS`→`home_shots`,
  `HST`→`home_shots_on_target`, `HF`→`home_fouls`, `HC`→`home_corners`,
  `HY`→`home_yellow_cards`, `HR`→`home_red_cards`, the odds columns, …).
  Required: `Date`, `HomeTeam`, `AwayTeam`, `FTHG`, `FTAG`.
- Dates are `DD/MM/YYYY` and stored as ISO.
- Encoding `utf-8-sig` (the provider's BOM).
- Upsert on `(league, season, date, home_team, away_team)`, team names compared
  case-insensitively after stripping.
- `source` is deliberately **excluded from the UPDATE SET**: a hand-created row
  stays `'manual'` even after a CSV carries it, so a later file that drops it
  cannot silently delete it.
- Rows the file no longer lists are removed **only if `source='csv'`**; manual
  rows are kept and counted as protected. Orphaned manual stats are reported.
- Ids are preserved, so `manual_stats.match_id` keeps pointing at the right match.

CLI: `python scripts/import_data.py` (whole tree). Exit code 1 only when a file
was skipped/refused.

### 12.3 Publish — `POST /admin/published/matches`

Writes a row to `published_matches`. It adds nothing to `matches` and learns
nothing. Duplicate protection: same `date` + `home_team` + `away_team` → **409**.

### 12.4 Backup — `scripts/export_data.py`

```
data/export/matches/<league>/<season>.csv
data/export/manual_stats.csv
data/export/published_matches.csv
data/export/team_profiles.csv
data/export/team_aliases.csv
data/export/fixture_settings.csv
data/export/manifest.json
```

`--verify` compares the database against the files and prints `VERIFY OK`; it
fails if **any** of the admin tables is missing, not just `manual_stats`.
`--restore` imports the files, matching on natural keys and **remapping ids**
when it has to, rewriting `manual_stats.match_id` through the same map — so a
restored database reconnects typed stats to the right matches. Profiles, aliases
and per-league switches are restored by their natural keys with `id` dropped.

`manifest.json` records the export time and row counts; Admin → Data reads it to
show when the last backup ran.

---

## 13. Tests — what guards what

### `scripts/verify_engine.py` — 10 sections

| # | Guards |
|---|---|
| 1 | **Leakage**: the cutoff must exclude the match and everything after it |
| 2 | **Regression pin**: a known value must not drift (does not prove correctness; section 1 does) |
| 3 | Missing data must not crash and must report no support |
| 4 | `find_teams` must search away teams too |
| 5 | League average and fair line are cutoff-aware |
| 6 | `snap_to_half_line` never returns a whole number |
| 7 | Wilson interval and McNemar z pinned against hand-computed values |
| 8 | A CSV refresh must not delete hand-entered matches (throwaway copy) |
| 9 | Hand-entered counts must reach the engine, and only that stat (throwaway copy) |
| 10 | A typed team profile must not outrank counted data, be used before its date, reach a CSV-backed market, or pass itself off as evidence (throwaway copy) |

Sections 8–10 write, so they run on throwaway copies. The real database is never
touched.

### `scripts/smoke_test.py` — 64 checks (68 with `--with-download`)

Runs against a live server. Covers health, markets, leagues, team search, publish
+ duplicate 409, the dashboard day list, analysis (cutoff, fair line, line
override, sources), evidence rows, the profile CRUD and its effect on analysis,
the blank-profile 400, cleanup, unknown ids, fixture settings, the available
fixtures window, the alias round trip, a token-less fetch being a clear 400, the
season calendar (whole season, results, the 14-day window, past-season fallback,
unknown league 404), and the read-only NBA routes (status, published, dates,
fixtures, analysis, x.5 lines, verdict consistency, unknown game 404).

The fixture checks are deliberately read-only plus a self-cleaning alias. There is
**no** fetch and **no** publish-all in the smoke test: on a real installation
those would publish live fixtures as a side effect of running a test.

`--with-download` adds a real network download and asserts every league arrived,
that the import did not run, and that an unreadable season is rejected with 400.
Opt-in because it uses the network and rewrites `data/raw/`.

### `scripts/verify_fixtures.py` — 36 checks, offline

Runs against a throwaway **copy** of the database and needs no network. Built
rows stand in for `fetch_competition` output. It pins the things that would be
silent failures:

- an unknown provider name is **never** guessed (suggestions only);
- accented and unaccented spellings resolve identically;
- ingest never changes the `matches` row count;
- only mapped, scheduled fixtures auto-publish;
- postponed and unmapped fixtures stay off the dashboard;
- published rows carry canonical names, not provider names;
- running auto-publish twice creates no duplicate;
- a saved alias re-resolves its held fixture **and auto-publishes it**;
- publishing it again stays idempotent;
- an alias pointing at a non-existent team is refused;
- re-ingesting after a refresh keeps the mapping and the `published_id`;
- the full season is stored once per provider id, with every team mapped;
- a fixture beyond the window is `outside` with `window_opens = date − 14 days`;
- calendar ingest publishes nothing and never rewrites a linked result;
- a past season with no calendar falls back to `matches`.

### `scripts/verify_nba.py` - 57 checks, offline

Runs against a throwaway **copy** of the database, so a destructive check cannot
damage the live data. It pins:

- every NBA table exists and `ensure_schema` is repeatable;
- exactly two team rows per game, no orphan team, and the home/away flags agree
  with the game;
- the two team rows' points equal the game score;
- `possessions` matches `FGA + 0.44*FTA - OREB + TOV` on every row;
- a projection's season sample is **strictly** before the fixture, and the
  fixture's own row fails the cutoff;
- every line is x.5 and `supported` is exactly `projection > line`;
- baselines never exceed their sample and their rate matches the counts;
- all eight counting stats are projected for combined, home and away; their parts
  sum, stay in plausible ranges, and two-pointers equal field goals minus threes;
- team counting-stat lines use the team's own baseline, not the league's average;
- every board pick carries its `market_group`, `scope` and `pick_rank`; the
  legacy default view caps at rank 2, a market filter returns every stored rank,
  and the Board paginates honestly;
- the complete `board_markets` snapshot stores both teams, qualification and
  result; the grouped Board returns one default card per game, marks exactly one
  selected row, records a reason for it, excludes fixtures with no qualifying
  row from the default, a selected market shows home/away/combined together,
  and cards paginate honestly;
- the context view stays empty until a context model is measured;
- the split-half season labels (`2026-10-20` → `2026-27`);
- auto-publish is idempotent, reversible, refuses an unknown game, and changes
  **zero** football rows;
- every published game has two known teams, each with completed games;
- half totals are available exactly when line scores exist.

### `scripts/verify_predict.py` — 36 checks, offline

Runs against a throwaway **copy**. It pins the honesty of the percentage:

- `ensure_tables` is repeatable;
- the quantile grid is ordered and its cumulative probabilities never decrease;
- a bigger edge can never lower the probability;
- a probability is clamped to 0.05–0.95;
- a 120-match sample is pulled closer to 50% than a 2,000-match sample;
- a stored pick reproduces from its market's measured error;
- every pick clears the 55% bar, has n ≥ 120, and names its line source;
- every stored pick has a rank, the legacy default view caps at two ranks per
  fixture, and a market filter returns every stored rank for that market;
- football picks never use a manual-only market;
- the board is ranked by probability and respects its limit;
- a real result grades a win, a loss and a push correctly;
- a complete matchup line settles from the same actual and leaves its numbers
  untouched;
- an unplayed pick is never graded, and a graded pick is never graded twice;
- building the board publishes nothing;
- the snapshot has no stake, odds or price column.

### `scripts/backtest_nba.py` — walk-forward scoring, no network

For every completed game, it projects using only games strictly before it and
scores the error against the actual result and against a naive league-average
baseline. This is the NBA equivalent of §9, and the number that decides whether
the model is worth showing.

### Other checks

```powershell
.venv\Scripts\python.exe scripts\export_data.py --verify
.venv\Scripts\python.exe scripts\db_info.py
.venv\Scripts\python.exe scripts\explore.py
.venv\Scripts\python.exe scripts\migrate_schema.py
```

---

## 14. Current data state

Measured on the live database after the final tidy:

| Thing | Value |
|---|---|
| Historical matches | **4,542** |
| Leagues | 6 (Premier League, La Liga, Bundesliga, Serie A, Ligue 1, Eredivisie) |
| Seasons | 2024-25, 2025-26, 2026-27 |
| 2026-27 matches | 426 |
| Latest completed match | **2026-10-19** (one day behind today — the provider's own lag) |
| Published fixtures | **127** — the 2 manual ones plus **125** auto-published from football-data.org |
| Published matchdays | **13** (2026-10-18 → 2026-11-02) |
| Available fixtures | **125** in the 14-day window, all mapped and published |
| Team aliases | **6** — Espanyol, Deportivo, Fortuna Sittard, NEC, PSV, AZ |
| Auto-publish | on for all six leagues (a missing `fixture_settings` row means on) |
| Fixture token | configured; last fetch 2026-10-20 03:56 UTC |
| Manual stat rows | **0** |
| Team profiles | **0** |
| Raw CSV files | 18 (6 leagues × 3 seasons) |
| Board picks / matchup lines | **2,427 / 3,853** |
| Measured markets | **35** |
| NBA games / team-games | **3,690 / 7,380** |
| NBA published games | **107** |
| Orphaned records | 0 |
| Duplicate published fixtures | 0 |
| Export | verified, matches and published counts equal to the DB, including `board_picks` (2,427), `board_markets` (3,853) and `prediction_stats` (35) |

The eight manual-only markets therefore currently hold **0** matches of data, and
say so.

NBA, measured on the same day:

| Thing | Value |
|---|---|
| NBA games | **3,690** (2023-24, 2024-25, 2025-26) |
| NBA team-game rows | **7,380** |
| Games with quarter scores | **1,230** (2024-25; the 2025-26 provider gap is documented) |
| NBA fixtures | **1,200** regular-season games for 2026-27 |
| NBA published | **107** in the 14-day window (3 tonight) |
| NBA backtest | 14.86 vs 15.86 naive (2024-25), 15.35 vs 15.92 (2025-26) |
| NBA verify | **57/57** |
| NBA people verify | **32/32** (rosters, production, availability, sync log) |
| Official NBA roster | **518 players across 30 teams**, 2026-27 |
| Player production | **1,723 player-season rows** (2023-24 → 2025-26) |
| Availability | **90 entries** per snapshot, 100% matched to an NBA id |
| NBA backup | included in the export, restore round-trip tested |

The board, measured on the same day:

| Thing | Value |
|---|---|
| Measured football markets | **6** (every CSV-backed market), n = 387 each |
| Measured NBA markets | **29**: 5 points/period + 8 counting stats × 3 scopes |
| Board picks in the window | **2,427** (447 football + 1,980 NBA), every qualifying market; legacy default view ranks 1–2 per fixture; max 26 stored per fixture |
| Complete matchup lines | **3,853** (750 football + 3,103 NBA), including rows below the qualifying bar; max 29 per NBA game |
| Football markets beating a naive average | 3 of 6 (shots on target, fouls, yellow cards) |
| NBA game-total error | MAE **15.08** on 2,500 games, sigma 18.88 |
| NBA counting-stat error | all 8 combined and 16 team markets beat the reference line they are tested against (see 20.3a) |

---

## 15. Build history, beginning to end

The project was not built in one pass. Each phase fixed something the previous one
could not express. This is the order, and what each phase added.

### Phase 1 — the engine and the data

CSV import into SQLite, per-team home/away/recent/H2H averages, the 45/35/20
weighted blend, a cutoff parameter, and a terminal interface (`cli.py`). This is
where the statistical idea lived: a match total compared to a line, with the
evidence attached.

### Phase 2 — the first API and UI

FastAPI over the engine, a single-file frontend with a dashboard, a match centre
and an admin area. One process, one port. The first version analysed historical
matches by id.

### Phase 3 — published vs historical

The realisation that "what users may see" and "what the model learns from" are
different things. `published_matches` was split out, and a published fixture began
to be analysed from **team names + date**, not from a match id. That is why a
fixture can be analysed before either side has played.

### Phase 4 — schema migration and one season definition

`home_goals`/`away_goals` were `NOT NULL`, so scheduled fixtures could not be
stored; there was no `status` and no `source`. `migrate_schema.py` rebuilt the
table and created the side tables. `seasons.py` was added after a season label in
a different shape (`2026-2027`) inserted a **second copy** of a season instead of
refreshing it — measured: 50 matches became 100, then 150 under a third name.

### Phase 5 — the importer rework

The first importer deleted a season and re-inserted it. That preserved nothing:
SQLite assigned new ids, so every `manual_stats` row pointed at a match that no
longer existed. The rework made it an **id-preserving upsert** on
`(league, season, date, home_team, away_team)`, added `source`, and limited stale
deletion to CSV rows. It also pinned the league to the folder and the season to
the file name.

### Phase 6 — manual stats and the weekly worklist

The eight markets no free CSV carries were given a home: `manual_stats`, one row
per **match**, counts not percentages, with a worklist endpoint
(`/admin/matches/missing-manual`) and a Stats tab filtered by league and window.
The order "refresh first, then type" became explicit, because stats attach to a
match that must already exist.

### Phase 7 — the download

`fetch_csvs.py` was written and wired to **Admin → Data → Download latest CSVs**.
The first real run pulled six files and took the database from 4,429 to 4,485
matches: 56 new, 0 removed, freshness 30 days → 8 days behind. The validation
step (refuse anything that is not a season CSV) exists because a download that
writes an error page over a good file makes the *next import* delete a season.

### Phase 8 — team profiles

Typing two counts per fixture does not scale to eight markets. A per-team per-game
average is the same quantity the engine derives, so `team_profiles` was added as a
labelled substitute for the derivation — with the precedence rule, the `as_of`
cutoff, `PROFILE_WEIGHT = 3`, no match ids, and the eight-market restriction that
makes it structurally unable to touch a measured market.

### Phase 9 — the model evaluation

`backtester.py` gained Wilson intervals, a paired McNemar comparison, MAE/bias, the
push rule, the x.5 snapping, the degenerate-predictor exclusion and `--shrink-k`.
The pooled result made `shrunk` the default and demoted 45/35/20 to a legacy
method that is still scored. It also produced the uncomfortable results in section
9 — fouls behind the blend, corners with no skill — which are recorded rather than
hidden.

### Phase 10 — finishing

The dashboard date fallback (a fixture published for another day is never
invisible), five UI honesty bugs, the error-swallowing `loadTab`, the smoke test
growth from 20 to 33 checks, the README rewrite, and the tidy: two stray published
fixtures and the pre-migration `.bak` deleted, `__pycache__` removed, export
re-verified.

### Phase 11 — automatic fixtures

The daily-publishing problem. `available_fixtures`, `team_aliases`,
`fixture_settings` and `app_settings` were added, plus `scripts/fixtures.py`:

- a `football-data.org` provider (free plan: 12 competitions, all six of ours);
- a normaliser + curated exception list + **no fuzzy auto-accept**;
- an ingest that upserts by `(provider, provider_id)` and never resets
  `published_id`;
- auto-publish that requires mapping, `scheduled`, no duplicate and historical
  data on both sides;
- a 12-hour catch-up scheduler, because a laptop is not a server;
- **Admin → Fixtures** with per-league switches, evidence counts, select/publish,
  and a one-time mapping box.

Verified with 26 offline checks on a throwaway database copy and 8 live smoke
checks. The real network call is now covered too: the free key is in `.env`, and
the first run fetched 125 fixtures across the six leagues.

### Phase 12 — the whole season, and the 14-day rule

Publishing looks two weeks ahead, but users kept asking what happens after that.
`season_fixtures` was added as a read-only calendar (2,058 fixtures, 0 unmapped)
with `sync_calendar` on a weekly scheduler and a **Full season** screen. Played
fixtures open the cutoff-aware analysis; anything beyond the window says
"Opens 23 Oct" instead of showing a projection built on old form. Past seasons
fall back to `matches`, so no calendar is stored for them.

### Phase 13 — the board

Everything above answers "what does the model think about this match?". Nothing
answered "which of today's stats is most likely to land on the model's side?".
`scripts/predict.py` measures each market's walk-forward error, turns the
residual distribution into a probability, and stores the complete matchup
snapshot in `board_markets` before kickoff. Qualifying rows are also frozen in
`board_picks`; both are graded from the real result. The Board groups the
snapshot into one card per fixture + market family, showing home, away and
combined together. See §21.

---

## 16. Silent bugs found and fixed

These are recorded because every one of them produced a *plausible wrong number*
or silently deleted work — none of them crashed.

| Bug | What it did | Fix |
|---|---|---|
| Whole-number fair lines | `round(raw*2)/2` produced lines like 0.0, 3.0; every push booked as UNDER; goals base rate read 43.5% instead of 54.8% | `snap_to_half_line`; pushes dropped for every predictor |
| DELETE+INSERT import | new ids orphaned every `manual_stats` row | id-preserving upsert |
| Season label shapes | `2026-2027` created a second season; 50 → 100 → 150 matches | `seasons.py`, canonical on write and read |
| League from `Div` | a misplaced file silently filed under the wrong league | league from the folder; `Div` mismatch warning |
| Stale-row sweep | a CSV refresh deleted hand-entered matches | `source`; only `csv` rows swept |
| Early-season fair line | a goals line of 6.5 from almost no data | league reference shrunk toward a long-run prior |
| `baseline_league_avg` | answered OVER 351/376 times; looked like skill | degenerate detectors; excluded from `best_baseline` |
| Back button | left the chips rendered with `selectedMatch = null`; chip click threw | clear chips/panel; null guard |
| "Rest league average" | claimed a league average when none existed | `has_league_reference` |
| "6 matches" for typed data | a typed average displayed a fake sample size | "typed average, no match count" |
| Blank profile counted | two all-blank rows counted as filled | count values, not rows |
| Blank profile accepted | PUT saved nothing and returned OK | 400 "Nothing to save" |
| Download overwrite | an HTML error page could replace a season file | validate before writing; refuse |
| Dashboard today-only | fixtures for any other day were invisible | `/published/dates` + nearest-day fallback + day arrows and matchday strip |

---

## 17. Known limits

- **Authentication is a single admin token, not user accounts.** 64 application
  routes. Production requires `ADMIN_TOKEN`, disables `/docs`, and uses an empty
  CORS allow-list because the UI is same-origin. A local instance without the
  token is open by design and must stay on `127.0.0.1`.
- **No password reset or multi-user roles.** One token unlocks all admin work;
  rotate it in the host's environment settings if it is exposed.
- **No Champions League data.** football-data.co.uk covers domestic leagues only,
  and the fixture provider's Champions League competition has no history here, so
  publishing it would only ever say "no data".
- **The free fixture plan delays schedules.** Fixtures arrive, but a kickoff
  minute may lag; the provider's exact `Time` is only available in
  football-data.co.uk's `fixtures.csv` for the current matchweek.
- **No live scores.** In-play data is the provider's €12/month tier and is not
  needed: analysis is pre-match.
- **A CSV figure cannot be hand-corrected.** `manual_stats` holds only the eight
  manual markets; a wrong corner or foul count has no correction path.
- **The download is not an API.** Static season files only; the provider's own lag
  means a fresh download still ends a few days behind today.
- **A typed profile is not evidence.** It carries no match ids, cannot be
  backtested or calibrated, and produces no fair line.
- **The eight manual markets start empty** and hold only what is typed.
- **2026-27 is partial** (426 matches).
- **No audit trail** (`data_sources` / `data_imports` not implemented).
- **SQLite**, not PostgreSQL.
- **No AI explanation layer.** When it arrives it may only narrate numbers the
  engine already computed.
- **NBA availability cannot be priced yet.** The injury feed has no history,
  so it is displayed and stored (with a capture time), never used as a model
  input until enough history clears the same gates as every other factor.
- **No player-prop markets.** The NBA model is team-level; rosters inform
  context and role profiles, not individual lines.
- **No European competitions.** UCL fixtures and goals history are available on
  the free tier, but adding them now trades honesty for coverage — see §19.
  Europa League is **403** (not in the free plan).

---

## 18. Workflow

**Daily - nothing to do.** The football scheduler fetches football-data.org
every 12 hours and auto-publishes everything that passes the gate. The NBA
scheduler pulls results and the schedule every 6 hours, auto-publishes eligible
games, refreshes availability every 3 hours, and marks the Board stale after a
successful pull. Open the dashboard: today's matches, or the next matchday when
today is empty. The Board and game pages read the data that is already stored;
no manual import or code edit is part of the daily path.

**Weekly:**

1. **Admin → Data → Download latest CSVs** — downloads the current season for the
   six leagues and imports it in one click (completed matches = history).
2. **Admin → Stats** — filter to the last 7 days and a league, then type the eight
   manual markets per match (home and away side by side).
3. **Or Admin → Profiles** — once a week, per league and season, type each team's
   per-game averages for the markets you have data for.
4. **Admin → Fixtures** — only if something reads **Needs mapping**. Fix the
   spelling once; mapping publishes it in the same step.
5. **Admin → Data → Export now** — refresh `data/export/`.

**Checks:**

```powershell
.venv\Scripts\python.exe scripts\verify_engine.py
.venv\Scripts\python.exe scripts\smoke_test.py
.venv\Scripts\python.exe scripts\export_data.py --verify
```

---

## 19. European competitions (UCL) — assessed, deferred

Measured against the live key on 2026-10-20, because "can we add the Champions
League?" deserves numbers rather than an opinion.

**What the provider gives (free tier).** `CL` returns 200: 27 fixtures in the
next 14 days, and — the important part — **finished** matches with scores going
back at least two seasons (189 in 2025-26, 189 in 2024-25, 36 so far this
season). Europa League is **403**; the free tier does not include it. So UCL
fixtures and UCL *goals* history are both reachable.

**Why it is still not a config change.**

1. **Only goals history exists.** The provider returns scores, not corners,
   cards, shots or fouls. Of the seven CSV-backed markets, six would have no UCL
   rows at all, so their competition reference would be the hardcoded
   `DEFAULT_MARKET_LINES` value.
2. **The fallback masquerades as a reference.** With `basis="market_default"`,
   `get_league_reference` still returns a *number* (2.5 for goals), and the API
   sets `has_league_reference` from "is it not None" — so it is **True**. The
   frontend would then print *"N% own data, rest league average"* for a league
   average that does not exist. Unreachable today (all six leagues clear the
   `prior_min=50` long-run pool), but it would fire on **every** UCL card. Any
   UCL work must fix this first.
3. **Most opponents can never be mapped.** Of 27 upcoming UCL fixtures, only
   **12** have both clubs in the six tracked leagues. Eleven have one club
   outside the dataset (Porto, Fenerbahçe, Galatasaray, Bodø/Glimt, Shakhtar,
   Club Brugge, Sporting, LASK, Slovan, Sabah, AEK, Viking, Slavia) and four have
   neither. `upsert_alias` refuses a team that is not already in `matches`, so
   those 15 fixtures would sit in **Needs mapping** permanently — the queue that
   was just cleaned to zero. Ingesting UCL would need a rule that skips
   unanalysable fixtures and reports the count instead of queuing them.
4. **The card would look normal and be weaker.** Same fixture, two labels:

   | League label | Fair line | Reference | Basis |
   |---|---|---|---|
   | `La Liga` (Real Madrid v Barcelona, goals) | 3.5 | 3.05 over 78 matches | `season_shrunk` |
   | `Champions League` (same fixture) | 2.5 | 2.5 over 0 matches | `market_default` |

   Corners: 9.5 generic versus a real 9.76. The history box would read *"No
   completed Champions League matches exist before this fixture"*.

**Verdict.** One to two days makes UCL *look* right; a week or more makes it
*honest* (truthful reference flag, a CL history import that fills goals only, an
ingest filter for unmappable clubs, an explicit decision for the six
statistics with no UCL data, and a UCL goals backtest before any line is shown).
That is roadmap item 13 — league validation — grown teeth, and it edits the
frozen part of the system. The NBA is an isolated new module by comparison.

**When UCL becomes worth it:** when a second free source for UCL per-match
statistics (corners/cards/shots) exists, or when the goal-only version has been
backtested and the card can say "reference: none — competition has no history
yet" without pretending otherwise.

---

## 20. The NBA module — built and measured

The NBA is a **second sport**, not a new market list. It shares the FastAPI app,
the PWA shell and the four-concept discipline; it shares no table, no engine
formula and no publish gate with football.

### 20.1 Why separate

Football's model is a weighted average of goals, corners and cards shrunk toward
a league reference. Basketball is a fixed number of possessions, and every point
is a rate per possession. Sharing the tables would force that difference into the
frozen football pipeline; sharing the formulas would produce numbers that look
plausible and mean nothing. So the module is parallel:

```
nba_teams        team_id is the identity; no alias gate is needed
nba_games        one completed game, with quarter scores when the provider has them
nba_team_games   one row per team per game: the counting unit, with derived
                 possessions / off_rating / def_rating / pace
nba_fixtures     the forward calendar (candidates)
nba_published    what the dashboard may show (a decision)
nba_settings     last fetch / last publish, same idea as fixture_settings
```

`nba_games` and `nba_fixtures` are never merged: ingesting history writes only
`nba_games`, refreshing the schedule writes only `nba_fixtures`, publishing
writes only `nba_published`.

Files: `scripts/nba_schema.py`, `scripts/nba_fetch.py`, `scripts/nba_stats.py`,
`scripts/nba_fixtures.py`, `scripts/nba_people.py`, `scripts/verify_nba.py`,
`scripts/verify_nba_people.py`, `scripts/backtest_nba.py`.
The API routes are §10; the UI is a second sport switch on the dashboard.

### 20.1a Rosters, production and availability

The original NBA module knew teams and team box scores, never *who* plays. That
is now a separate layer with its own tables and its own honesty rules:

```
nba_players       one row per player, from the official roster
nba_rosters       season + team + player, with `active` rather than deletion
nba_player_stats  official season per-game production (3 completed seasons)
nba_injuries      timestamped availability snapshots (ESPN feed)
nba_sync_log      every pull: job, status, rows, error
```

Sources, verified live on 2026-10-20:

| Endpoint / feed | Job | Result |
|---|---|---|
| `CommonTeamRoster` | official 30-man rosters | **30/30 teams, 518 players** in 2026-27 |
| `LeagueDashPlayerStats` | season per-game production | **1,723 rows** across 2023-24 → 2025-26 |
| ESPN injuries JSON | current availability | **90 entries** in the latest snapshot, 100% resolved to an NBA player id |

Two rules:

- **Confirmed is not projected.** A roster spot is confirmed by the league. A
  role is minutes-based and labelled as such; `nba_player_stats` has no starts
  column, so the system never claims a confirmed lineup.
- **Availability is displayed, not priced.** The injury feed has no history, so
  its effect cannot be measured. It is stored with a capture time, shown in the
  admin roster view, and never moves a probability until enough stored history
  clears the same gates as every other factor.

The provider attributes a player's whole season to his final team, so per-team
minute sums are **not** a rotation and are never used as one. Production is
joined on the player, which is why a summer signing still shows the evidence he
earned at his old club.

Sync cadence: availability every 3 hours, rosters daily, player production
every 3 days. `verify_nba_people.py` covers 32 invariants offline.

### 20.2 Data source and what it actually gave us

`nba_api` 1.11.4 against stats.nba.com: free, keyless, official.

| Endpoint | Job | Measured |
|---|---|---|
| `LeagueGameLog` | completed team box scores | 3 seasons, **3,690 games / 7,380 team rows** (2023-24, 2024-25, 2025-26) |
| `ScheduleLeagueV2` | the forward calendar | **1,200** regular-season games for 2026-27 |
| `ScoreboardV2` | quarter and half scores | works for 2024-25 (**1,230 games**) |

Two data facts that shaped the code:

- **Neutral-site games.** The game log prints both sides as away (`DAL @ DET`
  and `DET @ DAL`), so parsing `MATCHUP` dropped 5 games per season. Home and
  away now come from the official schedule, and the log is matched by `game_id`.
- **Six TBD rows.** The 2026-27 schedule contains six cup/placeholder rows with
  no teams. They are skipped, not guessed; a later refresh picks them up when
  the teams are known.

The 2025-26 line-score gap (22 Oct – 25 Dec 2025) is real, so first-half and
first-quarter totals are currently measured on **2024-25's 1,230 games**. They
are withheld (`available: false` with a reason) below
`MIN_QUARTER_GAMES = 100`.

### 20.3 The model

```
possessions = FGA + 0.44 * FTA - OREB + TOV
off_rating  = 100 * points / possessions
pace        = possessions * 48 / game_minutes

expected_off   = own_off * opponent_def / league_off
expected_points = expected_off * expected_possessions / 100
expected_possessions = (home_pace + away_pace) / 2
```

Each team has three nested estimates, each shrunk toward the one above:

```
prior  = every stored game before the fixture   (k = 12)
season = this season, shrunk toward prior       (k = 8)
recent = last 10, shrunk toward season          (k = 5)
rating = 0.65 * season + 0.25 * recent + 0.10 * prior
```

That structure is what makes opening night work without a special case: with
zero games this season, `season` collapses to `prior`, so the model uses last
season and the UI says so. The league reference is itself shrunk: current
season toward prior seasons with `k = 200` games.

Home edge is **measured** (1.8 points on the day it was built) and applied half
to each side, so it splits the team totals without changing the total.

The **fair line is the league-average matchup**, not the projection rounded.
Rounding the projection would make `projection > line` true by construction and
turn the verdict into a tautology. The projection is compared with that line;
the admin can save a real market line, which then replaces it and is labelled
`"market"`.

The range is the central 68% and 90% of historical game totals (σ ≈ 20). It is
labelled as a spread, not a calibrated prediction interval, because no NBA
calibration has been run yet.

Overtime is explicit: the projection is regulation (48 minutes), the stored
result includes OT, `went_to_ot` is recorded, and the panel says so.

### 20.3a Team counting stats — combined and each team

The same per-100-possession unit now covers eight counting stats:

```
own_rate           = team's stat per 100 possessions, shrunk as points are
allowed_rate       = what that team's opponents collect per 100 possessions
expected_per100    = own_rate * allowed_rate / league_rate
expected_stat      = (expected_per100 ± home_edge/2) * expected_possessions/100
```

Two-pointers are **field goals made minus three-pointers made**, not a separate
feed, so a made three cannot be counted twice. `turnovers` uses the opponent's
turnovers forced; `steals`, `blocks`, `assists`, `fouls`, `rebounds` and the two
shooting splits use the same own-rate × opponent-allowed shape. The hierarchy
(prior → season → last 10, blended 0.65/0.25/0.10) and the shrinkage constants
are the same as points. Home edge is measured per stat and split ±edge/2, so it
cannot move the combined total.

Every stat has three markets: **combined**, **home team**, **away team**. The
combined market is compared with the league reference; a team market is compared
with that team's own per-game baseline (season after five games, otherwise prior
season). The Board stores all three in `board_markets`; a selected market shows
home, away and combined together on one card. The legacy ranked view still keeps
the top two qualifying picks per fixture.

Walk-forward measurement on 2,500 games (1,122 for period markets). Every row
below beats the reference line it is tested against:

| Stat | Combined MAE | Home MAE | Away MAE | Naive combined | Naive home | Naive away |
|---|---|---|---|---|---|---|
| Rebounds | 7.175 | 5.100 | 5.098 | 7.420 | 5.358 | 5.294 |
| 2-pointers | 5.640 | 3.857 | 3.970 | 6.264 | 4.024 | 4.153 |
| 3-pointers | 4.218 | 2.930 | 2.978 | 4.544 | 2.996 | 3.060 |
| Assists | 5.559 | 3.836 | 3.762 | 6.208 | 4.012 | 4.027 |
| Turnovers | 4.524 | 3.019 | 3.024 | 4.823 | 3.209 | 3.222 |
| Steals | 3.454 | 2.351 | 2.416 | 3.604 | 2.446 | 2.526 |
| Blocks | 2.740 | 1.908 | 1.863 | 2.853 | 1.985 | 1.970 |
| Fouls | 5.192 | 3.180 | 3.201 | 5.402 | 3.352 | 3.392 |

The team naive columns use the same own-baseline line the Board actually tests,
not the easier league reference. That is accuracy, not a claim about beating a
priced market: no closing lines were used. It also does not make a team baseline
a market line — a typed line still wins where one is stored, and no stat-line
columns exist yet.

### 20.4 Backtest — the number that decides whether to trust it

`scripts/backtest_nba.py` walks forward: every projection uses only games
strictly before it, and the error is scored against the actual result and
against a naive "league average so far" baseline.

| Season | Games | MAE | Naive | Improvement | Side agreement |
|---|---|---|---|---|---|
| 2024-25 | 1,230 | **14.86** | 15.86 | **+1.00** | 63.2% (1,072) |
| 2025-26 | 1,230 | **15.35** | 15.92 | **+0.57** | 60.4% (1,004) |

"Side agreement" is only counted where the model was at least 1 point away from
the league line, so it is not a coin flip on rounding. It is not a claim about
beating a market: no closing lines were used.

A weight sweep (0.65/0.25/0.10 vs 0.50/0.35/0.15 vs 0.80/0.15/0.05) moved MAE by
**0.07 points at most**. The exact blend is not where the edge is, so the
documented weights were kept rather than tuned to the fourth decimal.

### 20.5 What publishes, and what never does

The gate is small because `TEAM_ID` removes the reconciliation problem:

- regular season only (`game_id` starts `002`);
- both teams known;
- status `scheduled`;
- both teams have at least one completed game the engine can count;
- inside the 14-day window.

Auto-publish runs on every schedule refresh and writes only `nba_published`. It
never touches `nba_games`, never deletes a publication, and never un-publishes
because a refresh stopped seeing a fixture. There is no recurring publish step
for the user.

### 20.6 Deliberately absent

- **Player props.** Minutes, lineups, injuries, rotations, usage, trades and
  blowouts make them a false-confidence machine in October. Excluded from v1.
- **Rest, back-to-backs, travel.** Known effects, not yet measured here.
- **Live in-game scores.** Final team box scores and results sync automatically
  every six hours; a published row keeps its pre-game projection, and the Board
  grades from `nba_games`. There is no live possession or quarter feed in the
  user view.
- **A market-line feed.** Point lines are typed by the admin, as football's are.
  Counting-stat lines are league/team references; there is no typed rebound,
  assist, turnover, steal, block, foul or shooting line yet.
- **A basketball admin view.** Auto-publish leaves little to administer; lines
  are edited in the game panel. A dedicated admin tab is future work.

### 20.7 Commands

```powershell
.venv\Scripts\python.exe scripts\nba_schema.py
.venv\Scripts\python.exe scripts\nba_fetch.py --all
.venv\Scripts\python.exe scripts\nba_fetch.py --quarters --season 2025-26
.venv\Scripts\python.exe scripts\verify_nba.py
.venv\Scripts\python.exe scripts\backtest_nba.py --season 2025-26
```

The backend starts an NBA scheduler beside the football one: every 30 minutes it
checks whether the last successful schedule pull is more than 6 hours old, and
if so pulls the current season's history, the schedule, auto-publishes and fills
up to two missing line-score dates. After each successful step it writes
`last_history_fetch`, `last_schedule_fetch`, `last_publish` and
`last_quarter_fetch`; those timestamps are what stop the loop from repeating and
what the admin status reads. It also marks `last_board_build` stale, so the next
pass rebuilds the Board from the new data instead of waiting for the ordinary
six-hour rebuild.

`LeagueGameLog` is upserted on `(game_id, team_id)`. The conflict update refreshes
**every** counting stat (field goals, threes, free throws, rebounds, assists,
steals, blocks, turnovers, fouls) and the derived possessions/ratings/pace, not
only the final score. That is what lets a completed game update its statistics
and let grading use the real result. Availability runs every 3 hours, rosters
daily and player production every 3 days on a separate people scheduler.

---

## 21. The prediction board — measured probability, not a verdict

`scripts/predict.py` · `prediction_stats`, `board_markets`, `board_picks` ·
`GET /board?grouped=1`

### Why an edge is not a probability

"Model 11.8, line 9.5" is a 2.3-corner gap. "Model 226.4, line 228.5" is a
2.1-point gap. The two are not comparable, and neither is a probability: corners
swing by 3.6 a game, NBA team totals by 12.3. The board therefore divides every
edge by **that market's own measured error** before it ranks anything. That is
what lets a 60% in corners sit beside a 60% in goals and mean the same thing.

This module does not project. `stats.py` and `nba_stats.py` remain the only
sources of a projection; `predict.py` only measures how wrong those projections
have been.

### The measurement

For each market, the engine is walked forward over the most recent completed
matches, exactly as the dashboard calls it:

```
residual = actual − projection
```

- football: 400 most recent matches per market, no date filter, 387 usable after
  matches the engine could not analyse (promoted clubs, missing columns);
- NBA: the 2,500 most recent games, because only 2024-25 carries line scores and
  the half/quarter markets would otherwise have no residuals at all.

Stored per market: `sigma`, `mae`, `bias`, the residual quantile grid, and a
**holdout calibration curve** — the distribution is fitted on the first 70% of
matches and scored on the last 30%.

Measured on the live database (2026-10-20):

| sport | market | n | sigma | combined MAE | home MAE | away MAE | naive reference |
|---|---|---:|---:|---:|---:|---:|---:|
| football | goals | 387 | 1.741 | 1.407 | — | — | 1.402 |
| football | shots | 387 | 6.490 | 5.472 | — | — | 5.105 |
| football | shots on target | 387 | 3.359 | 2.738 | — | — | 2.764 |
| football | fouls | 387 | 5.757 | 4.596 | — | — | 4.674 |
| football | corners | 387 | 3.647 | 2.897 | — | — | 2.858 |
| football | yellow cards | 387 | 1.914 | 1.565 | — | — | 1.572 |
| NBA | game total | 2500 | 18.879 | 15.076 | — | — | 15.855 |
| NBA | home team total | 2500 | 11.933 | 9.554 | — | — | 10.030 |
| NBA | away team total | 2500 | 11.798 | 9.381 | — | — | 9.999 |
| NBA | first-half total | 1122 | 11.974 | 9.589 | — | — | 9.972 |
| NBA | first-quarter total | 1122 | 8.188 | 6.564 | — | — | 6.691 |
| NBA | rebounds | 2500 | 9.106 | 7.175 | 5.100 | 5.098 | 7.420 |
| NBA | 2-pointers | 2500 | 7.049 | 5.640 | 3.857 | 3.970 | 6.264 |
| NBA | 3-pointers | 2500 | 5.280 | 4.218 | 2.930 | 2.978 | 4.544 |
| NBA | assists | 2500 | 6.927 | 5.559 | 3.836 | 3.762 | 6.208 |
| NBA | turnovers | 2500 | 5.664 | 4.524 | 3.019 | 3.024 | 4.823 |
| NBA | steals | 2500 | 4.305 | 3.454 | 2.351 | 2.416 | 3.604 |
| NBA | blocks | 2500 | 3.409 | 2.740 | 1.908 | 1.863 | 2.853 |
| NBA | fouls | 2500 | 6.607 | 5.192 | 3.180 | 3.201 | 5.402 |

The period markets have 1,122 games because only 2024-25 carries line scores in
this database; the 2025-26 provider gap is documented in §20.

Three of the six football markets beat a plain league average; three do not. The
board shows that per pick (`beats_naive`) rather than hiding it. All 29 NBA
markets beat the reference line they are actually tested against — the league
line for combined markets, the team's own baseline for team markets — so the
flag is true there rather than merely absent.

### Counting stats: one rate per possession, eight markets

Every team counting stat uses the same unit as the rest of the NBA engine. For
each team, the model measures the stat **per 100 possessions**, from SUM/SUM
rather than an average of per-game rates, so a fast game is weighted by how much
basketball it contained. It then uses the same multiplicative matchup as points:

```
expected_stat_per100 = own_rate × opponent_allowed_rate / league_rate
expected_stat        = (expected_stat_per100 ± home_edge/2) × expected_possessions / 100
```

`own_rate` is the team's own rate per 100 possessions; the opponent term is the
rate its opponents collected. Both use the same three-level hierarchy as offence
and defence — prior → season (k=8) → last 10 (k=5), blended 0.65/0.25/0.10 — so a
three-game sample cannot produce a confident projection. The home edge is
measured per 100 possessions for each stat and split evenly.

The eight stats are rebounds, 2-pointers, 3-pointers, assists, turnovers,
steals, blocks and fouls. **Two-pointers are `FGM − 3PM`**, not a separate feed,
so a made three is never counted twice. Each stat has three markets —
`<stat>_total`, `home_<stat>` and `away_<stat>` — and the combined projection is
the sum of the two team projections, not a separate model. The fair line comes
from the league rate and league pace, never from the rounded projection. On the
board, a team stat line is compared to that team's own baseline (season once five
games exist, otherwise the prior season); the combined market keeps the league
reference.

Measured on 2,500 games, every one of the 24 stat markets beats the reference
line it is tested against — the league line for combined markets, the team's own
baseline for team markets. The combined and team MAE values are in §20.3a. That
is the only reason they are on the board, and the card still shows the measured
error.

### Market, scope, and the filter contract

Every board row stores two filter fields beside the engine key:

| engine key | `market_group` | `scope` |
|---|---|---|
| `game_total` | `points` | `combined` |
| `home_team_total` | `points` | `home` |
| `away_team_total` | `points` | `away` |
| `first_half_total` | `points_1h` | `combined` |
| `first_quarter_total` | `points_1q` | `combined` |
| `rebounds_total` | `rebounds` | `combined` |
| `home_rebounds` | `rebounds` | `home` |
| `away_rebounds` | `rebounds` | `away` |
| `two_pm_total`, `home_two_pm`, `away_two_pm` | `two_pm` | combined / home / away |
| `three_pm_total`, `home_three_pm`, `away_three_pm` | `three_pm` | combined / home / away |
| `assists_total`, `home_assists`, `away_assists` | `assists` | combined / home / away |
| `turnovers_total`, `home_turnovers`, `away_turnovers` | `turnovers` | combined / home / away |
| `steals_total`, `home_steals`, `away_steals` | `steals` | combined / home / away |
| `blocks_total`, `home_blocks`, `away_blocks` | `blocks` | combined / home / away |
| `fouls_total`, `home_fouls`, `away_fouls` | `fouls` | combined / home / away |
| football `goals`, `corners`, … | the same key | `combined` |

The grouped Board filters on `date`, `sport`, `league` and `market_group`, never
on the engine key. It reads `board_markets` and groups rows by fixture +
`market_group`. A new market family is a new key plus a taxonomy row; the
frontend does not learn a new filter architecture. The response returns
`cards`, `total`, `row_total`, `settled_lines`, `won_lines`, `lost_lines`,
`hit_rate`, `has_more`, `markets` (market families stored for the date, with a
game count), `leagues`, `accuracy` and `history`, so the UI can paginate,
show a measured record, and keep a saved market visible even when its count is
zero.

The legacy `GET /board` response still accepts `scope`, `team`, `source`,
`confidence`, `status` and `sort` and filters `board_picks` on `market_group` +
`scope`. Confidence bands (High ≥ 65%, Medium 58–65%, Low 55–58%), the source
view and the status filter are **legacy API options**, not controls on the
current Board. `source=board` and `source=agree` return an empty page with a
`source_note` until the context layer has been measured; the core model is never
relabelled to fill them.

### From residual to probability

```
p_over = 1 − F(−edge)          F = empirical CDF of the residuals
p      = 0.5 + (p_over − 0.5) × n/(n+50)
p      = clamp(p, 0.05, 0.95)
```

- `edge = projection − line`, so `actual > line ⇔ residual > −edge`;
- the shrinkage factor is 0.886 at n=387 and 0.960 at n=1200, so a thin market
  cannot look as certain as a fat one;
- the clamp means no single match is ever called a certainty.

The holdout check is the honest part. On the live data a 60% claim landed 57–62%
in five of six football markets (goals 57%, shots 57%, shots on target 56%,
corners 62%, yellow cards 68%), and 59–66% for NBA team totals. Rebounds are
close: a 60% claim landed 58% combined, 58% home and 57% away, so the displayed
percentage is still slightly optimistic in that band. Fouls is
over-confident (a 60% claim lands 68%), which is visible on every fouls card.

### Which line is being tested

| line_source | Used for | Meaning |
|---|---|---|
| `league` | football | the league's own average for that market |
| `league_average` | NBA game and period totals, combined counting stats | the league's average total |
| `team_baseline` | NBA team totals and team counting stats | that team's usual count (season once ≥5 games, else the prior season) |
| `market` | anything the admin typed | a real price |

The team-total rule matters. A team total compared to the league's average team
total is a statement about how good the team is, not about tonight: the best
offence clears it almost every night. Comparing it to the team's **own**
baseline instead makes the pick say "more or less than this team usually
scores", which is what a team-total question actually is. The probability is
unchanged by this choice — the residual is measured as `actual − projection`, and
the line only decides where on that distribution the edge falls.

### Ranking and eligibility

- only **published** fixtures — the board is a user-facing view, not a second
  dashboard;
- only markets with **n ≥ 120** and a CSV-backed football history;
- `board_markets` keeps every available home/away/combined line, including rows
  below the qualifying bar;
- a row becomes a `board_picks` pick at **probability ≥ 0.55**;
- there is no storage cap per fixture. The legacy ranked view reads ranks 1-2 by
  default; selecting a market or scope reads every stored rank;
- the grouped default is a **selection layer**, not a dump: it shows only
  fixtures with at least one qualifying row, chooses the strongest family by
  measured probability shrunk for sample size (`n / (n + 250)`), and gives a
  tiny tie-break to a market whose MAE beats its naive baseline. The chosen row
  is the only one marked `selected`; its `reason` is returned with the card.

### Snapshot and grading

A complete matchup line is written once into `board_markets` before kickoff; a
qualifying row is also written into `board_picks`. A rebuild:

- fills a line only while the fixture is still in the future — a game already
  under way is skipped, so nothing is published after the fact;
- replaces an ungraded line only if its `start_time` is still in the future;
- never touches a graded line;
- fills missing lines.

`grade()` settles `win` / `loss` / `push` from the real result and records the
actual value for both tables. The scheduled grader runs every 30 minutes and
includes **today**, so a finished game can show `HIT`/`MISS` without waiting for
tomorrow. It only changes `result`, `actual` and `graded_at`; the line,
projection, probability and timestamp stay frozen. A row still ungradeable seven
days later becomes `void` rather than pending forever. The record on the board is
therefore a real record, not a number recomputed after the fact.

### What the board does not do

- it does not invent a market line: football uses the league reference unless a
  market line is entered, and every card says which one it used;
- it does not claim a league line is beatable — the footer says the opposite;
- it does not hide a market that fails its naive baseline;
- it does not use the manual-only football markets, which have no walk-forward
  history to calibrate against.

---

*End of document. Everything above is implemented, measured or explicitly listed
as absent.*
