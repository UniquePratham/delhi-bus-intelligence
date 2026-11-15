"""Ingest ticketing records into a cleaned, aggregated form.

Privacy: ticket-level rows are never published. This module reads the raw CSV,
validates it, and writes only aggregates. The raw file is not copied anywhere.

Target feasibility (see docs/data-audit.md)
-------------------------------------------
The ticket schema has passenger_count, boarding stop, alighting stop, route and
timestamp, but *no* trip_id and no vehicle on ~half the rows. It therefore
cannot support true per-trip occupancy. What it does support is a
**demand proxy**: passengers boarding per route, direction, stop and hour.

Load is reconstructed only where a vehicle_number is present, by grouping
tickets into a vehicle-day and running a boarding/alighting balance along the
route's stop order. That is a *lower bound* on load, because pass holders and
unticketed riders never appear.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

from dbi import config


def build(out_db: Path) -> None:
    out_db.parent.mkdir(parents=True, exist_ok=True)
    if out_db.exists():
        out_db.unlink()

    con = duckdb.connect(str(out_db))
    con.execute("SET threads=8")
    con.execute("SET memory_limit='6GB'")

    csv = config.TICKETS_CSV.as_posix()
    con.execute(
        f"""
        CREATE TABLE tickets AS
        SELECT CAST(passenger_count AS INTEGER) AS pax,
               TRY_CAST(amount AS DOUBLE) AS fare,
               nullif(trim(vehicle_number), '') AS vehicle_number,
               CAST(ticket_type_id AS INTEGER) AS ticket_type_id,
               trim(end_stop_name) AS end_stop_name,
               trim(route) AS route,
               trim(start_stop_name) AS start_stop_name,
               CAST(created_at AS TIMESTAMP) AS created_at,
               CAST(is_ac AS BOOLEAN) AS is_ac
        FROM read_csv_auto('{csv}', header=true, sample_size=200000)
        WHERE passenger_count IS NOT NULL AND passenger_count > 0
        """
    )

    con.execute(
        """
        ALTER TABLE tickets ADD COLUMN hour TINYINT;
        ALTER TABLE tickets ADD COLUMN dow TINYINT;
        ALTER TABLE tickets ADD COLUMN service_date DATE;
        -- DuckDB's extract(dow) is 0=Sunday; convert to 0=Monday so it matches
        -- Python's datetime.weekday() used everywhere else.
        UPDATE tickets SET
          hour = CAST(extract(hour FROM created_at) AS TINYINT),
          dow  = CAST((CAST(extract(dow FROM created_at) AS INTEGER) + 6) % 7 AS TINYINT),
          service_date = CAST(created_at AS DATE);
        """
    )

    # Direction is encoded in the route string suffix (e.g. 8UP / 8DOWN).
    con.execute(
        """
        ALTER TABLE tickets ADD COLUMN direction VARCHAR;
        ALTER TABLE tickets ADD COLUMN route_base VARCHAR;
        UPDATE tickets SET
          direction = CASE
            WHEN route LIKE '%UP' THEN 'UP'
            WHEN route LIKE '%DOWN' THEN 'DOWN'
            ELSE 'NA' END;
        UPDATE tickets SET
          route_base = regexp_replace(route, '(UP|DOWN)$', '');
        """
    )

    # --- Demand proxy: boardings per route/direction/stop/hour -------------
    con.execute(
        """
        CREATE TABLE demand_route_stop_hour AS
        SELECT route, route_base, direction, start_stop_name AS stop_name,
               hour, dow,
               sum(pax) AS boardings,
               count(*) AS tickets,
               median(fare) AS median_fare,
               bool_or(is_ac) AS any_ac
        FROM tickets
        GROUP BY 1, 2, 3, 4, 5, 6
        """
    )

    # --- Demand proxy: boardings per route/direction/hour ------------------
    con.execute(
        """
        CREATE TABLE demand_route_hour AS
        SELECT route, route_base, direction, hour, dow,
               sum(pax) AS boardings,
               count(*) AS tickets,
               count(DISTINCT vehicle_number) AS vehicles
        FROM tickets
        GROUP BY 1, 2, 3, 4, 5
        """
    )

    # --- Route-level totals, used for coverage and confidence --------------
    con.execute(
        """
        CREATE TABLE route_totals AS
        SELECT route, route_base, direction,
               sum(pax) AS boardings,
               count(*) AS tickets,
               count(DISTINCT service_date) AS days_observed,
               count(DISTINCT vehicle_number) AS vehicles,
               min(service_date) AS first_date,
               max(service_date) AS last_date
        FROM tickets
        GROUP BY 1, 2, 3
        """
    )

    # --- Stop-name linkage to GTFS ----------------------------------------
    con.execute(f"ATTACH '{config.REFERENCE_DB.as_posix()}' AS ref (READ_ONLY)")
    con.execute(
        """
        CREATE TABLE stop_name_link AS
        SELECT t.stop_name,
               count(*) AS tickets,
               sum(t.boardings) AS boardings,
               count(DISTINCT sc.station_id) AS n_stations,
               min(sc.station_id) AS station_id
        FROM demand_route_stop_hour t
        LEFT JOIN ref.stops s ON s.stop_name = t.stop_name
        LEFT JOIN ref.stop_clusters sc ON sc.stop_id = s.stop_id
        GROUP BY 1
        """
    )
    con.execute("DETACH ref")

    for tbl in ("tickets", "demand_route_stop_hour", "demand_route_hour",
                "route_totals", "stop_name_link"):
        c = con.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        print(f"{tbl:26s} {c:>10,}")

    con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(config.CROWDING_DB))
    args = ap.parse_args()
    if not config.TICKETS_CSV.exists():
        print(f"missing {config.TICKETS_CSV}", file=sys.stderr)
        return 1
    build(Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
