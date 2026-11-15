"""Audit: how well do GPS pings map onto GTFS stops?

Uses a lat/lon grid bucket so the join is O(n) instead of O(n*m).
"""
import duckdb

GTFS = "data/raw/gtfs/bus"
GPS_DB = "data/raw/gps/bus_movements_2026_10_15.db"
CELL = 0.005  # ~550 m at Delhi latitude

con = duckdb.connect()
con.execute("SET threads=8")
con.execute("SET memory_limit='6GB'")

con.execute(f"CREATE VIEW gps AS SELECT * FROM sqlite_scan('{GPS_DB}','vehicle_feed')")
con.execute(
    f"""CREATE VIEW stops AS SELECT CAST(stop_id AS BIGINT) sid, stop_name,
        CAST(stop_lat AS DOUBLE) lat, CAST(stop_lon AS DOUBLE) lon
        FROM read_csv_auto('{GTFS}/stops.txt', header=true)"""
)

# Grid-bucket both sides, then only compare within a 3x3 neighbourhood of cells.
q = f"""
WITH s AS (
  SELECT vehicle_id, timestamp, CAST(lat AS DOUBLE) lat, CAST(lng AS DOUBLE) lon
  FROM gps USING SAMPLE 300000 ROWS
),
sg AS (
  SELECT *, CAST(floor(lat/{CELL}) AS BIGINT) cy, CAST(floor(lon/{CELL}) AS BIGINT) cx FROM s
),
st AS (
  SELECT *, CAST(floor(lat/{CELL}) AS BIGINT) cy, CAST(floor(lon/{CELL}) AS BIGINT) cx FROM stops
),
pairs AS (
  SELECT sg.vehicle_id, sg.timestamp, st.sid, st.stop_name,
    2*6371000*asin(sqrt(
      pow(sin(radians(st.lat - sg.lat)/2),2) +
      cos(radians(sg.lat))*cos(radians(st.lat))*pow(sin(radians(st.lon - sg.lon)/2),2)
    )) AS dist
  FROM sg JOIN st
    ON abs(sg.cy - st.cy) <= 1 AND abs(sg.cx - st.cx) <= 1
),
nearest AS (
  SELECT * FROM pairs
  QUALIFY row_number() OVER (PARTITION BY vehicle_id, timestamp ORDER BY dist) = 1
)
SELECT count(*) AS pings,
  quantile_cont(dist, 0.5) AS p50,
  quantile_cont(dist, 0.75) AS p75,
  quantile_cont(dist, 0.90) AS p90,
  quantile_cont(dist, 0.95) AS p95,
  quantile_cont(dist, 0.99) AS p99,
  round(100.0*sum(CASE WHEN dist<=30 THEN 1 ELSE 0 END)/count(*),1) AS pct_le30m,
  round(100.0*sum(CASE WHEN dist<=50 THEN 1 ELSE 0 END)/count(*),1) AS pct_le50m,
  round(100.0*sum(CASE WHEN dist<=100 THEN 1 ELSE 0 END)/count(*),1) AS pct_le100m
FROM nearest
"""
print(con.execute(q).fetchdf().to_string())
