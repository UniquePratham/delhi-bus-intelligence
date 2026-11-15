# Agent Build Prompt — Problem 1
## Real-Time Bus Traffic Model and Journey Planner (Delhi DTC)

**Role:** Act as a senior data engineer, geospatial engineer, machine-learning engineer, backend architect, and technical lead. Build a reproducible, testable system that uses Delhi DTC bus GPS traces to estimate road-segment travel times and return plausible bus journey options between two locations.

**Priority:** This is the primary project. Optimize for a defensible working prototype and measurable accuracy—not a flashy interface that hides weak modelling.

---

## 1. Objective and expected outcome

Build a Google-Maps-like *bus journey-planning API prototype* for Delhi. Given an origin and destination (coordinates, address/geocoded place, or supported stop), the system should:

1. Resolve the origin and destination to the transport/road graph.
2. Find feasible bus-based journeys using available route, stop, and road-network information.
3. Estimate travel time for each relevant journey, accounting for time of day and observed/historical bus movement.
4. Return ranked journey options with leg details, transfer information where supported, estimated duration, and confidence/data-quality indicators.
5. Provide a map-friendly response that a frontend can render.
6. Be evaluated against held-out observed journeys and carefully selected external reference journeys, including Google Maps where comparison is permitted and methodologically fair.

Do **not** claim to reproduce Google Maps or represent bus GPS as a complete measurement of all road traffic. Describe the system accurately as a bus-observation-informed travel-time and journey-planning prototype.

## 2. Source data and first actions

Use the supplied October 2026 data:

- GPS archive: https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/delhi-dtc-bus-gtfs-rt-2026-10.7z
- Data README: https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/README.md
- Checksums: https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/SHA256SUMS
- Full problem brief: https://docs.google.com/document/d/1rfEu-ptgN4MNlcTKvv59nzc_OVjPmbk_hAxpe5HCirc/preview

The archive is approximately 16.1 GB compressed and 124 GB unpacked, containing one SQLite database per day for 1–31 October 2026. Treat these as brief-provided figures; verify actual downloaded sizes and contents.

**Before implementing a schema-dependent pipeline:**
1. Read the README and full brief.
2. Verify archive integrity against SHA256SUMS.
3. Inspect every SQLite database's schema, indexes, row counts, date coverage, null rates, coordinate ranges, timestamp units/time zones, and representative records.
4. Identify the authoritative route, trip, vehicle, stop, and feed identifiers and how they relate.
5. Record findings in `docs/data-audit.md`. Do not guess column names or semantics.
6. Establish whether route shapes, GTFS static data, stop locations, and vehicle-position records are included or need a separately documented source.

Do not repeatedly download or unpack the full archive. Support resumable downloads and per-day processing. Never load all 124 GB into RAM.

## 3. Key engineering questions to resolve

- What is the GPS sampling interval, and how irregular is it?
- Are timestamps UTC, local time, or mixed? Normalize internally and preserve source values.
- Are coordinates WGS84 latitude/longitude? Check invalid and out-of-city points.
- Are vehicle IDs stable across days?
- Can positions be linked to a route, trip, direction, and scheduled/known stop sequence?
- Are there duplicate, delayed, stale, or out-of-order messages?
- Are route shapes available? If not, how will the system infer plausible paths?
- Does the data observe buses only while in service, and how will depot, deadheading, layover, and off-route movement be handled?
- What is the spatial/temporal coverage? Which routes and hours have enough observations for reliable estimates?

Write assumptions and unresolved limitations explicitly.

## 4. Proposed architecture

Build modular components; avoid a monolith.

### A. Ingestion and raw-data layer
- Download and verify source archives with checksum validation.
- Process one daily SQLite database at a time.
- Preserve raw data as immutable source data where storage allows.
- Convert selected fields to partitioned Parquet, organized by date and optionally route/region.
- Use DuckDB and/or Polars for columnar analytics; use SQLite only for source inspection/extraction.
- Make ingestion idempotent: rerunning a day must not duplicate records.
- Maintain a manifest with source filename, checksum, extraction status, row counts, schema version, and processing timestamp.
- Add structured logs, progress reporting, retries, and clear failure recovery.

### B. Data quality and GPS cleaning
- Validate coordinate bounds and timestamps.
- Remove exact duplicates while retaining an audit count.
- Detect impossible jumps, implausible speeds, long gaps, stale positions, and GPS noise.
- Do not silently delete outliers: label them and document thresholds.
- Handle missing route/trip IDs using conservative inference only; attach an inference flag and confidence.
- Separate observed facts from inferred values.

### C. Map matching and trajectory reconstruction
- Use road geometry from a documented, licensed source such as OpenStreetMap where appropriate.
- Evaluate a map-matching approach such as OSRM Match, Valhalla, or a transparent custom method.
- Match GPS sequences—not isolated points wherever possible—to plausible road edges.
- Respect directionality, road connectivity, route shapes, and temporal continuity.
- Keep unmatched/low-confidence points rather than forcing every point onto a road.
- Store matched edge IDs, match confidence, and reasons for rejection.
- Validate matching visually and quantitatively on sampled trajectories.

### D. Road-segment travel-time estimation
For each matched road edge or stable road segment, estimate:
- observed traversal time and speed;
- sample count and coverage;
- time-of-day and day-of-week distributions;
- robust central tendency (median/trimmed mean) and variability;
- freshness of the latest observations;
- confidence/uncertainty.

Avoid deriving segment travel time from two GPS points unless the bus can be confidently assigned to a connected path and the elapsed interval is handled correctly. Account for GPS sampling gaps and stop dwell time. Distinguish moving speed from end-to-end bus journey time.

Build baseline models first:
1. Free-flow or road-class baseline (only if defensible).
2. Historical segment median by time bucket/day type.
3. Current/near-current observed speed with fallback to historical estimates.
4. A supervised model (e.g., LightGBM/XGBoost) only after a reliable target and leakage-safe train/test split exist.

Potential features, subject to availability:
- road segment/road class;
- hour, weekday, weekend/holiday indicator;
- historical segment speed/travel-time statistics;
- recent observed speed and observation age;
- route/direction;
- weather or event variables only if a documented source is added and the feature is actually available at prediction time.

Use interpretable baselines and feature importance. Do not add LSTM/transformers merely for novelty.

### E. Time-dependent graph and journey planning
- Construct a directed graph from road/route/stop information.
- Model travel time as a function of departure time, not a fixed edge weight.
- Use a time-dependent shortest-path algorithm or a carefully justified approximation.
- For public-transport journeys, model access/egress, route boarding, stop sequence, dwell, transfers, and waiting time where the data supports them.
- If the brief expects a bus journey planner rather than a general road navigation engine, prioritize feasible bus routes and transfers. Do not return arbitrary car routes as bus journeys.
- Provide multiple alternatives with transparent ranking criteria.
- Handle disconnected stops, invalid locations, and insufficient-data edges gracefully.
- Use historical fallback estimates when live observations are unavailable and label them as historical.

### F. API
Use FastAPI (or justify an equivalent). Provide versioned endpoints, for example:

- `GET /health`
- `GET /v1/locations/search?q=...`
- `GET /v1/stops/nearby?lat=...&lon=...`
- `POST /v1/journeys/plan`

Illustrative request (adapt only after defining the actual contract):
```json
{
  "origin": {"lat": 28.6139, "lon": 77.2090},
  "destination": {"lat": 28.5355, "lon": 77.3910},
  "departure_time": "2026-11-14T08:30:00+05:30",
  "max_transfers": 2,
  "alternatives": 3
}
```

Illustrative response:
```json
{
  "request_id": "generated-id",
  "data_as_of": "2026-11-14T08:00:00+05:30",
  "journeys": [
    {
      "duration_minutes": 48,
      "confidence": "medium",
      "estimate_type": "historical_plus_recent_observations",
      "legs": [],
      "warnings": []
    }
  ]
}
```

The final schema must be documented with Pydantic models and OpenAPI. Include units, timezone rules, nullability, confidence semantics, and error responses. Never fabricate live freshness: if the supplied data is historical October 2026 data and no live feed is connected, call it historical/offline mode.

### G. Frontend / demonstration
Build a simple, usable journey-planning interface only after the API works:
- origin/destination inputs;
- departure-time selection;
- journey alternatives;
- estimated duration and transfer count;
- map with stops/route geometry where available;
- clear indication of live, recent, or historical estimate;
- confidence and data-coverage explanation;
- graceful empty/error states.

A clean functional map is more valuable than decorative UI.

## 5. Evaluation and validation (mandatory)

Create a leakage-safe evaluation plan before tuning models.

### Data split
- Prefer chronological holdout: train on earlier dates and test on later dates.
- Prevent the same vehicle trajectory or near-duplicate observations from leaking across train/test.
- Fit preprocessing and feature transforms on training data only.
- Report coverage as well as accuracy; a model that predicts only easy segments is not necessarily better.

### Metrics
For segment-level predictions:
- MAE in minutes or seconds;
- RMSE;
- median absolute error;
- percentage error where target values make it meaningful;
- prediction coverage and sample counts.

For journey-level predictions:
- absolute journey-time error;
- median absolute percentage error (with safeguards near zero);
- route/stop feasibility;
- ranking quality of alternatives if ground truth exists.

### Baselines and external checks
- Compare against historical median and simple road-class/route baselines.
- Create a reproducible sample of origin-destination pairs covering central, peripheral, short, and long journeys.
- Record departure time, date, route/stop pair, observed or reference duration, source, and comparison conditions.
- Google Maps comparisons must be manual or use an authorized, terms-compliant API. Do not scrape Google Maps or imply its estimates are ground truth.
- Compare like with like: bus mode, departure time, route assumptions, and date.
- Include confidence intervals or error distributions, not only one average score.

Create `docs/evaluation.md`, a machine-readable test-pair file, and a reproducible evaluation script.

## 6. Scalability, cost, and deployment

- Start with a local/small-sample pipeline and estimate storage/compute before full processing.
- Use streaming/batch processing and partitioned Parquet.
- Avoid keeping 124 GB of extracted SQLite files if they can be processed and safely archived/removed according to the project policy.
- If cloud compute is used, estimate storage, CPU, memory, bandwidth, and runtime first.
- Do not create, resize, or destroy paid cloud resources without explicit authorization from the project owner.
- Use environment variables/secrets management; never commit API keys.
- Containerize reproducible services with Docker Compose where useful.
- Add resource limits, health checks, persistent-volume planning, backups for derived artifacts, and teardown instructions.
- Document how to run a small sample without downloading the full archive.

## 7. Suggested repository structure

```text
delhi-bus-traffic/
  README.md
  pyproject.toml
  Dockerfile
  docker-compose.yml
  .env.example
  configs/
  data/                 # ignored; never commit raw data
  docs/
    data-audit.md
    architecture.md
    assumptions-limitations.md
    evaluation.md
    api-contract.md
  notebooks/            # exploratory only; production logic belongs in src/
  scripts/
    inspect_data.py
    download_data.py
    ingest_day.py
    build_features.py
    evaluate.py
  src/
    ingestion/
    quality/
    map_matching/
    traffic/
    graph/
    routing/
    api/
  tests/
```

Adapt the structure if the inspected data or chosen stack justifies it. Keep raw/large data out of Git and provide `.gitignore` rules.

## 8. Delivery phases and gates

**Phase 0 — Discovery**
- Read brief/README, inspect schema, verify checksums.
- Deliver data audit, risk register, architecture diagram, storage/compute estimate.
- Do not start a full 124 GB processing run before this gate.

**Phase 1 — Vertical-slice prototype**
- Process a small number of representative hours/days.
- Map-match a sample.
- Estimate travel times for a small set of segments.
- Return one valid journey through an API.
- Demonstrate the complete path from source record to API response.

**Phase 2 — Reliable historical model**
- Process all usable days incrementally.
- Build time-of-day/day-of-week segment profiles.
- Implement baselines and leakage-safe evaluation.
- Report coverage and error.

**Phase 3 — Routing and alternatives**
- Add bus route/stop feasibility and transfers as supported by data.
- Return multiple plausible alternatives.
- Validate sample journeys and edge cases.

**Phase 4 — Interface and deployment**
- Build map interface, Docker deployment, monitoring, documentation, and demo script.

**Phase 5 — Optional live-feed integration**
- Only if an authorized live endpoint and credentials are available.
- Keep live ingestion separate from historical replay.
- Show feed timestamp, staleness, and fallback behavior.

Each phase must end with working tests and a short report. Do not claim a phase is complete without evidence.

## 9. Required tests

At minimum:
- timestamp parsing/timezone normalization;
- invalid coordinates and duplicate detection;
- impossible-jump/outlier handling;
- map-matching confidence and unmatched cases;
- segment travel-time calculation;
- time-bucket fallback behavior;
- time-dependent routing;
- disconnected graph and no-route responses;
- API schema validation;
- deterministic evaluation on a fixed sample;
- data freshness labels and historical fallback.

## 10. Final deliverables

1. Working source repository and setup instructions.
2. Data audit and documented data lineage.
3. Reproducible ingestion and feature pipeline.
4. Road-segment travel-time model with baseline comparison.
5. Journey-planning API and OpenAPI documentation.
6. Working map-based demo.
7. Evaluation report with sample pairs, metrics, coverage, limitations, and Google Maps comparison methodology.
8. Docker setup and deployment/teardown guide.
9. Short demo script and screenshots.
10. Clear list of incomplete items and known limitations.

## 11. Agent operating rules

- Inspect before assuming; never invent schemas, routes, stops, metrics, or results.
- Prefer a small end-to-end working slice over broad unfinished scaffolding.
- Keep code modular, typed, tested, and documented.
- Use configuration rather than hard-coded paths, dates, credentials, or thresholds.
- Record every major modelling assumption.
- Preserve provenance: every prediction should be traceable to its input observations/model version.
- Never label historical October observations as live traffic.
- Never report fabricated accuracy or Google Maps comparisons.
- Ask for approval before incurring cloud costs, deleting source data, or using external paid APIs.
- At the end of each phase, report: what works, commands run, test results, data processed, compute/storage consumed, remaining risks, and next recommended step.

**Start now with Phase 0. Do not jump straight to model training or frontend development.**
