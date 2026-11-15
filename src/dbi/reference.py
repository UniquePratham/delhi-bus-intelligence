"""Build the shared transport reference layer from the static GTFS feed.

The GPS feed and the GTFS feed use *different* route_id spaces (verified: 0%
semantic agreement), so route identity is never taken from the GPS route_id.
Instead the reference layer exposes the GTFS network, and the traffic pipeline
links GPS observations to it geometrically via stop passages.
"""
from __future__ import annotations

import sys

import duckdb

from dbi import config

GTFS = config.GTFS_DIR


def build() -> None:
    config.PROCESSED.mkdir(parents=True, exist_ok=True)
    if config.REFERENCE_DB.exists():
        config.REFERENCE_DB.unlink()

    con = duckdb.connect(str(config.REFERENCE_DB))
    con.execute("SET threads=8")

    con.execute(
        f"""
        CREATE TABLE stops AS
        SELECT CAST(stop_id AS INTEGER) AS stop_id,
               stop_name,
               CAST(stop_lat AS DOUBLE) AS lat,
               CAST(stop_lon AS DOUBLE) AS lon
        FROM read_csv_auto('{GTFS}/stops.txt', header=true)
        """
    )

    con.execute(
        f"""
        CREATE TABLE routes AS
        SELECT CAST(route_id AS INTEGER) AS route_id,
               route_long_name AS route_name,
               agency_id,
               CASE
                 WHEN route_long_name LIKE '%UP' THEN 'UP'
                 WHEN route_long_name LIKE '%DOWN' THEN 'DOWN'
                 ELSE 'NA'
               END AS direction
        FROM read_csv_auto('{GTFS}/routes.txt', header=true)
        """
    )

    con.execute(
        f"""
        CREATE TABLE trips AS
        SELECT trip_id, CAST(route_id AS INTEGER) AS route_id
        FROM read_csv_auto('{GTFS}/trips.txt', header=true)
        """
    )

    # GTFS times may exceed 24:00:00 for trips running past midnight.
    con.execute(
        f"""
        CREATE TABLE stop_times AS
        SELECT trip_id,
               CAST(stop_sequence AS INTEGER) AS stop_sequence,
               CAST(stop_id AS INTEGER) AS stop_id,
               arrival_time,
               departure_time,
               CAST(split_part(arrival_time, ':', 1) AS INTEGER) * 3600
             + CAST(split_part(arrival_time, ':', 2) AS INTEGER) * 60
             + CAST(split_part(arrival_time, ':', 3) AS INTEGER) AS arrival_s
        FROM read_csv_auto('{GTFS}/stop_times.txt', header=true)
        """
    )

    # Consecutive stop pairs per trip: the unit the travel-time model predicts.
    con.execute(
        """
        CREATE TABLE trip_stop_pairs AS
        SELECT a.trip_id,
               a.stop_id AS from_stop,
               b.stop_id AS to_stop,
               a.stop_sequence AS from_seq,
               b.arrival_s - a.arrival_s AS scheduled_s
        FROM stop_times a
        JOIN stop_times b
          ON a.trip_id = b.trip_id AND b.stop_sequence = a.stop_sequence + 1
        """
    )

    # Canonical ordered stop sequence per route, taken from the trip with the
    # most stops so that short-turn variants do not truncate the pattern.
    con.execute(
        """
        CREATE TABLE route_patterns AS
        WITH ranked AS (
          SELECT t.route_id, t.trip_id, count(*) AS n_stops
          FROM trips t JOIN stop_times st USING (trip_id)
          GROUP BY 1, 2
        ),
        best AS (
          SELECT route_id, trip_id
          FROM ranked
          QUALIFY row_number() OVER (PARTITION BY route_id ORDER BY n_stops DESC, trip_id) = 1
        )
        SELECT b.route_id, st.stop_sequence, st.stop_id, st.arrival_s
        FROM best b JOIN stop_times st USING (trip_id)
        ORDER BY b.route_id, st.stop_sequence
        """
    )

    # Which routes serve each ordered stop pair. Used to constrain the
    # geometric GPS->network link to pairs that actually exist in the network.
    con.execute(
        """
        CREATE TABLE route_stop_pairs AS
        SELECT DISTINCT t.route_id, p.from_stop, p.to_stop
        FROM trip_stop_pairs p JOIN trips t USING (trip_id)
        """
    )

    con.execute(
        """
        CREATE TABLE stop_pair_schedule AS
        SELECT from_stop, to_stop,
               count(*) AS n_obs,
               median(scheduled_s) AS scheduled_s
        FROM trip_stop_pairs
        WHERE scheduled_s > 0
        GROUP BY 1, 2
        """
    )

    for tbl in ("stops", "routes", "trips", "stop_times", "trip_stop_pairs",
                "route_patterns", "route_stop_pairs", "stop_pair_schedule"):
        n = con.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        print(f"{tbl:22s} {n:>10,}")

    con.close()


if __name__ == "__main__":
    sys.exit(build())
