# Delhi Bus Intelligence

A bus-GPS travel-time model and journey planner for Delhi, extended with
ticketing-derived crowding intelligence.

Built for the Transport Stack Hackathon 2026. Problem 1 (traffic model and
journey planner) is the core; Problem 2 (crowdedness) is an integrated module
that degrades gracefully when its data is thin.

> **These estimates are historical, not live.** Both datasets are October 2026
> snapshots. Nothing here is a live feed, and the UI says so.

## Submission deliverables

| Item | Where |
|---|---|
| Proposal — problem, approach, data | [docs/proposal.md](docs/proposal.md) |
| Working prototype | <https://delhi-bus-intelligence.vercel.app> |
| Source code + README | this repository |
| Technical note — method, assumptions, limitations | [docs/technical-note.md](docs/technical-note.md) |
| Demo video | [docs/demo.mp4](docs/demo.mp4) |

Supporting detail: [data-audit.md](docs/data-audit.md),
[assumptions-limitations.md](docs/assumptions-limitations.md),
[evaluation-traffic.md](docs/evaluation-traffic.md),
[evaluation-crowding.md](docs/evaluation-crowding.md),
[architecture.md](docs/architecture.md), [api-contract.md](docs/api-contract.md).

## What it does

* Predicts bus travel time between any two stations, varying by hour of week.
* Plans journeys with walking legs, transfers and alternatives.
* Attaches a crowding estimate to each bus leg where the data supports one.
* Reports confidence and data provenance on every estimate.

## Results

| Metric | Value |
|---|---:|
| Segment MAE vs observed runs (held-out day, 1.24 M runs) | **22.4 s** |
| GTFS timetable MAE on the same runs | 41.6 s |
| **Improvement over the timetable** | **46%** |
| Journey MAE vs 400 real observed trips | **0.93 min** |
| Journeys within 10 minutes | **100%** |
| Improvement over the timetable on journeys | **60%** |
| Crowding coverage (routes) | 63% |
| Crowding evidence on typical journey legs | 72% |
| API latency (p50) | **~1.0 s** |

Full detail: [evaluation-traffic.md](docs/evaluation-traffic.md),
[evaluation-crowding.md](docs/evaluation-crowding.md).

## Quick start

Requires Python 3.11+ and the two data artifacts in place (see
[Data](#data) below).

```bash
pip install -e ".[dev]"

# Build everything: reference layer, GPS pipeline, models, evaluation.
python -m dbi.pipeline

# Serve the API and the map UI.
uvicorn dbi.api.main:app --port 8077
# open http://127.0.0.1:8077/
```

`python -m dbi.pipeline` takes roughly 45 minutes on 8 cores, dominated by
reading eleven 4.5 GB SQLite files. Use `--skip-gps` to rebuild only the models
from existing parquet.

Docker is also supported, but the image contains no data — run the pipeline
first so `data/processed/` exists:

```bash
docker compose up --build
```

## Evaluator website

**Live: <https://delhi-bus-intelligence.vercel.app>**

The static evaluator website is in [`web-next`](web-next). It includes the
browser-side journey planner, 3D map, traffic evidence, and crowding explorer;
it does not require the Python API or the large DuckDB files.

Run it locally:

```bash
cd web-next
npm install
npm run dev
# open http://localhost:3000
```

Deploy it to Vercel from an authenticated Vercel CLI session:

```bash
cd web-next
npx vercel login
npx vercel --prod
```

The deployment uses the checked-in static model artifacts under
[`web-next/public/data`](web-next/public/data) and caches them immutably.

> **Vercel project setting:** the project's **Root Directory must be
> `web-next`**. With it empty, a Git build runs `npm install` at the repo root,
> where there is no `package.json`, and fails in four seconds with
> `ENOENT ... /vercel/path0/package.json`.

## Demo video

[`docs/demo.mp4`](docs/demo.mp4) — 2:52, narrated, with burned-in subtitles.
It walks the whole prototype: autocomplete, planning, the OSM-routed map, the
2D/3D toggle, preference re-ranking, the hour curve, the crowding explorer and
the evidence section.

Rebuild it (needs `ffmpeg` and a running site):

```bash
python scripts/make_narration.py                      # voice + subtitles
python scripts/record_narrated_demo.py http://127.0.0.1:3100/
```

`scripts/demo_script.json` holds the narration and captions; edit it and re-run
both commands to change the video.

## API

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness |
| `GET /v1/metadata` | Data provenance, network size, model levels |
| `GET /v1/stops/nearby?lat=&lon=&radius_m=` | Stations near a point |
| `GET /v1/stops/search?q=` | Resolve a place name |
| `POST /v1/journeys/plan` | Plan journeys |
| `GET /v1/traffic/segments?from_station=&to_station=&at=` | One stretch |
| `GET /v1/crowding/routes/{route}?hour=` | Crowding along a route |

```bash
curl -X POST http://127.0.0.1:8077/v1/journeys/plan \
  -H 'Content-Type: application/json' \
  -d '{
    "origin": {"name": "Kashmere Gate ISBT"},
    "destination": {"name": "Nehru Place"},
    "departure_time": "2026-10-15T08:30:00+05:30",
    "max_transfers": 2,
    "alternatives": 3,
    "preference": "balanced"
  }'
```

Every response carries `data_status` with `live_feed: false`, and every leg
carries a `confidence` and, where available, a `crowding` object with its
`target_type` and `time_scope`.

## How it works

```
GPS pings ──► snap to station ──► station runs ──► travel-time model
                                                        │
GTFS stops ──► cluster to stations ─────────────────────┤
                                                        ▼
Tickets ──► demand proxy ──► load index ──► crowding ──► journey planner ──► API ──► map UI
```

Three decisions carry most of the weight:

1. **Route identity is never read from the GPS feed.** Its `route_id` shares no
   ID space with GTFS (0% semantic agreement over 36,719 pairs). GPS is linked
   to the network geometrically, by stop passage.
2. **Stops are clustered into stations first.** GTFS assigns a separate
   `stop_id` per platform; 10,559 stop_ids collapse to 3,688 stations. Without
   this, measured run times were 2.4× too short.
3. **Passages are timed at closest approach, not first contact.** First contact
   truncates ~240 m from every run and inflates implied speed from 20 to
   38 km/h.

Read [technical-note.md](docs/technical-note.md) for the full method.

## Honest limitations

* **The ticket file is a ~2% sample** (15 passengers per vehicle-day against
  several hundred for a real bus). Absolute occupancy is not identifiable, so
  crowding is published as a relative `load_index`, never as passengers or a
  percentage.
* **Crowding coverage is 63% of routes**, and 87% of cells are low confidence.
  Missing values are reported as `unknown`, never as "not crowded". In practice
  72% of bus legs in a typical journey do carry an estimate.
* **Route geometry is reconstructed, not published.** `shapes.txt` is absent
  from the feed, so no official route geometry exists. Every consecutive station
  pair on every route is routed on the **real OpenStreetMap Delhi road network**
  (`scripts/route_on_osm.py`), extracted from the Geofabrik India PBF: 922,941
  nodes, 1,961,233 edges, one strongly connected component, with real junction
  topology and one-way tags. Each station is projected onto the nearest road
  segment (median offset 3.7 m) rather than snapped to the nearest node, so
  every drawn point is on a road by construction and consecutive pairs share an
  exact endpoint. 12,175 of 12,352 pairs (98.6%) get a path. The 177 omitted
  pairs are explained, not hidden (`scripts/explain_omitted_pairs.py`):
  **154** are stations 3–34 km apart, which are not real consecutive stops but a
  GTFS artefact; **23** are same-place platform pairs within 366 m of each other
  (e.g. "Ram Prastha Temple → Ram Prastha Temple", 117 m apart but 5.9 km by
  road) where the bus must drive around to the correct carriageway of a divided
  road. None is drawn as a shortcut, and when a planned journey contains one the
  map says so on screen ("1 of this route's 38 stretches have no road path and
  are not drawn"), verified by `scripts/check_omission_note.py`.

  An independent audit (`scripts/audit_osm_roads.py`) samples 2.22 M points
  along the drawn lines at 10 m spacing and measures the distance to the nearest
  OSM road: **median 0.08 m, p90 0.23 m, p99 0.33 m, max 6.98 m, and zero pairs
  with any point beyond 25 m**. Every drawn link is either an actual OSM edge
  (379,432), a station's projection onto its road segment (25,747), or a
  zero-length link that draws nothing (333) — **zero links are not on a road**.
  Zero pairs are drawn as a bare station-to-station chord. All three demo routes
  are complete and continuous: 0 missing pairs, 0 breaks, 0 m worst gap. Where
  the road genuinely goes around a barrier the line follows it — High Court to
  Zoo is 217 m apart and 1,924 m by road, which is what a bus actually drives.
* **No Google Maps comparison.** Its terms forbid scraping, and a car route is
  not a bus route. Validation uses 400 real observed bus trips instead.

## Data

Place these before running the pipeline:

```
data/raw/gps/bus_movements_2026_10_01.db   # from delhi-dtc-bus-gtfs-rt-2026-10.7z
data/raw/gps/bus_movements_2026_10_05.db   # Mon
data/raw/gps/bus_movements_2026_10_06.db   # Tue
data/raw/gps/bus_movements_2026_10_07.db   # Wed
data/raw/gps/bus_movements_2026_10_08.db   # Thu
data/raw/gps/bus_movements_2026_10_09.db   # Fri
data/raw/gps/bus_movements_2026_10_10.db   # Sat
data/raw/gps/bus_movements_2026_10_11.db   # Sun
data/raw/gps/bus_movements_2026_10_15.db
data/raw/gps/bus_movements_2026_10_22.db
data/raw/gps/bus_movements_2026_10_29.db   # evaluation holdout
data/raw/October-Delhi.csv                 # from October-Delhi.csv.zip
data/raw/gtfs/bus/*.txt                    # static GTFS
```

Eleven days, covering a full week plus three later Thursdays, so every weekday
and both weekend days have a profile.

Extract a single GPS day without unpacking the whole archive:

```bash
7z x delhi-dtc-bus-gtfs-rt-2026-10.7z bus_movements_2026_10_15.db
```

The static GTFS feed is not bundled with the hackathon artifacts. It is
available from the Delhi Open Transit Data portal (`otd.delhi.gov.in`); the
upstream download endpoint was returning HTTP 504 during development, so the
mirror at `ethanc8/delhi-gtfs` was used. See [data-audit.md](docs/data-audit.md).

## Repository layout

```
src/dbi/
  config.py            paths and tunables
  geo.py               haversine, point-to-segment
  names.py             stop-name normalisation and matching
  reference.py         GTFS -> reference tables
  stations.py          stop clustering (union-find)
  pipeline.py          end-to-end runner
  traffic/
    ingest.py          SQLite -> cleaned parquet
    stop_passage.py    GPS -> station runs
    schedule.py        scheduled baseline in station space
    model.py           travel-time profiles
    evaluate.py        held-out evaluation
  crowding/
    ingest.py          ticket ETL
    model.py           demand proxy, load index, categories
    evaluate.py        pattern checks
  journey/
    planner.py         RAPTOR-style search
    validate.py        validation against real trips
  api/main.py          FastAPI service
web/index.html         map UI
tests/                 unit and integration tests
docs/                  audit, technical note, evaluations
```

## Tests

```bash
pytest -q
```

52 tests: name normalisation, geospatial helpers, ranking logic, and planner
integration. The planner tests skip automatically if the databases have not been
built.

Two extra checks that are not part of the pytest suite:

```bash
python scripts/check_degradation.py   # planner works with crowding removed
python scripts/ui_smoke.py            # map UI renders in a real browser
```

## Reproducing every number

| Claim | Command |
|---|---|
| Segment MAE, baselines | `python -m dbi.traffic.evaluate` |
| Journey MAE vs real trips | `python -m dbi.journey.validate --sample 400` |
| Crowding pattern checks | `python -m dbi.crowding.evaluate` |
| Build the OSM Delhi road graph | `python scripts/build_osm_from_pbf.py` |
| Route every station pair on OSM | `python scripts/route_on_osm.py` |
| On-road audit of every drawn point | `python scripts/audit_osm_roads.py` |
| Explain every omitted pair | `python scripts/explain_omitted_pairs.py` |
| Demo routes are continuous | `python scripts/check_continuity.py` |
| Omitted stretches are reported in the UI | `python scripts/check_omission_note.py` |
| That note does not collide with map chrome | `python scripts/check_note_layout.py` |

`scripts/route_on_roads.py` and the older two-stage pipeline
(`build_route_geometry.py`, `ensure_route_geometry.py`, `snap_guide_to_roads.py`
and friends) are superseded. They routed on CARTO *rendering* tiles, which are
not a routing network: fragmented topology, duplicate carriageways and missing
links. Running any of them overwrites `web-next/public/data/geom_*.bin` with
geometry that has no on-road guarantee. `route_on_osm.py` is the only supported
builder.

Outputs land in `data/artifacts/`.
