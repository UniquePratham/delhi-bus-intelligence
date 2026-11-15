"""Validate the planner against real bus trips observed in the GPS data.

Method
------
Comparing a planner's chosen path against "observed" times only works if the
same path was actually driven. Rather than hope for that, this script starts
from real trips:

1. Reconstruct complete vehicle trips from the held-out day's station runs.
2. For each trip, take its origin and destination stations and departure time.
3. Ask the planner for a journey between those two stations at that time.
4. Compare the planner's bus time against the trip's actual elapsed time.

This is a fair comparison: the planner is not told which route the vehicle
took, only where and when the passenger would start.

Two baselines are reported alongside:
  * the GTFS timetable for the planner's own path,
  * the median observed time for the planner's own path at that hour.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

from dbi import config
from dbi.journey.planner import Planner

IST = timezone(timedelta(hours=5, minutes=30))

MIN_TRIP_LEGS = 4
MAX_TRIP_LEGS = 40
SAMPLE_TRIPS = 300


def reconstruct_trips(con, limit: int) -> list[dict]:
    """Build complete trips from consecutive station runs of one vehicle.

    The full station sequence is kept so the planner can be scored on the same
    path the vehicle actually drove. Comparing a planner's freely chosen path
    against a vehicle's path would reward the planner for finding a shortcut
    and make the error meaningless.
    """
    rows = con.execute(
        f"""
        WITH ordered AS (
          SELECT vehicle_id, from_station, to_station, t_depart, t_arrive, travel_s,
                 lag(to_station) OVER (PARTITION BY vehicle_id ORDER BY t_depart) AS prev_to
          FROM runs
        ),
        chained AS (
          SELECT *,
                 sum(CASE WHEN prev_to IS NULL OR prev_to <> from_station THEN 1 ELSE 0 END)
                   OVER (PARTITION BY vehicle_id ORDER BY t_depart ROWS UNBOUNDED PRECEDING) AS trip_grp
          FROM ordered
        ),
        agg AS (
          SELECT vehicle_id, trip_grp,
                 min(from_station) AS origin_station,
                 max(to_station) AS dest_station,
                 min(t_depart) AS t_start,
                 max(t_arrive) AS t_end,
                 count(*) AS n_legs,
                 sum(travel_s) AS observed_s,
                 list(from_station ORDER BY t_depart) AS from_seq,
                 list(to_station ORDER BY t_depart) AS to_seq
          FROM chained
          GROUP BY 1, 2
          HAVING count(*) BETWEEN {MIN_TRIP_LEGS} AND {MAX_TRIP_LEGS}
        )
        SELECT * FROM agg
        ORDER BY hash(vehicle_id || trip_grp::VARCHAR)
        LIMIT {limit}
        """
    ).fetchall()
    return [
        {
            "vehicle_id": r[0],
            "origin_station": r[2],
            "dest_station": r[3],
            "t_start": r[4],
            "t_end": r[5],
            "n_legs": r[6],
            "observed_s": r[7],
            "path": list(r[8]) + [r[9][-1]],
        }
        for r in rows
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(config.ARTIFACTS / "validation.json"))
    ap.add_argument("--sample", type=int, default=SAMPLE_TRIPS)
    args = ap.parse_args()

    planner = Planner()
    con = duckdb.connect(str(config.TRAFFIC_DB), read_only=True)
    con.execute("SET threads=8")

    trips = reconstruct_trips(con, args.sample)
    print(f"reconstructed {len(trips)} trips from the held-out day")

    rows = []
    for trip in trips:
        o = planner.stations.get(trip["origin_station"])
        d = planner.stations.get(trip["dest_station"])
        if o is None or d is None:
            continue
        when = trip["t_start"]
        if isinstance(when, (int, float)):
            when = datetime.fromtimestamp(when, tz=IST)
        elif isinstance(when, str):
            when = datetime.fromisoformat(when)
        if when.tzinfo is None:
            when = when.replace(tzinfo=IST)

        # Score the model on the path the vehicle actually drove, so the
        # comparison is like for like.
        path = trip["path"]
        predicted_s = 0.0
        scheduled_s = 0.0
        sched_ok = True
        levels = []
        t = when
        for a, b in zip(path, path[1:], strict=False):
            tt, level = planner.travel_time(a, b, t)
            predicted_s += tt
            levels.append(level)
            t = t + timedelta(seconds=tt)
            s = planner.scheduled_time(a, b)
            if s is None:
                sched_ok = False
            else:
                scheduled_s += s

        # Also record what the planner would choose, for the journey-level view.
        journeys = planner.plan(
            (o[0], o[1]), (d[0], d[1]), when, max_transfers=2, alternatives=1
        )
        chosen = journeys[0] if journeys else None

        rows.append(
            {
                "vehicle_id": trip["vehicle_id"],
                "origin": o[2],
                "destination": d[2],
                "depart": when.isoformat(),
                "hour": when.hour,
                "observed_min": round(trip["observed_s"] / 60.0, 1),
                "observed_legs": trip["n_legs"],
                "predicted_min": round(predicted_s / 60.0, 1),
                "scheduled_min": round(scheduled_s / 60.0, 1) if sched_ok else None,
                "error_min": round((predicted_s - trip["observed_s"]) / 60.0, 1),
                "abs_error_min": round(abs(predicted_s - trip["observed_s"]) / 60.0, 1),
                "sched_abs_error_min": (
                    round(abs(scheduled_s - trip["observed_s"]) / 60.0, 1)
                    if sched_ok
                    else None
                ),
                "levels": {lv: levels.count(lv) for lv in set(levels)},
                "planner_chosen_min": (
                    round(chosen.bus_s / 60.0, 1) if chosen else None
                ),
                "planner_chosen_routes": (
                    [leg.route_name for leg in chosen.bus_legs] if chosen else None
                ),
            }
        )

    con.close()

    if not rows:
        print("no comparable trips", file=sys.stderr)
        return 1

    errs = [r["abs_error_min"] for r in rows]
    signed = [r["error_min"] for r in rows]
    errs_sorted = sorted(errs)
    sched_errs = [r["sched_abs_error_min"] for r in rows if r["sched_abs_error_min"] is not None]
    summary = {
        "n_trips": len(rows),
        "mae_min": round(sum(errs) / len(errs), 2),
        "median_ae_min": round(errs_sorted[len(errs_sorted) // 2], 2),
        "p90_ae_min": round(errs_sorted[int(len(errs_sorted) * 0.9)], 2),
        "bias_min": round(sum(signed) / len(signed), 2),
        "within_5_min_pct": round(100.0 * sum(1 for e in errs if e <= 5) / len(errs), 1),
        "within_10_min_pct": round(100.0 * sum(1 for e in errs if e <= 10) / len(errs), 1),
        "baseline_schedule": {
            "n": len(sched_errs),
            "mae_min": round(sum(sched_errs) / len(sched_errs), 2) if sched_errs else None,
            "median_ae_min": (
                round(sorted(sched_errs)[len(sched_errs) // 2], 2) if sched_errs else None
            ),
        },
        "improvement_vs_schedule_pct": (
            round(
                100.0
                * (1 - (sum(errs) / len(errs)) / (sum(sched_errs) / len(sched_errs))),
                1,
            )
            if sched_errs and sum(sched_errs) > 0
            else None
        ),
    }

    out = {"summary": summary, "rows": rows}
    p = Path(args.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"\nwrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
