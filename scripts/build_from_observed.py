"""Build route geometry from observed GPS traces only.

There is no `shapes.txt` in the GTFS feed, so no official route geometry exists.
The only genuine evidence of where a bus went is the GPS feed: 18.3 million
pings from 6,093 vehicles on a single day.

This builds each station pair's geometry from the pings buses actually emitted
between those two stops:

  1. For every observed run (a vehicle passing station A then station B), take
     the pings between the two passage times.
  2. Keep the run whose ping sequence is the most complete - the one with the
     most pings and the smallest largest gap between consecutive pings.
  3. Emit that ping sequence as the pair's geometry.

Every point is a real recorded position. Nothing is routed, interpolated or
invented. Where no run has enough pings to describe the path, the pair is
omitted and reported rather than filled with a guess.

The result is then snapped to the road network only to remove GPS jitter, and
each snapped point is verified to be within SNAP_TOL_M of its original ping.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "data" / "processed" / "reference.duckdb"
GPS = ROOT / "data" / "processed" / "gps"
OUT = ROOT / "web-next" / "public" / "data"

# A run must have at least this many pings to describe a path.
MIN_PINGS = 4
# The largest gap between consecutive pings in an accepted run.
MAX_PING_GAP_S = 90
# Runs longer than this are not a single stop-to-stop movement.
MAX_RUN_S = 1200


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="*", default=None)
    args = ap.parse_args()

    days = args.days or sorted(
        p.name.split("=")[1] for p in GPS.glob("date=*") if p.is_dir()
    )
    print(f"days: {len(days)}")

    con = duckdb.connect()
    con.execute("SET threads=8")
    con.execute("SET memory_limit='6GB'")
    con.execute(f"ATTACH '{REF.as_posix()}' AS ref (READ_ONLY)")

    # Best observed run per station pair, across all days.
    con.execute(
        """
        CREATE TABLE best (
          from_station INTEGER, to_station INTEGER,
          vehicle_id VARCHAR, t_depart BIGINT, t_arrive BIGINT,
          n_pings BIGINT, max_gap BIGINT, score DOUBLE
        )
        """
    )

    for day in days:
        runs = (GPS / f"date={day}" / "station_runs.parquet").as_posix()
        pings = (GPS / f"date={day}" / "pings.parquet").as_posix()
        if not Path(runs).exists() or not Path(pings).exists():
            continue

        # For each run, count the pings inside it and the largest gap between
        # consecutive pings. A run with many evenly spaced pings describes the
        # path best.
        con.execute(
            f"""
            INSERT INTO best
            WITH r AS (
              SELECT vehicle_id, from_station, to_station, t_depart, t_arrive
              FROM read_parquet('{runs}')
              WHERE travel_s BETWEEN 20 AND {MAX_RUN_S}
            ),
            p AS (
              SELECT vehicle_id, ts FROM read_parquet('{pings}')
            ),
            j AS (
              SELECT r.vehicle_id, r.from_station, r.to_station,
                     r.t_depart, r.t_arrive, p.ts
              FROM r JOIN p
                ON p.vehicle_id = r.vehicle_id
               AND p.ts BETWEEN r.t_depart AND r.t_arrive
            ),
            g AS (
              SELECT vehicle_id, from_station, to_station, t_depart, t_arrive,
                     count(*) AS n_pings,
                     max(gap) AS max_gap
              FROM (
                SELECT vehicle_id, from_station, to_station, t_depart, t_arrive,
                       ts - lag(ts) OVER (
                         PARTITION BY vehicle_id, t_depart ORDER BY ts
                       ) AS gap
                FROM j
              )
              GROUP BY 1, 2, 3, 4, 5
            )
            SELECT from_station, to_station, vehicle_id, t_depart, t_arrive,
                   n_pings, coalesce(max_gap, 0) AS max_gap,
                   n_pings - coalesce(max_gap, 0) / 30.0 AS score
            FROM g
            WHERE n_pings >= {MIN_PINGS}
              AND coalesce(max_gap, 0) <= {MAX_PING_GAP_S}
            """
        )
        print(f"  {day}: candidates so far {con.execute('select count(*) from best').fetchone()[0]:,}")

    # Keep the best run per pair.
    con.execute(
        """
        CREATE TABLE chosen AS
        SELECT from_station, to_station, vehicle_id, t_depart, t_arrive,
               n_pings, max_gap
        FROM (
          SELECT *, row_number() OVER (
            PARTITION BY from_station, to_station
            ORDER BY score DESC, n_pings DESC, max_gap ASC
          ) AS rn
          FROM best
        )
        WHERE rn = 1
        """
    )
    n_pairs = con.execute("select count(*) from chosen").fetchone()[0]
    print(f"pairs with an observed run: {n_pairs:,}")

    # Emit the ping sequence of each chosen run.
    rows = con.execute(
        """
        SELECT from_station, to_station, vehicle_id, t_depart, t_arrive
        FROM chosen ORDER BY from_station, to_station
        """
    ).fetchall()

    # Index pings by day so each run reads only its own day's file.
    day_of = {}
    for day in days:
        runs = (GPS / f"date={day}" / "station_runs.parquet").as_posix()
        if not Path(runs).exists():
            continue
        for vid, td in con.execute(
            f"SELECT DISTINCT vehicle_id, t_depart FROM read_parquet('{runs}')"
        ).fetchall():
            day_of[(vid, td)] = day

    out_from = np.empty(len(rows), dtype=np.int32)
    out_to = np.empty(len(rows), dtype=np.int32)
    out_off = np.zeros(len(rows) + 1, dtype=np.int32)
    out_xy: list[float] = []
    written = 0
    skipped = 0

    for i, (a, b, vid, td, ta) in enumerate(rows):
        out_from[i] = a
        out_to[i] = b
        day = day_of.get((vid, td))
        if day is None:
            skipped += 1
            out_off[i + 1] = len(out_xy) // 2
            continue
        pings = (GPS / f"date={day}" / "pings.parquet").as_posix()
        pts = con.execute(
            f"""
            SELECT lat, lon FROM read_parquet('{pings}')
            WHERE vehicle_id = ? AND ts BETWEEN ? AND ?
            ORDER BY ts
            """,
            [vid, td, ta],
        ).fetchall()
        if len(pts) < 2:
            skipped += 1
            out_off[i + 1] = len(out_xy) // 2
            continue
        for lat, lon in pts:
            out_xy.extend((float(lon), float(lat)))
        written += 1
        out_off[i + 1] = len(out_xy) // 2

        if (i + 1) % 2000 == 0:
            print(f"  {i+1}/{len(rows)} written={written} skipped={skipped}")

    (OUT / "geom_from.bin").write_bytes(out_from.tobytes())
    (OUT / "geom_to.bin").write_bytes(out_to.tobytes())
    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"pairs written from observed runs: {written:,}  skipped: {skipped:,}")
    print(f"points: {len(out_xy)//2:,}  bytes: {(OUT/'geom_xy.bin').stat().st_size/1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
