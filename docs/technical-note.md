# Technical note

How the system links GPS to the network, estimates travel time per stretch, and
combines live and historical data.

## 1. The linkage problem

The GPS feed and the GTFS feed do not share identifiers.

| Attempted join | Result |
|---|---|
| GPS `route_id` → GTFS `route_id` | 18% match by value, **0% by meaning** |
| GPS `trip_id` → GTFS `trip_id` | 0.4% match |
| GPS `vehicle_id` → ticket `vehicle_number` | **99.97% match** |

The 0% semantic result is the important one. For 36,719 (vehicle, route) pairs
where a vehicle both emitted GPS pings under a `route_id` and sold tickets under
a route name, the GTFS route that `route_id` points at agreed with the ticket
route **8 times**. The two ID spaces are unrelated.

So route identity is never read from the GPS feed. Instead:

1. **Cluster stops into stations.** GTFS assigns a separate `stop_id` to each
   platform at the same place: 10,559 stop_ids collapse to 3,963 names, and
   same-name stops sit a median 29 m apart. Union-find over stops within 60 m
   gives 3,688 stations (p99 intra-station spread 51 m).
2. **Snap each ping to its nearest station** within 120 m, using a 0.005°
   grid bucket so the join is O(n) rather than O(n·m).
3. **Collapse consecutive pings at one station into a visit**, timed at the
   ping's **closest approach** to the station centroid.
4. **Chain consecutive visits into station-to-station runs**, keeping only
   pairs that exist as a real consecutive pair on some GTFS route.

### Why closest approach, not first contact

Timing a passage at the first ping inside the snap radius biases travel time
downwards, because the bus is already partway through the run when it enters the
radius. Measured on 2026-10-15:

| Timing rule | Median run time | Implied speed |
|---|---:|---:|
| First ping inside radius | 33 s | 38.5 km/h |
| **Closest approach** | **66 s** | **20.0 km/h** |

38.5 km/h is not a plausible median for Delhi buses including stops; 20 km/h is.
The first-contact rule was silently truncating ~240 m from every run.

## 2. Travel-time model

For each directed station pair, run time is predicted as a function of hour of
week, using a shrinkage hierarchy:

```
pair × hour-of-week  →  pair × hour-band  →  pair (all hours)
                     →  scheduled time × observed/scheduled ratio
                     →  global hour-band  →  global median
```

Each level is a median of observed runs. The level that fired is recorded per
prediction, so the API can report confidence honestly rather than asserting one
number for everything.

The scheduled-time level sits *above* the global median deliberately: it is
pair-specific, so it knows this stretch is long or short even though it assumes
a constant speed. The global median knows nothing about the pair at all.

### Hour bands

| Band | Hours |
|---|---|
| night | 00–05 |
| early_peak | 06–07 |
| morning_peak | 08–11 |
| midday | 12–16 |
| evening_peak | 17–20 |
| late | 21–23 |

### Leakage control

Profiles are fitted on four days and evaluated on a fifth that is never used to
fit anything. The observed/scheduled scaling factor is also computed from
training runs only. An earlier version computed it from the evaluation day and
was corrected.

## 3. Combining live and historical data

Historical profiles are the prior. When recent observations exist for a pair
they are blended in with a weight that grows with the number of observations:

```
w_live    = n_live / (n_live + K)          K = 8
estimate  = (1 - w_live) · historical + w_live · live_median
```

K = 8 means a pair needs eight fresh observations before live data carries half
the weight. Two fresh runs cannot override a month of history.

**In this submission the live term is inactive**, because the supplied data is a
historical snapshot with no live feed attached. The mechanism is implemented and
documented so that connecting a GTFS-RT feed is a configuration change, not a
rewrite. The API reports `live_feed: false` and the UI says so.

### When there is little or no data

The brief asks what the model does when a road has few buses at some hours. The
answer is the hierarchy above, plus explicit confidence:

| Situation | Behaviour | Reported confidence |
|---|---|---|
| Pair observed at this hour-of-week | Use it | high |
| Pair observed in this hour band | Use the band median | medium |
| Pair observed at other hours | Use the pair median | medium |
| Pair never observed | Scale the timetable by the band's observed/scheduled ratio | low |
| Nothing at all | Global hour-band median | low |

The planner surfaces this per leg, and the journey card warns when any leg is
low confidence.

## 4. Journey planning

A round-based label-setting search (RAPTOR-style) over the station graph:

* Round 0 is the access walk from the origin to nearby stations.
* Each round adds one **route boarding**, not one station hop. Within a round a
  passenger may ride a route across any number of stations for the price of one
  boarding.
* Transfers are bounded by `max_transfers`.
* Alternatives are found by re-running the search with the previous journey's
  stations banned, so the second option is genuinely different rather than a
  cosmetic variant.

Consecutive bus legs on the same route are merged before being returned.
Without this, a 15 km trip reported 40 "transfers", because the search relaxes
station pair by station pair.

## 5. Crowding

The ticket file is a **~2% sample**: 2.56 M tickets over 31 days across ~6,000
vehicles is 15 passengers per vehicle-day, against several hundred for a real
bus. Absolute occupancy is therefore not identifiable, and no occupancy figure
is produced.

What is produced:

1. **`demand_proxy`** — boardings per route, direction, stop and hour. A direct
   count, not a model.
2. **`load_index`** — a relative onboard load, from a boarding/alighting balance
   run along each vehicle-day's stop sequence, averaged across vehicle-days.
3. **`crowding_category`** — `seats_available` / `standing_room` / `crowded`,
   relative to each route's own 60th and 85th percentiles.

### Why the mean, not the median

Per vehicle-day the sample is sparse (median 6 tickets), so the median onboard
count is 0–4 and carries no signal. The mean across vehicle-days preserves the
aggregate boarding/alighting balance and reproduces the expected rise-and-fall
along a route, which the median destroys.

### Stop-name linkage

Ticket stop names and GTFS stop names are written by different systems. Matching
is layered:

| Method | Ticket volume covered |
|---|---:|
| Exact name | 84.4% |
| Normalised key (case, punctuation, brackets, abbreviations) | 90.6% |
| + token-subset fallback | ~93% |

The token-subset fallback requires at least two shared significant tokens, so
"Metro Station Kashmere Gate" can reach "Kashmere Gate Metro Station Gate 4"
but a single generic word like "Terminal" cannot link two unrelated places.

## 6. Known limitations

1. **No route geometry.** `shapes.txt` is absent from the feed, so map lines are
   stop-to-stop straight lines, not road-following polylines.
2. **Crowding coverage.** 63% of routes have any crowding estimate, and 87% of
   cells are low confidence. 909 ticket route strings have no GTFS route at all.
3. **No live data.** Everything is a historical October 2026 snapshot.
4. **Walking is straight-line.** Access and egress legs use great-circle
   distance at 1.25 m/s, not a pedestrian network.
5. **Eleven days of GPS.** A full week plus three later Thursdays. Seasonal
   effects, festivals and the monsoon are not represented.

## 7. Bugs found during revalidation

These are recorded because each produced plausible output while being wrong.

| Bug | Effect | Fix |
|---|---|---|
| DuckDB `extract(dow)` is 0=Sunday; Python `weekday()` is 0=Monday | Every hour-of-week lookup shifted by one day. A Tuesday query was served Monday's profile, and the weekend comparison was inverted. | Convert with `(dow + 6) % 7`; pinned by `tests/test_time_conventions.py` |
| `hour_of_week` stored as `TINYINT` | Overflowed at 127 once all seven weekdays were present, so the model could not be built on a full week | `SMALLINT` |
| Alternatives found by banning every station on the path | Long trips returned a single option | Ban only the first ride's route |
| Crowding tables loaded per request | 5–20 s per query | Load once at startup; compact array store |
| Route-name fallback accepted ambiguous keys | `D-0001DOWN` linked to `DW-1DOWN`, attaching crowding from the wrong route | Accept only unambiguous keys |
| Search copied leg lists at every relaxation | 11 s per query | Parent-pointer labels, per-round label sets |
