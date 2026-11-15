"""Cluster GTFS stops into physical stations.

The Delhi GTFS feed assigns a distinct stop_id to each platform/agency at the
same physical place: 10,559 stop_ids collapse to ~3,963 distinct names, and
same-name stops sit a median 29 m apart. Snapping GPS to raw stop_ids
therefore produces spurious "runs" between co-located platforms.

This module builds a station layer by union-find over stops within
CLUSTER_RADIUS_M, then rewrites route patterns in station space.
"""
from __future__ import annotations

import sys

import duckdb

from dbi import config

CLUSTER_RADIUS_M = 60.0


def build() -> None:
    con = duckdb.connect(str(config.REFERENCE_DB))
    con.execute("SET threads=8")

    con.execute("DROP TABLE IF EXISTS stop_clusters")
    con.execute("DROP TABLE IF EXISTS stations")
    con.execute("DROP TABLE IF EXISTS station_patterns")
    con.execute("DROP TABLE IF EXISTS station_pairs")

    # Candidate edges: stops within the radius, found via a grid bucket.
    con.execute(
        f"""
        CREATE TEMP TABLE edges AS
        WITH s AS (
          SELECT stop_id, lat, lon,
                 CAST(floor(lat/0.001) AS BIGINT) AS cy,
                 CAST(floor(lon/0.001) AS BIGINT) AS cx
          FROM stops
        )
        SELECT a.stop_id AS a_id, b.stop_id AS b_id
        FROM s a JOIN s b
          ON abs(a.cy - b.cy) <= 1 AND abs(a.cx - b.cx) <= 1 AND a.stop_id < b.stop_id
        WHERE 2*6371000*asin(sqrt(
                pow(sin(radians(b.lat - a.lat)/2),2) +
                cos(radians(a.lat))*cos(radians(b.lat))*
                pow(sin(radians(b.lon - a.lon)/2),2))) <= {CLUSTER_RADIUS_M}
        """
    )

    # Union-find in SQL: iteratively propagate the minimum id across edges.
    con.execute("CREATE TEMP TABLE comp AS SELECT stop_id, stop_id AS root FROM stops")
    for _ in range(40):
        changed = con.execute(
            """
            WITH upd AS (
              SELECT c.stop_id, least(c.root, e.min_root) AS new_root
              FROM comp c
              JOIN (
                SELECT a_id, min(root) AS min_root FROM (
                  SELECT e.a_id, c2.root FROM edges e JOIN comp c2 ON c2.stop_id = e.b_id
                  UNION ALL
                  SELECT e.b_id, c2.root FROM edges e JOIN comp c2 ON c2.stop_id = e.a_id
                ) GROUP BY a_id
              ) e ON e.a_id = c.stop_id
            )
            SELECT count(*) FROM upd u JOIN comp c USING (stop_id) WHERE u.new_root < c.root
            """
        ).fetchone()[0]
        if changed == 0:
            break
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE comp AS
            WITH upd AS (
              SELECT c.stop_id, least(c.root, e.min_root) AS new_root
              FROM comp c
              JOIN (
                SELECT a_id, min(root) AS min_root FROM (
                  SELECT e.a_id, c2.root FROM edges e JOIN comp c2 ON c2.stop_id = e.b_id
                  UNION ALL
                  SELECT e.b_id, c2.root FROM edges e JOIN comp c2 ON c2.stop_id = e.a_id
                ) GROUP BY a_id
              ) e ON e.a_id = c.stop_id
            )
            SELECT c.stop_id, coalesce(u.new_root, c.root) AS root
            FROM comp c LEFT JOIN upd u USING (stop_id)
            """
        )

    con.execute(
        """
        CREATE TABLE stop_clusters AS
        SELECT c.stop_id, c.root AS station_id
        FROM comp c
        """
    )

    # Station geometry: centroid of its member stops, plus a representative name.
    con.execute(
        """
        CREATE TABLE stations AS
        SELECT sc.station_id,
               avg(s.lat) AS lat,
               avg(s.lon) AS lon,
               count(*) AS n_stops,
               arg_max(s.stop_name, s.stop_id) AS name
        FROM stop_clusters sc JOIN stops s USING (stop_id)
        GROUP BY 1
        """
    )

    # Route patterns rewritten in station space, collapsing repeats.
    con.execute(
        """
        CREATE TABLE station_patterns AS
        WITH mapped AS (
          SELECT rp.route_id, rp.stop_sequence, sc.station_id
          FROM route_patterns rp JOIN stop_clusters sc USING (stop_id)
        ),
        collapsed AS (
          SELECT route_id, stop_sequence, station_id,
                 lag(station_id) OVER (PARTITION BY route_id ORDER BY stop_sequence) AS prev
          FROM mapped
        )
        SELECT route_id, stop_sequence, station_id
        FROM collapsed
        WHERE prev IS NULL OR station_id <> prev
        ORDER BY route_id, stop_sequence
        """
    )

    con.execute(
        """
        CREATE TABLE station_pairs AS
        WITH seq AS (
          SELECT route_id, station_id,
                 lead(station_id) OVER (PARTITION BY route_id ORDER BY stop_sequence) AS next_station
          FROM station_patterns
        )
        SELECT DISTINCT route_id, station_id AS from_station, next_station AS to_station
        FROM seq
        WHERE next_station IS NOT NULL AND next_station <> station_id
        """
    )

    for tbl in ("stop_clusters", "stations", "station_patterns", "station_pairs"):
        n = con.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        print(f"{tbl:20s} {n:>10,}")

    con.close()


if __name__ == "__main__":
    sys.exit(build())
