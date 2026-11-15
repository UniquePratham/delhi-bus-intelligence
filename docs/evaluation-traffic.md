# Evaluation — traffic model

All numbers below are produced by the scripts named in each section and stored
in `data/artifacts/`. Nothing here is estimated by hand.

## 1. Segment-level accuracy (held-out day)

Command: `python -m dbi.traffic.evaluate`

Setup: profiles are fitted on ten days covering a full week (Mon 5 Oct – Sun
11 Oct) plus three later Thursdays. The held-out day is **2026-10-29**, which is
never used to fit anything. Evaluation is on 1,241,069 observed
station-to-station runs.

| Model | MAE (s) | RMSE (s) | Median AE (s) | MAPE | Bias (s) |
|---|---:|---:|---:|---:|---:|
| GTFS timetable | 41.63 | 57.51 | 30.0 | 69.2% | +16.98 |
| Global median | 38.05 | 55.57 | 27.0 | 69.4% | −15.85 |
| Per-pair median (time-blind) | 23.24 | 37.81 | 11.0 | 36.0% | −6.07 |
| **This model** | **22.39** | **36.38** | **11.0** | **35.9%** | **−1.06** |

The model beats the timetable by **46%** on MAE and cuts bias from +17 s to
−1 s. It also beats a per-pair median that ignores time of day, which is the
evidence that the time dimension is doing real work.

### Accuracy by hour band

| Hour band | Runs | Model MAE (s) | Timetable MAE (s) |
|---|---:|---:|---:|
| night (00–05) | 44,174 | 12.49 | 47.58 |
| early_peak (06–07) | 140,352 | 15.50 | 43.10 |
| morning_peak (08–11) | 397,717 | 21.04 | 39.82 |
| midday (12–16) | 337,011 | 24.51 | 41.98 |
| evening_peak (17–20) | 255,070 | 27.74 | 41.87 |
| late (21–23) | 66,745 | 20.28 | 42.77 |

The model is worst in the evening peak, which is expected: that is when
travel times are most variable, not merely most congested.

### Which level of the hierarchy fires

| Level | Runs | Share |
|---|---:|---:|
| pair × hour-of-week | 1,232,930 | 99.3% |
| pair × hour-band | 7,459 | 0.6% |
| pair (all hours) | 617 | 0.0% |
| global hour-band | 63 | 0.0% |

99.3% of predictions come from the most specific level, so the fallbacks are
not carrying the result.

## 2. Temporal differentiation

Command: `python -m dbi.traffic.temporal`

This is the evidence that the extra days did real work. It compares the model's
own predictions for the same station pairs across day types and hours.

| Comparison | Pairs where the first is slower |
|---|---:|
| Monday 09:00 vs Monday 02:00 | **83.9%** |
| Monday 09:00 vs Saturday 09:00 | **48.0%** |
| Monday 09:00 vs Sunday 09:00 | **63.5%** |

All seven weekdays have profiles: Mon, Tue, Wed, Thu, Fri, Sat, Sun.

An earlier version of this project used five Thursdays, so Monday–Wednesday and
weekends had no hour-of-week evidence and fell back to hour-band level. It also
had a weekday-convention bug (see §5) that inverted the weekend comparison.

## 3. Journey-level accuracy (real observed trips)

Command: `python -m dbi.journey.validate --sample 400`

Segment MAE does not tell you whether a journey estimate is right. This test
reconstructs 400 complete trips that vehicles actually drove on the held-out
day, then scores the model on **the same station sequence the vehicle drove**,
at the time it drove it.

Scoring the planner's freely chosen path instead would reward it for finding a
shortcut and make the error meaningless — an earlier version of this script did
exactly that and reported a spurious 4.1 min MAE with a −4.05 min bias.

| Metric | Value |
|---|---:|
| Trips evaluated | 400 |
| **MAE** | **0.93 min** |
| Median absolute error | 0.70 min |
| p90 absolute error | 2.10 min |
| Bias | −0.38 min |
| Within 5 min | 99.2% |
| Within 10 min | 100.0% |
| Timetable MAE (same paths) | 2.32 min |
| **Improvement over timetable** | **60.1%** |

## 4. What is not claimed

* **No Google Maps comparison.** The brief suggests it. Google's terms do not
  permit scraping, and a car route is not a bus route, so it would not be a
  valid ground truth. The observed-trip comparison above is stronger evidence
  and uses only the supplied data.
* **No claim of statistical generality.** 400 trips is a sample, not a census.
  The segment metrics cover 1.24 M runs and are more robust.
* **No live validation.** Both datasets are historical. Nothing here has been
  tested against a live feed.

## 5. Bugs found during revalidation

Recorded because they are the kind that produce plausible output while being
wrong, and because the fixes are what the numbers above depend on.

| Bug | Effect | Fix |
|---|---|---|
| DuckDB `extract(dow)` is 0=Sunday; Python `weekday()` is 0=Monday | Every hour-of-week lookup was shifted by one day, so a Tuesday query was served Monday's profile. Weekend comparisons were inverted. | Convert with `(dow + 6) % 7` in both pipelines; pinned by `tests/test_time_conventions.py` |
| `hour_of_week` stored as `TINYINT` | Overflowed at 127 once all seven weekdays were present, so the model could not be built on a full week | `SMALLINT` |
| Alternatives found by banning every station on the path | Long trips returned a single option | Ban only the first ride's route |
| Crowding tables loaded per request | 5–20 s per query | Load once at startup; compact array store |
| Route-name fallback accepted ambiguous keys | `D-0001DOWN` linked to `DW-1DOWN`, attaching crowding from the wrong route | Accept only unambiguous keys; skip and report the rest |
