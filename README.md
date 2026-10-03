# CalorieTracker

A local, multi-person calorie and body-composition tracker. You build your own
food database by typing what is on the package, log foods and exercise by how
much you had, and the app fits *your* daily energy burn from your own weight
history so it can project where you are heading.

One local SQLite file. No accounts, no cloud, no API keys.

```powershell
python -m pip install -r requirements.txt
python run.py          # http://127.0.0.1:8000
```

## The vocabulary

This distinction runs through the whole app, so it is worth stating plainly:

- A **blueprint** (food or activity) is the reusable definition. `煮鸡蛋`, 649 kJ
  per 100 g. It belongs to nobody.
- A **record** is one instance of a blueprint, attributed to exactly **one**
  person, on one day, in a given amount. Kuro ate 2 pieces at breakfast today.

Blueprints are shared by everyone. Records are private to their person. A record
never gets split between people — if two people ate the same thing, that is two
records.

## Screens

**Log** — daily totals for whoever the header has selected. Energy against
target, a tile per macro, records grouped by meal with activity in its own
group.

**Database** — foods and activities, in two filtered lists. Foods are per any
gram amount (default 100 g); activities are per session and must carry negative
energy.

**People** — each person's date of birth, height, sex constant and movement
multiplier; their weight log; and a weight curve.

**Predict** — the projection: weight, BMI, and simulated intake-vs-burn, with a
horizon table and the forecast archive.

**Targets & units** — per-person daily targets, the global energy unit, and the
model constants.

## How the energy model works

```
Δweight/day = (food − logged activity − burn) / 32213 kJ/kg
```

`burn` is everything you spend that is **not** deliberate logged activity.
The app estimates it two ways and prefers the measured one:

| | source | available |
| --- | --- | --- |
| **formula** | Mifflin-St Jeor BMR × movement multiplier | immediately |
| **fitted** | solved from your weight + intake history | after ~7 days with 2 weigh-ins |

Fitting is a closed-form least squares with one unknown:

```
burn = mean( lagged_intake − Δweight × 32213 )
```

Three details that matter:

- **The lag kernel.** Today's scale reflects the last few days of eating, not
  just today, so intake is passed through a 4-day kernel before the comparison.
  Editable; values are normalised on save.
- **Interpolated, never extrapolated, weight.** Weigh-ins are spread linearly
  between them, and the series stops at your most recent weigh-in. The model
  never claims to know a weight you have not measured.
- **A plausibility guard.** If the fitted burn lands outside 0.5×–2× the formula
  estimate, it is rejected and the formula is used instead, with a message. One
  mistyped weight or one record logged in the wrong unit would otherwise imply a
  burn of 1 MJ/day and produce a nonsense forecast.

Accuracy, measured in `tests/test_body.py` against synthetic people whose true
burn is planted: recovers 7968/9969/12470 kJ against planted 8000/10000/12500,
and stays within 1% at activity frequencies from daily to every 30th day.

## How the projection works

The uncertainty is in the **inputs**, not the weight. Weight is the integral of
the balance, so simulating it directly compounds error into a random walk whose
band is useless within a year.

Instead:

1. Collect the person's completed days (today is never used — an incomplete day
   would drag the fit).
2. Resample realistic future intake by **block bootstrap**: draw real 3–7 day
   blocks, stratified by day-of-week, keeping each day's intake paired with its
   activity. This preserves the weekly rhythm and the serial correlation without
   estimating a transition matrix that 30 days of data cannot support.
3. Push each of ~1500 paths through the deterministic equation above.
4. Report percentiles.

The result is a band that is tight at two weeks and honest at a year, rather
than uniformly vague or falsely precise.

Burn uncertainty is drawn per path too, so parameter error propagates into the
band instead of being hidden.

The seed is derived from the person, the as-of date and the inputs, so
re-rendering the chart does not make it jitter.

## Why block bootstrap rather than a Markov chain

A first-order Markov chain over intake needs a K×K transition matrix: roughly
200–400 days of data for 5 states. With a 30-day window you would be fitting 20
parameters from 30 observations, and a confidently-wrong matrix produces bands
that are *too narrow* — precise exactly when it has no right to be.

Measured day-to-day energy-intake autocorrelation in adults is weak (r ≈ 0.1–0.3),
so at that level "tomorrow ≈ your mean" is nearly as good a forecast and the
simulated paths come out nearly identical to plain resampling. The block
bootstrap delivers the same "future looks like the past, dependence included"
with zero fitted parameters.

## Forecast archive and scoring

*Save this forecast* freezes the current projection with the parameters it was
made from: burn, kJ/kg, lag kernel, seed, model version. Snapshots are immutable
on purpose — if old forecasts were silently recomputed with a better model, you
could never tell whether it had actually improved.

Once a horizon date arrives and there is a real weigh-in near it, each forecast
is scored:

- **MAE** — mean absolute error in kg
- **Bias** — signed. Positive means you weighed more than predicted.
- **Within 1 kg** — how often the median was close
- **In band** — how often reality landed inside the 5–95% band. If this is far
  below 90% the band is lying about its own confidence; far above means it is
  needlessly wide.

A horizon with no weigh-in within ±4 days is skipped, not scored against a
stale number.

Tick any subset of saved forecasts to overlay them on the chart.

## Honest reporting

- **Missing nutrients are shown, not filled in silently.** Each nutrient carries
  a coverage figure — the share of the day's food that actually had a value.
  Affected tiles are outlined in amber and annotated (`~8% of today's food has
  no sugar value`). A nutrient nothing was recorded for reads `no data`, never
  `0 g`.
- **Energy is required**, but if a row somehow has none, the day falls back to
  Atwater factors (17/17/37 kJ per g) and is tagged `est.`; with nothing at all
  it reports `unavailable` rather than `0`.
- **Cold start is labelled.** With no weight data the projection runs off the
  formula and is stamped *uncalibrated*, with what to do about it. With no weight
  at all it refuses and says so.
- **Activity never touches macros.** A 10 km run cannot subtract 40 g of protein.

## Layout

```
app/
  db.py          schema + migration (no ORM, no external DB)
  body.py        BMR, weight interpolation, lag kernel, burn fit, block bootstrap
  nutrition.py   unit conversion, per-100 g normalisation, coverage aggregation
  repo.py        queries: blueprints, records, people, weights, forecasts
  main.py        FastAPI routes + request validation
  templates/     single page
  static/        style.css, app.js (hand-rolled SVG charts, no JS dependencies)
tests/
  test_body.py      model maths; recovers planted burns
  test_core.py      blueprints, units, coverage, CRUD
  test_people.py    people, weights, records, activities, forecasts, scoring
  test_migration.py old database -> new schema, data preserved
  test_ui.py        real Chromium; fails on any console error
  screenshot.py     renders shot-*.png from a demo dataset
```

`body.py` has no I/O, which is why the maths can be tested directly against
known answers.

## Tests

```powershell
python tests/test_body.py
python tests/test_core.py
python tests/test_people.py
python tests/test_migration.py

# browser tests need: pip install playwright; playwright install chromium
python tests/test_ui.py
python tests/screenshot.py     # writes shot-*.png
```

All use throwaway databases; your real data is never touched.

## API

Interactive docs at `/docs`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/config` | settings, people, constants |
| `GET` | `/api/foods?kind=food\|activity` | list; per-100 g in the display unit |
| `POST` `PUT` `DELETE` | `/api/foods[/{id}]` | blueprints |
| `POST` | `/api/foods/{id}/derive-energy` | estimate energy from macros |
| `GET` `POST` `PUT` `DELETE` | `/api/persons[/{id}]` | people (targets live here) |
| `GET` | `/api/persons/{id}/metrics` | weight, BMI, BMR, both burn estimates |
| `GET` `POST` | `/api/persons/{id}/weights` | weight log (one per day) |
| `POST` | `/api/entries` | record |
| `DELETE` | `/api/entries/{id}` | remove record |
| `GET` | `/api/day/{date}?person_id=` | totals, coverage, per-meal, targets |
| `GET` | `/api/history?person_id=&days=` | recent days |
| `GET` | `/api/persons/{id}/forecast?save=true` | project, optionally freeze |
| `GET` `DELETE` | `/api/persons/{id}/forecasts`, `/api/forecasts/{id}` | archive + scorecard |
| `PUT` | `/api/settings` | display unit, model constants |

## Not built

Recipes, barcode scanning, favourites, body-fat percentage, CSV export, cloud
sync. The schema has room; they are simply not there.
