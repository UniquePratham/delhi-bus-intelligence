# Architecture

A modular monolith. Each module has a clear boundary and can be tested alone;
none of them requires a separate process.

```
                    ┌──────────────────────────┐
                    │   web/index.html         │
                    │   Leaflet map + cards    │
                    └────────────┬─────────────┘
                                 │ HTTP
                    ┌────────────▼─────────────┐
                    │  dbi.api.main (FastAPI)  │
                    │  ranking, provenance     │
                    └───────┬──────────┬───────┘
                            │          │
              ┌─────────────▼──┐  ┌────▼──────────────┐
              │ dbi.journey    │  │ dbi.crowding      │
              │ planner.py     │  │ ingest / model    │
              │ RAPTOR search  │  │ demand, load idx  │
              └───────┬────────┘  └────┬──────────────┘
                      │                │
              ┌───────▼────────┐       │
              │ dbi.traffic    │       │
              │ ingest         │       │
              │ stop_passage   │       │
              │ schedule       │       │
              │ model          │       │
              └───────┬────────┘       │
                      │                │
              ┌───────▼────────────────▼───────┐
              │  dbi.reference + dbi.stations  │
              │  GTFS -> stations, patterns    │
              └────────────────────────────────┘
```

## Module responsibilities

| Module | Owns | Depends on |
|---|---|---|
| `dbi.reference` | GTFS tables: stops, routes, trips, stop_times, patterns | GTFS files |
| `dbi.stations` | Stop clustering into stations | `reference` |
| `dbi.traffic.ingest` | Raw SQLite → cleaned parquet | GPS files |
| `dbi.traffic.stop_passage` | GPS → station runs | `reference`, `stations`, parquet |
| `dbi.traffic.schedule` | Scheduled baseline in station space | `reference`, `stations` |
| `dbi.traffic.model` | Travel-time profiles, holdout split | station runs, `schedule` |
| `dbi.crowding.ingest` | Ticket ETL, demand proxy | ticket CSV |
| `dbi.crowding.model` | Load index, categories | `reference`, `stations`, tickets |
| `dbi.journey.planner` | Search, crowding enrichment | `traffic`, `reference`, `crowding` |
| `dbi.api.main` | HTTP, ranking, provenance | `journey` |

## Storage

Three DuckDB files, all rebuildable from raw data:

| File | Contents | Size |
|---|---|---:|
| `data/processed/reference.duckdb` | GTFS tables, stations, patterns, schedule | ~120 MB |
| `data/processed/traffic.duckdb` | Runs, profiles, holdout split | ~400 MB |
| `data/processed/crowding.duckdb` | Tickets, demand, load index, categories | ~500 MB |

Intermediate GPS data is partitioned parquet:

```
data/processed/gps/date=2026_10_15/pings.parquet        # cleaned pings
data/processed/gps/date=2026_10_15/station_runs.parquet # station-to-station runs
```

Raw data is never committed and never modified.

## Why a monolith

The brief allows either. A monolith was chosen because:

* The whole system is one analytical pipeline plus one read-only API. There is
  no independent scaling requirement.
* DuckDB gives columnar performance in-process, so there is no database server
  to operate.
* A reviewer can run the whole thing with two commands.

The module boundaries are real: `dbi.crowding` can be deleted and the planner
still works, returning `unknown` crowding. That is tested.

## Data flow, end to end

1. **GTFS → stations.** 10,559 stop_ids cluster into 3,688 stations by
   union-find over a 60 m radius.
2. **GPS → pings.** Each daily SQLite is deduplicated, bbox-filtered and written
   to parquet. 29.3 M raw rows → 18.3 M distinct pings.
3. **Pings → station runs.** Each ping snaps to its nearest station within
   120 m; consecutive visits are chained; a run is kept only if the station pair
   exists on some GTFS route. 18.3 M pings → 1.3 M runs per day.
4. **Runs → profiles.** Median run time per (pair × hour-of-week), with
   fallbacks. Fitted on four days, evaluated on the fifth.
5. **Tickets → demand.** Boardings per route × direction × stop × hour.
6. **Tickets → load index.** Boarding/alighting balance along each vehicle-day's
   stop sequence, averaged across vehicle-days.
7. **Query → journey.** RAPTOR search over the station graph, priced by the
   profiles, enriched with crowding, ranked by preference.

## Failure isolation

| If this fails | The system still |
|---|---|
| Crowding DB missing | Plans journeys, reports `unknown` crowding |
| A station pair has no profile | Falls back through the hierarchy, marks confidence |
| A route has no crowding | Falls back to route-hour, then route, then `unknown` |
| No journey exists | Returns an empty list with an explanatory warning |
| A place name is unknown | Returns 422 with the name that failed |
