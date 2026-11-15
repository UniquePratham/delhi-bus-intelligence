# Evaluation — crowding model

Command: `python -m dbi.crowding.evaluate`

There is **no ground-truth occupancy** in the supplied data, so this document
does not report an accuracy figure it cannot support. Instead the model is
checked against patterns that must hold if the reconstruction is sound.

## 1. Hourly demand profile

| Hour | Boardings | | Hour | Boardings |
|---:|---:|---|---:|---:|
| 00 | 1,574 | | 12 | 92,972 |
| 01 | 607 | | 13 | 77,220 |
| 02 | 447 | | 14 | 75,468 |
| 03 | 470 | | 15 | 81,242 |
| 04 | 2,160 | | 16 | 107,848 |
| 05 | 25,590 | | 17 | 179,427 |
| 06 | 71,507 | | 18 | 254,896 |
| 07 | 158,390 | | 19 | 239,134 |
| **08** | **334,404** | | 20 | 179,487 |
| **09** | **374,218** | | 21 | 105,056 |
| 10 | 216,108 | | 22 | 46,592 |
| 11 | 119,819 | | 23 | 8,970 |

* Peak hour: **09:00** (374,218 boardings)
* Trough hour: **02:00** (447 boardings)
* **Peak-to-trough ratio: 837×**

Two clear peaks (08–09 and 18–19) with a midday dip and a collapse overnight.
This is the expected shape for a commuter network, and it is the strongest
evidence that the reconstruction is tracking real demand rather than noise.

## 2. Load shape along a route

For 831 routes with at least 8 well-observed stretches, the correlation between
stop sequence and load index:

| Metric | Value |
|---|---:|
| Routes tested | 831 |
| Median correlation | +0.136 |
| Routes with rising load | 60.5% |

A positive correlation means load grows along the route, which is the expected
boarding/alighting pattern: a bus fills as it runs and empties toward the
terminus. The median is modest because many routes are already full at their
origin, so the curve is flat rather than rising.

## 3. Weekday vs weekend

| Period | Boardings |
|---|---:|
| Weekdays (5 days) | 2,147,861 |
| Weekend (2 days) | 605,745 |
| Ratio | 3.55× |

Per day, a weekday carries ~430k boardings against ~303k for a weekend day, so
weekdays are ~1.4× busier per day. This is the expected direction and it is
what the model now reproduces.

An earlier version reported the opposite (weekend busier than weekday) because
of a weekday-convention bug: DuckDB's `extract(dow)` is 0=Sunday while Python's
`weekday()` is 0=Monday, so every day was mislabelled by one. See
[evaluation-traffic.md](evaluation-traffic.md) §5.

## 4. Week-to-week stability

Correlation of the route × hour profile between consecutive weeks:

| Comparison | Correlation |
|---|---:|
| Week 0 vs week 1 | 0.958 |
| Week 1 vs week 2 | 0.968 |
| Week 2 vs week 3 | 0.973 |

18,208 route × hour cells. Correlations above 0.95 mean the profile is stable
enough to be worth publishing: a commuter who learns the pattern in week 1 will
not be misled in week 4.

## 5. Coverage

| Metric | Value |
|---|---:|
| Routes with crowding data | 1,424 |
| Routes in the ticket file | 2,259 |
| **Route coverage** | **63.0%** |
| Cells (route × hour × station) | 460,415 |
| High confidence | 1,344 (0.3%) |
| Medium confidence | 57,279 (12.4%) |
| Low confidence | 401,792 (87.3%) |

Coverage is the honest weak point. 37% of routes have no crowding estimate at
all, and 87% of cells are low confidence. The causes are structural:

* The ticket file is a ~2% sample, so most route × hour × stretch cells have
  only a handful of observations.
* Ticket stop names match GTFS stations for ~93% of ticket volume, so ~7% of
  boardings cannot be placed on the network at all.
* 909 ticket route strings have no GTFS route at all — depot-specific variants
  like `OMS(+)BURARI-DHAULAKUAN` that the static feed does not model. They hold
  13% of ticket volume.
* A further 366 route strings reduce to an ambiguous key (`405STLUP` → `405UP`
  matches several routes). These are **skipped rather than guessed**, because
  attaching crowding from the wrong route would be worse than reporting
  `unknown`.

In practice, **72% of bus legs in a typical journey carry a crowding estimate**
(measured across seven origin–destination pairs), because the routes a planner
actually uses are the busy ones the ticket file covers well.

The API reports `unknown` for uncovered cases rather than guessing, and the
planner falls back through route-hour and route-level averages, marking each
fallback with its scope and capping confidence at `low`.

## 6. Category distribution

| Category | Cells | Share |
|---|---:|---:|
| seats_available | 280,189 | 61.4% |
| standing_room | 110,376 | 24.2% |
| crowded | 65,752 | 14.4% |

Categories are **relative to each route's own load distribution** (60th and 85th
percentiles), not absolute occupancy. A "crowded" stretch is one that is busy
*for that route*. This is deliberate: absolute load is not identifiable from a
sample, so an absolute threshold would be a fabricated number.

## 7. What is not claimed

* **No occupancy percentage.** The data cannot support it. See
  [data-audit.md](data-audit.md) §3.
* **No passenger counts.** `load_index` is a relative index, not a headcount.
* **No ground-truth accuracy.** There is no independent occupancy observation
  in the supplied data to compare against. The pattern checks above are
  consistency evidence, not accuracy evidence.
* **No claim that low-confidence cells are usable.** 87.3% of cells are low
  confidence and the UI says so.
