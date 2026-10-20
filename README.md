# Sports Analyzer

A private statistical research tool for **football and the NBA**. For a chosen
fixture, stat and line it shows the evidence behind a number — season averages,
recent form, venue splits, head-to-head — combines them with a model built for
that sport, and says whether the data sits above or below the line, with the
sample size attached.

It is **not** a betting product. It does not say "bet this". Every number it shows
carries the count of distinct matches behind it, and it says plainly when there is
not enough evidence.

Two things sit on top of the per-fixture analysis:

- **The board** selects the day's strongest measured opportunities - one market
  family per game, only when a line clears the measured bar - and freezes the
  complete home/away/combined matchup before kickoff, then grades it against the
  result.
- **The full season** screen shows the whole calendar for a league, with results
  for games already played and "analysis opens 14 days before kickoff" beyond the
  window, instead of a stale projection.

---

## Start it

```powershell
start.bat
```

Then open **http://localhost:8001/app/**

The API and the web UI are served by the same process, so there is one command,
one port, and no CORS or second-server confusion. API docs live at
`http://localhost:8001/docs`.

To start it manually instead:

```powershell
.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8001 --app-dir backend
```

### Production

The app is one process with in-process schedulers and a persistent SQLite
database. The production image, Render Blueprint, Fly.io configuration, secrets
and verification steps are in **[DEPLOYMENT.md](DEPLOYMENT.md)**.

Set `APP_ENV=production` and a long `ADMIN_TOKEN` on the host. Every `/admin/*`
route, plus the NBA publish/refresh writes, then requires that token; the Admin
screen asks for it and keeps it only in the browser session. The app refuses to
start in production if `ADMIN_TOKEN` is missing. `/docs` is disabled in
production, CORS is empty by default (the UI is same-origin), and `/health` is
the health check.

---

## The four data concepts (the core design rule)

These are deliberately separate, and mixing them up is the mistake this design
exists to prevent.

| Concept | Table | What it is |
|---|---|---|
| **Historical matches** | `matches` | What the engine learns from. Comes from the CSV import. |
| **Published matches** | `published_matches` | What users are allowed to see on the dashboard. |
| **Manual stats** | `manual_stats` | Hand-entered values the free CSVs do not contain. |
| **Team profiles** | `team_profiles` | A typed per-game average. Not a match, not evidence. |

- Publishing a match does **not** add it to the historical dataset.
- Adding historical data does **not** publish it to the dashboard.
- A user only ever sees the admin-published list, never the whole database.
- A team profile is the weakest concept of the four: it fills a gap where nothing
  has been counted, it is labelled as typed wherever it is used, and it can never
  outrank counted data.

---

## Layout

```
Sports-Analyzer/
  start.bat                  one-click local server + browser
  Dockerfile                 production image, one Uvicorn worker
  docker-entrypoint.sh       seed the volume, fix ownership, drop privileges
  render.yaml  fly.toml      one always-on host with a persistent disk
  DEPLOYMENT.md              production secrets, hosts and verification
  backend/
    main.py                  FastAPI: validation, routing, JSON shaping
    requirements.txt
  scripts/
    stats.py                 the engine (averages, weighting, fair lines)
    matches_engine.py        batch layer over stats.py
    backtester.py            cutoff-aware model vs baseline evaluation
    import_data.py           CSV -> SQLite (id-preserving upsert, source-aware)
    fetch_csvs.py            download the current season file for every league
    seasons.py               season labels ('2026-27') — one definition, shared
    export_data.py           SQLite -> CSV and back (admin work survives a rebuild)
    manual_stats.py          enter the stats the CSVs lack
    cli.py                   terminal analysis (fastest sanity check)
    migrate_schema.py        create/upgrade the schema (idempotent) + unique index
    verify_engine.py         engine regression checks (known-correct values)
    smoke_test.py            end-to-end HTTP test (needs the server running)
    nba_schema.py            create the NBA tables (idempotent)
    nba_fetch.py             NBA history, schedule and quarter scores -> SQLite
    nba_stats.py             the NBA engine (possessions, ratings, projections)
    nba_fixtures.py          NBA publish gate, auto-publish, dashboard queries
    nba_people.py            NBA rosters, player production and availability
    predict.py               measure model error, build and grade the board
    verify_nba.py            NBA structural checks (57, offline, on a copy)
    verify_nba_people.py     roster/availability checks (32, offline, on a copy)
    verify_predict.py        board honesty checks (offline, on a copy)
    backtest_nba.py          walk-forward NBA scoring against a naive baseline
    db_info.py               print the schema and row counts
    explore.py               what is actually in the DB, per league and season
  frontend/
    index.html               the whole PWA (dashboard, match centre, admin)
    manifest.json  icon.svg  sw.js
  data/
    raw/<league>/<season>.csv    the source CSVs, replaced as the season runs
    export/                      the database written out as CSV (the backup)
    sports_analyzer.db
```

---

## Markets

Configured in one place — `STAT_COLUMNS` in `scripts/stats.py`. Add a stat there
once and every averaging function, the API and the UI pick it up.

| source | markets |
|---|---|
| from the CSVs | goals, shots, shots on target, fouls, corners, yellow cards, red cards |
| hand-entered only | tackles, throw-ins, aerial duels, offsides, interceptions, goal kicks, saves, substitutions |

The second row will **never** come from a CSV — no free provider publishes them.
They start completely empty, and an empty market does **not** invent a number:
the API returns `weighted_avg: null` with the note *"no source had usable
values"*, and the only thing that still works is the line, which falls back to a
fixed default until the league has data of its own. That is deliberate — a
market with no data should say so, not quote the league average as if it were a
finding.

The engine needs **both** teams to have a value before a market will speak. One
side typed is not enough: the estimator works on a match total (home expected +
away expected), so a fixture where only the home team has data still reports
nothing. The first pair of typed rows is what turns the market on.

Two ways to fill them, and they can be mixed: **counts per match** on Admin → Stats
(the measured route), or each team's **per-game average** on Admin → Profiles (the
typed route, clearly labelled as such — see *Team profiles* below). Counted data
always wins where it exists.

**Admin → Data** shows the number of completed matches behind each market and
flags anything under 30 as *thin*.

---

## Lines: fixed or fair

A single hardcoded line for every fixture is not a real test — strong attacking
teams clear it and the whole list skews OVER. So the engine supports both:

- **Fixed line** — the bookmaker's number, entered in the UI.
- **Fair line** — when no line is given, the league's own average for that stat is
  used, computed **strictly before that match** (never the future).

The UI shows which one it used (`line_source`).

### Lines are always x.5, and pushes are dropped

A line of `3.0` is not a valid totals line, because a match with exactly 3 goals
is a **push** — neither over nor under. A scorer that tests `actual > line`
silently books every push as an UNDER.

That is not a theoretical concern. The first version of this backtester snapped
the fair line with `round(raw * 2) / 2`, which returns whole numbers, and the
resulting lines were goals **3.0**, shots **24.0**, fouls **22.0**, red cards
**0.0**. Red cards at 0.0 is the clearest failure: a match with no red card is a
push, but it was counted as an UNDER, which is why "always back under" appeared to
score 91%.

Two safeguards now exist, and both are tested in `verify_engine.py`:

1. `snap_to_half_line` makes the fair line land on an x.5 boundary **always**, on
   an exact tie picking the higher line. Bookmakers do this for the same reason.
2. Any match still landing exactly on the line is detected and **dropped from
   every predictor**, model and baselines alike, and the count is reported.

The effect on goals O2.5, Premier League 2025-26: the base rate moved from a
reported **43.5%** OVER to **54.8%**, and "always under" from 56.5% to 45.2%.
The bug had inverted the finding.

### A baseline that cannot lose

In per-match fair-line mode the line *is* the snapped league average, so
`baseline_league_avg` is really asking *"is the average above the snapped version
of itself?"* — a rounding artefact, not a forecast. Measured on 376 matches it
answered OVER 351 times at line 2.5 and UNDER every time at 3.5 and 6.5.

The backtester now detects this automatically: any predictor that answers the
same way more than 95% of the time is flagged, and `always_over`/`always_under`
are exempt because being the base rate is their entire job. **Do not quote
`baseline_league_avg` as a benchmark** in fair-line mode.

### Early-season fair lines are shrunk, not raw

The fair line from a handful of matches is meaningless. In the 2025-26 opener the
league "average" was computed from almost no data and produced a goals fair line
of **6.5**.

That is now fixed rather than merely warned about. `get_league_reference` shrinks
the season average toward a long-run prior:

```
value = (n x season_average + k x prior) / (n + k)        k = 10 matches
```

so at 3 matches played the prior dominates, and at 60 the season's own numbers
do. The prior is the same league's average over **all earlier seasons** once that
pool reaches 50 matches, and the market's conventional line only before then.

Every response reports which basis was used (`line_reference.basis` is one of
`season_shrunk`, `league_long_run`, `market_default`), so the UI can say *"season
average 9.38 over 50 matches, shrunk toward the league long run"* rather than
presenting a number of unknown provenance. A season label that does not match the
`matches` table used to silently kill the reference and produce a line of 10.5
with `season_average: null`; `normalize_season` now canonicalises the label on
write *and* on read.

---

## How the engine works

```
value = (n x season home/away total + k x league reference) / (n + k)      k = 10
```

The headline number is the two teams' season home/away totals pulled toward the
league average in proportion to how little data stands behind them. `n` is the
**smaller** of the two teams' sample sizes, so one well-covered team cannot mask a
barely-covered opponent.

- `reliability` = `n / (n + k)` for each source, so the UI can say how much of it
  survives shrinkage: a source with 5 matches at `k = 10` is one third itself and
  two thirds league average.
- `evidence_points` counts **distinct matches** behind the number — a match that
  appears in both the form set and the H2H set is counted once.
- `low_confidence` is set when fewer than 3 distinct matches back the figure.
- Recent form and head-to-head are still computed and returned in full, but they
  do **not vote**. On the pooled backtest they added variance rather than
  accuracy, so they are shown as evidence instead of silently moving the number.
- If season form is missing the estimator falls back in a fixed order (recent
  form, then the league reference, then H2H), and `method_note` always says which
  route was taken. A number is never returned without saying what produced it.

### The legacy 45/35/20 blend

Still present as `method="blend"`, and still scored head to head by the
backtester, so the change can be re-checked at any time:

```
season home/away form (45%)  +  last-5 recent form (35%)  +  head-to-head (20%)
```

`calculate_weighted_market` drops any source with no data and rescales the
remaining weights so they still sum to 1 — a missing H2H does not quietly drag the
number toward zero. It is no longer the default: `"shrunk"` replaced it because it
measured better, not because it reads better. See the model status below.

### The cutoff rule

Every query accepts `before_date` and `exclude_match_id`, and uses `date < ?`
(strictly before). This is what stops a backtest from letting a match see its own
result or the future. `scripts/verify_engine.py` asserts it: Marseille v Paris SG
goals O2.5 returns **4.31** with no cutoff (leaky) and **4.33** with the cutoff
(correct).

---

## Honest status of the model

The 45/35/20 weights were a **hypothesis, not a validated finding**. The
backtester compares the engine against the benchmarks that actually matter — a
paired comparison with the legacy blend, the majority class, and the strongest
simple baseline — rather than against "always OVER", which flatters a model
whenever the base rate is far from 50%.

Run it (a quick single-league sanity run):

```powershell
.venv\Scripts\python.exe -u scripts\backtester.py --markets goals,corners,fouls `
    --league "Premier League" --season 2025-26 --limit 250
```

or the full evaluation this table comes from:

```powershell
.venv\Scripts\python.exe -u scripts\backtester.py `
    --markets goals,shots,shots_on_target,corners,fouls,yellow_cards,red_cards --shrink-k 10
```

Fair per-match lines, all six leagues, roughly 4,280 scored matches per market
(pushes excluded for the engine and every baseline alike):

| market | engine | legacy 45/35/20 | engine vs legacy, calls | engine vs legacy, error | engine MAE |
|---|---|---|---|---|---|
| goals | 56.8% (55.3–58.3) | 55.8% | no clear difference | **more accurate** | 1.328 |
| shots | 53.0% (51.5–54.5) | 53.8% | no clear difference | **more accurate** | 4.776 |
| shots on target | 55.8% (54.3–57.3) | 56.0% | no clear difference | **more accurate** | 2.478 |
| corners | 52.0% (50.5–53.5) | 52.3% | no clear difference | **more accurate** | 2.761 |
| fouls | 58.1% (56.6–59.5) | **59.4%** | **behind (clear)** | no clear difference | 4.292 |
| yellow cards | 56.8% (55.3–58.3) | 55.6% | no clear difference | **more accurate** | — |
| red cards | 83.9% | 81.5% | **ahead (clear)** | no clear difference | 0.311 |

**Against a plain baseline, the picture is much less flattering, and that is the
honest reading:**

- **goals** — the best real baseline (`baseline_homeaway`, 56.1%) is level with
  the engine. No clear difference.
- **shots on target** — level with `baseline_homeaway` (55.6%), clearly ahead of
  a plain league average (53.2%).
- **yellow cards** — clearly ahead of `baseline_homeaway` (55.3%). The one market
  where the engine beats every simple baseline on calls.
- **corners** — the majority class (51.9%) sits *inside* the engine's interval,
  so there is no measurable skill here. Worse, its predicted total is **less**
  accurate than a plain league average (MAE 2.761 vs 2.717, clear).
- **shots** — same weakness on error: 4.776 against a league average's 4.668
  (clear, +0.107).
- **fouls** — ahead of a plain league average on calls, but **clearly behind the
  legacy blend** (345 calls against 404). This is the one hit-rate regression,
  and it is the direct reason `--shrink-k` is on the list to sweep: k=10 was
  inherited, never tuned.
- **red cards** — degenerate: one side wins over 95% of the time, so its 83.9%
  is arithmetic, not insight. It is excluded from `best_baseline` and no skill
  claim is made. `experiment_shrunk` tracks `model_engine` within noise in every
  market, so shrinkage strength is not what separates them.

**What the engine is actually better at:** predicting the *total*. Its predicted
total is clearly closer than the legacy 45/35/20 blend in five of the six
non-degenerate markets, and level in the sixth (fouls). That is a real,
reproducible improvement.

**What it is not better at:** beating a trivial rule on the *over/under call*.
Only yellow cards clears its best simple baseline. Corners shows no skill at all
on the call and is worse than a league average on the number.

Conclusion: **do not treat the verdict as a selection tool.** The engine, the
cutoff discipline and the evaluation harness are sound — they are what produced
this table, including the results that embarrass the model. The weights and the
signal are the next real piece of work, not more UI.

---

## The board — the complete matchup, not a filter maze

Open **Today's board** from either sport screen. Choose a sport, date, league and
market. A card then shows the whole matchup for that statistic: home line, away
line and combined line, plus the model projection and the final actual once the
game is settled.

There is no Scope, View, Status, Confidence or Sort control. Those questions are
answered on the card itself. The board is a reader, not a calculator: changing a
filter only reads the predictions that were stored before kickoff. It never
re-runs the engine and never fills a past date after the fact.

### The only filters

- **Sport** — All, Football or NBA. Each sport keeps its own saved filter set on
  the device.
- **Date** — a date picker, with previous/next arrows for browsing historical
  cards. A past date shows the stored prediction, the actual and `HIT`/`MISS`.
- **League** — the competitions that actually have stored lines on that date.
  NBA games share the `NBA` label.
- **Market** — the measured statistic family. All eight NBA counting stats
  (rebounds, 2-pointers, 3-pointers, assists, turnovers, steals, blocks and
  fouls) expose combined, home and away together. The menu shows the number of
  games with stored lines for that family. A family with no stored lines gets an
  explicit empty state; it is never silently reset.

**Default view** - the curated Board, not a dump of the model. A fixture appears
only when at least one row cleared the measured bar, and only that fixture's
strongest market family is shown. The strongest row on the card is marked
**Board pick**.

**Market selected** - every published fixture for that family, with all three
lines, including a row that sat below the qualifying bar. Those cards are marked
**Below bar**, so the complete stored set is still available without pretending
it is a selection.

**Why this pick** records the evidence behind the selected line: the measured
percentage and sample, the model's distance from the line, the line source, and
whether the market beat its naive baseline. It is derived from the frozen row and
the stored measurement, never from a second confidence score.

**Load more** pages by game card. Nothing is recalculated while paging.

### Reading a card

```
NBA · 20 OCT · 20:00                         Detroit Pistons v Boston Celtics
Rebounds
HOME  Detroit Pistons
LINE   Under 44.5 reb  59%  own baseline     MODEL 43.19 reb   ACTUAL 42 reb  ✓ HIT
AWAY  Boston Celtics
LINE   Under 46.5 reb  69%  own baseline     MODEL 43.14 reb   ACTUAL 49 reb  ✕ MISS
COMBINED  Combined total
LINE   Under 87.5 reb  54%  league line      MODEL 86.33 reb   ACTUAL 91 reb  ✕ MISS
Measured on 2,500 games · 1 hit · 2 misses
```

- **Line** is the reference being tested. It is always a half line; there is no
  push in normal play.
- **Model** is the stored projection. It is not created by opening the card.
- **Actual** is the recorded final stat once the game is complete.
- **HIT / MISS** is calculated from the stored side and line, never typed in by
  hand. A row with no final result says *Not played*.
- **59%** is the measured share of similar past games, not a confidence score,
  price or stake. A row below the old 55% bar is still visible inside a selected
  market, and its probability is dimmed.
- **own baseline** means the team's usual count; **league line** means the
  league reference. A **market line** is one entered by the admin.
- Football cards are combined totals only. The frozen football engine has no
  separate home/away model, so the card says so instead of inventing a split.
  Period markets (1st half, 1st quarter) are combined-only for the same reason.

The footer repeats the important part: this is a record of what the model
projected, not a recommendation to bet. There are no odds, stakes or prices.

### What is eligible

- Football: only the six CSV-backed markets (goals, shots, shots on target,
  fouls, corners, yellow cards). The manual-only markets have no walk-forward
  history behind them, so they cannot be calibrated and never appear.
- NBA: game total, both team totals, first-half total and first-quarter total,
  plus **combined, home-team and away-team** markets for rebounds, 2-pointers,
  3-pointers, assists, turnovers, steals, blocks and fouls — 29 measured markets.
  The period markets appear only when line scores exist.
- A market needs at least **120** measured matches. A row becomes a qualifying
  **board pick** at **55%** measured probability. The complete home/away/combined
  snapshot is stored in `board_markets`; qualifying rows are also stored in
  `board_picks`. The default card view shows only fixtures with a qualifying row
  and picks one market family per fixture; a selected market shows every stored
  row for that family, including rows below the qualifying bar.

### It is a record, not a preview

Each matchup line is written once, before kickoff, into `board_markets`, and each
qualifying pick into `board_picks`. A rebuild fills missing lines and grades
finished ones; it never rewrites a line whose game has started, and it does not
create one for a game already under way. The scheduler grades finished games
every 30 minutes and fills `actual`, `result` and `graded_at`; it never changes
the line, projection, probability or start time. That is what makes the
`HIT`/`MISS` marks a real record instead of a number recomputed after the fact.

### What it still cannot do

The honest limit from the section above still applies: in most football markets
the over/under call does not beat a trivial baseline on average. The board does
not hide that - it measures it, flags it, and selects on the calibrated number
plus sample reliability rather than on the size of the edge alone. Treat a card
as "this is the strongest measured case the system can make today", never as a
promise.

The selection is deliberately not a claim that the line is beatable. It also
does not use factors the app cannot measure: there is no historical injury feed,
so availability is displayed but not used as a selection weight, and no starting
lineup is assumed.

---

## Keeping the dataset current

The historical dataset stops wherever the last CSV ended. It is **not** the list
of matches on the dashboard: publishing a fixture is a separate act that puts
nothing into `matches`. Analysing a published fixture needs only two team names
and a date, so a new fixture can be analysed before either side has played. What
goes stale is the *evidence* — if the last month of matches is missing, the form
numbers are a month old.

Two ways to get **matches** in, and they are complementary. A third section covers
the statistics that no CSV carries.

### 1. Replace the season CSV (the normal route)

football-data.co.uk keeps updating its season file as matches are played, so the
refresh is to download over `data/raw/<league>/<season>.csv`, then run either

```powershell
.venv\Scripts\python.exe scripts\import_data.py
```

or **Admin → Data → Refresh from CSVs**, which is the same function in-process so
the two cannot drift apart.

#### The download half is now built in

You no longer have to fetch the six files by hand. **Admin → Data → Download
latest CSVs** pulls the current season for every mapped league straight from
football-data.co.uk into `data/raw/<league>/<season>.csv` and then refreshes. The
same thing from a terminal:

```powershell
.venv\Scripts\python.exe scripts\fetch_csvs.py --season 2026-27          # download only
.venv\Scripts\python.exe scripts\fetch_csvs.py --season 2026-27 --import # and import
```

URL scheme: `https://www.football-data.co.uk/mmz4281/<code>/<div>.csv`, where the
season code is the provider's own (`2026-27` → `2627`) and the division comes from
`LEAGUE_DIV_CODES` (`premier-league`=E0, `bundesliga`=D1, `la-liga`=SP1,
`serie-a`=I1, `ligue-1`=F1, `eredivisie`=N1). Seasons without a mapping (e.g.
Champions League) have no free source and stay manual.

**A download cannot destroy a good file.** Each response is parsed just far enough
to confirm it is a season CSV — header must contain `Div`, `Date`, `HomeTeam` and
`AwayTeam`, and there must be at least one match row — before it is allowed to
replace what is on disk. An outage, a captcha or a moved URL returns an HTML error
page, and writing that over a 380-row season would make the next import *sweep
away* every match in it. Such a response is reported as `refused` and the existing
file is left alone. Measured on the real provider:

```
  OK      premier-league  60 match row(s)  [new]
  OK      bundesliga      45 match row(s)  [new]
  OK      eredivisie      72 match row(s)  [new]
  OK      la-liga         78 match row(s)  [new]
  OK      ligue-1         54 match row(s)  [new]
  OK      serie-a         60 match row(s)  [new]
```

and the file arrives UTF-8 **with a BOM**, which is why the importer reads
`utf-8-sig`. Refreshing a throwaway copy of the real database from that file took
the Premier League from 50 to 60 matches — 50 updated, 10 inserted, 0 removed,
latest match 2026-09-20 → 2026-10-12 — with every existing id preserved.

**The file name is the season.** `data/raw/premier-league/2026-27.csv` is season
2026-27. This matters because the season is half of the key the import matches on:
a file called `2026-2027.csv` would not match the existing rows and would insert a
**second copy of every match** under a season of its own. Measured on the real
database, the Premier League's 50 matches became 100. So the name is validated, not
trusted (`scripts/seasons.py`):

- `2026_27.csv` and `2026/2027.csv` are read as the canonical `2026-27`.
- something that is not a season at all — `E0.csv` — is **skipped with an
  explanation** and the command exits non-zero, rather than inventing a season.

A whole-season file is self-correcting: anything the provider later fixes comes
with it. The import upserts on `(league, season, date, home_team, away_team)`, so
existing rows keep their id and their hand-entered stats, and it prints exactly
what changed:

```
  Premier League 2026-27: 60 rows (60 updated, 0 inserted, 0 removed)  [was 60]
```

A folder whose name is not in `LEAGUE_NAMES` is imported under a name derived from
the folder, and the run says so — otherwise a CSV dropped in `england/` would split
the league in two and halve the evidence behind every team in it.

Related trap, same cause: the league comes from the **folder**, never from the
file's own `Div` column, so a Bundesliga file dropped into `premier-league/` would
be filed as Premier League. The first division code in each file is checked against
the folder, and a mismatch is reported:

```
! premier-league\2099-00.csv: its Div column says 'D1' but this folder means 'E0'.
  It will be stored as Premier League. Move the file to the right folder.
```

### Why `matches.source` exists

The import removes rows the CSV no longer lists, because a postponed fixture
genuinely does move to another date. But it cannot tell a row that *vanished from
the file* from a row a human typed in — so before this was fixed, a refresh would
delete hand-added matches and orphan their stats, silently.

Every row now records where it came from: `source` is `'csv'` or `'manual'`, and
only `'csv'` rows may be swept. A hand-added match is kept, and the run says so:

```
    kept 1 hand-entered match(es) the CSV does not list
```

Section 8 of `verify_engine.py` guards this, on a throwaway copy of the database.

### 2. Add a match by hand

For a league with no CSV, or a match the file has not caught up with. The endpoint
exists (`POST /admin/matches`, written as `source='manual'`) and the match appears
in **Admin → Stats** as soon as it is completed.

### 3. Typing the eight hand-entered markets

This is **per match, not per team**, and that is not a matter of taste:

- The engine's unit is the match. It adds home + away to form a match total and
  compares that to the line, so a team's season average has nowhere to go.
- A number typed against a team has **no date**, so it cannot respect the cutoff
  rule. Every form figure in the app is "as of this fixture's date"; a hand-typed
  average would leak the future into the past, and a backtest could not include it
  honestly.
- A count also carries more than a percentage: 12 aerial duels won out of 18 gives
  you the 67% share, but 67% gives you nothing back.

So the order is: **refresh the CSVs first, then type.** A match only exists in the
database once the file containing it has been imported, and stats attach to a
match, so entering them before the refresh has nowhere to put them.

**Admin → Stats** lists completed matches with no manual entry, newest first, and
filters by league and by window (7 / 14 / 30 days, or any date) so a week can be
worked as a week. It reports the full count for the window, not just the rows on
screen. Each row opens a form with home and away side by side for all eight
markets; leave blank anything you do not have. Saving takes effect immediately —
the next analysis uses the numbers, and the backups capture them.

There is no coverage requirement. Shrinkage handles thin data (see *Early-season
fair lines are shrunk, not raw*), so three matches is still worth more than none,
and the app shows the sample size next to the figure so you can judge it yourself.

### 4. Team profiles: one average per team instead of two counts per match

Typing two counts per fixture does not scale to the eight markets no CSV carries.
Eight markets × two teams × a weekend of fixtures is hours of work, and a stats site
already publishes the thing the engine actually needs: each team's **per-game**
average.

That number is not a rival to the engine. The "home / away average" source is
literally *the home team's expected total plus the away team's expected total*, so
a per-game average is the same quantity the engine would otherwise derive from
match rows. A team profile substitutes for the **derivation**, not for the model.

**Admin → Profiles** is a table of teams × the eight manual markets. Type each
team's per-game figure and one date the figures were read, then save. Rules that
make it safe:

- **Measured data always wins.** A profile only fills a stat whose counted average
  is `None`, so turning one on can never overwrite a number the database counted.
- **It is never evidence.** A profile contributes a value and a stated weight
  (`PROFILE_WEIGHT = 3` pseudo-matches, so it stays a minority voice under
  shrinkage) but **no match ids** — and evidence points are counted from match ids.
  With two profiles and nothing counted the app says *"Based on 0 distinct
  match(es)"* and *"Low confidence"*, because that is the truth.
- **The date is enforced.** A profile is only used for a fixture on or after its
  `as_of` date, so a typed average can never rewrite the past. Analysis of an
  upcoming fixture is unaffected.
- **It says so where you read it.** The card labels the row *"Home / Away average
  (partly typed, not measured)"*, prints *"typed average, no match count"* instead
  of a fake sample size, and adds: *"Partly typed, not measured… it has no sample
  size behind it, so it cannot be backtested the way a counted average can."*
- **No league average, no shrink.** When a market has no measured league data, the
  "reference" it would shrink toward is only `DEFAULT_MARKET_LINES` — a guess. A
  typed average is not pulled toward a guess; the reference is dropped and the
  figure stands alone, and the UI says *"the team data alone — no league average
  to shrink toward"* rather than inventing a reliability percentage.
- **Typed, not measured, is the honest description.** A profile cannot be
  backtested and cannot be calibrated. It is a labelled input, not a finding.

Only the eight CSV-less markets are in a profile (`PROFILE_STATS`), so a profile
structurally cannot displace a measured market. `scripts/verify_engine.py` section
10 asserts all of the above, including the cutoff and the do-not-overwrite rule.

### Backing it up, and putting it back

```powershell
.venv\Scripts\python.exe scripts\export_data.py            # database -> data/export/
.venv\Scripts\python.exe scripts\export_data.py --verify   # compare, change nothing
.venv\Scripts\python.exe scripts\export_data.py --restore  # data/export/ -> database
```

The export writes every match, every hand-entered stat, every team profile, every
published fixture, every provider→team alias, the season calendar, the NBA
tables, and the board — the measured error (`prediction_stats`), the complete
matchup snapshot (`board_markets`) and the qualifying frozen picks
(`board_picks`). `--restore` matches on natural keys and **remaps ids
when it has to**, rewriting `manual_stats.match_id` through the same map — so
restoring into a freshly rebuilt database reconnects the hand-entered stats to the
right matches instead of orphaning them. Without that the export would be a pretty
file that loses the very thing it exists to protect. `--verify` now fails if *any*
of those tables is missing from the export, not just `manual_stats`.

The same export runs from **Admin → Data → Export now**. A full rebuild is then
lossless:

```powershell
.venv\Scripts\python.exe scripts\migrate_schema.py     # create/upgrade the schema
.venv\Scripts\python.exe scripts\import_data.py        # load the raw CSVs
.venv\Scripts\python.exe scripts\export_data.py --restore
```

### Seeing where you stand

**Admin → Data** answers the two questions you otherwise have to guess at:

- **Dataset freshness** — the latest match played, how many days behind today that
  is, and a **Download latest CSVs** / **Refresh from CSVs** pair that fetches and
  imports the whole dataset and reports what changed: new, refreshed, removed, and
  how much of your hand-entered work was kept. Real run, on the live database:
  `New matches 56 · Refreshed 4,429 · Removed 0`, freshness 30 days → 8 days
  behind. (The residual 8 days is the provider's own lag, not ours: its
  current-season file ended at 2026-10-12 on the day it was fetched.)
- **Markets with data** — completed matches behind each market, with the eight
  hand-entered ones tagged `MANUAL` and anything under 30 matches tagged `THIN`.
- **Hand-entered work** — manual stat records, hand-added matches, and orphaned
  records, which should be zero.
- **Backup** — when the last export ran, and a button to run another.

---

## Automatic fixtures (the forward calendar)

The CSV route above only knows matches that have already been **played**. To fill
the dashboard without publishing by hand every day, the backend also reads a
forward calendar from **football-data.org**.

### The free key

The fixture list costs nothing. The provider's Free plan is **€0/month**, covers
**12 competitions** (including all six of ours: Premier League, Bundesliga, La
Liga, Serie A, Ligue 1, Eredivisie) and allows **10 calls a minute**. Live scores
are the €12/month tier and are **not needed** — this tool analyses before
kickoff.

1. Register at <https://www.football-data.org/client/register> (Free plan, no card).
2. Copy the token.
3. Put it in `.env` in the project root:

   ```
   FOOTBALL_DATA_TOKEN=your-key
   ```

4. Restart the backend. **Admin → Fixtures** should say *Provider connected*.

The key is read from the environment first, then from `.env`. It is never written
to the database or logged.

### What happens on a normal day

```
football-data.org
      │  every 12 hours (and on startup)
      ▼
available_fixtures          ← candidates; the provider says these exist
      │
      │  mapping gate: league known · both names match a team in `matches`
      │                · scheduled · not a duplicate · has historical data
      ▼
published_matches           ← what the user dashboard reads
      │
      ▼
user PWA
```

**Admin → Fixtures** shows the next 14 days grouped by date, with kickoff, both
names and the historical evidence for each side (*counted strictly before
kickoff*). Each fixture is one of:

| State | Meaning |
|---|---|
| **Published** | already on the dashboard |
| **Needs mapping** | a provider name is not in the historical dataset |
| **No history** | mapped, but one side has no matches before kickoff |

and the buttons are **Fetch now**, **Publish selected** and **Publish all
eligible**, plus a per-league auto-publish switch.

### The mapping gate is deliberately strict

A provider calls a club `Manchester United FC`; the database calls it
`Man United`. If those were treated as two teams, the fixture would publish and
then analyse as *no usable data* while looking perfectly normal. So a name may
auto-publish only when one of these is true:

- an alias saved earlier matches it exactly;
- the provider name (or its short name) equals a name already in `matches`;
- the normalised names are identical (`Arsenal FC` → `Arsenal`);
- a curated exception names it (the big clubs in the six leagues).

Fuzzy matching is used **only** to suggest candidates in the UI. It is never
allowed to publish on its own. Anything unresolved waits in **Needs mapping**;
map it once and every future fixture with that name resolves. Saving a mapping
also re-checks the fixtures it was blocking and auto-publishes them in the same
step, so a spelling fix is the only action needed.

### What it never does

- **Never writes to `matches`.** Publishing is not evidence. The historical
  dataset still comes only from the CSV import.
- **Never un-publishes or re-publishes on refresh.** `published_id` survives a
  provider refresh; a fixture you hid stays hidden.
- **Never publishes a duplicate.** Same date + both teams is detected, and the
  existing published row is reused instead of inserted twice.
- **Never auto-publishes a fixture whose teams have no historical data**, because
  the only thing the dashboard could then show is "no usable data".

### The daily job

A single background thread checks every 30 minutes whether the last successful
fetch is older than **12 hours**. A laptop that was off at 06:00 therefore catches
up the moment it starts, and a server left running fetches once and then idles.
Set `SPORTS_ANALYZER_SCHEDULER=0` to turn it off (the tests do).

### Limits worth knowing

- Times arrive in **UTC**. Ghana is UTC+0, so they display unchanged. The free
  plan says *"schedules delayed"*: the fixture list is right, but a kickoff
  minute may lag. football-data.co.uk's `fixtures.csv` carries the exact `Time`
  for our six divisions and is the cross-check.
- If the provider is down, the existing `available_fixtures` stay and the
  dashboard is unaffected; the next fetch catches up.
- No Champions League: it is not in the historical dataset, so publishing it
  would only ever say "no data".

### The whole season, and the 14-day rule

The dashboard shows only published fixtures, and publishing only ever looks two
weeks ahead. That is deliberate: a projection for a match nine months away would
be this season's form wearing a date it does not belong to.

The season screen shows the rest of the calendar without analysing it. Open
**Full season** from the football dashboard, then pick a league and a season:

- played fixtures show their result and open the cutoff-aware analysis;
- fixtures inside the 14-day window show **Open ›** once published;
- anything beyond it says **"Opens 23 Oct"** — the date analysis begins, exactly
  14 days before kickoff.

The calendar is read-only. It is stored in `season_fixtures`, never published
from, and refreshed weekly (six provider calls, spaced out). Past seasons need no
stored calendar: they are served straight from `matches`, which is complete for
2024-25 and 2025-26.

---

## The NBA (a second sport, not a new market list)

The dashboard has a **Football / NBA** switch. The NBA is a separate module: its
own tables (`nba_*`), its own engine and its own publish gate. It reads and writes
no football table, and it does **not** inherit football's 45/35/20 weights,
`SHRINK_K = 10` or goals-based reference formula.

### Where the data comes from

`nba_api` against stats.nba.com — free, keyless, official. Three sources:

| Source | Used for |
|---|---|
| `LeagueGameLog` | completed team box scores (3 seasons, 3,690 games) |
| `ScheduleLeagueV2` | the forward calendar (1,200 regular-season games for 2026-27) |
| `ScoreboardV2` | quarter and half scores (1,230 games for 2024-25) |

**You do not need to run a command every day.** The backend runs an NBA
scheduler beside the football one. Every 30 minutes it checks the last successful
schedule pull; when that is more than **6 hours** old it:

1. pulls the current season's completed team box scores (`LeagueGameLog`);
2. pulls the current schedule (`ScheduleLeagueV2`);
3. updates existing games and every counting-stat column, not only the score;
4. auto-publishes eligible games inside the 14-day window;
5. fills up to two missing quarter/half-score dates;
6. records `last_history_fetch`, `last_schedule_fetch`, `last_publish` and
   `last_quarter_fetch`;
7. marks the Board stale so the next scheduler pass rebuilds it from the new
   data instead of waiting for the ordinary six-hour rebuild.

Availability is refreshed every 3 hours, rosters daily, and player production
every 3 days. A laptop that was asleep catches up on the next start; a failure is
recorded in `nba_sync_log` rather than silently skipped.

The same job can be run by hand for diagnosis:

```powershell
.venv\Scripts\python.exe scripts\nba_fetch.py --all
```

The manual command is a diagnostic, not part of the daily workflow. It is safe to
run because every write is an upsert on a natural key, but the scheduler already
does it.

### The model

Basketball is possessions, not events:

```
possessions = FGA + 0.44 * FTA - OREB + TOV
off_rating  = 100 * points / possessions
pace        = possessions * 48 / game_minutes

expected_off    = own_off * opponent_def / league_off
expected_points = expected_off * expected_possessions / 100
```

Each team has three nested estimates, each shrunk toward the one above it:

```
prior  = every stored game before the fixture   (k = 12)
season = this season, shrunk toward prior       (k = 8)
recent = last 10, shrunk toward season          (k = 5)
rating = 0.65 * season + 0.25 * recent + 0.10 * prior
```

That is what makes opening night honest instead of broken: with no games this
season, `season` collapses to `prior`, so the model uses last season and the panel
says so. Home edge is measured (1.8 points when built) and split between the two
team totals, so it never changes the game total.

The **fair line is the league-average matchup**, not the projection rounded —
rounding the projection would make "model above line" true by construction. Type
a real market line in the game panel and it replaces the league line and is
labelled `Market line`; otherwise the panel says `League line`.

Game total, each team total, first-half total and first-quarter total are shown,
but the period markets are **withheld with a reason** until at least 100 games
with line scores exist. Overtime is explicit: the projection is regulation, the
result includes OT, and `went_to_ot` is stored.

Every team counting stat uses the same unit as the rest of the engine: **count
per 100 possessions**, measured with SUM/SUM so fast games count for more. The
expected rate is `own × opponent allowed / league`, then multiplied by expected
possessions. The hierarchy and blend are the same as points. Two-pointers are
derived as **field goals made minus threes**, so a made three cannot be counted
twice.

Each of the eight stats — rebounds, 2-pointers, 3-pointers, assists, turnovers,
steals, blocks and fouls — produces **combined**, **home team** and **away team**
markets. Combined markets use the league reference; team markets use that team's
own per-game baseline on the board. Every one beats the reference line it is
tested against in the 2,500-game walk-forward:

| Stat | Combined MAE | Home MAE | Away MAE | Naive combined | Naive home | Naive away |
|---|---|---|---|---|---|---|
| Rebounds | 7.18 | 5.10 | 5.10 | 7.42 | 5.36 | 5.29 |
| 2-pointers | 5.64 | 3.86 | 3.97 | 6.26 | 4.02 | 4.15 |
| 3-pointers | 4.22 | 2.93 | 2.98 | 4.54 | 3.00 | 3.06 |
| Assists | 5.56 | 3.84 | 3.76 | 6.21 | 4.01 | 4.03 |
| Turnovers | 4.52 | 3.02 | 3.02 | 4.82 | 3.21 | 3.22 |
| Steals | 3.45 | 2.35 | 2.42 | 3.60 | 2.45 | 2.53 |
| Blocks | 2.74 | 1.91 | 1.86 | 2.85 | 1.99 | 1.97 |
| Fouls | 5.19 | 3.18 | 3.20 | 5.40 | 3.35 | 3.39 |

The team naive columns use the same own-baseline line the board actually tests,
not an easier league reference. Team baselines are not market lines. Where the
admin has typed a real line, it still wins; no typed counting-stat line columns
exist yet.

### Is it any good? The backtest says so

```powershell
.venv\Scripts\python.exe scripts\backtest_nba.py --season 2025-26
```

Every projection uses only games strictly before it, and is scored against the
actual result and against a naive "league average so far" baseline:

| Season | Games | MAE | Naive | Improvement | Side agreement |
|---|---|---|---|---|---|
| 2024-25 | 1,230 | **14.86** | 15.86 | **+1.00** | 63.2% (1,072) |
| 2025-26 | 1,230 | **15.35** | 15.92 | **+0.57** | 60.4% (1,004) |

"Side agreement" counts only games where the model was at least 1 point away from
the league line, so it is not rounding noise, and it is **not** a claim about
beating a market — no closing lines were used. A weight sweep moved MAE by at
most 0.07, so the documented blend was kept rather than tuned to noise.

### Publishing

`TEAM_ID` is the same integer in the schedule, the game log and the box score, so
the gate is small: regular season only, both teams known, status `scheduled`, both
teams have at least one completed game, inside 14 days. Auto-publish runs on every
schedule refresh and writes only `nba_published`. It never touches `nba_games`,
never deletes a publication and never un-publishes on refresh. There is no
recurring publish step.

The NBA dashboard works like football's: **‹ / ›** step a calendar day, the date
strip lists nearby matchdays, and the automatic landing starts at today and falls
back to the next matchday, saying so. Tapping a game opens the full game page:
projection and range, the rating matchup, counting stats and the sample note.

The game page's **Markets** section has its own filters, because exploring all 29
markets in one list would be unusable: **All**, **Points**, **Rebounds**,
**Assists**, **Turnovers**, **Steals**, **Blocks**, **2-pointers**,
**3-pointers** and **Fouls**. Each category shows the combined, home and away
lines that exist for it. Filtering is local to the already-fetched analysis; it
does not call the model again. The main Board stays selective, while this page
is where the complete statistical picture lives.

### Rosters, production and availability

The NBA now knows *who* plays, not just which team scored:

- **Official rosters.** `CommonTeamRoster` gives all 30 teams (**518 players**
  for 2026-27). A player who leaves is marked inactive, never deleted.
- **Official production.** `LeagueDashPlayerStats` adds **1,723 player-season
  rows** across 2023-24 → 2025-26: minutes, points, rebounds, assists, steals,
  blocks, turnovers, shooting, true-shooting and a usage proxy.
- **Availability.** An ESPN injury snapshot is stored with a capture time and
  matched to an NBA player id by name. **Admin → NBA** shows the latest
  snapshot, estimated minutes unavailable, and every sync job with its errors.

Two rules keep this honest:

1. **Confirmed is not projected.** A roster spot is confirmed by the league. A
   role is minutes-based and labelled as such — there is no starts column, so
   the app never claims a confirmed lineup.
2. **Availability is displayed, not priced.** The injury feed has no history,
   so its effect cannot be measured. It never moves a probability until enough
   stored history clears the same gates as every other factor.

Production is joined on the player, not the team, so a summer signing still
shows the evidence he earned at his old club. The provider attributes a
player's whole season to his final team, so per-team minute sums are not a
rotation and are never used as one.

Sync cadence: availability every 3 hours, rosters daily, production every 3
days. Refresh by hand with **Admin → NBA → Refresh rosters & availability**, or:

```powershell
.venv\Scripts\python.exe scripts\nba_people.py --rosters --stats --injuries
```

### What the NBA deliberately leaves out

- **Player props.** Minutes, lineups, rotations, usage, trades and blowouts
  make them a false-confidence machine early in a season.
- **Rest days, back-to-backs and travel as model inputs.** They are computed
  for the board's context layer, but none moves a projection until it clears
  the measured gates. That integration is the next phase, not a claim.
- **Live in-game scores.** Final results and quarter lines sync automatically
  every six hours; a published game keeps its pre-game analysis.
- **A market-line feed.** Point lines are typed, as football's are. Counting-stat
  lines are league/team references; there is no typed rebound, assist, turnover,
  steal, block, foul or shooting line yet.

### NBA checks

```powershell
.venv\Scripts\python.exe scripts\verify_nba.py          # 57 structural checks, offline, on a copy
.venv\Scripts\python.exe scripts\verify_nba_people.py   # 32 roster/availability checks, offline
.venv\Scripts\python.exe scripts\backtest_nba.py        # walk-forward MAE
```

`verify_nba.py` guards identity, the possessions formula, the cutoff, x.5 lines,
baseline arithmetic, all 29 counting-stat projections and scopes, the Board's
taxonomy/rank/filter contract, default selection/reason, publish idempotency,
and that NBA publishing changes **zero** football rows.

---

## Daily workflow

**Weekly data refresh** — see *Keeping the dataset current* above. **Admin → Data
→ Download latest CSVs** fetches the season for every league and refreshes in one
click (or drop the files in `data/raw/<league>/<season>.csv` yourself, file name =
season, then **Refresh from CSVs**). Hand-entered work is kept.

**Then the eight manual markets** — **Admin → Stats**, filtered to the last week
and a league. Work the list top to bottom; each row is one match, home and away
side by side. Refreshing first is what puts the week's fixtures in the list.

**Or, once a week, the per-game averages** — **Admin → Profiles**, one league and
season at a time: type each team's per-game figure for the markets you have data
for, set the date you read them, save. This is the cheap route for the eight
CSV-less markets; it is labelled as typed rather than measured everywhere it is
used, and counts entered on the Stats tab always take precedence.

**Manual stats for a match**

Use the admin UI (**Admin → Stats**) or:

```powershell
.venv\Scripts\python.exe scripts\manual_stats.py --match-id 15196 `
    --home-tackles 17 --away-tackles 21 --home-saves 4
```

**Publish matches for the users**

Normally this happens without you: **Admin → Fixtures** fetches the forward
calendar and auto-publishes what passes the gate. You only open it when a row says
**Needs mapping**. To add a fixture the provider does not cover, use **Admin →
Publish**; publishing is what makes a fixture visible on the dashboard. Duplicate
fixtures (same date and both teams) are rejected with a clear message rather than
silently duplicated.

The dashboard shows one day at a time and starts at today. If today has nothing
published it falls back to the **nearest day that does** — the next published day
if there is one ahead, otherwise the most recent one behind — and says so on
screen: *"Nothing is published for today. Showing the next matchday, Wed 21
Oct."* The date in the header changes to the day actually on screen.

You can then move around without waiting for the fallback:

- **‹ / ›** in the header step one calendar day at a time, forwards or backwards.
- The **date strip** under the header lists the nearby matchdays that actually
  have published fixtures, plus today. Tap one to jump straight to it; days with
  no published matches are not offered, so you land on real fixtures.
- **Tap the date itself** to go back to today. If today is empty, that also
  restores the nearest-matchday fallback above.
- The **Today** chip takes you to today exactly, even when today has no matches —
  it will say "No published matches" rather than quietly jumping elsewhere.

An explicitly chosen date never falls back on its own: if you ask for the 22nd,
you get the 22nd, empty or not. Only the start-at-today path jumps.

That fallback exists because of a real trap: publish a Saturday fixture and on
Sunday the dashboard would read "No published matches" while the fixture sat in
the database. An empty screen is indistinguishable from "there is nothing", which
is a lie by omission. `GET /published/dates` is what makes both the fallback and
the date strip possible; it lists only days with a `published` (not `hidden`)
fixture.

---

## Checks

```powershell
.venv\Scripts\python.exe scripts\verify_engine.py         # engine, backtest maths, import safety
.venv\Scripts\python.exe scripts\verify_fixtures.py      # fixture mapping gate, auto-publish, duplicates
.venv\Scripts\python.exe scripts\verify_nba.py           # NBA schema, formula, cutoff, 29 markets, cards, publish gate (57)
.venv\Scripts\python.exe scripts\backtest_nba.py         # NBA walk-forward MAE vs naive baseline
.venv\Scripts\python.exe scripts\smoke_test.py            # end-to-end HTTP (server must be running)
.venv\Scripts\python.exe scripts\export_data.py --verify  # database vs the CSV export
.venv\Scripts\python.exe scripts\db_info.py               # schema and row counts
.venv\Scripts\python.exe scripts\explore.py               # row counts per league/season, market coverage
.venv\Scripts\python.exe scripts\migrate_schema.py        # create/upgrade the schema, idempotent
```

These exist because several bugs in this project were **silent** — no crash, just
a plausible wrong number, or work quietly deleted. `verify_engine.py` pins the
values that were checked by hand so a refactor cannot change them unnoticed. Its
later sections each cover one class of silent failure found so far:

- **1** — leakage: a cutoff that lets a match see its own result.
- **6** — whole-number fair lines, which turn matches into pushes and book every
  push as an UNDER.
- **7** — significance maths; if the Wilson interval or the McNemar z is wrong,
  the whole backtest report lies.
- **8** — a CSV refresh deleting hand-entered matches (runs on a copy).
- **9** — hand-entered counts failing to reach the engine, or disturbing a
  CSV-backed statistic while doing so (runs on a copy).
- **10** — a typed team profile outranking counted data, being used before its
  date, reaching a CSV-backed market, or passing itself off as evidence.

---

## Known limits

- **No Champions League data.** football-data.co.uk covers domestic leagues only,
  and no free source was found. Open problem.
- **A CSV figure cannot be hand-corrected.** `manual_stats` holds only the eight
  manual-only markets, so a wrong corner or foul count in a provider file has no
  path to correction from the app — the CSV is the single source for those
  markets. Extending the overlay is a small change (add the columns, widen
  `MANUAL_STAT_COLUMNS`), deliberately not made: with the season CSV replaced on
  each refresh, an error in it is usually fixed by the provider anyway.
- **The download is a download, not an API.** football-data.co.uk publishes static
  season files and nothing else, so **Admin → Data → Download latest CSVs** fetches
  those files over HTTP and validates them before they may replace what is on disk.
  Leagues with no mapping in `LEAGUE_DIV_CODES` (Champions League) stay manual, and
  the provider updates its own file with a lag — a fresh download still ends a few
  days behind today.
- **A typed team profile is not evidence and cannot be measured.** It is a labelled
  input for markets nothing counts, it carries no match ids, and nothing about it
  can be backtested or calibrated. Where a profile is the only source the app says
  *"Based on 0 distinct match(es)"* and *"Low confidence"* rather than manufacturing
  a sample. Counts typed on the Stats tab always take precedence.
- **A profile does not produce a fair line.** With no measured league data for a
  manual market, the line still falls back to `DEFAULT_MARKET_LINES`, so the line
  stays generic even when both teams have typed averages. Deriving a league
  reference from profiles is deliberately not done: it would turn typed numbers
  into a benchmark that looks measured.
- **The eight hand-entered markets start empty** and only hold what has been typed
  in (per-match counts, or per-team averages). With no data at all they return
  `weighted_avg: null` — no number is invented from a league prior. Only the *line*
  falls back to a default, and the market is flagged `THIN` until real data exists.
- **The 2026-27 season is partial** (426 matches across the six leagues, latest
  2026-10-19).
- **Admin authentication is environment-based.** Production requires a long
  `ADMIN_TOKEN`; the browser sends it as `X-Admin-Token` and keeps it in
  `sessionStorage`, never in the page source. Local development without the
  variable remains open, so do not expose a local instance directly. The
  production startup guard fails closed if the token is missing.
- **No audit trail** (`data_sources` / `data_imports` are not implemented), so a
  number cannot yet be traced back to the file it came from.
- **SQLite**, not PostgreSQL. The API is written so the storage swap does not
  require rebuilding the frontend.
- **Player-level data** is not started. Team totals are the NBA v1 unit; player
  props need minutes, lineups, injuries and rotations, and they are deliberately
  out of scope until the team model is measured.
- **The NBA has no current-season sample at launch.** 2026-27 began on
  2026-10-20, so every projection is built from the three prior seasons (246
  team-games per club) and the panel says *"No 2026-27 games have been played
  yet"*. That is the honest state, not a bug.
- **The NBA's period markets rest on 2024-25.** stats.nba.com has a known
  line-score gap for 2025-26 (22 Oct – 25 Dec), so first-half and first-quarter
  totals are measured on 1,230 games from the season before. Below 100 games with
  line scores they are withheld with a reason rather than guessed.
- **NBA final scores are stored, but a published game keeps its pre-game
  projection.** The game page does not rewrite a projection after the result;
  the Board grades the frozen rows from `nba_games`. There is no live in-game
  score feed.
- **There is no dedicated NBA admin tab.** Auto-publish leaves almost nothing to
  administer; market lines are typed in the game panel. A status view exists at
  `/nba/status`.
- **The NBA backtest is not a market backtest.** It scores the model against the
  actual result and a naive baseline. No historical closing lines are stored, so
  no claim is made about beating a bookmaker.
- **No European competitions.** Champions League fixtures and score history are
  on the free plan and were measured and deferred (the provider publishes no
  corners, cards or shots for them, so six of the seven counted markets would
  have no competition baseline). See §19 of `SYSTEM.md`.
- **No AI explanation layer.** It is deliberately deferred, and when it arrives it
  must never invent numbers — it may only narrate what the engine already
  computed.
