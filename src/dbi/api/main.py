"""FastAPI service for the Delhi bus journey planner.

Every response carries provenance: which data the estimate came from, how
fresh it is, and how confident each leg is. The service never claims to be
live — the underlying data is a historical October 2026 snapshot.
"""
from __future__ import annotations

import gc
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dbi import config
from dbi.journey.planner import Journey, Planner


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Load the network, model and crowding tables before the first request.

    Without this the first query pays several seconds of table loading, which
    looks like a slow API rather than a cold cache.

    The GC thresholds are also raised here. The planner holds several large
    long-lived dicts (800k travel-time profiles, 350k crowding cells). With
    CPython's default thresholds, every query triggers repeated full
    collections over those dicts, which took a 0.55 s query to 2–13 s. Raising
    the thresholds keeps collection proportional to real allocation.
    """
    gc.set_threshold(50_000, 50, 100)
    planner().crowding()
    yield


app = FastAPI(
    title="Delhi Bus Intelligence",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "Bus-GPS travel-time model and journey planner for Delhi, extended with "
        "ticketing-derived crowding intelligence. Estimates are derived from a "
        "historical October 2026 snapshot, not a live feed."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_planner: Planner | None = None

# How many minutes of extra travel a commuter will accept to avoid a crowded
# bus. Used by the "less crowded" ranking so comfort cannot justify an
# arbitrarily long detour.
COMFORT_BUDGET_MIN = 30.0


def planner() -> Planner:
    global _planner
    if _planner is None:
        _planner = Planner()
    return _planner


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------
class Place(BaseModel):
    lat: float | None = None
    lon: float | None = None
    name: str | None = None


class JourneyRequest(BaseModel):
    origin: Place
    destination: Place
    departure_time: datetime
    max_transfers: int = Field(2, ge=0, le=4)
    alternatives: int = Field(3, ge=1, le=5)
    preference: Literal["fastest", "less_crowded", "balanced"] = "balanced"
    crowding_weight: float = Field(0.35, ge=0.0, le=1.0)


class LegOut(BaseModel):
    kind: str
    from_name: str
    to_name: str
    from_lat: float | None = None
    from_lon: float | None = None
    to_lat: float | None = None
    to_lon: float | None = None
    depart: str
    arrive: str
    duration_minutes: float
    route: str | None = None
    distance_m: float | None = None
    confidence: str
    crowding: dict | None = None


class JourneyOut(BaseModel):
    rank: int
    duration_minutes: float
    transfers: int
    walk_minutes: float
    bus_minutes: float
    arrive: str
    crowding_summary: dict
    legs: list[LegOut]
    warnings: list[str]


class PlanResponse(BaseModel):
    request_id: str
    data_status: dict
    origin: dict
    destination: dict
    journeys: list[JourneyOut]
    warnings: list[str]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _resolve(place: Place) -> tuple[float, float, str]:
    p = planner()
    try:
        return p.resolve_place(lat=place.lat, lon=place.lon, name=place.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _crowding_summary(journey: Journey) -> dict:
    """Summarise crowding across bus legs without hiding a bad leg.

    The summary reports the *worst* leg, because averaging a crowded stretch
    with an empty one would tell a commuter the trip is comfortable when part
    of it is not.
    """
    order = {"crowded": 3, "standing_room": 2, "seats_available": 1, "unknown": 0}
    legs = [leg for leg in journey.bus_legs if leg.crowding]
    if not legs:
        return {
            "category": "unknown",
            "target_type": "load_index",
            "confidence": "none",
            "reason": "crowding module unavailable",
        }
    worst = max(legs, key=lambda leg: order.get(leg.crowding["category"], 0))
    known = [leg for leg in legs if leg.crowding["category"] != "unknown"]
    return {
        "category": worst.crowding["category"],
        "target_type": "load_index",
        "confidence": worst.crowding["confidence"],
        "legs_known": len(known),
        "legs_total": len(legs),
        "note": (
            "Relative crowding from a ticketing sample, not measured occupancy."
            if known
            else "No ticketing evidence for these legs."
        ),
    }


def _score(journey: Journey, preference: str, weight: float) -> float:
    """Ranking score. Lower is better.

    Crowding is normalised to 0..1 so it can be added to minutes without
    mixing units.

    Unknown crowding is penalised at a *middle* value, not zero. Treating an
    unknown leg as comfortable would let a journey with no evidence outrank a
    journey known to have seats, which is the opposite of what a commuter
    wants. It is also not penalised as heavily as a known-crowded leg, because
    absence of evidence is not evidence of crowding.

    ``less_crowded`` is a *trade-off*, not a pure comfort sort: comfort is worth
    at most ``COMFORT_BUDGET_MIN`` minutes. Without that cap the mode happily
    recommends a 95-minute trip over a 54-minute one, which no commuter would
    accept.

    The penalty uses the **worst** leg, matching ``crowding_summary``. Averaging
    would let one crowded stretch be diluted by two empty ones, so a journey
    that is partly unbearable would score as comfortable.
    """
    minutes = journey.duration_s / 60.0
    if preference == "fastest":
        return minutes
    order = {
        "crowded": 1.0,
        "standing_room": 0.5,
        "seats_available": 0.0,
        "unknown": 0.35,
    }
    legs = [leg for leg in journey.bus_legs if leg.crowding]
    if not legs:
        return minutes
    penalty = max(order.get(leg.crowding["category"], 0.35) for leg in legs)
    if preference == "less_crowded":
        return minutes + COMFORT_BUDGET_MIN * penalty
    return minutes + weight * penalty * 30.0


def _to_out(journey: Journey, rank: int) -> JourneyOut:
    p = planner()
    warnings = []
    low = [leg for leg in journey.bus_legs if leg.confidence == "low"]
    if low:
        warnings.append(
            f"{len(low)} of {len(journey.bus_legs)} bus legs use a low-confidence "
            "estimate (no direct observation for that stretch and hour)."
        )
    if any(leg.crowding and leg.crowding["category"] == "unknown" for leg in journey.bus_legs):
        warnings.append("Crowding is unknown for at least one leg.")

    def coords(sid):
        if sid is None or sid not in p.stations:
            return None, None
        lat, lon, _ = p.stations[sid]
        return lat, lon

    legs_out = []
    for leg in journey.legs:
        flat, flon = coords(leg.from_station)
        tlat, tlon = coords(leg.to_station)
        legs_out.append(
            LegOut(
                kind=leg.kind,
                from_name=leg.from_name,
                to_name=leg.to_name,
                from_lat=flat,
                from_lon=flon,
                to_lat=tlat,
                to_lon=tlon,
                depart=leg.depart.isoformat(),
                arrive=leg.arrive.isoformat(),
                duration_minutes=round(leg.duration_s / 60.0, 1),
                route=leg.route_name,
                distance_m=round(leg.distance_m, 0) if leg.distance_m is not None else None,
                confidence=leg.confidence,
                crowding=leg.crowding,
            )
        )

    return JourneyOut(
        rank=rank,
        duration_minutes=round(journey.duration_s / 60.0, 1),
        transfers=journey.transfers,
        walk_minutes=round(journey.walk_s / 60.0, 1),
        bus_minutes=round(journey.bus_s / 60.0, 1),
        arrive=journey.arrive.isoformat(),
        crowding_summary=_crowding_summary(journey),
        legs=legs_out,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/v1/metadata")
def metadata():
    p = planner()
    return {
        "data_status": {
            "traffic": "historical",
            "crowding": "historical",
            "traffic_period": "2026-10-01 to 2026-10-29",
            "crowding_period": "2026-10-01 to 2026-10-31",
            "live_feed": False,
            "note": (
                "Estimates are derived from a historical October 2026 snapshot. "
                "They are not a live feed."
            ),
        },
        "network": {
            "stations": len(p.stations),
            "routes": len(p.route_patterns),
            "directed_station_pairs": sum(len(v) for v in p.edges.values()),
        },
        "model": {
            "levels": [
                "pair x hour-of-week",
                "pair x hour-band",
                "pair (all hours)",
                "scheduled time scaled by observed/scheduled ratio",
                "global hour-band",
                "global median",
            ],
            "crowding_target": "load_index (relative, from a ticketing sample)",
        },
    }


@app.get("/v1/stops/nearby")
def stops_nearby(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius_m: float = Query(800, gt=0, le=5000),
    limit: int = Query(10, ge=1, le=50),
):
    p = planner()
    out = [
        {"station_id": sid, "name": name, "distance_m": round(d, 0)}
        for d, sid, name in p.nearby(lat, lon, radius_m)[:limit]
    ]
    return {"count": len(out), "stops": out}


@app.get("/v1/stops/search")
def stops_search(q: str = Query(..., min_length=2), limit: int = Query(10, ge=1, le=50)):
    p = planner()
    try:
        lat, lon, name = p.resolve_place(name=q)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"query": q, "best": {"name": name, "lat": lat, "lon": lon}}


@app.post("/v1/journeys/plan", response_model=PlanResponse)
def plan_journeys(req: JourneyRequest):
    p = planner()
    o_lat, o_lon, o_label = _resolve(req.origin)
    d_lat, d_lon, d_label = _resolve(req.destination)

    journeys = p.plan(
        (o_lat, o_lon),
        (d_lat, d_lon),
        req.departure_time,
        max_transfers=req.max_transfers,
        # Ask for a few extra so ranking has something to choose between, but
        # not so many that the search dominates latency. Each extra alternative
        # is a full search pass.
        alternatives=min(req.alternatives + 1, 4),
    )
    if not journeys:
        return PlanResponse(
            request_id=str(uuid.uuid4()),
            data_status=_data_status(),
            origin={"label": o_label, "lat": o_lat, "lon": o_lon},
            destination={"label": d_label, "lat": d_lat, "lon": d_lon},
            journeys=[],
            warnings=[
                "No bus journey found between these points. Try a larger walking "
                "radius or a different departure time."
            ],
        )

    for j in journeys:
        p.enrich_crowding(j)

    journeys.sort(key=lambda j: _score(j, req.preference, req.crowding_weight))
    chosen = journeys[: req.alternatives]

    return PlanResponse(
        request_id=str(uuid.uuid4()),
        data_status=_data_status(),
        origin={"label": o_label, "lat": o_lat, "lon": o_lon},
        destination={"label": d_label, "lat": d_lat, "lon": d_lon},
        journeys=[_to_out(j, i) for i, j in enumerate(chosen, 1)],
        warnings=[],
    )


def _data_status() -> dict:
    return {
        "traffic": "historical",
        "crowding": "historical",
        "traffic_as_of": "2026-10-29T23:59:59+05:30",
        "crowding_as_of": "2026-10-31T23:59:59+05:30",
        "live_feed": False,
    }


@app.get("/v1/traffic/segments")
def traffic_segment(
    from_station: int = Query(...),
    to_station: int = Query(...),
    at: datetime | None = None,
):
    p = planner()
    when = at or datetime(2026, 10, 15, 9, 0)
    tt, level = p.travel_time(from_station, to_station, when)
    sched = p.scheduled_time(from_station, to_station)
    return {
        "from_station": from_station,
        "to_station": to_station,
        "at": when.isoformat(),
        "predicted_s": round(tt, 1),
        "scheduled_s": sched,
        "level": level,
        "confidence": {"pair_hour": "high", "pair_band": "medium", "pair": "medium"}.get(
            level, "low"
        ),
    }


@app.get("/v1/crowding/routes/{route_name}")
def crowding_route(route_name: str, hour: int = Query(9, ge=0, le=23)):
    import duckdb

    con = duckdb.connect(str(config.CROWDING_DB), read_only=True)
    rows = con.execute(
        """
        SELECT seq, crowding_category, confidence, round(load_index, 2) AS load_index,
               n_vehicle_days
        FROM crowding_levels
        WHERE route = ? AND hour = ?
        ORDER BY seq
        """,
        [route_name, hour],
    ).fetchall()
    con.close()
    if not rows:
        raise HTTPException(status_code=404, detail=f"no crowding data for {route_name} at {hour}:00")
    return {
        "route": route_name,
        "hour": hour,
        "target_type": "load_index",
        "note": "Relative crowding from a ticketing sample, not measured occupancy.",
        "stretches": [
            {
                "seq": r[0],
                "category": r[1],
                "confidence": r[2],
                "load_index": r[3],
                "n_vehicle_days": r[4],
            }
            for r in rows
        ],
    }


# --------------------------------------------------------------------------
# Static frontend
# --------------------------------------------------------------------------
WEB = config.ROOT / "web"
if WEB.exists():
    app.mount("/app", StaticFiles(directory=str(WEB), html=True), name="app")

    @app.get("/")
    def index():
        return FileResponse(str(WEB / "index.html"))
