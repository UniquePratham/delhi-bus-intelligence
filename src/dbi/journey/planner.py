"""Time-dependent journey planning over the station graph.

Approach
--------
A round-based label-setting search (RAPTOR-style) over the station graph:

* Round 0 is the access walk from the origin to nearby stations.
* Each round adds one bus leg, priced with the travel-time model at the time
  the passenger would actually reach that station.
* Transfers are bounded by ``max_transfers``.
* The best label per station is kept, and the search is repeated with the
  best journey's stations excluded to surface genuinely different
  alternatives rather than cosmetic variants of the same path.

Travel times come from the traffic model, so the same query at 09:00 and at
23:00 returns different answers. Where a pair has no observation the model
falls back through its hierarchy and the leg is marked with lower confidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import duckdb

from dbi import config
from dbi.aliases import ALIASES
from dbi.geo import haversine_m

HOUR_BANDS = [
    (0, 5, "night"),
    (6, 7, "early_peak"),
    (8, 11, "morning_peak"),
    (12, 16, "midday"),
    (17, 20, "evening_peak"),
    (21, 23, "late"),
]

# Confidence reported per leg, from the model level that produced the estimate.
LEVEL_CONFIDENCE = {
    "pair_hour": "high",
    "pair_band": "medium",
    "pair": "medium",
    "schedule_scaled": "low",
    "global_band": "low",
    "global": "low",
}


def hour_band(hour: int) -> str:
    for lo, hi, name in HOUR_BANDS:
        if lo <= hour <= hi:
            return name
    return "late"


@dataclass
class Leg:
    kind: str  # "walk" | "bus"
    from_station: int | None
    to_station: int | None
    from_name: str
    to_name: str
    depart: datetime
    arrive: datetime
    route_id: int | None = None
    route_name: str | None = None
    distance_m: float | None = None
    level: str = "unknown"
    crowding: dict | None = None

    @property
    def duration_s(self) -> float:
        return (self.arrive - self.depart).total_seconds()

    @property
    def confidence(self) -> str:
        if self.kind == "walk":
            return "measured"
        return LEVEL_CONFIDENCE.get(self.level, "low")


@dataclass
class Journey:
    legs: list[Leg] = field(default_factory=list)

    @property
    def depart(self) -> datetime:
        return self.legs[0].depart

    @property
    def arrive(self) -> datetime:
        return self.legs[-1].arrive

    @property
    def duration_s(self) -> float:
        return (self.arrive - self.depart).total_seconds()

    @property
    def transfers(self) -> int:
        return max(0, sum(1 for leg in self.legs if leg.kind == "bus") - 1)

    @property
    def walk_s(self) -> float:
        return sum(leg.duration_s for leg in self.legs if leg.kind == "walk")

    @property
    def bus_s(self) -> float:
        return sum(leg.duration_s for leg in self.legs if leg.kind == "bus")

    @property
    def bus_legs(self) -> list[Leg]:
        return [leg for leg in self.legs if leg.kind == "bus"]


class Planner:
    """Loads the network and model into memory once, then answers queries."""

    def __init__(self, traffic_db=None, reference_db=None, crowding_db=None):
        self.traffic_db = traffic_db or config.TRAFFIC_DB
        self.reference_db = reference_db or config.REFERENCE_DB
        self.crowding_db = crowding_db or config.CROWDING_DB

        con = duckdb.connect(str(self.reference_db), read_only=True)
        con.execute("SET threads=8")
        self.stations: dict[int, tuple[float, float, str]] = {
            r[0]: (r[1], r[2], r[3])
            for r in con.execute("SELECT station_id, lat, lon, name FROM stations").fetchall()
        }
        self.edges: dict[int, list[tuple[int, int, str]]] = {}
        for frm, to, rid, rname in con.execute(
            """
            SELECT sp.from_station, sp.to_station, sp.route_id, r.route_name
            FROM station_pairs sp JOIN routes r USING (route_id)
            """
        ).fetchall():
            self.edges.setdefault(frm, []).append((to, rid, rname))
        # Ordered station list per route, used to ride a route in one round.
        self.route_patterns: dict[int, list[int]] = {}
        for rid, sid in con.execute(
            "SELECT route_id, station_id FROM station_patterns ORDER BY route_id, stop_sequence"
        ).fetchall():
            self.route_patterns.setdefault(rid, []).append(sid)
        # (route, station) -> position in the pattern, so the search does not
        # scan the pattern list on every boarding.
        self.route_index: dict[tuple[int, int], int] = {
            (rid, sid): i
            for rid, pattern in self.route_patterns.items()
            for i, sid in enumerate(pattern)
        }
        self.route_names: dict[int, str] = {
            r[0]: r[1] for r in con.execute("SELECT route_id, route_name FROM routes").fetchall()
        }
        con.close()

        tcon = duckdb.connect(str(self.traffic_db), read_only=True)
        tcon.execute("SET threads=8")
        self.pair_hour = {
            (r[0], r[1], r[2]): r[3]
            for r in tcon.execute(
                "SELECT from_station, to_station, hour_of_week, travel_s FROM profile_pair_hour"
            ).fetchall()
        }
        self.pair_band = {
            (r[0], r[1], r[2]): r[3]
            for r in tcon.execute(
                "SELECT from_station, to_station, hour_band, travel_s FROM profile_pair_band"
            ).fetchall()
        }
        self.pair = {
            (r[0], r[1]): r[2]
            for r in tcon.execute(
                "SELECT from_station, to_station, travel_s FROM profile_pair"
            ).fetchall()
        }
        self.global_band = {
            r[0]: r[1]
            for r in tcon.execute(
                "SELECT hour_band, travel_s FROM profile_global_band"
            ).fetchall()
        }
        self.global_median = tcon.execute("SELECT travel_s FROM profile_global").fetchone()[0]
        self.schedule = {
            (r[0], r[1]): r[2]
            for r in tcon.execute(
                "SELECT from_station, to_station, scheduled_s FROM schedule"
            ).fetchall()
        }
        # Observed/scheduled ratio per hour band, used to scale the timetable
        # for pairs the model has never seen. Computed from training runs only,
        # so the held-out day is never used to fit anything.
        self.band_factor = {
            r[0]: r[1]
            for r in tcon.execute(
                """
                SELECT r.hour_band,
                       median(r.travel_s / nullif(s.scheduled_s, 0)) AS factor
                FROM train_runs r JOIN schedule s USING (from_station, to_station)
                WHERE s.scheduled_s > 0
                GROUP BY 1
                """
            ).fetchall()
        }
        tcon.close()

        self._crowding: dict | None = None

    # -- crowding is optional: the planner works without it -----------------
    def crowding(self) -> dict:
        """Load crowding into compact arrays, keyed by (route, hour, station).

        A dict of dicts for 460k cells costs hundreds of MB and slows every
        subsequent search through GC pressure. Three parallel arrays plus an
        index dict keep the same lookups at a fraction of the footprint.
        """
        if self._crowding is None:
            try:
                ccon = duckdb.connect(str(self.crowding_db), read_only=True)
                ccon.execute("SET threads=8")
                rows = ccon.execute(
                    """
                    SELECT route, hour, station_id,
                           crowding_band, confidence, load_index
                    FROM crowding_levels
                    """
                ).fetchall()
                ccon.close()
                index: dict[tuple[str, int, int], int] = {}
                cats: list[str] = []
                confs: list[str] = []
                loads: list[float] = []
                for route, hour, station, cat, conf, load in rows:
                    index[(route, hour, station)] = len(cats)
                    cats.append(cat)
                    confs.append(conf)
                    loads.append(load)
                self._crowding = index
                self._crowding_cat = cats
                self._crowding_conf = confs
                self._crowding_load = loads

                # Fallback 1: the route's all-hours profile for a station.
                ccon = duckdb.connect(str(self.crowding_db), read_only=True)
                self._crowding_any_hour = {
                    (r[0], r[1]): (r[2], round(r[3], 2))
                    for r in ccon.execute(
                        """
                        SELECT route, station_id,
                               arg_max(crowding_band, n_vehicle_days) AS category,
                               avg(load_index) AS load_index
                        FROM crowding_levels
                        GROUP BY 1, 2
                        """
                    ).fetchall()
                }
                # Fallback 2: the route at this hour, averaged over stretches.
                self._crowding_route_hour = {
                    (r[0], r[1]): (r[2], round(r[3], 2))
                    for r in ccon.execute(
                        """
                        SELECT route, hour,
                               arg_max(crowding_band, n_vehicle_days) AS category,
                               avg(load_index) AS load_index
                        FROM crowding_levels
                        GROUP BY 1, 2
                        """
                    ).fetchall()
                }
                # Fallback 3: the route overall.
                self._crowding_route = {
                    r[0]: (r[1], round(r[2], 2))
                    for r in ccon.execute(
                        """
                        SELECT route,
                               arg_max(crowding_band, n_vehicle_days) AS category,
                               avg(load_index) AS load_index
                        FROM crowding_levels
                        GROUP BY 1
                        """
                    ).fetchall()
                }
                ccon.close()
            except Exception:
                self._crowding = {}
                self._crowding_cat = []
                self._crowding_conf = []
                self._crowding_load = []
                self._crowding_any_hour = {}
                self._crowding_route_hour = {}
                self._crowding_route = {}
        return self._crowding

    # -- travel time -------------------------------------------------------
    def travel_time(self, frm: int, to: int, when: datetime) -> tuple[float, str]:
        """Predicted seconds for one station pair, plus the level used.

        The hierarchy falls back from the most specific evidence to the most
        general. The scheduled time is used *before* the global median because
        it is pair-specific: it knows this stretch is long or short, even
        though it assumes a constant speed. The global median knows nothing
        about the pair at all.
        """
        return self.travel_time_key(frm, to, when.weekday() * 24 + when.hour, when.hour)

    def travel_time_key(
        self, frm: int, to: int, hour_of_week: int, hour: int
    ) -> tuple[float, str]:
        """Same as ``travel_time`` but takes a precomputed time key.

        The search calls this hundreds of thousands of times per query, and
        recomputing ``weekday()`` and the hour band on every call dominated the
        profile. Callers inside the search hoist those out of the loop.
        """
        band = hour_band(hour)
        v = self.pair_hour.get((frm, to, hour_of_week))
        if v is not None:
            return v, "pair_hour"
        v = self.pair_band.get((frm, to, band))
        if v is not None:
            return v, "pair_band"
        v = self.pair.get((frm, to))
        if v is not None:
            return v, "pair"
        # Pair-specific but time-blind: scale the timetable by the observed
        # ratio of real to scheduled time for this hour band.
        sched = self.schedule.get((frm, to))
        if sched is not None:
            factor = self.band_factor.get(band, 1.0)
            return sched * factor, "schedule_scaled"
        v = self.global_band.get(band)
        if v is not None:
            return v, "global_band"
        return self.global_median, "global"

    def scheduled_time(self, frm: int, to: int) -> float | None:
        return self.schedule.get((frm, to))

    # -- places ------------------------------------------------------------
    def nearby(self, lat: float, lon: float, radius_m: float = config.MAX_WALK_M):
        out = []
        for sid, (slat, slon, name) in self.stations.items():
            d = haversine_m(lat, lon, slat, slon)
            if d <= radius_m:
                out.append((d, sid, name))
        out.sort()
        return out

    def resolve_place(self, lat=None, lon=None, name=None):
        """Accept coordinates or a stop name and return (lat, lon, label).

        Name matching is layered:

        1. An exact colloquial alias ("CP", "Connaught Place") wins outright,
           so "CP" cannot fuzzy-match "CPRTI".
        2. Token-based ranked match: exact name, then all tokens present, then
           any token present; shortest name first within each tier. This makes
           "Kashmere Gate ISBT" resolve to "ISBT Kashmere Gate" while keeping
           "Nehru Place" away from "Nehru Nagar".
        """
        if lat is not None and lon is not None:
            return lat, lon, f"{lat:.5f},{lon:.5f}"
        if name:
            q = name.strip().lower()
            tokens = [t for t in q.replace(",", " ").split() if t]
            if not tokens:
                raise ValueError("empty place name")
            con = duckdb.connect(str(self.reference_db), read_only=True)

            def lookup(target: str):
                return con.execute(
                    """
                    SELECT lat, lon, name FROM stations
                    WHERE lower(name) LIKE '%' || ? || '%'
                    ORDER BY length(name) LIMIT 1
                    """,
                    [target.lower()],
                ).fetchone()

            row = lookup(ALIASES[q]) if q in ALIASES else None

            if row is None:
                row = con.execute(
                    """
                    WITH cand AS (
                      SELECT lat, lon, name, lower(name) AS lname FROM stations
                      WHERE lower(name) LIKE '%' || ? || '%'
                    ),
                    scored AS (
                      SELECT *,
                        (SELECT count(*) FROM unnest(?::VARCHAR[]) AS t(tok)
                         WHERE lname LIKE '%' || tok || '%') AS hits
                      FROM cand
                    )
                    SELECT lat, lon, name FROM scored
                    ORDER BY
                      CASE WHEN lname = ? THEN 0
                           WHEN hits = ? THEN 1
                           ELSE 2 END,
                      length(name)
                    LIMIT 1
                    """,
                    [tokens[0], tokens, q, len(tokens)],
                ).fetchone()

            con.close()
            if row:
                return row[0], row[1], row[2]
            raise ValueError(f"no station matching {name!r}")
        raise ValueError("provide either coordinates or a name")

    # -- search ------------------------------------------------------------
    def _search(
        self,
        access: list[tuple[float, int, str]],
        egress: dict[int, tuple[float, str]],
        depart: datetime,
        max_transfers: int,
        banned_stations: set[int] | None = None,
        banned_routes: set[int] | None = None,
    ) -> Journey | None:
        """One label-setting pass. Returns the fastest journey found.

        A round is one *route boarding*, not one station hop: within a round a
        passenger may ride a route across any number of stations for the price
        of a single boarding. Counting each hop as a transfer would make a
        15 km trip look like 40 transfers.

        A label records the *boarding station and route*, not a copied leg list
        and not the previous station. Copying the list at every relaxation made
        a query take seconds; pointing at the previous station is wrong, because
        mid-pattern stations are never added to the label set. Reconstructing
        from (boarding station, route) is both correct and O(path length), and
        it merges each ride into one leg for free.

        Labels are kept **per round**. A single global label set would let a
        station improved in round *k* be used to board a route in the same
        round, which silently grants free transfers and breaks the
        ``max_transfers`` contract.
        """
        # labels[k][station] = (arrival, boarding_station, route_id, alight)
        # boarding_station is None for the access walk.
        banned = banned_stations or set()
        banned_routes = banned_routes or set()
        Label = tuple[datetime, int | None, int | None, int | None]
        labels: list[dict[int, Label]] = [{}]
        for dist, sid, _name in access:
            if sid in banned:
                continue
            arrive = depart + timedelta(seconds=dist / config.WALK_SPEED_MS)
            cur = labels[0].get(sid)
            if cur is None or arrive < cur[0]:
                labels[0][sid] = (arrive, None, None, None)

        for _round in range(max_transfers + 1):
            # Everything reachable with at most `_round` boardings.
            reachable: dict[int, Label] = {}
            for k in range(_round + 1):
                for sid, label in labels[k].items():
                    cur = reachable.get(sid)
                    if cur is None or label[0] < cur[0]:
                        reachable[sid] = label

            boardable: dict[int, tuple[int, datetime]] = {}
            for sid, (arrive, _b, _r, _a) in reachable.items():
                for nxt, rid, _rname in self.edges.get(sid, ()):
                    if nxt in banned:
                        continue
                    cur = boardable.get(rid)
                    if cur is None or arrive < cur[1]:
                        boardable[rid] = (sid, arrive)

            improved: dict[int, Label] = {}
            for rid, (start_sid, start_arrive) in boardable.items():
                if rid in banned_routes:
                    continue
                pattern = self.route_patterns.get(rid)
                if not pattern:
                    continue
                idx = self.route_index.get((rid, start_sid))
                if idx is None:
                    continue
                arrive = start_arrive
                prev = start_sid
                for nxt in pattern[idx + 1:]:
                    if nxt in banned:
                        break
                    # Hoist the time key: recomputing weekday() and the hour
                    # band per relaxation dominated the profile.
                    tt, _level = self.travel_time_key(
                        prev, nxt, arrive.weekday() * 24 + arrive.hour, arrive.hour
                    )
                    arrive = arrive + timedelta(seconds=tt)
                    cur = improved.get(nxt)
                    if cur is None or arrive < cur[0]:
                        improved[nxt] = (arrive, start_sid, rid, nxt)
                    prev = nxt

            labels.append(improved)

        # Best egress station across all rounds, then reconstruct once.
        best_sid = None
        best_round = 0
        best_total = None
        for k, layer in enumerate(labels):
            for sid, (arrive, _b, _r, _a) in layer.items():
                if sid not in egress:
                    continue
                dist, _name = egress[sid]
                total = arrive + timedelta(seconds=dist / config.WALK_SPEED_MS)
                if best_total is None or total < best_total:
                    best_total, best_sid, best_round = total, sid, k
        if best_sid is None:
            return None

        # Walk back through (boarding station, route) labels, round by round.
        rides: list[tuple[int, int, int]] = []  # (route_id, board, alight)
        sid: int | None = best_sid
        k = best_round
        while sid is not None and k >= 0:
            label = labels[k].get(sid)
            if label is None:
                k -= 1
                continue
            _arrive, board, rid, alight = label
            if board is None or rid is None:
                break
            rides.append((rid, board, alight if alight is not None else sid))
            sid = board
            k -= 1
        rides.reverse()
        if not rides:
            return None

        # Rebuild legs with the model's own timings, so the reported times
        # match what the search actually priced.
        legs: list[Leg] = []
        access_dist, access_sid, access_name = access[0]
        for dist, s, name in access:
            if s == rides[0][1]:
                access_dist, access_sid, access_name = dist, s, name
                break
        t = depart
        walk_arrive = t + timedelta(seconds=access_dist / config.WALK_SPEED_MS)
        legs.append(
            Leg(
                kind="walk", from_station=None, to_station=access_sid,
                from_name="Origin", to_name=access_name,
                depart=t, arrive=walk_arrive, distance_m=access_dist,
            )
        )
        t = walk_arrive
        for rid, board, alight in rides:
            pattern = self.route_patterns[rid]
            i0 = self.route_index[(rid, board)]
            i1 = self.route_index[(rid, alight)]
            if i1 <= i0:
                continue
            level = "pair_hour"
            for a, b in zip(pattern[i0:i1], pattern[i0 + 1 : i1 + 1], strict=False):
                tt, lv = self.travel_time(a, b, t)
                t = t + timedelta(seconds=tt)
                if LEVEL_CONFIDENCE.get(lv, "low") == "low":
                    level = lv
            legs.append(
                Leg(
                    kind="bus", from_station=board, to_station=alight,
                    from_name=self.stations[board][2], to_name=self.stations[alight][2],
                    depart=walk_arrive if not legs[1:] else legs[-1].arrive,
                    arrive=t, route_id=rid, route_name=self.route_names.get(rid, ""),
                    level=level,
                )
            )
        # Fix depart times so legs are contiguous.
        for prev, cur in zip(legs, legs[1:], strict=False):
            cur.depart = prev.arrive

        dist, _name = egress[best_sid]
        legs.append(
            Leg(
                kind="walk", from_station=best_sid, to_station=None,
                from_name=self.stations[best_sid][2], to_name="Destination",
                depart=t, arrive=t + timedelta(seconds=dist / config.WALK_SPEED_MS),
                distance_m=dist,
            )
        )
        return Journey(legs=legs)

    @staticmethod
    def _merge_bus_legs(legs: list[Leg]) -> list[Leg]:
        """Collapse consecutive bus legs on the same route into one leg.

        The search relaxes station pair by station pair, which is what the
        travel-time model prices. A passenger experiences that as a single
        ride, so the legs are merged before being returned; otherwise a 15 km
        trip reports 40 "transfers".
        """
        out: list[Leg] = []
        for leg in legs:
            prev = out[-1] if out else None
            if (
                prev is not None
                and leg.kind == "bus"
                and prev.kind == "bus"
                and prev.route_id == leg.route_id
                and prev.to_station == leg.from_station
            ):
                prev.to_station = leg.to_station
                prev.to_name = leg.to_name
                prev.arrive = leg.arrive
                # Keep the weakest confidence across the merged ride.
                if LEVEL_CONFIDENCE.get(leg.level, "low") == "low":
                    prev.level = leg.level
                continue
            out.append(leg)
        return out

    def plan(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        depart: datetime,
        max_transfers: int = 2,
        alternatives: int = 3,
        max_access_m: float = config.MAX_WALK_M,
    ) -> list[Journey]:
        access = self.nearby(origin[0], origin[1], max_access_m)
        egress = {sid: (d, name) for d, sid, name in self.nearby(destination[0], destination[1], max_access_m)}
        if not access or not egress:
            return []

        journeys: list[Journey] = []
        banned_routes: set[int] = set()
        banned_stations: set[int] = set()
        seen: set[tuple] = set()
        # Each pass is a full search, so cap the work. `alternatives + 2` gives
        # room for duplicates without letting a hard query run unbounded; an
        # earlier `alternatives * 2` ran ten searches for a three-option
        # request and dominated latency.
        for _ in range(alternatives + 2):
            if len(journeys) >= alternatives:
                break
            j = self._search(
                access, egress, depart, max_transfers, banned_stations, banned_routes
            )
            if j is None:
                break
            j.legs = self._merge_bus_legs(j.legs)
            key = tuple(
                (leg.route_id, leg.from_station, leg.to_station) for leg in j.bus_legs
            )
            if key in seen:
                # Force progress by banning this journey's first route.
                if j.bus_legs and j.bus_legs[0].route_id is not None:
                    banned_routes.add(j.bus_legs[0].route_id)
                continue
            seen.add(key)
            journeys.append(j)
            # Ban the first ride's route so the next pass must start
            # differently. Banning every station on the path (an earlier
            # approach) removed so much of the graph that long trips returned
            # a single option.
            if j.bus_legs and j.bus_legs[0].route_id is not None:
                banned_routes.add(j.bus_legs[0].route_id)
        return journeys

    # -- enrichment --------------------------------------------------------
    def enrich_crowding(self, journey: Journey) -> None:
        """Attach a crowding estimate to each bus leg, where one exists.

        Lookup order: the exact route/hour/stretch, then the route's all-hours
        profile for that stretch. Anything still missing is reported as
        ``unknown`` with a reason — never silently treated as comfortable.
        """
        table = self.crowding()
        if not table:
            return
        for leg in journey.bus_legs:
            if leg.route_id is None or leg.from_station is None:
                continue
            idx = table.get((leg.route_name, leg.depart.hour, leg.from_station))
            if idx is not None:
                leg.crowding = {
                    "category": self._crowding_cat[idx],
                    "confidence": self._crowding_conf[idx],
                    "target_type": "load_index",
                    "load_index": round(self._crowding_load[idx], 2),
                    "time_scope": "hour",
                }
                continue
            fallback = self._crowding_any_hour.get((leg.route_name, leg.from_station))
            if fallback is not None:
                leg.crowding = {
                    "category": fallback[0],
                    "confidence": "low",
                    "target_type": "load_index",
                    "load_index": fallback[1],
                    "time_scope": "all_hours",
                    "reason": "no evidence for this hour; using the route's all-hours profile",
                }
                continue
            rh = self._crowding_route_hour.get((leg.route_name, leg.depart.hour))
            if rh is not None:
                leg.crowding = {
                    "category": rh[0],
                    "confidence": "low",
                    "target_type": "load_index",
                    "load_index": rh[1],
                    "time_scope": "route_hour",
                    "reason": "no evidence for this stretch; using the route average for this hour",
                }
                continue
            rt = self._crowding_route.get(leg.route_name)
            if rt is not None:
                leg.crowding = {
                    "category": rt[0],
                    "confidence": "low",
                    "target_type": "load_index",
                    "load_index": rt[1],
                    "time_scope": "route",
                    "reason": "no evidence for this stretch or hour; using the route average",
                }
                continue
            leg.crowding = {
                "category": "unknown",
                "confidence": "none",
                "target_type": "load_index",
                "reason": "no ticketing evidence for this route",
            }
