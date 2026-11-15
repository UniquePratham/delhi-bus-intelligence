# API contract

Base URL: `http://127.0.0.1:8077`. All responses are JSON.

Every response that contains an estimate carries provenance. The service never
claims to be live: `data_status.live_feed` is always `false` for this dataset.

## `GET /health`

```json
{"status": "ok"}
```

## `GET /v1/metadata`

Data provenance, network size and the model's fallback hierarchy.

```json
{
  "data_status": {
    "traffic": "historical",
    "crowding": "historical",
    "traffic_period": "2026-10-01 to 2026-10-29",
    "crowding_period": "2026-10-01 to 2026-10-31",
    "live_feed": false,
    "note": "Estimates are derived from a historical October 2026 snapshot. They are not a live feed."
  },
  "network": {"stations": 3688, "routes": 2403, "directed_station_pairs": 95254},
  "model": {
    "levels": [
      "pair x hour-of-week",
      "pair x hour-band",
      "pair (all hours)",
      "scheduled time scaled by observed/scheduled ratio",
      "global hour-band",
      "global median"
    ],
    "crowding_target": "load_index (relative, from a ticketing sample)"
  }
}
```

## `GET /v1/stops/nearby`

| Param | Type | Default | Notes |
|---|---|---|---|
| `lat` | float | required | −90..90 |
| `lon` | float | required | −180..180 |
| `radius_m` | float | 800 | 0..5000 |
| `limit` | int | 10 | 1..50 |

```json
{
  "count": 2,
  "stops": [
    {"station_id": 750, "name": "ISBT Kashmere Gate", "distance_m": 0},
    {"station_id": 15729, "name": "ISBT Kashmiri Gate", "distance_m": 81}
  ]
}
```

## `GET /v1/stops/search`

| Param | Type | Notes |
|---|---|---|
| `q` | string | min 2 chars |

Resolves a place name, including colloquial aliases ("CP", "Connaught Place",
"IGI Airport"). Returns 404 when nothing matches.

```json
{"query": "Connaught Place", "best": {"name": "Shivaji Stadium Terminal", "lat": 28.6327, "lon": 77.2156}}
```

## `POST /v1/journeys/plan`

### Request

```json
{
  "origin": {"name": "Kashmere Gate ISBT"},
  "destination": {"name": "Nehru Place"},
  "departure_time": "2026-10-15T08:30:00+05:30",
  "max_transfers": 2,
  "alternatives": 3,
  "preference": "balanced",
  "crowding_weight": 0.35
}
```

`origin` and `destination` accept either `{"lat": .., "lon": ..}` or
`{"name": ".."}`.

| Field | Type | Default | Notes |
|---|---|---|---|
| `departure_time` | ISO 8601 | required | Include the offset |
| `max_transfers` | int | 2 | 0..4 |
| `alternatives` | int | 3 | 1..5 |
| `preference` | enum | `balanced` | `fastest` \| `less_crowded` \| `balanced` |
| `crowding_weight` | float | 0.35 | 0..1, used by `balanced` only |

### Response

```json
{
  "request_id": "uuid",
  "data_status": {
    "traffic": "historical",
    "crowding": "historical",
    "traffic_as_of": "2026-10-29T23:59:59+05:30",
    "crowding_as_of": "2026-10-31T23:59:59+05:30",
    "live_feed": false
  },
  "origin": {"label": "ISBT Kashmere Gate", "lat": 28.6669, "lon": 77.2293},
  "destination": {"label": "Nehru Place", "lat": 28.5486, "lon": 77.2492},
  "journeys": [
    {
      "rank": 1,
      "duration_minutes": 38.8,
      "transfers": 2,
      "walk_minutes": 0.0,
      "bus_minutes": 38.8,
      "arrive": "2026-10-15T09:08:48+05:30",
      "crowding_summary": {
        "category": "crowded",
        "target_type": "load_index",
        "confidence": "low",
        "legs_known": 2,
        "legs_total": 3,
        "note": "Relative crowding from a ticketing sample, not measured occupancy."
      },
      "legs": [
        {
          "kind": "walk",
          "from_name": "Origin",
          "to_name": "ISBT Kashmere Gate",
          "from_lat": null, "from_lon": null,
          "to_lat": 28.6669, "to_lon": 77.2293,
          "depart": "2026-10-15T08:30:00+05:30",
          "arrive": "2026-10-15T08:30:00+05:30",
          "duration_minutes": 0.0,
          "route": null,
          "distance_m": 0,
          "confidence": "measured",
          "crowding": null
        },
        {
          "kind": "bus",
          "from_name": "ISBT Kashmere Gate",
          "to_name": "GOLCHA CINEMA",
          "from_lat": 28.6669, "from_lon": 77.2293,
          "to_lat": 28.6362, "to_lon": 77.2401,
          "depart": "2026-10-15T08:30:00+05:30",
          "arrive": "2026-10-15T08:39:30+05:30",
          "duration_minutes": 9.5,
          "route": "241DOWN",
          "distance_m": null,
          "confidence": "low",
          "crowding": {
            "category": "seats_available",
            "confidence": "low",
            "target_type": "load_index",
            "load_index": 12.4,
            "time_scope": "route_hour",
            "reason": "no evidence for this stretch; using the route average for this hour"
          }
        }
      ],
      "warnings": [
        "3 of 3 bus legs use a low-confidence estimate (no direct observation for that stretch and hour)."
      ]
    }
  ],
  "warnings": []
}
```

### Field semantics

**`confidence` on a leg** — which level of the travel-time hierarchy produced
the estimate:

| Value | Meaning |
|---|---|
| `measured` | A walk leg; distance is computed, not predicted |
| `high` | Pair observed at this hour of week |
| `medium` | Pair observed in this hour band, or at other hours |
| `low` | Timetable scaled by the band ratio, or a global fallback |

**`crowding.category`** — one of `seats_available`, `standing_room`, `crowded`,
`unknown`. It is **relative to the route's own load distribution**, not an
absolute occupancy band.

**`crowding.target_type`** — always `load_index` for this dataset. It is never
`occupancy` or `passenger_count`, because the data cannot support those.

**`crowding.time_scope`** — how specific the evidence is:

| Value | Meaning |
|---|---|
| `hour` | Route × hour × station observed directly |
| `all_hours` | Route × station, averaged over hours |
| `route_hour` | Route × hour, averaged over stretches |
| `route` | Route overall |

**`crowding_summary.category`** — the **worst** leg, not the average. Averaging
a crowded stretch with an empty one would tell a commuter the trip is
comfortable when part of it is not.

**`crowding_summary.legs_known` / `legs_total`** — how many bus legs had any
evidence. A commuter can see at a glance how much of the answer is grounded.

### Ranking

`fastest` sorts by duration. `less_crowded` adds `COMFORT_BUDGET_MIN × penalty`
(30 minutes) to the duration. `balanced` adds
`crowding_weight × penalty × 30` minutes.

The penalty is the **worst** leg, matching `crowding_summary`. Averaging would
let one crowded stretch be diluted by two empty ones, so a journey that is
partly unbearable would score as comfortable.

Unknown crowding is penalised at 0.35 on a 0..1 scale — between
`seats_available` (0.0) and `crowded` (1.0). Treating it as comfortable would
let an unevidenced journey outrank one known to have seats.

`less_crowded` is a trade-off, not a pure comfort sort. The 30-minute budget
means comfort can justify a detour of at most 30 minutes for a fully crowded
trip, 15 for standing room. Without that cap the mode recommended a 95-minute
trip over a 54-minute one.

## `GET /v1/traffic/segments`

| Param | Type | Notes |
|---|---|---|
| `from_station` | int | required |
| `to_station` | int | required |
| `at` | ISO 8601 | defaults to 2026-10-15T09:00 |

```json
{
  "from_station": 750,
  "to_station": 110,
  "at": "2026-10-15T09:00:00",
  "predicted_s": 132.0,
  "scheduled_s": 180.0,
  "level": "pair_hour",
  "confidence": "high"
}
```

## `GET /v1/crowding/routes/{route_name}`

| Param | Type | Default | Notes |
|---|---|---|---|
| `hour` | int | 9 | 0..23 |

Returns 404 when the route has no crowding data at that hour.

```json
{
  "route": "241DOWN",
  "hour": 8,
  "target_type": "load_index",
  "note": "Relative crowding from a ticketing sample, not measured occupancy.",
  "stretches": [
    {"seq": 2, "category": "seats_available", "confidence": "low", "load_index": 1.0, "n_vehicle_days": 2}
  ]
}
```

## Errors

| Status | When |
|---|---|
| 404 | No station matches the name; no crowding data for the route/hour |
| 422 | Malformed request, or a place name that cannot be resolved |

Error bodies are `{"detail": "..."}`.

## Frontend

`GET /` serves the map UI. `GET /app/*` serves its static assets.
