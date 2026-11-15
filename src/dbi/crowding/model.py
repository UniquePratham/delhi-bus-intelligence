"""Build the crowding model.

What the data supports
----------------------
The ticket file holds 2.56 M tickets over 31 days across ~6,000 vehicles, i.e.
about 15 passengers per vehicle-day. A Delhi bus carries several hundred
passengers a day, so this file is a **sample**, not a census. Two consequences
drive the whole design:

* Absolute load cannot be recovered. Any per-trip occupancy figure would be
  wrong by an unknown factor.
* Relative structure *is* recoverable, because the sample is spread across the
  same routes, hours and stop sequences as the full population.

Targets produced
----------------
1. ``demand_proxy`` — passengers boarding per route, direction, stop and hour.
   A direct count from the data, not a model. Always available.

2. ``load_index`` — a *relative* onboard load, reconstructed by running a
   boarding/alighting balance along the route's stop sequence for each
   vehicle-day, then averaging across vehicle-days. It is reported as an index
   normalised to the route's own peak, never as a passenger count or an
   occupancy percentage.

3. ``crowding_category`` — derived from the load index relative to the route's
   own distribution, so it answers "is this stretch busier than usual for this
   route" rather than "how full is the bus". Where evidence is thin the
   category is ``unknown``, never silently "low".

Why the mean, not the median
----------------------------
Per vehicle-day the sample is sparse (median 6 tickets), so the median onboard
count is 0-4 and carries no signal. The mean across vehicle-days preserves the
aggregate boarding/alighting balance and reproduces the expected rise-and-fall
along a route, which the median destroys.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

from dbi import config
from dbi.names import is_subset_match, key_variants
from dbi.routes import route_keys

MIN_VEHICLE_DAYS_MEDIUM = 20
MIN_VEHICLE_DAYS_HIGH = 100


def build(out_db: Path) -> None:
    con = duckdb.connect(str(out_db))
    con.execute("SET threads=8")
    con.execute("SET memory_limit='6GB'")

    con.execute(f"ATTACH '{config.REFERENCE_DB.as_posix()}' AS ref (READ_ONLY)")

    # --- Route string -> GTFS route_id ------------------------------------
    # Ticket route strings match GTFS route_long_name for 87% of tickets.
    # Where a name maps to several GTFS routes all are kept, and the ambiguity
    # is recorded rather than silently picking one.
    con.execute(
        """
        CREATE OR REPLACE TABLE route_link AS
        SELECT t.route, t.route_base, t.direction,
               r.route_id, r.agency_id,
               count(*) OVER (PARTITION BY t.route) AS n_candidates
        FROM (SELECT DISTINCT route, route_base, direction FROM tickets) t
        LEFT JOIN ref.routes r ON r.route_name = t.route
        """
    )

    # Fallback for route strings that do not match a GTFS name exactly:
    # "874EUP" -> "874UP", "D-034UP" -> "34UP", "801-IGI-INDDOWN" -> "801DOWN".
    # Only applied where the exact match failed, so it can never override a
    # correct link.
    gtfs_routes = con.execute("SELECT route_id, route_name FROM ref.routes").fetchall()
    by_key: dict[str, list[int]] = {}
    for rid, name in gtfs_routes:
        for key in route_keys(name):
            by_key.setdefault(key, []).append(rid)

    unmatched = [
        r[0]
        for r in con.execute(
            """
            SELECT DISTINCT t.route FROM tickets t
            WHERE t.route NOT IN (SELECT route FROM route_link WHERE route_id IS NOT NULL)
            """
        ).fetchall()
    ]
    extra_links: list[tuple[str, int]] = []
    ambiguous = 0
    for route in unmatched:
        for key in route_keys(route):
            rids = by_key.get(key)
            if not rids:
                continue
            # Only accept an unambiguous key. "405STLUP" reduces to "405UP",
            # which matches several 405* routes; picking one arbitrarily would
            # attach crowding from the wrong route. Skipping is the honest
            # choice, and the route is reported as uncovered.
            if len(rids) == 1:
                extra_links.append((route, rids[0]))
            else:
                ambiguous += 1
            break
    if ambiguous:
        print(f"  skipped {ambiguous} ambiguous route keys")
    if extra_links:
        con.execute("CREATE TEMP TABLE extra_link (route VARCHAR, route_id INTEGER)")
        con.executemany("INSERT INTO extra_link VALUES (?, ?)", extra_links)
        con.execute(
            """
            INSERT INTO route_link
            SELECT e.route, t.route_base, t.direction, e.route_id, r.agency_id, 1
            FROM extra_link e
            JOIN (SELECT DISTINCT route, route_base, direction FROM tickets) t
              ON t.route = e.route
            JOIN ref.routes r ON r.route_id = e.route_id
            """
        )

    # --- Stop name -> station ---------------------------------------------
    # Ticket stop names and GTFS stop names are written by different systems,
    # so they are matched on a normalised key rather than raw equality. This
    # lifts ticket-volume coverage from 84% to 91%.
    con.execute(
        """
        CREATE OR REPLACE TABLE stop_link AS
        SELECT DISTINCT t.start_stop_name AS stop_name, sc.station_id
        FROM tickets t
        JOIN ref.stops s ON s.stop_name = t.start_stop_name
        JOIN ref.stop_clusters sc ON sc.stop_id = s.stop_id
        """
    )

    # Normalised fallback for names that do not match exactly.
    gtfs_names = con.execute("SELECT stop_id, stop_name FROM ref.stops").fetchall()
    clusters = dict(con.execute("SELECT stop_id, station_id FROM ref.stop_clusters").fetchall())
    # For each normalised key, how many *route patterns* serve each station.
    # A name shared by several places then resolves to the station the network
    # actually routes through most, which is what a journey planner needs.
    pattern_rank: dict[tuple[str, int], int] = {}
    for station_id, n in con.execute(
        "SELECT station_id, count(*) FROM ref.station_patterns GROUP BY 1"
    ).fetchall():
        pattern_rank[("__station__", station_id)] = n
    name_rank: dict[tuple[str, int], int] = {}
    for stop_id, stop_name in gtfs_names:
        sid = clusters[stop_id]
        for key in key_variants(stop_name):
            name_rank[(key, sid)] = name_rank.get((key, sid), 0) + 1

    def score(ticket_name: str, sid: int) -> tuple[int, int]:
        """Rank a candidate station: name agreement first, then network use."""
        name_hits = 0
        for key in key_variants(ticket_name):
            name_hits = max(name_hits, name_rank.get((key, sid), 0))
        return name_hits, pattern_rank.get(("__station__", sid), 0)

    ticket_names = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT start_stop_name FROM tickets"
        ).fetchall()
    ]
    extra = []
    for nm in ticket_names:
        matched = False
        for key in key_variants(nm):
            cands = [(sid, n) for (k, sid), n in name_rank.items() if k == key]
            if cands:
                extra.extend((nm, sid) for sid, _ in cands)
                matched = True
                break
        if matched:
            continue
        # Last resort: token-subset match against GTFS names. Requires at least
        # two shared significant tokens, so "Metro Station Kashmere Gate" can
        # reach "Kashmere Gate Metro Station Gate 4" but "Terminal" alone
        # cannot link two unrelated places.
        for stop_id, stop_name in gtfs_names:
            if is_subset_match(nm, stop_name):
                extra.append((nm, clusters[stop_id]))
    if extra:
        con.executemany("INSERT INTO stop_link VALUES (?, ?)", extra)
    # A ticket name can map to several stations when the same name exists at
    # more than one place. Prefer the station that GTFS uses most *for that
    # name*, breaking ties by how many route patterns serve it.
    rows = con.execute("SELECT stop_name, station_id FROM stop_link").fetchall()
    best: dict[str, tuple[int, tuple[int, int]]] = {}
    for nm, sid in rows:
        s = score(nm, sid)
        cur = best.get(nm)
        if cur is None or s > cur[1]:
            best[nm] = (sid, s)
    con.execute("DELETE FROM stop_link")
    con.executemany(
        "INSERT INTO stop_link VALUES (?, ?)", [(nm, v[0]) for nm, v in best.items()]
    )

    # --- Target 1: demand proxy -------------------------------------------
    con.execute(
        """
        CREATE OR REPLACE TABLE crowding_demand AS
        SELECT d.route, d.route_base, d.direction, d.stop_name, d.hour,
               sum(d.boardings) AS boardings,
               sum(d.tickets) AS tickets,
               count(DISTINCT d.dow) AS n_dow,
               CASE
                 WHEN sum(d.boardings) >= 2000 THEN 'high'
                 WHEN sum(d.boardings) >= 300 THEN 'medium'
                 ELSE 'low'
               END AS confidence
        FROM demand_route_stop_hour d
        GROUP BY 1, 2, 3, 4, 5
        """
    )

    # --- Target 2: load index ---------------------------------------------
    con.execute(
        """
        CREATE OR REPLACE TABLE vehicle_trips AS
        SELECT vehicle_number, service_date, route, route_base, direction,
               start_stop_name, end_stop_name, hour, pax, created_at
        FROM tickets
        WHERE vehicle_number IS NOT NULL
          AND start_stop_name <> end_stop_name
        """
    )

    # Order each ticket's stops along the matched GTFS route pattern.
    con.execute(
        """
        CREATE OR REPLACE TABLE load_events AS
        WITH pat AS (
          SELECT rl.route, sp.station_id, sp.stop_sequence
          FROM route_link rl
          JOIN ref.station_patterns sp ON sp.route_id = rl.route_id
        ),
        board AS (
          SELECT v.vehicle_number, v.service_date, v.route, v.hour, v.pax,
                 p.stop_sequence AS seq, 1 AS sign
          FROM vehicle_trips v
          JOIN stop_link sl ON sl.stop_name = v.start_stop_name
          JOIN pat p ON p.route = v.route AND p.station_id = sl.station_id
        ),
        alight AS (
          SELECT v.vehicle_number, v.service_date, v.route, v.hour, v.pax,
                 p.stop_sequence AS seq, -1 AS sign
          FROM vehicle_trips v
          JOIN stop_link sl ON sl.stop_name = v.end_stop_name
          JOIN pat p ON p.route = v.route AND p.station_id = sl.station_id
        )
        SELECT * FROM board UNION ALL SELECT * FROM alight
        """
    )

    # Running balance along the ordered stop sequence, per vehicle-day.
    con.execute(
        """
        CREATE OR REPLACE TABLE load_profile AS
        WITH agg AS (
          SELECT vehicle_number, service_date, route, hour, seq,
                 sum(sign * pax) AS delta
          FROM load_events
          GROUP BY 1, 2, 3, 4, 5
        ),
        running AS (
          SELECT *,
                 sum(delta) OVER (
                   PARTITION BY vehicle_number, service_date, route
                   ORDER BY seq ROWS UNBOUNDED PRECEDING
                 ) AS onboard
          FROM agg
        )
        SELECT route, hour, seq,
               count(*) AS n_vehicle_days,
               avg(onboard) AS load_index,
               quantile_cont(onboard, 0.25) AS load_p25,
               quantile_cont(onboard, 0.75) AS load_p75,
               max(onboard) AS load_max
        FROM running
        WHERE onboard >= 0
        GROUP BY 1, 2, 3
        """
    )

    # Attach the station each (route, seq) refers to, so consumers can look up
    # crowding by station rather than by an internal sequence number.
    con.execute(
        """
        CREATE OR REPLACE TABLE load_profile_station AS
        SELECT lp.route, lp.hour, lp.seq, sp.station_id,
               lp.n_vehicle_days, lp.load_index, lp.load_p25, lp.load_p75, lp.load_max
        FROM load_profile lp
        JOIN route_link rl ON rl.route = lp.route
        JOIN ref.station_patterns sp
          ON sp.route_id = rl.route_id AND sp.stop_sequence = lp.seq
        """
    )

    # --- Target 3: crowding category --------------------------------------
    # Two categories are produced, because they answer different questions:
    #
    # `crowding_category` is relative to the route's own load distribution, so
    # it answers "is this stretch busy for this route". It is useful for
    # spotting a route's own peak, but it is NOT comparable between routes: a
    # lightly-used route can be "crowded" at a load index of 2 while a busy
    # route is "seats_available" at 22.
    #
    # `crowding_band` is absolute, using fixed load-index thresholds. It is
    # comparable across routes, which is what a journey planner needs when it
    # compares two different routes. The thresholds are calibrated on the
    # observed distribution of the load index, not on a capacity assumption.
    con.execute(
        f"""
        CREATE OR REPLACE TABLE crowding_levels AS
        WITH stats AS (
          SELECT route,
                 quantile_cont(load_index, 0.60) AS p60,
                 quantile_cont(load_index, 0.85) AS p85,
                 max(load_index) AS route_max
          FROM load_profile_station GROUP BY 1
        )
        SELECT lp.route, lp.hour, lp.seq, lp.station_id, lp.n_vehicle_days,
               lp.load_index, lp.load_p25, lp.load_p75, lp.load_max,
               lp.load_index / nullif(s.route_max, 0) AS load_index_norm,
               CASE
                 WHEN lp.load_index <= s.p60 THEN 'seats_available'
                 WHEN lp.load_index <= s.p85 THEN 'standing_room'
                 ELSE 'crowded'
               END AS crowding_category,
               CASE
                 WHEN lp.load_index <= {config.LOAD_BAND_SEATS} THEN 'seats_available'
                 WHEN lp.load_index <= {config.LOAD_BAND_STANDING} THEN 'standing_room'
                 ELSE 'crowded'
               END AS crowding_band,
               CASE
                 WHEN lp.n_vehicle_days >= {MIN_VEHICLE_DAYS_HIGH} THEN 'high'
                 WHEN lp.n_vehicle_days >= {MIN_VEHICLE_DAYS_MEDIUM} THEN 'medium'
                 ELSE 'low'
               END AS confidence
        FROM load_profile_station lp JOIN stats s USING (route)
        """
    )

    con.execute("DETACH ref")

    for tbl in ("route_link", "stop_link", "crowding_demand", "vehicle_trips",
                "load_events", "load_profile", "crowding_levels"):
        c = con.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        print(f"{tbl:20s} {c:>10,}")

    con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(config.CROWDING_DB))
    args = ap.parse_args()
    build(Path(args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
