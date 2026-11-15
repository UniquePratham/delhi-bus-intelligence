"""Export the built model to compact static artifacts for the web app.

Vercel is serverless, so the 1 GB of DuckDB cannot ship. Everything the browser
needs is precomputed here into binary blobs that the client-side planner reads
directly.

Layout
------
stations.bin   Float32 lat/lon interleaved, plus names in stations.json
routes.json    route_id -> name
patterns.bin   CSR: route -> ordered station ids
pairs.bin      CSR: station -> (neighbour, route) edges
travel.bin     CSR: pair -> (hour_of_week, seconds) observations
crowding.bin   sparse: (route, hour, station) -> band, confidence, load
meta.json      counts, provenance, evaluation metrics

All integers are little-endian. Sizes are chosen to keep the whole payload
under a few MB after brotli, which Vercel applies automatically.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import duckdb

from dbi import config

OUT = config.ROOT / "web-next" / "public" / "data"


def write_u8(path: Path, values: list[int]) -> None:
    path.write_bytes(bytes(values))


def write_u16(path: Path, values: list[int]) -> None:
    path.write_bytes(struct.pack(f"<{len(values)}H", *values))


def write_u32(path: Path, values: list[int]) -> None:
    path.write_bytes(struct.pack(f"<{len(values)}I", *values))


def export() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    ref = duckdb.connect(str(config.REFERENCE_DB), read_only=True)
    ref.execute("SET threads=8")
    traf = duckdb.connect(str(config.TRAFFIC_DB), read_only=True)
    traf.execute("SET threads=8")
    crowd = duckdb.connect(str(config.CROWDING_DB), read_only=True)
    crowd.execute("SET threads=8")

    # ---- stations -------------------------------------------------------
    stations = ref.execute(
        "SELECT station_id, lat, lon, name FROM stations ORDER BY station_id"
    ).fetchall()
    station_ids = [r[0] for r in stations]
    sid_to_idx = {sid: i for i, sid in enumerate(station_ids)}

    coords: list[float] = []
    for _sid, lat, lon, _name in stations:
        coords.extend((lat, lon))
    (OUT / "stations.bin").write_bytes(struct.pack(f"<{len(coords)}f", *coords))
    (OUT / "stations.json").write_text(
        json.dumps(
            {
                "ids": station_ids,
                "names": [r[3] for r in stations],
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    # ---- routes ---------------------------------------------------------
    routes = ref.execute("SELECT route_id, route_name FROM routes ORDER BY route_id").fetchall()
    route_ids = [r[0] for r in routes]
    rid_to_idx = {rid: i for i, rid in enumerate(route_ids)}
    (OUT / "routes.json").write_text(
        json.dumps(
            {"ids": route_ids, "names": [r[1] for r in routes]},
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    # ---- route patterns (CSR) -------------------------------------------
    pat_rows = ref.execute(
        "SELECT route_id, station_id FROM station_patterns ORDER BY route_id, stop_sequence"
    ).fetchall()
    pat_offsets = [0] * (len(route_ids) + 1)
    pat_stations: list[int] = []
    cur_rid = None
    for rid, sid in pat_rows:
        if rid != cur_rid:
            cur_rid = rid
            pat_offsets[rid_to_idx[rid]] = len(pat_stations)
        pat_stations.append(sid_to_idx[sid])
    # Fill any route with no pattern, then close the CSR.
    for i in range(len(route_ids)):
        if pat_offsets[i] == 0 and i > 0:
            pat_offsets[i] = pat_offsets[i - 1]
    pat_offsets[len(route_ids)] = len(pat_stations)
    write_u32(OUT / "patterns_off.bin", pat_offsets)
    write_u16(OUT / "patterns_sta.bin", pat_stations)

    # ---- edges: station -> (neighbour, route) ---------------------------
    edge_rows = ref.execute(
        """
        SELECT sp.from_station, sp.to_station, sp.route_id
        FROM station_pairs sp
        ORDER BY sp.from_station
        """
    ).fetchall()
    edge_off = [0] * (len(station_ids) + 1)
    edge_to: list[int] = []
    edge_route: list[int] = []
    cur = None
    for frm, to, rid in edge_rows:
        if frm != cur:
            cur = frm
            edge_off[sid_to_idx[frm]] = len(edge_to)
        edge_to.append(sid_to_idx[to])
        edge_route.append(rid_to_idx[rid])
    for i in range(len(station_ids)):
        if edge_off[i] == 0 and i > 0:
            edge_off[i] = edge_off[i - 1]
    edge_off[len(station_ids)] = len(edge_to)
    write_u32(OUT / "edges_off.bin", edge_off)
    write_u16(OUT / "edges_to.bin", edge_to)
    write_u16(OUT / "edges_route.bin", edge_route)

    # ---- travel times (CSR over station pairs) --------------------------
    pair_rows = traf.execute(
        "SELECT from_station, to_station FROM profile_pair ORDER BY from_station, to_station"
    ).fetchall()
    pair_index = {(f, t): i for i, (f, t) in enumerate(pair_rows)}
    write_u16(OUT / "pair_from.bin", [sid_to_idx[f] for f, _ in pair_rows])
    write_u16(OUT / "pair_to.bin", [sid_to_idx[t] for _, t in pair_rows])

    hour_rows = traf.execute(
        """
        SELECT from_station, to_station, hour_of_week, travel_s
        FROM profile_pair_hour
        ORDER BY from_station, to_station, hour_of_week
        """
    ).fetchall()
    hour_off = [0] * (len(pair_rows) + 1)
    hour_how: list[int] = []
    hour_sec: list[int] = []
    cur = None
    for frm, to, how, sec in hour_rows:
        key = (frm, to)
        if key != cur:
            cur = key
            hour_off[pair_index[key]] = len(hour_how)
        hour_how.append(int(how))
        hour_sec.append(min(65535, max(0, int(round(sec)))))
    for i in range(len(pair_rows)):
        if hour_off[i] == 0 and i > 0:
            hour_off[i] = hour_off[i - 1]
    hour_off[len(pair_rows)] = len(hour_how)
    write_u32(OUT / "hour_off.bin", hour_off)
    write_u8(OUT / "hour_how.bin", hour_how)
    write_u16(OUT / "hour_sec.bin", hour_sec)

    # ---- band + base + schedule fallbacks -------------------------------
    band_rows = traf.execute(
        """
        SELECT from_station, to_station, hour_band, travel_s
        FROM profile_pair_band
        """
    ).fetchall()
    band_names = ["night", "early_peak", "morning_peak", "midday", "evening_peak", "late"]
    band_idx = {n: i for i, n in enumerate(band_names)}
    band_vals = [0] * (len(pair_rows) * 6)
    for frm, to, band, sec in band_rows:
        i = pair_index.get((frm, to))
        if i is not None:
            band_vals[i * 6 + band_idx[band]] = min(65535, max(0, int(round(sec))))
    write_u16(OUT / "band.bin", band_vals)

    base_rows = traf.execute(
        "SELECT from_station, to_station, travel_s FROM profile_pair"
    ).fetchall()
    base_vals = [0] * len(pair_rows)
    for frm, to, sec in base_rows:
        i = pair_index.get((frm, to))
        if i is not None:
            base_vals[i] = min(65535, max(0, int(round(sec))))
    write_u16(OUT / "base.bin", base_vals)

    sched_rows = traf.execute(
        "SELECT from_station, to_station, scheduled_s FROM schedule"
    ).fetchall()
    sched_vals = [0] * len(pair_rows)
    for frm, to, sec in sched_rows:
        i = pair_index.get((frm, to))
        if i is not None:
            sched_vals[i] = min(65535, max(0, int(round(sec))))
    write_u16(OUT / "sched.bin", sched_vals)

    # Global hour-band medians and the observed/scheduled ratio per band.
    gband = {
        r[0]: r[1]
        for r in traf.execute("SELECT hour_band, travel_s FROM profile_global_band").fetchall()
    }
    gmedian = traf.execute("SELECT travel_s FROM profile_global").fetchone()[0]
    band_factor = {
        r[0]: r[1]
        for r in traf.execute(
            """
            SELECT r.hour_band, median(r.travel_s / nullif(s.scheduled_s, 0))
            FROM train_runs r JOIN schedule s USING (from_station, to_station)
            WHERE s.scheduled_s > 0 GROUP BY 1
            """
        ).fetchall()
    }

    # ---- crowding (sparse) ----------------------------------------------
    crow_rows = crowd.execute(
        """
        SELECT route, hour, station_id, crowding_band, confidence, load_index
        FROM crowding_levels
        """
    ).fetchall()
    band_code = {"seats_available": 0, "standing_room": 1, "crowded": 2}
    conf_code = {"low": 0, "medium": 1, "high": 2}
    crow_route: list[int] = []
    crow_hour: list[int] = []
    crow_sta: list[int] = []
    crow_band: list[int] = []
    crow_conf: list[int] = []
    crow_load: list[int] = []
    for route_name, hour, station_id, band, conf, load in crow_rows:
        # crowding_levels keys on the route *name*; map it back to an index.
        rid = next((r[0] for r in routes if r[1] == route_name), None)
        if rid is None or station_id not in sid_to_idx:
            continue
        crow_route.append(rid_to_idx[rid])
        crow_hour.append(int(hour))
        crow_sta.append(sid_to_idx[station_id])
        crow_band.append(band_code.get(band, 0))
        crow_conf.append(conf_code.get(conf, 0))
        crow_load.append(min(255, max(0, int(round(load)))))
    write_u16(OUT / "crowd_route.bin", crow_route)
    write_u8(OUT / "crowd_hour.bin", crow_hour)
    write_u16(OUT / "crowd_sta.bin", crow_sta)
    write_u8(OUT / "crowd_band.bin", crow_band)
    write_u8(OUT / "crowd_conf.bin", crow_conf)
    write_u8(OUT / "crowd_load.bin", crow_load)

    # ---- metadata -------------------------------------------------------
    eval_traffic = json.loads((config.ARTIFACTS / "eval_traffic.json").read_text(encoding="utf-8"))
    eval_crowd = json.loads((config.ARTIFACTS / "eval_crowding.json").read_text(encoding="utf-8"))
    validation = json.loads((config.ARTIFACTS / "validation.json").read_text(encoding="utf-8"))
    temporal = json.loads((config.ARTIFACTS / "eval_temporal.json").read_text(encoding="utf-8"))

    meta = {
        "generated": "2026-11-15",
        "provenance": {
            "traffic": "historical",
            "crowding": "historical",
            "traffic_period": "2026-10-01 to 2026-10-29",
            "crowding_period": "2026-10-01 to 2026-10-31",
            "live_feed": False,
            "note": "Estimates come from a historical October 2026 snapshot, not a live feed.",
        },
        "counts": {
            "stations": len(station_ids),
            "routes": len(route_ids),
            "station_pairs": len(pair_rows),
            "hour_profiles": len(hour_how),
            "crowding_cells": len(crow_route),
        },
        "bands": band_names,
        "global_band": gband,
        "global_median": gmedian,
        "band_factor": band_factor,
        "evaluation": {
            "traffic": eval_traffic,
            "crowding": eval_crowd,
            "journeys": validation["summary"],
            "temporal": temporal,
        },
    }
    (OUT / "meta.json").write_text(json.dumps(meta, separators=(",", ":")), encoding="utf-8")

    ref.close()
    traf.close()
    crowd.close()

    total = sum(f.stat().st_size for f in OUT.iterdir())
    print(f"exported to {OUT}")
    for f in sorted(OUT.iterdir()):
        print(f"  {f.name:20s} {f.stat().st_size / 1024:9.1f} KB")
    print(f"  {'TOTAL':20s} {total / 1024 / 1024:9.2f} MB")


if __name__ == "__main__":
    sys.exit(export())
