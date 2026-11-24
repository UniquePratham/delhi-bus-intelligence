# Data audit

Everything below was measured on the supplied artifacts, not assumed. Commands
to reproduce each number are given.

## 1. Artifact verification

| File | Size | SHA-256 | Status |
|---|---:|---|---|
| `delhi-dtc-bus-gtfs-rt-2026-10.7z` | 15.3 GB | `703bd6ea…c16c9` | matches `SHA256SUMS` |
| `October-Delhi.csv.zip` | 63.1 MB | `3b0d051b…df7be` | matches `SHA256SUMS` |

The 7z archive is non-solid and holds 31 entries, `bus_movements_2026_10_01.db`
through `bus_movements_2026_10_31.db`. A single day extracts in ~30 s and is
~4.5 GB.

## 2. GPS feed (`vehicle_feed`)

Schema as documented, with `lat`/`lng` stored as **text** and `route_id` as a
**float**.

Measured on 2026-10-15:

| Metric | Value |
|---|---:|
| Rows | 29,271,900 |
| Distinct `(vehicle_id, timestamp)` | 18,288,180 |
| Duplicate rows | 37.5% |
| Distinct vehicles | 6,093 |
| Distinct `route_id` | 2,127 |
| Distinct `trip_id` | 88,114 |
| Distinct timestamps | 86,222 |
| Median ping interval | 11 s |
| Rows outside Delhi bbox | 16,626 (0.06%) |
| `speed` range | 0–50 (units ambiguous) |

### Timestamp semantics

`timestamp` is Unix epoch seconds in **UTC**. `date` and `time` are the IST
rendering of the same instant. Verified:

```
timestamp 1792002594 -> 2026-10-14 23:59:54 UTC -> 2026-10-15 00:00 IST
```

The `time` column is truncated to the second and is therefore not usable for
sub-second ordering; `timestamp` is.

### The `speed` column is not usable

The reported `speed` has a median of 10.3 while the speed implied by
consecutive positions has a median of 2.0 m/s. The field is not in m/s and its
units are undocumented, so **it is not used anywhere in this project**. All
speeds are derived from position deltas.

## 3. Ticketing feed

| Metric | Value |
|---|---:|
| Rows | 2,559,899 |
| Passengers (`passenger_count` sum) | 2,753,606 |
| Distinct routes | 2,259 |
| Distinct vehicles | 6,810 |
| Date range | 2026-10-01 00:00:01 to 2026-10-31 23:58:22 IST |
| `vehicle_number` null | ~50% |
| Distinct `ticket_type_id` | 1 (all rows) |

### The ticket file is a sample, not a census

This is the single most important finding for Problem 2.

| Metric | Value |
|---|---:|
| Tickets per day | ~100,000 |
| Vehicles per day | ~6,000 |
| **Passengers per vehicle-day** | **15.2 (median 6)** |

A Delhi bus carries several hundred passengers a day. The file therefore
represents roughly 2% of ridership. Consequences:

* Absolute onboard load **cannot** be recovered. Any per-trip occupancy figure
  would be wrong by an unknown factor.
* Relative structure **is** recoverable, because the sample is spread across
  the same routes, hours and stop sequences as the population.

The sample is also not a clean fixed fraction: tickets-per-1000-GPS-pings varies
17× between vehicles (p05 0.50, p50 2.27, p95 8.71), and correlates only weakly
with ping volume (r = 0.14). A per-vehicle scaling factor is therefore not
defensible either.

## 4. Static GTFS

Not bundled with the artifacts. Sourced from the Delhi Open Transit Data feed
(`otd.delhi.gov.in`), mirrored at `ethanc8/delhi-gtfs` because the upstream
download endpoint returns HTTP 504.

| File | Rows |
|---|---:|
| `stops.txt` | 10,559 |
| `routes.txt` | 2,403 |
| `trips.txt` | 89,393 |
| `stop_times.txt` | 3,724,320 |
| `calendar.txt` | 1 service, valid 2024-01-01 to 2027-01-01 |

`shapes.txt` is **absent** from this feed, so published route geometry is not
available. The project works from stop sequences for travel times, and routes
map polylines on the real OpenStreetMap Delhi road network for the map (see
`scripts/build_osm_from_pbf.py` and `scripts/route_on_osm.py`).

### Stop IDs are platform-level, not station-level

10,559 `stop_id`s collapse to 3,963 distinct names. Same-name stops sit a median
**29 m** apart (p90 401 m). Snapping GPS to raw `stop_id`s produces spurious
"runs" between co-located platforms — measured as a median run time of 33 s
against a scheduled 80 s, i.e. a 2.4× under-estimate.

Fix: cluster stops within 60 m into **stations** (union-find). 10,559 stops →
3,688 stations, with p99 intra-station spread of 51 m and no self-loops.

## 5. Entity linkage

### GPS `route_id` does **not** join to GTFS `route_id`

| Test | Result |
|---|---|
| GPS distinct `route_id` | 2,127 |
| Matching a GTFS `route_id` | 385 (18%) |
| Semantic agreement (GPS route_id → GTFS `route_long_name` vs the route the same vehicle sells tickets on) | **0.0%** (8 of 36,719 pairs) |

The two feeds use unrelated ID spaces. Route identity is therefore **never**
taken from the GPS `route_id`.

### GPS `trip_id` does not join either

355 of 88,114 GPS `trip_id`s match a GTFS `trip_id` exactly (0.4%). The GPS
`trip_id` format is `<route_id>_<hour>_<minute>[_<seq>]`, which is a different
scheme from GTFS.

### Vehicle IDs **do** bridge the datasets

| Test | Result |
|---|---|
| GPS distinct vehicles | 6,093 |
| Ticket distinct vehicles | 6,810 |
| Overlap | **6,091 (99.97%)** |

`vehicle_id` / `vehicle_number` is the one reliable join key.

### Ticket route strings → GTFS `route_long_name`

| Metric | Value |
|---|---:|
| Ticket route strings | 2,259 |
| Matching a GTFS `route_long_name` | 1,350 (59.8%) |
| **Ticket volume covered** | **87.0%** |

Unmatched strings are depot-specific variants (`OMS(+)BURARI-DHAULAKUAN`,
`801-IGI-INDDOWN`, `D-034UP`). 10 GTFS route names are duplicated across two
`route_id`s; both are kept and the ambiguity is recorded.

### Ticket stop names → GTFS stations

| Method | Ticket volume covered |
|---|---:|
| Exact name match | 84.4% |
| Normalised key (case, punctuation, brackets, abbreviations) | 90.6% |
| Normalised key + token-subset fallback | **~93%** |

## 6. Join feasibility matrix

| From | To | Key | Feasible | Notes |
|---|---|---|---|---|
| GPS | GTFS routes | `route_id` | **No** | Different ID spaces, 0% semantic agreement |
| GPS | GTFS trips | `trip_id` | **No** | 0.4% match, different scheme |
| GPS | GTFS stations | geometry | **Yes** | Nearest-station snap, 120 m radius |
| GPS | Tickets | `vehicle_id` | **Yes** | 99.97% overlap |
| Tickets | GTFS routes | `route` → `route_long_name` | **Partial** | 87% of ticket volume |
| Tickets | GTFS stations | `stop_name` | **Partial** | ~93% of ticket volume |
| Tickets | GPS trips | `vehicle_id` + time | **Yes** | Vehicle-day granularity only |

## 7. What the data cannot support

Stated plainly, because the brief asks for it:

1. **True occupancy.** The ticket file is a ~2% sample with no trip_id. Load is
   reported as a relative `load_index`, never as passengers or a percentage.
2. **Route identity from GPS.** The GPS `route_id` is unusable against GTFS.
   Routes are inferred geometrically from stop passages.
3. **Route geometry.** `shapes.txt` is absent, so map paths are routed on the
   real OpenStreetMap Delhi road network (98.6% of route pairs) rather than
   published polylines. Pairs with no road path are omitted from the map, never
   drawn as a straight chord.
4. **Live data.** Both datasets are historical October 2026 snapshots. Nothing
   in this project is a live feed, and the UI says so.
5. **Weekday coverage.** Eleven GPS days are processed: a full week (Mon 5 Oct
   – Sun 11 Oct) plus three later Thursdays. Every weekday and both weekend days
   have a profile, but Tuesday, Wednesday, Saturday and Sunday rest on a single
   day each, so their hour-of-week cells are noisier than Thursday's.
