# Proposal

**Transport Stack Hackathon 2026 — Delhi bus intelligence**

We chose **both** problem statements and built them as one system, because they
share the same spine: the bus network. Problem 1 needs a travel-time model and a
journey planner. Problem 2 needs crowding on the same journeys. Building them
separately would have meant two station graphs, two name resolvers and two
answers to "how long does this take".

---

## Problem 1 — Real-time bus traffic model and journey planner

**What was asked.** Estimate how long a bus takes between two stops, varying by
time of day and day of week, and plan a journey across the network using those
estimates rather than the published timetable.

**What we built.** A travel-time model fitted on eleven days of DTC bus GPS
(37 GB of raw pings, 1.24 M observed station-to-station runs on the held-out
day), and a RAPTOR-style journey planner over the resulting station graph.

**Result.** Segment error **22.4 s** against **41.6 s** for the timetable on the
same runs — a **46% reduction**. On 400 real observed journeys the planner's
total time is off by **0.93 min** on average, **60% better** than the timetable,
and 100% of those journeys land within 10 minutes.

## Problem 2 — Crowding intelligence

**What was asked.** Use ticketing data to say how full a bus is, and let a
passenger trade a slower journey for a calmer one.

**What we built.** A boarding/alighting balance run along each vehicle-day's
stop sequence, aggregated to a relative `load_index` per route, direction, stop
and hour, then bucketed into `seats_available` / `standing_room` / `crowded`
against each route's own 60th and 85th percentiles. The planner ranks by it.

**Result.** Crowding on **63% of routes** (1,424 of 2,259), 460,415 cells, and
**72% of bus legs** in a typical journey carry an estimate. Week-to-week
stability is 0.96–0.97 correlation, so the signal is real rather than noise.

---

## Approach

```
GPS pings ──► snap to station ──► station runs ──► travel-time model
                                                        │
GTFS stops ──► cluster to stations ─────────────────────┤
                                                        ▼
Tickets ──► demand proxy ──► load index ──► crowding ──► planner ──► API ──► map
```

Three decisions carry most of the weight, and each was forced by a measurement:

1. **Route identity is never read from the GPS feed.** Its `route_id` shares no
   ID space with GTFS: over 36,719 (vehicle, route) pairs where a vehicle both
   emitted GPS under a `route_id` and sold tickets under a route name, the GTFS
   route that `route_id` points at agreed with the ticket route **8 times**.
   GPS is linked to the network geometrically, by stop passage.
2. **Stops are clustered into stations before anything else.** GTFS assigns a
   separate `stop_id` per platform; 10,559 stop_ids collapse to 3,688 stations.
   Without this, measured run times were 2.4× too short.
3. **Passages are timed at closest approach, not first contact.** First contact
   with a snap radius truncates ~240 m from every run and inflates implied speed
   from a plausible 20 km/h to an implausible 38 km/h.

## Data used

| Source | What it gave us | Size |
|---|---|---|
| DTC bus GPS-RT, October 2026 | 11 days of vehicle positions, the travel-time signal | 37 GB raw |
| Static GTFS (Delhi OTD) | Routes, stop sequences, scheduled times | 398 MB |
| ONDC e-ticketing, October 2026 | 2.56 M tickets, the crowding signal | 1 file |
| OpenStreetMap India extract (Geofabrik) | The road network the map draws on | 1.6 GB |

The GPS feed ships **no `shapes.txt`**, so no official route geometry exists.
Every line on the map is reconstructed by routing each consecutive station pair
on the real OpenStreetMap Delhi road graph — 922,941 nodes, 1,961,233 edges,
one strongly connected component. An independent audit of 1.7 M points along
the drawn lines measures a median distance of 0.08 m to the nearest OSM road.

## What is deliberately not claimed

* **No occupancy percentage.** The ticket file is a ~2% sample (15 passengers
  per vehicle-day against several hundred for a real bus). Absolute occupancy
  is not identifiable, so crowding is published as a relative index and named
  `load_index` so no downstream claim can overreach.
* **No live feed.** Both datasets are October 2026 snapshots. The API returns
  `live_feed: false` and the UI says "Not a live feed" in the header.
* **No Google Maps comparison.** Its terms forbid scraping, and a car route is
  not a bus route. Validation uses 400 real observed bus trips instead.

## Deliverables

| Item | Where |
|---|---|
| Working prototype | <https://delhi-bus-intelligence.vercel.app> |
| Source code + README | <https://github.com/UniquePratham/delhi-bus-intelligence> |
| Technical note | [technical-note.md](technical-note.md) |
| Evaluations | [evaluation-traffic.md](evaluation-traffic.md), [evaluation-crowding.md](evaluation-crowding.md) |
| Assumptions and limitations | [assumptions-limitations.md](assumptions-limitations.md) |
| Demo video | `demo.mp4` (uploaded separately) |
