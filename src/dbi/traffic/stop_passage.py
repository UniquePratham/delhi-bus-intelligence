"""Link GPS pings to the GTFS network by detecting station passages.

The GPS feed carries a route_id from a different ID space than GTFS (verified:
0% semantic agreement), so route identity is never taken from the feed.
Instead each ping is snapped to the nearest *station* (clustered GTFS stops).

Timing a passage at the first ping inside a snap radius biases travel time
downwards, because the bus is already partway through the run when it enters
the radius. Each passage is therefore timed at its *closest approach* to the
station centroid, which is an unbiased estimate of the moment the bus passed
the stop.

A run is kept only when the station pair exists as a real consecutive pair on
some GTFS route, which removes most spurious snaps.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

from dbi import config

CELL = 0.005  # ~550 m grid bucket, so snapping is O(n) not O(n*m)


def detect(pings: Path, out: Path) -> dict[str, int]:
    con = duckdb.connect(str(config.REFERENCE_DB))
    con.execute("SET threads=8")
    con.execute("SET memory_limit='6GB'")

    con.execute(
        f"CREATE OR REPLACE TEMP VIEW pings AS SELECT * FROM read_parquet('{pings.as_posix()}')"
    )

    # 1. Snap every ping to its nearest station within the snap radius.
    con.execute(
        f"""
        CREATE TEMP TABLE snapped AS
        WITH p AS (
          SELECT vehicle_id, ts, lat, lon,
                 CAST(floor(lat/{CELL}) AS BIGINT) AS cy,
                 CAST(floor(lon/{CELL}) AS BIGINT) AS cx
          FROM pings
        ),
        s AS (
          SELECT station_id, lat, lon,
                 CAST(floor(lat/{CELL}) AS BIGINT) AS cy,
                 CAST(floor(lon/{CELL}) AS BIGINT) AS cx
          FROM stations
        ),
        cand AS (
          SELECT p.vehicle_id, p.ts, s.station_id,
                 2*6371000*asin(sqrt(
                   pow(sin(radians(s.lat - p.lat)/2),2) +
                   cos(radians(p.lat))*cos(radians(s.lat))*
                   pow(sin(radians(s.lon - p.lon)/2),2))) AS dist_m
          FROM p JOIN s ON abs(p.cy - s.cy) <= 1 AND abs(p.cx - s.cx) <= 1
        )
        SELECT vehicle_id, ts, station_id, dist_m
        FROM cand
        WHERE dist_m <= {config.SNAP_RADIUS_M}
        QUALIFY row_number() OVER (PARTITION BY vehicle_id, ts ORDER BY dist_m) = 1
        """
    )

    # 2. Group consecutive pings at the same station into one visit, then time
    #    the visit at its closest approach to the station centroid.
    con.execute(
        """
        CREATE TEMP TABLE visits AS
        WITH ordered AS (
          SELECT vehicle_id, ts, station_id, dist_m,
                 lag(station_id) OVER (PARTITION BY vehicle_id ORDER BY ts) AS prev_station
          FROM snapped
        ),
        grouped AS (
          SELECT vehicle_id, ts, station_id, dist_m,
                 sum(CASE WHEN prev_station IS NULL OR station_id <> prev_station THEN 1 ELSE 0 END)
                   OVER (PARTITION BY vehicle_id ORDER BY ts ROWS UNBOUNDED PRECEDING) AS grp
          FROM ordered
        )
        SELECT vehicle_id, station_id, grp,
               arg_min(ts, dist_m) AS t_pass,
               min(dist_m) AS min_dist_m,
               count(*) AS n_pings
        FROM grouped
        GROUP BY 1, 2, 3
        """
    )

    # 3. Consecutive visits become station-pair observations.
    con.execute(
        """
        CREATE TEMP TABLE runs AS
        SELECT vehicle_id,
               station_id AS from_station,
               lead(station_id) OVER w AS to_station,
               t_pass AS t_depart,
               lead(t_pass) OVER w AS t_arrive,
               min_dist_m
        FROM visits
        WINDOW w AS (PARTITION BY vehicle_id ORDER BY grp)
        """
    )

    con.execute(
        f"""
        CREATE OR REPLACE TABLE station_runs AS
        SELECT r.vehicle_id, r.from_station, r.to_station,
               r.t_depart, r.t_arrive,
               r.t_arrive - r.t_depart AS travel_s,
               r.min_dist_m,
               to_timestamp(r.t_depart) AT TIME ZONE 'Asia/Kolkata' AS depart_ist
        FROM runs r
        WHERE r.to_station IS NOT NULL
          AND r.from_station <> r.to_station
          AND r.t_arrive - r.t_depart BETWEEN {config.MIN_DT_S} AND {config.MAX_DT_S}
          AND EXISTS (
            SELECT 1 FROM station_pairs p
            WHERE p.from_station = r.from_station AND p.to_station = r.to_station
          )
        """
    )

    n_runs = con.execute("SELECT count(*) FROM station_runs").fetchone()[0]
    n_veh = con.execute("SELECT count(DISTINCT vehicle_id) FROM station_runs").fetchone()[0]
    n_pairs = con.execute(
        "SELECT count(DISTINCT (from_station, to_station)) FROM station_runs"
    ).fetchone()[0]

    out.parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"COPY station_runs TO '{out.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    con.close()
    return {"runs": n_runs, "vehicles": n_veh, "station_pairs": n_pairs}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    args = ap.parse_args()

    pings = config.PROCESSED / "gps" / f"date={args.date}" / "pings.parquet"
    out = config.PROCESSED / "gps" / f"date={args.date}" / "station_runs.parquet"
    if not pings.exists():
        print(f"missing {pings}; run dbi.traffic.ingest first", file=sys.stderr)
        return 1

    s = detect(pings, out)
    print(
        f"{args.date}  runs={s['runs']:,}  vehicles={s['vehicles']:,}  "
        f"station_pairs={s['station_pairs']:,}  -> {out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
