"""Ingest one day of raw GPS SQLite into a cleaned Parquet partition.

The raw feed is noisy: duplicate rows, out-of-bounds coordinates, and
non-monotonic timestamps all occur. Cleaning happens here, once, so every
downstream stage can assume a well-formed table.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

from dbi import config


def ingest(db_path: Path, out_path: Path) -> dict[str, int]:
    con = duckdb.connect()
    con.execute("SET threads=8")
    con.execute("SET memory_limit='6GB'")

    src = f"sqlite_scan('{db_path.as_posix()}', 'vehicle_feed')"

    stats = con.execute(
        f"""
        SELECT count(*) AS raw_rows,
               count(DISTINCT (vehicle_id, timestamp)) AS distinct_pings
        FROM {src}
        """
    ).fetchone()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"""
        COPY (
          WITH cleaned AS (
            SELECT vehicle_id,
                   CAST(lat AS DOUBLE) AS lat,
                   CAST(lng AS DOUBLE) AS lon,
                   CAST(speed AS DOUBLE) AS speed,
                   CAST(route_id AS BIGINT) AS feed_route_id,
                   trip_id AS feed_trip_id,
                   CAST(timestamp AS BIGINT) AS ts
            FROM {src}
            WHERE vehicle_id IS NOT NULL
              AND TRY_CAST(lat AS DOUBLE) BETWEEN {config.LAT_MIN} AND {config.LAT_MAX}
              AND TRY_CAST(lng AS DOUBLE) BETWEEN {config.LON_MIN} AND {config.LON_MAX}
              AND TRY_CAST(timestamp AS BIGINT) IS NOT NULL
          ),
          deduped AS (
            SELECT *,
                   row_number() OVER (
                     PARTITION BY vehicle_id, ts ORDER BY speed DESC
                   ) AS rn
            FROM cleaned
          )
          SELECT vehicle_id, lat, lon, speed, feed_route_id, feed_trip_id, ts,
                 to_timestamp(ts) AT TIME ZONE 'Asia/Kolkata' AS ts_ist,
                 CAST(floor(ts / 3600) AS BIGINT) AS hour_epoch
          FROM deduped
          WHERE rn = 1
          ORDER BY vehicle_id, ts
        ) TO '{out_path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )

    kept = con.execute(f"SELECT count(*) FROM read_parquet('{out_path.as_posix()}')").fetchone()[0]
    con.close()
    return {"raw_rows": stats[0], "distinct_pings": stats[1], "kept": kept}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY_MM_DD")
    ap.add_argument("--db", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else config.GPS_DIR / f"bus_movements_{args.date}.db"
    out_path = (
        Path(args.out)
        if args.out
        else config.PROCESSED / "gps" / f"date={args.date}" / "pings.parquet"
    )

    if not db_path.exists():
        print(f"missing {db_path}", file=sys.stderr)
        return 1

    s = ingest(db_path, out_path)
    print(
        f"{args.date}  raw={s['raw_rows']:,}  distinct={s['distinct_pings']:,}  "
        f"kept={s['kept']:,}  -> {out_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
