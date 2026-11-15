# Agent Build Prompt — Problem 2
## Crowdedness-Aware Journey Planner (Delhi DTC)

**Role:** Act as a senior data engineer, transport analytics researcher, machine-learning engineer, and backend architect. Build a defensible passenger-crowdedness estimation system from Delhi bus e-ticketing/ETM data and integrate its outputs into a journey-planning experience.

**Priority:** This is a standalone implementation prompt for Problem 2. It must also be designed as a modular extension that can later connect to the traffic and journey-planning platform described in the Problem 1 prompt.

---

## 1. Objective and expected outcome

Build a model that estimates bus crowdedness by route, direction, trip, stop segment, and time period to the finest granularity the data genuinely supports. Integrate these estimates into a journey planner so commuters can compare options on both travel time and expected crowding.

The system should:
1. Ingest and audit the supplied e-ticketing/ETM records.
2. Understand exactly what each ticket row represents.
3. Estimate boarding/alighting demand and passenger load only where fields support it.
4. Produce route/time/segment crowdedness estimates with uncertainty and coverage.
5. Expose estimates through a documented API.
6. Display interpretable crowding levels beside journey options.
7. Validate estimates against known peak/off-peak patterns and any independent ground truth available.

Do not equate ticket count with onboard occupancy unless the data model justifies that inference. Ticket issuance, passenger count, boarding, alighting, transfers, cancellations, and actual occupancy are different concepts.

## 2. Source data and first actions

Use the supplied October 2026 data:

- E-ticketing archive: https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/October-Delhi.csv.zip
- Data README: https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/README.md
- Checksums: https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/SHA256SUMS
- Full problem brief: https://docs.google.com/document/d/1ocS9rG4_E_Fb9DJcqTG8SClrLJJ_UJqGGAhgPbV8tr4/preview

The brief describes approximately 2.56 million tickets across 1–31 October 2026, with a 66 MB compressed CSV and approximately 290 MB unpacked. Verify actual values and schema rather than treating these figures as guaranteed.

**Before modelling:**
1. Read the README and full brief.
2. Verify the archive checksum.
3. Inspect CSV encoding, delimiter, header, row count, column types, null rates, unique values, duplicate rates, date coverage, and malformed rows.
4. Identify ticket IDs, route/trip/vehicle IDs, timestamps, fare/product types, boarding stop, alighting stop, passenger count, and cancellation/refund indicators—only if present.
5. Inspect route/stop reference data and how it links to Transport Stack identifiers.
6. Document findings in `docs/data-audit.md`. Never invent absent fields.

## 3. Critical data feasibility gate

Before designing the target variable, answer these questions from the actual schema and documentation:

- Does a ticket row correspond to one passenger, a group, or a transaction?
- Is passenger quantity explicit?
- Are boarding and destination/alighting stops present?
- Is the bus trip or vehicle identifiable?
- Is the route direction identifiable?
- Are timestamps generated at booking, issuance, validation, or boarding?
- Are cancelled/refunded/voided tickets distinguishable?
- Are cash/manual tickets or other passenger categories missing?
- Can a ticket be linked to a particular trip rather than only a route?
- Are stop IDs stable and joinable to Transport Stack route/stop data?

Create a **target feasibility matrix** with columns: target, required fields, fields present, inference required, confidence, and decision.

Possible levels:
- **Level 1 — Route/time demand:** tickets per route per time bucket. Usually the safest target if route and timestamp exist.
- **Level 2 — Trip-level passenger count:** possible if tickets can be assigned to trips and passenger quantity is known.
- **Level 3 — Stop-to-stop load profile:** requires reliable boarding and alighting stop data, trip assignment, and passenger counts.
- **Level 4 — Actual occupancy/capacity percentage:** additionally requires usable vehicle capacity and reasonably complete passenger accounting or independent occupancy observations.

Only claim the highest level supported by the data. If destination stops or trip linkage are absent, do not fabricate stop-level occupancy. Build a clearly labelled demand/crowding proxy and state its limitations.

## 4. Proposed architecture

### A. Ingestion and data quality
- Download with checksum verification and preserve the original archive.
- Extract and process the CSV with Polars or DuckDB.
- Normalize timestamps and explicitly document timezone.
- Standardize route/stop/trip identifiers using reference tables.
- Deduplicate only with a defensible key; quantify removed duplicates.
- Separate valid, invalid, cancelled, refunded, and uncertain records where possible.
- Preserve an audit trail and source-row identifiers.
- Make processing reproducible and idempotent.

### B. Feature engineering
Potential features, only when available:
- route, direction, trip, stop, route segment;
- time of day, weekday, weekend, holiday;
- ticket issuance/validation timestamp;
- ticket type/fare category;
- passenger quantity;
- boarding and alighting stop;
- historical route demand and recent demand;
- service frequency or scheduled headway, if a documented source is available;
- vehicle capacity, only if known and correctly mapped.

Use sensible time buckets initially (e.g. 15, 30, or 60 minutes), then choose based on data volume and operational usefulness. Avoid overly granular estimates with sparse samples.

### C. Crowdedness estimation

Build transparent baselines before advanced ML.

**If boarding and alighting are available and reliable:**
- For each trip, calculate passenger load after each stop:
  `load_after_stop = load_before_stop + boardings - alightings`.
- Enforce non-negative loads and flag impossible sequences.
- Aggregate segment load profiles by route, direction, time bucket, and day type.
- Estimate occupancy percentage only when vehicle capacity is available and valid.
- Handle incomplete ticket coverage; ticket records may not include every passenger.

**If only route/time ticket counts are available:**
- Estimate route-time demand, not actual occupancy.
- Normalize by service frequency/trips only if service data is available.
- Call the result a crowding proxy or demand intensity.
- Avoid presenting a ticket count as a passenger load.
- Calibrate categories using observed patterns or independent data if possible.

Candidate models:
1. Historical median/mean by route and time bucket.
2. Day-of-week and time-of-day seasonal baseline.
3. LightGBM/XGBoost regression or count modelling where the target is well-defined.
4. Hierarchical/pooled estimates for sparse routes and time buckets.
5. Quantile estimates or prediction intervals to communicate uncertainty.

Do not train a model until the target definition and leakage-safe evaluation split are approved. Avoid random row splits where timestamps from the same service period can leak.

### D. Crowdedness categories and semantics
Create categories only after examining distributions and, where possible, vehicle capacity:
- Low
- Moderate
- High
- Very high

If actual capacity is unavailable, categories must be relative demand-intensity bands, not claims about physical occupancy. Document thresholds and how they were chosen.

Every estimate should carry:
- estimate value and unit/meaning;
- category;
- route/trip/segment/time scope;
- sample count;
- data timestamp/period;
- confidence or uncertainty;
- target type (`occupancy`, `passenger_load`, or `demand_proxy`);
- warnings about incomplete ticket coverage.

### E. Journey planner integration
The crowdedness module should not own routing. It should expose a stable service/API that a journey planner can call.

For each candidate journey:
- retrieve expected crowding for each bus leg and relevant time window;
- aggregate leg-level estimates into a journey summary without hiding a very crowded leg;
- show crowding category, estimate type, and confidence;
- permit users to trade off time versus crowding;
- retain the fastest option and less-crowded alternatives;
- handle missing estimates by displaying “unknown” rather than silently assigning “low”.

Potential ranking objective:
`generalized_cost = travel_time + lambda * crowding_penalty + transfer_penalty`
where `lambda` is configurable and the crowding penalty is derived from documented categories/values. Do not imply this is universally optimal; expose the trade-off.

### F. API
Use FastAPI or an equivalent documented service. Example endpoints:

- `GET /health`
- `GET /v1/crowding/routes/{route_id}?datetime=...`
- `GET /v1/crowding/segments/{segment_id}?datetime=...`
- `POST /v1/crowding/batch-estimates`
- `GET /v1/crowding/metadata`

Illustrative response:
```json
{
  "route_id": "route-id",
  "time_bucket": "2026-11-14T08:00:00+05:30",
  "target_type": "demand_proxy",
  "category": "high",
  "estimate": 42.0,
  "unit": "tickets_per_30_min",
  "sample_count": 180,
  "confidence": "medium",
  "warnings": ["No alighting-stop field; this is not onboard occupancy."]
}
```

The actual response must match the target supported by the data. Document schemas, units, timezone, confidence, and error handling.

### G. Frontend
- Journey alternatives with duration and crowding side by side.
- Route/segment crowding heatmap where spatial data supports it.
- Time-of-day selector.
- Explain whether a value is measured occupancy or a proxy.
- Show “unknown/insufficient data” for unsupported cases.
- Provide a data coverage/last-updated panel.
- Make the fastest and least-crowded options easy to compare.

## 5. Evaluation and validation (mandatory)

### Data split
- Use chronological holdout, preferably by date or service period.
- Avoid leakage between tickets from the same trip/service where relevant.
- Fit encoders, aggregations, and thresholds on training data only.
- Evaluate separately for high-volume and sparse routes.

### Metrics
Choose metrics based on the actual target:
- Passenger count/load: MAE, RMSE, Poisson deviance where appropriate.
- Occupancy percentage: MAE in percentage points and calibration.
- Demand proxy: MAE/RMSE or count-model deviance, plus ranking/correlation.
- Crowding category: macro-F1, per-class precision/recall, confusion matrix.
- Uncertainty: interval coverage and interval width.
- Coverage: fraction of route/time buckets with supported estimates.

### Spot checks
- Compare weekday morning/evening peaks with off-peak periods.
- Compare weekdays and weekends.
- Inspect high-demand routes and low-sample routes separately.
- Manually inspect representative route/time profiles.
- If independent occupancy counts or operator observations exist, reserve them as external validation.
- Do not use expected peak patterns as proof of accuracy; they are sanity checks, not ground truth.

Create `docs/evaluation.md`, a reproducible evaluation script, and a fixed sample of route/time cases.

## 6. Scalability, privacy, and deployment
- The CSV is relatively manageable, but use typed columnar processing and reproducible pipelines.
- Minimize retained personal or ticket-level identifiers. Hash/tokenize or drop identifiers after deduplication/linkage if they are not required.
- Do not expose individual ticket or passenger records in APIs, logs, dashboards, or demos.
- Aggregate results to route/time/segment levels.
- Keep credentials and paid service keys out of source control.
- Use Docker where useful; document environment variables and resource needs.
- Do not create paid cloud resources or incur API costs without explicit authorization.
- Document data retention and deletion choices.

## 7. Suggested repository structure

```text
delhi-bus-crowding/
  README.md
  pyproject.toml
  Dockerfile
  docker-compose.yml
  .env.example
  configs/
  data/                  # ignored; never commit raw ticket data
  docs/
    data-audit.md
    target-feasibility.md
    architecture.md
    assumptions-limitations.md
    evaluation.md
    api-contract.md
  scripts/
    inspect_data.py
    ingest_tickets.py
    build_features.py
    train.py
    evaluate.py
  src/
    ingestion/
    quality/
    identity_resolution/
    features/
    demand/
    crowding/
    api/
  tests/
```

## 8. Delivery phases and gates

**Phase 0 — Discovery and target feasibility**
- Read brief/README, verify checksum, inspect schema and reference data.
- Produce data audit and target feasibility matrix.
- Decide whether the system can estimate actual occupancy, passenger load, or only demand proxy.
- Do not proceed to occupancy modelling until this gate is passed.

**Phase 1 — Baseline**
- Build route/time ticket-volume profiles.
- Validate timestamps, route joins, duplicates, and missingness.
- Create visual peak/off-peak sanity checks.
- Publish a baseline API response.

**Phase 2 — Stronger estimates**
- If fields support it, reconstruct trip/segment passenger load.
- Otherwise improve demand proxy with service frequency and hierarchical estimates where supported.
- Add uncertainty and sparse-data fallback.
- Train and compare appropriate models.

**Phase 3 — Journey planner integration**
- Add crowding lookup to candidate bus legs.
- Show fastest and less-crowded alternatives.
- Add configurable time/crowding preference.
- Ensure missing estimates remain unknown.

**Phase 4 — Validation and deployment**
- Complete temporal evaluation, spot checks, Docker deployment, documentation, and demo.

Each phase must have working tests and a concise evidence report.

## 9. Required tests

- CSV parsing, schema drift, malformed rows;
- timestamp and timezone normalization;
- duplicate and cancellation handling;
- route/stop/trip join integrity;
- passenger-count aggregation;
- load conservation across stop sequences where applicable;
- non-negative occupancy and capacity bounds;
- sparse-route fallback;
- demand proxy is never labelled actual occupancy;
- time-bucket boundary behavior;
- chronological split and leakage checks;
- API response semantics and missing-data behavior;
- privacy checks for identifiers in logs/responses.

## 10. Final deliverables

1. Working source repository and reproducible setup.
2. Data audit and target feasibility matrix.
3. Clean, aggregated ticketing dataset or reproducible feature pipeline.
4. Crowdedness/load/demand model with explicit target definition.
5. API and OpenAPI documentation.
6. Journey-planner integration or demonstrable integration contract.
7. Evaluation report, peak/off-peak spot checks, uncertainty, and coverage.
8. Docker setup and deployment guide.
9. Demo script and screenshots.
10. Clear limitations and unsupported inferences.

## 11. Agent operating rules

- Never assume tickets equal passengers or passengers equal occupancy.
- Never invent boarding/alighting stops, vehicle capacities, route IDs, or trip links.
- Keep measured fields, inferred fields, and model predictions distinct.
- Label proxy estimates honestly.
- Do not fabricate model accuracy or ground truth.
- Protect ticket-level identifiers and personal information.
- Ask before incurring cloud/API costs or deleting source data.
- At each phase, report code delivered, tests, records processed, metrics, coverage, resource use, risks, and next step.

**Start with Phase 0. Your first deliverable is a data audit and a clear decision on what crowdedness target the data can actually support.**
