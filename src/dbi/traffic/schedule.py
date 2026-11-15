"""Build the scheduled baseline in station space.

The GTFS timetable assumes a constant speed, so it is a weak predictor of real
travel time. It is still the right *baseline* to beat, and it is the only
source of truth for which station pairs are connected at all.
"""
from __future__ import annotations

import sys

import duckdb

from dbi import config


def build() -> None:
    con = duckdb.connect(str(config.REFERENCE_DB))
    con.execute("SET threads=8")

    con.execute("DROP TABLE IF EXISTS station_pair_schedule")

    # Scheduled seconds between consecutive stations, per route, taken from
    # the canonical pattern trip for that route.
    con.execute(
        """
        CREATE TABLE station_pair_schedule AS
        WITH pat AS (
          SELECT sp.route_id, sp.station_id, sp.stop_sequence
          FROM station_patterns sp
        ),
        nxt AS (
          SELECT route_id, station_id AS from_station, stop_sequence,
                 lead(station_id) OVER (PARTITION BY route_id ORDER BY stop_sequence) AS to_station
          FROM pat
        ),
        pairs AS (
          SELECT route_id, from_station, to_station
          FROM nxt WHERE to_station IS NOT NULL AND to_station <> from_station
        ),
        -- Scheduled run time per route/station pair, from the trip that
        -- realises this pattern.
        timed AS (
          SELECT p.route_id, p.from_station, p.to_station,
                 median(tsp.scheduled_s) AS scheduled_s,
                 count(*) AS n_trips
          FROM pairs p
          JOIN trips t ON t.route_id = p.route_id
          JOIN trip_stop_pairs tsp ON tsp.trip_id = t.trip_id
          JOIN stop_clusters c1 ON c1.stop_id = tsp.from_stop
          JOIN stop_clusters c2 ON c2.stop_id = tsp.to_stop
          WHERE c1.station_id = p.from_station AND c2.station_id = p.to_station
          GROUP BY 1, 2, 3
        )
        SELECT from_station, to_station,
               median(scheduled_s) AS scheduled_s,
               count(*) AS n_routes,
               sum(n_trips) AS n_trips
        FROM timed
        WHERE scheduled_s > 0
        GROUP BY 1, 2
        """
    )

    n = con.execute("SELECT count(*) FROM station_pair_schedule").fetchone()[0]
    print(f"station_pair_schedule {n:,}")
    con.close()


if __name__ == "__main__":
    sys.exit(build())
