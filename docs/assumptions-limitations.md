# Assumptions and limitations

Every assumption below is a decision that could be wrong. They are listed so a
reviewer can challenge them rather than discover them.

## Data assumptions

### 1. The ticket file is a sample, not a census

**Assumption:** the 2.56 M tickets represent ~2% of ridership, sampled without
systematic bias across routes and hours.

**Evidence for:** the sample reproduces the expected diurnal shape (peak/trough
ratio 837×), the expected weekday pattern, and is stable week to week
(correlation 0.96–0.97).

**Evidence against:** tickets-per-1000-GPS-pings varies 17× between vehicles
(p05 0.50, p50 2.27, p95 8.71) and correlates only weakly with ping volume
(r = 0.14). The sample is therefore **not** a clean fixed fraction.

**Consequence:** absolute load is not recoverable. Crowding is published as a
relative `load_index` and a route-relative category, never as passengers or an
occupancy percentage.

### 2. Ticket stop names can be matched to GTFS stations

**Assumption:** normalised name matching links ticket stops to the right
station.

**Coverage:** 84.4% exact, 90.6% normalised, ~93% with token-subset fallback.

**Risk:** the token-subset fallback could link two different places that share
two tokens. It requires at least two shared significant tokens and prefers the
station the network routes through most, but it is a heuristic.

**Consequence:** ~7% of ticket volume cannot be placed on the network and is
excluded from load reconstruction. It still counts in the demand proxy.

### 3. GPS pings within 120 m of a station are that station

**Assumption:** a bus passing within 120 m of a station centroid is serving it.

**Risk:** on dense corridors two stations can be within 240 m of each other, so
a ping could snap to the wrong one. Station clustering (60 m) reduces but does
not eliminate this.

**Mitigation:** a run is kept only if the station pair exists as a real
consecutive pair on some GTFS route, which rejects most wrong snaps.

### 4. Closest approach approximates the moment of passing

**Assumption:** the ping nearest a station centroid is the moment the bus passed
the stop.

**Evidence:** this is what makes implied speed plausible (20 km/h median rather
than 38 km/h). First-contact timing truncates ~240 m from every run.

**Risk:** with an 11 s median ping interval, the closest ping can be up to ~5.5 s
from the true passage. On a 66 s median run that is ~8% noise, which is
acceptable but not negligible.

### 5. The `speed` column is unusable

**Assumption:** the reported `speed` is in undocumented units and cannot be
trusted.

**Evidence:** median reported 10.3 vs median implied 2.0 m/s. A 5× discrepancy
with no documented unit.

**Consequence:** all speeds are derived from position deltas. The `speed` column
is not used anywhere.

## Modelling assumptions

### 6. Hour of week is the right time resolution

**Assumption:** travel time depends mainly on hour of week, not on finer
granularity (minute) or coarser (month).

**Evidence:** the model beats a time-blind per-pair median by 4% on MAE and cuts
bias from −6.1 s to −1.1 s, so the time dimension carries real signal. Monday
09:00 is predicted slower than Sunday 09:00 for 63.5% of pairs, which is the
expected direction.

**Risk:** 168 hour-of-week values exist in principle, and the data covers all
seven weekdays but only eleven days total. A weekday observed once (Tuesday,
Wednesday, Saturday, Sunday) has a profile fitted to a single day, so its
hour-of-week cells are noisier than Thursday's, which has five days behind it.

### 7. Median is the right aggregator

**Assumption:** the median run time per cell is the right point estimate.

**Rationale:** run times are right-skewed (a bus can be delayed arbitrarily but
not arrive early), so the mean would be pulled up by outliers.

**Consequence:** the model under-predicts the tail. A commuter planning for the
worst case would want a higher quantile; the API exposes p25/p75 in the profile
tables but the planner uses the median.

### 8. Walking is straight-line at 1.25 m/s

**Assumption:** access and egress legs use great-circle distance at a constant
walking speed.

**Risk:** this under-estimates real walking, which must follow streets. On a
400 m straight-line walk the real distance could be 500–600 m.

**Consequence:** walk legs are optimistic. They are reported separately so a
commuter can see them.

### 9. Seated capacity is 42 (38 for AC)

**Assumption:** a standard Delhi bus seats 42.

**Status:** this is a **planning assumption, not a measured value**. It is used
only to convert a load index into a category, and the categories are relative to
each route's own distribution, so the absolute value has limited influence.

**Risk:** a 9 m midi bus seats far fewer. The brief explicitly warns that "a
load of 60 means different things on a 12 m and a 9 m bus". The data does not
identify bus length, so this cannot be corrected.

### 10. Unknown crowding is penalised at 0.35

**Assumption:** a leg with no crowding evidence should rank between
"seats available" (0.0) and "crowded" (1.0).

**Rationale:** treating unknown as comfortable would let a journey with no
evidence outrank one known to have seats. Treating it as crowded would punish
routes merely for being under-observed.

**Risk:** 0.35 is a judgement call. It is exposed as `crowding_weight` in the
API so a client can change the balance.

## Scope limitations

| Limitation | Consequence |
|---|---|
| No `shapes.txt` in the feed | Map lines are stop-to-stop, not road-following |
| No live feed | Everything is a historical October 2026 snapshot |
| Eleven days of GPS | A full week plus three later Thursdays; no seasonal effects |
| No bus length data | Capacity cannot be adjusted per vehicle class |
| No pass-holder data | Load index excludes unticketed riders entirely |
| No ground-truth occupancy | Crowding accuracy cannot be measured, only consistency |
| No Google Maps comparison | Terms forbid scraping; a car route is not a bus route |

## Things deliberately not done

* **No occupancy percentage.** The data cannot support it.
* **No fabricated metrics.** Every number in the docs comes from a script in
  `src/` and is stored in `data/artifacts/`.
* **No "live" claims.** The API returns `live_feed: false` and the UI says so.
* **No ML model.** A gradient-boosted model was considered and rejected: with
  eleven days of data and 10k station pairs, the shrinkage hierarchy is more
  robust and far easier to explain. The brief asks for accuracy, not novelty.
* **No microservices.** A modular monolith with clear module boundaries is
  cheaper to run and easier to review.
