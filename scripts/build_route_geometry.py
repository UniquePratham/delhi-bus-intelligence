"""Reconstruct road-following geometry for every station pair from GPS traces.

The GTFS feed ships no `shapes.txt`, so the map previously drew a straight
chord between consecutive stations — a bus flying over the city. The GPS feed
does contain the real path: every run is a sequence of pings along the road.

A single run is too sparse (median 5 pings) to trace a road, but thousands of
runs traverse the same station pair. For each pair we project every ping onto
the chord between its two stations, bin by progress along that chord, and take
the median lateral offset per bin. The median rejects GPS noise and the odd
detour while preserving the road's actual curve.

Output is a compact binary polyline per pair, consumed by the browser planner.
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

BINS = 16
MIN_RUN_S = 20
MAX_RUN_S = 1200
# A ping further than this from the chord is a detour or a GPS outlier.
MAX_LATERAL_M = 400.0


def day_medians(con: duckdb.DuckDBPyConnection, date: str) -> None:
    runs = (GPS / f"date={date}" / "station_runs.parquet").as_posix()
    pings = (GPS / f"date={date}" / "pings.parquet").as_posix()
    if not Path(runs).exists() or not Path(pings).exists():
        return

    # Project each ping onto its run's chord in local metres, then bin.
    con.execute(
        f"""
        INSERT INTO samples
        WITH r AS (
          SELECT vehicle_id, from_station, to_station, t_depart, t_arrive
          FROM read_parquet('{runs}')
          WHERE travel_s BETWEEN {MIN_RUN_S} AND {MAX_RUN_S}
        ),
        p AS (
          SELECT vehicle_id, ts, lat, lon FROM read_parquet('{pings}')
        ),
        j AS (
          SELECT r.from_station, r.to_station, p.lat, p.lon
          FROM r JOIN p
            ON p.vehicle_id = r.vehicle_id
           AND p.ts BETWEEN r.t_depart AND r.t_arrive
        ),
        g AS (
          SELECT j.from_station, j.to_station, j.lat, j.lon,
                 a.lat AS alat, a.lon AS alon, b.lat AS blat, b.lon AS blon
          FROM j
          JOIN stations a ON a.station_id = j.from_station
          JOIN stations b ON b.station_id = j.to_station
        ),
        m AS (
          SELECT from_station, to_station,
                 (lon - alon) * cos(radians(alat)) * 111320.0 AS px,
                 (lat - alat) * 110540.0 AS py,
                 (blon - alon) * cos(radians(alat)) * 111320.0 AS dx,
                 (blat - alat) * 110540.0 AS dy
          FROM g
        ),
        proj AS (
          SELECT from_station, to_station,
                 (px * dx + py * dy) / nullif(dx * dx + dy * dy, 0) AS t,
                 (px * dy - py * dx) / nullif(sqrt(dx * dx + dy * dy), 0) AS lat_off
          FROM m
        )
        SELECT from_station, to_station,
               least({BINS - 1}, greatest(0, CAST(floor(t * {BINS}) AS INTEGER))) AS bin,
               median(lat_off) AS lat_off,
               count(*) AS n
        FROM proj
        WHERE t BETWEEN -0.05 AND 1.05
          AND abs(lat_off) <= {MAX_LATERAL_M}
        GROUP BY 1, 2, 3
        HAVING count(*) >= 3
        """
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="*", default=None)
    args = ap.parse_args()

    days = args.days or sorted(
        p.name.split("=")[1] for p in GPS.glob("date=*") if p.is_dir()
    )
    if not days:
        print("no GPS days found")
        return 1

    con = duckdb.connect()
    con.execute("SET threads=8")
    con.execute("SET memory_limit='6GB'")
    con.execute(f"ATTACH '{REF.as_posix()}' AS ref (READ_ONLY)")
    con.execute("CREATE TABLE stations AS SELECT station_id, lat, lon FROM ref.stations")
    con.execute(
        "CREATE TABLE samples (from_station INTEGER, to_station INTEGER, bin INTEGER, "
        "lat_off DOUBLE, n BIGINT)"
    )

    for d in days:
        before = con.execute("SELECT count(*) FROM samples").fetchone()[0]
        day_medians(con, d)
        after = con.execute("SELECT count(*) FROM samples").fetchone()[0]
        print(f"{d}: +{after - before} bin medians")

    # Combine days: median of the per-day medians, weighted by sample count.
    rows = con.execute(
        """
        SELECT from_station, to_station, bin, median(lat_off) AS lat_off
        FROM samples
        GROUP BY 1, 2, 3
        ORDER BY 1, 2, 3
        """
    ).fetchall()
    print(f"combined bins: {len(rows)}")

    stations = {
        sid: (lat, lon)
        for sid, lat, lon in con.execute(
            "SELECT station_id, lat, lon FROM stations"
        ).fetchall()
    }

    # Assemble one polyline per pair.
    by_pair: dict[tuple[int, int], dict[int, float]] = {}
    for f, t, b, off in rows:
        by_pair.setdefault((f, t), {})[b] = off

    pairs = sorted(by_pair)
    froms = np.empty(len(pairs), dtype=np.int32)
    tos = np.empty(len(pairs), dtype=np.int32)
    offsets = np.zeros(len(pairs) + 1, dtype=np.int32)
    coords: list[float] = []

    for i, (f, t) in enumerate(pairs):
        froms[i] = f
        tos[i] = t
        alat, alon = stations[f]
        blat, blon = stations[t]
        bins = by_pair[(f, t)]

        # Local metre frame, matching the projection used above.
        kx = math.cos(math.radians(alat)) * 111320.0
        ky = 110540.0
        dx = (blon - alon) * kx
        dy = (blat - alat) * ky
        length = math.hypot(dx, dy)

        # A degenerate chord has no meaningful frame; emit nothing for it but
        # still advance the offset cursor, or every later pair reads the wrong
        # slice.
        if length < 1e-6:
            offsets[i + 1] = len(coords) // 2
            continue

        nx, ny = -dy / length, dx / length

        pts: list[tuple[float, float]] = [(alon, alat)]
        for b in range(BINS):
            if b not in bins:
                continue
            frac = (b + 0.5) / BINS
            off = bins[b]
            # Offset is applied in metres, then converted back to degrees.
            mx = dx * frac + nx * off
            my = dy * frac + ny * off
            pts.append((alon + mx / kx, alat + my / ky))
        pts.append((blon, blat))

        # Reject a reconstruction that wanders far more than a road would.
        # A bus detours, but a path three times the chord is a bad trace.
        path_m = sum(
            math.hypot(
                (pts[j + 1][0] - pts[j][0]) * kx,
                (pts[j + 1][1] - pts[j][1]) * ky,
            )
            for j in range(len(pts) - 1)
        )
        if path_m > 3.0 * length:
            offsets[i + 1] = len(coords) // 2
            continue

        for lon, lat in pts:
            coords.extend((lon, lat))
        offsets[i + 1] = len(coords) // 2

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "geom_from.bin").write_bytes(froms.tobytes())
    (OUT / "geom_to.bin").write_bytes(tos.tobytes())
    (OUT / "geom_off.bin").write_bytes(offsets.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(coords, dtype=np.float32).tobytes())

    total_pts = len(coords) // 2
    print(f"pairs: {len(pairs)}  points: {total_pts}")
    print(f"bytes: {(OUT / 'geom_xy.bin').stat().st_size / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
