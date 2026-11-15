# Master Agent Prompt — Unified Delhi Bus Intelligence and Journey Planner
## Primary: Problem 1 (Traffic Model) | Integrated extension: Problem 2 (Crowdedness)

**Project decision:** Problem 1 is the preferred and primary problem statement. The submitted project will present a unified platform that implements the Problem 1 traffic/journey-planning capability as its core and integrates the Problem 2 crowdedness capability as a complementary module. The project must remain demonstrably useful if the crowdedness module is incomplete or the ticket data cannot support true occupancy.

**Role:** Act as the technical lead and a cross-functional team of data, geospatial, ML, backend, frontend, and DevOps engineers. Build a unified, reproducible, evidence-backed Delhi public-bus journey intelligence platform.

---

## 1. Product vision

Build a Delhi bus journey planner that answers:

- How long is this bus journey likely to take?
- Which feasible bus journey options connect my origin and destination?
- How does traffic affect the expected duration?
- How crowded are the bus legs likely to be?
- What alternatives trade a small increase in time for lower expected crowding?
- How fresh and reliable are these estimates?

The platform combines two independent evidence streams:

1. **Traffic intelligence (Problem 1 — primary):** bus GPS trajectories inform road/route segment travel-time estimates and time-dependent journey planning.
2. **Crowdedness intelligence (Problem 2 — secondary):** e-ticketing/ETM records inform passenger demand, load, or crowding estimates at the level the fields genuinely support.

The two modules must be independently testable and connected through stable interfaces. Do not couple the code so tightly that failure of one module breaks the other.

## 2. Source data

### Problem 1 — GPS
- Archive: https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/delhi-dtc-bus-gtfs-rt-2026-10.7z
- README: https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/README.md
- Checksums: https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/SHA256SUMS
- Brief: https://docs.google.com/document/d/1rfEu-ptgN4MNlcTKvv59nzc_OVjPmbk_hAxpe5HCirc/preview

### Problem 2 — Ticketing/ETM
- Archive: https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/October-Delhi.csv.zip
- README: https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/README.md
- Checksums: https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/SHA256SUMS
- Brief: https://docs.google.com/document/d/1ocS9rG4_E_Fb9DJcqTG8SClrLJJ_UJqGGAhgPbV8tr4/preview

The brief describes approximately 16.1 GB compressed / 124 GB unpacked GPS data and approximately 66 MB compressed / 290 MB unpacked ticketing data, both covering 1–31 October 2026. Verify all actual sizes, checksums, schemas, and coverage.

**Important:** Both supplied datasets are historical October 2026 snapshots. They do not, by themselves, provide live November 2026 traffic or passenger loads. The product must visibly distinguish historical estimates, replay/simulated data, and a genuinely connected live feed.

## 3. Non-negotiable discovery and data audit

Before full implementation:

1. Read both full problem briefs and the common README.
2. Verify both archives against checksums.
3. Inspect all schemas and sample records.
4. Audit timestamp semantics, timezone, coordinate reference system, IDs, nulls, duplicates, outliers, coverage, and data linkage.
5. Determine whether GPS records can be linked to ticket records by vehicle, route, trip, direction, date, or time.
6. Determine whether ticket records contain passenger quantity, boarding stop, alighting stop, trip, and vehicle fields.
7. Determine whether GTFS/static route/stop/shape data is supplied or must be sourced separately.
8. Create a join feasibility matrix. Do not assume that similarly named IDs refer to the same entity.
9. Write `docs/data-audit.md`, `docs/entity-linkage.md`, and `docs/assumptions-limitations.md`.

### Critical target rule for crowdedness
Decide whether ticketing supports:
- route/time demand counts;
- trip-level passenger counts;
- stop-to-stop passenger load;
- actual occupancy or capacity percentage.

Only implement and label the strongest level supported. If the data cannot support actual occupancy, use an explicitly named `demand_proxy` or equivalent. Never convert ticket counts directly into occupancy without evidence.

## 4. Product architecture

Use a modular architecture with these logical services:

```text
                 ┌────────────────────────────┐
                 │        Web Frontend        │
                 │ Map + Journey Comparison  │
                 └──────────────┬─────────────┘
                                │
                 ┌──────────────▼─────────────┐
                 │     Journey Planner API    │
                 │ candidate routes + ranking│
                 └───────┬─────────────┬──────┘
                         │             │
              ┌──────────▼──────┐ ┌────▼────────────┐
              │ Traffic Service│ │Crowding Service │
              │ Problem 1      │ │Problem 2        │
              └──────────┬──────┘ └────┬────────────┘
                         │             │
              ┌──────────▼──────┐ ┌────▼────────────┐
              │ GPS/Map-match   │ │ ETM/ticket ETL  │
              │ travel-time ML  │ │ demand/load ML  │
              └──────────┬──────┘ └────┬────────────┘
                         │             │
              ┌──────────▼─────────────▼────────────┐
              │   Shared transport reference layer │
              │ routes, trips, stops, shapes, IDs  │
              └────────────────────────────────────┘
```

This is a logical diagram, not a mandate to deploy every box as a separate microservice. For a prototype, a modular monolith or a small number of services may be cheaper and easier to operate. Keep clear module boundaries and API contracts.

### Shared transport reference layer
- Canonical route, stop, trip, direction, vehicle, and segment identifiers.
- Source-to-canonical ID mapping tables.
- Route geometries and stop sequences, with source and licensing metadata.
- Explicit validity dates for reference data.
- Never merge entities solely because their display names look similar.

### Traffic module — primary
Implement the complete Problem 1 pipeline:
- incremental GPS ingestion and data quality;
- trajectory reconstruction and map matching;
- road/route segment travel-time profiles;
- time-dependent travel-time prediction;
- graph-based journey planning;
- API with estimated durations, route legs, freshness, and confidence.

Start with robust historical baselines. Add advanced ML only if it improves leakage-safe held-out performance.

### Crowding module — secondary
Implement the Problem 2 pipeline:
- ticket ingestion, validation, deduplication, and route/stop/trip joins;
- target feasibility gate;
- route/time demand baseline;
- trip/segment load reconstruction only if the required fields exist;
- crowding categories and uncertainty;
- API returning target type, unit, time scope, coverage, and warnings.

This module must be usable independently and must not block the primary journey planner.

### Journey orchestration and ranking
The planner first generates feasible bus journeys using the traffic/routing module. It then enriches each bus leg with crowdedness estimates when available.

Provide at least these views:
1. **Fastest:** prioritize predicted total travel time.
2. **Less crowded:** prioritize lower estimated crowding while preserving feasibility.
3. **Balanced:** configurable trade-off between time and crowding.

A possible generalized cost is:

`cost = travel_time + alpha * crowding_penalty + beta * transfer_penalty`

- `alpha` and `beta` must be configurable and documented.
- Do not mix raw ticket counts, passenger counts, and occupancy percentages in one penalty without normalization.
- Do not hide a high-crowding leg behind a low journey average.
- Missing crowdedness must be shown as `unknown`, never silently treated as low.
- If crowding service is unavailable, return valid Problem 1 journeys with a clear partial-data warning.

## 5. Unified API contract

Use FastAPI or another justified framework. Version the API and define typed request/response schemas.

Suggested endpoints:
- `GET /health`
- `GET /v1/metadata`
- `GET /v1/stops/nearby`
- `POST /v1/journeys/plan`
- `GET /v1/traffic/segments/{segment_id}`
- `GET /v1/crowding/routes/{route_id}`
- `GET /v1/crowding/segments/{segment_id}`

Example journey request:
```json
{
  "origin": {"lat": 28.6139, "lon": 77.2090},
  "destination": {"lat": 28.5355, "lon": 77.3910},
  "departure_time": "2026-11-14T08:30:00+05:30",
  "max_transfers": 2,
  "alternatives": 3,
  "preference": "balanced",
  "crowding_weight": 0.35
}
```

Example response shape (illustrative; finalize after inspecting the data):
```json
{
  "request_id": "generated-id",
  "data_status": {
    "traffic": "historical",
    "crowding": "historical",
    "traffic_as_of": "2026-10-31T23:59:59+05:30",
    "crowding_as_of": "2026-10-31T23:59:59+05:30"
  },
  "journeys": [
    {
      "rank": 1,
      "preference": "balanced",
      "duration_minutes": 42,
      "traffic_confidence": "medium",
      "crowding_summary": {
        "category": "unknown",
        "target_type": "demand_proxy",
        "confidence": "low"
      },
      "legs": [],
      "warnings": []
    }
  ]
}
```

Do not use example values as actual model outputs. Every estimate needs provenance, units, scope, and data freshness.

## 6. User experience requirements

Create a responsive map-first interface:
- origin and destination search;
- departure date/time;
- map showing candidate bus routes and stops;
- journey cards with duration, transfers, and crowding;
- tabs/filters for Fastest, Less Crowded, and Balanced;
- per-leg detail, including crowdedness on each leg;
- visual distinction between measured occupancy and demand proxy;
- estimate freshness and confidence;
- data coverage and limitations panel;
- loading, no-route, partial-service, and error states.

The interface must never suggest the data is live if it is not. Provide an “About these estimates” explanation that ordinary commuters can understand.

## 7. Data processing and infrastructure strategy

### GPS data
- Do not load the full 124 GB into memory.
- Process one daily SQLite database at a time.
- Extract relevant columns and write partitioned Parquet.
- Use DuckDB/Polars for analytical workloads.
- Track checksums, manifests, row counts, and processing status.
- Keep large raw data out of Git.

### Ticketing data
- Use Polars/DuckDB for parsing and aggregation.
- Remove or pseudonymize identifiers not required after deduplication/linkage.
- Publish only aggregates, never ticket-level data.

### Cloud and costs
- Begin with a representative small sample and benchmark it.
- Estimate CPU, RAM, disk, bandwidth, and runtime before full processing.
- Use local processing when feasible.
- Do not create, resize, or destroy paid instances or purchase APIs without explicit user authorization.
- Provide a cost estimate and a teardown plan before deployment.
- Use Docker Compose for a reproducible prototype where useful.
- Keep secrets in environment variables or a secrets manager.

## 8. Evaluation strategy

Evaluate each module independently and the combined product.

### Traffic module
- Chronological holdout and trajectory-aware leakage prevention.
- Compare with historical median and simple baselines.
- Report segment MAE/RMSE, journey-time absolute error, coverage, and confidence.
- Validate representative origin-destination pairs.
- Google Maps comparison only via manual checks or an authorized terms-compliant API; do not scrape it or call it ground truth.

### Crowding module
- Target-specific metrics: passenger-load MAE/RMSE, occupancy percentage-point MAE, demand-count error, or category macro-F1.
- Chronological holdout.
- Route/time coverage and uncertainty.
- Peak/off-peak and weekday/weekend sanity checks.
- Independent operator/occupancy observations if available.

### Integrated planner
- Route feasibility and valid stop sequences.
- End-to-end journey-time error.
- Crowding enrichment coverage.
- Ranking consistency for fastest/less-crowded/balanced modes.
- Graceful operation when crowding data is missing.
- Latency and API reliability.

Create:
- `docs/evaluation-traffic.md`
- `docs/evaluation-crowding.md`
- `docs/evaluation-integrated.md`
- fixed, reproducible validation samples
- scripts to reproduce every reported metric.

Do not claim that spot checks establish statistical accuracy. Report limitations and sample sizes.

## 9. Repository layout

```text
delhi-bus-intelligence/
  README.md
  pyproject.toml
  Dockerfile
  docker-compose.yml
  .env.example
  configs/
  data/                         # ignored; raw data never committed
  docs/
    data-audit.md
    entity-linkage.md
    architecture.md
    assumptions-limitations.md
    api-contract.md
    evaluation-traffic.md
    evaluation-crowding.md
    evaluation-integrated.md
    deployment-costs.md
  scripts/
    inspect_gps.py
    inspect_tickets.py
    ingest_gps_day.py
    ingest_tickets.py
    build_traffic_features.py
    build_crowding_features.py
    train_traffic.py
    train_crowding.py
    evaluate_traffic.py
    evaluate_crowding.py
    evaluate_integrated.py
  src/
    common/
    reference_data/
    traffic/
      ingestion/
      quality/
      map_matching/
      features/
      models/
      graph/
      routing/
      api/
    crowding/
      ingestion/
      quality/
      features/
      targets/
      models/
      api/
    journey/
      orchestration/
      ranking/
      api/
    frontend/                   # or a separately managed frontend app
  tests/
    unit/
    integration/
    evaluation/
```

Adapt after data discovery. Avoid premature microservices and duplicated reference data.

## 10. Delivery roadmap and priority rules

### Milestone 0 — Discovery gate
Deliver:
- both data audits;
- target feasibility matrix for crowdedness;
- entity-linkage feasibility;
- architecture and cost estimate;
- risk register.

No full-scale processing until this is reviewed.

### Milestone 1 — Problem 1 vertical slice (highest priority)
Deliver a small but real end-to-end flow:
- a sample GPS day/hour;
- matched segments with confidence;
- baseline segment travel-time estimate;
- one feasible journey API response;
- reproducible test and evidence.

This is the first working demonstration and the core submission.

### Milestone 2 — Problem 1 coverage and evaluation
- process usable GPS data incrementally;
- build historical time-dependent profiles;
- add route alternatives;
- report held-out performance and coverage.

### Milestone 3 — Problem 2 independent baseline
- ingest ticket data;
- finish target feasibility gate;
- produce route/time demand profile or supported load estimate;
- evaluate and expose the crowding API.

### Milestone 4 — Integration
- enrich Problem 1 journey legs with crowding;
- implement Fastest, Less Crowded, Balanced;
- demonstrate graceful degradation when crowding is missing.

### Milestone 5 — Product polish and deployment
- map interface;
- docs, Docker, monitoring, demo;
- cost and teardown guide;
- final reproducible evaluation.

**Priority rule:** If time, compute, or data quality becomes constrained, preserve a complete and validated Problem 1 journey planner. Reduce the breadth of Problem 2 rather than compromising the primary project.

## 11. Definition of done

The project is ready to submit only when:

- A user can enter two locations and receive at least one plausible, data-backed bus journey option.
- The journey uses supported route/stop/graph information, not invented connectivity.
- Travel-time estimates have a baseline comparison and held-out evaluation.
- The response reports data freshness and confidence.
- Crowding is integrated where supported and explicitly unknown where unsupported.
- Fastest, Less Crowded, and Balanced modes behave as documented.
- The system works if the crowding module is unavailable.
- Setup and evaluation can be reproduced from documentation.
- No raw data, secrets, fabricated metrics, or misleading “live” claims are included.

## 12. Agent operating instructions

1. Start with Milestone 0; do not skip data audits.
2. Treat Problem 1 as the required core and Problem 2 as an independently optional extension.
3. Do not invent data fields, route relationships, stop sequences, capacities, ground truth, or performance results.
4. Keep all model outputs traceable to source data, processing version, model version, and prediction time.
5. Distinguish observed, inferred, predicted, and unavailable values in code and UI.
6. Build tests alongside each module.
7. Do not use paid cloud resources or APIs without approval.
8. Never delete original data without approval and a verified recovery plan.
9. At each milestone report: completed functionality, commands, tests, data processed, metrics, coverage, resource use, known risks, and next actions.
10. Keep the submission narrative honest: **“A bus-GPS-based traffic and journey-planning system, extended with ticketing-derived crowdedness intelligence.”**

**Begin with Milestone 0. Return the data audits, feasibility findings, proposed architecture, and resource estimate before launching full processing.**
