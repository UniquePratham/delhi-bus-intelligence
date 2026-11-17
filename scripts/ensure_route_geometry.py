"""Guarantee road geometry for every station pair a route can traverse.

The GPS-derived geometry covers 81.6% of route pairs. The remaining 16.7% had
no trace, so the map fell back to a straight chord between the two stations -
a line that cuts across parks and buildings.

This closes the gap: for every pair still missing geometry, route it on the
road graph directly, using the straight chord only as a weak directional guide.
Every drawn line then follows a road, with no chord fallback left.
"""
from __future__ import annotations

import pickle
from pathlib import Path

import duckdb
import numpy as np

from match_routes_to_roads import (
    CACHE,
    MAX_DETOUR,
    OUT,
    REF,
    _length_m,
    nearest_node,
    route_on_graph,
)

# Gap-fill tolerates a wider snap than the trace-matching pass: these pairs
# have no GPS evidence, so the road graph is the only source of truth.
GAP_SNAP_M = 400.0


def main() -> int:
    con = duckdb.connect(str(REF), read_only=True)
    stations = {
        sid: (lat, lon)
        for sid, lat, lon in con.execute("select station_id, lat, lon from stations").fetchall()
    }

    # Every consecutive station pair any route can traverse.
    route_pairs = con.execute(
        """
        WITH seq AS (
          SELECT route_id, stop_sequence, station_id,
                 lead(station_id) OVER (PARTITION BY route_id ORDER BY stop_sequence) AS nxt
          FROM station_patterns
        )
        SELECT DISTINCT station_id, nxt FROM seq WHERE nxt IS NOT NULL
        ORDER BY 1, 2
        """
    ).fetchall()

    # Existing geometry, keyed by direction.
    froms = np.fromfile(OUT / "geom_from.bin", dtype=np.int32)
    tos = np.fromfile(OUT / "geom_to.bin", dtype=np.int32)
    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    existing: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for i in range(len(froms)):
        pts = [(float(x), float(y)) for x, y in xy[offs[i] : offs[i + 1]]]
        if len(pts) >= 2:
            existing[(int(froms[i]), int(tos[i]))] = pts

    print(f"route pairs: {len(route_pairs)}  existing geometry: {len(existing)}")

    g = pickle.loads(CACHE.read_bytes())
    coords = g["coords"]

    out_from = np.empty(len(route_pairs), dtype=np.int32)
    out_to = np.empty(len(route_pairs), dtype=np.int32)
    out_off = np.zeros(len(route_pairs) + 1, dtype=np.int32)
    out_xy: list[float] = []

    kept = reversed_ = routed = failed = 0
    for i, (a, b) in enumerate(route_pairs):
        a, b = int(a), int(b)
        out_from[i] = a
        out_to[i] = b

        pts = existing.get((a, b))
        if pts:
            kept += 1
        else:
            rev = existing.get((b, a))
            if rev:
                pts = rev[::-1]
                reversed_ += 1
            else:
                # No trace: route on the road graph, guided by the chord.
                a_lat, a_lon = stations[a]
                b_lat, b_lon = stations[b]
                s, sd = nearest_node(g, a_lon, a_lat)
                e, ed = nearest_node(g, b_lon, b_lat)
                path = None
                # A station is a bus stop, so it is always near a road; a large
                # snap radius here only compensates for gaps in the tile graph.
                if s >= 0 and e >= 0 and sd <= GAP_SNAP_M and ed <= GAP_SNAP_M:
                    path = route_on_graph(g, s, e, [(a_lon, a_lat), (b_lon, b_lat)])
                if path and len(path) >= 2:
                    road = [coords[nid] for nid in path]
                    chord = _length_m([(a_lon, a_lat), (b_lon, b_lat)])
                    if chord <= 0 or _length_m(road) <= MAX_DETOUR * chord:
                        pts = road
                        routed += 1
                if not pts:
                    failed += 1

        if pts:
            for lon, lat in pts:
                out_xy.extend((lon, lat))
        out_off[i + 1] = len(out_xy) // 2

        if (i + 1) % 2000 == 0:
            print(f"  {i + 1}/{len(route_pairs)}")

    (OUT / "geom_from.bin").write_bytes(out_from.tobytes())
    (OUT / "geom_to.bin").write_bytes(out_to.tobytes())
    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    covered = len(route_pairs) - failed
    print(
        f"kept={kept} reversed={reversed_} newly_routed={routed} failed={failed}\n"
        f"coverage: {covered}/{len(route_pairs)} ({covered / len(route_pairs) * 100:.1f}%)"
    )
    print(f"points: {len(out_xy) // 2}  bytes: {(OUT / 'geom_xy.bin').stat().st_size / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
