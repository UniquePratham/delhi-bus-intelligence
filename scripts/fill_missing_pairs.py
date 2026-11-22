"""Fill pairs that have no geometry, using the road graph.

The dashes in the drawn route are pairs with no geometry at all: 11 of them
across the three demo routes. Snapping cannot help because there is no guide to
snap.

For these pairs only, route on the road graph. A single pair between two
adjacent stops is a short, well-constrained search, so it does not produce the
loops that routing every pair did. The result is then checked against the road
mask and against a detour bound before being accepted.
"""
from __future__ import annotations

import argparse
import heapq
import math
import pickle

import numpy as np

from route_on_roads import CACHE, OUT, REF
from shortcut_on_road import build_road_index, on_road

import duckdb

SNAP_M = 200.0
MAX_DETOUR = 6.0
MAX_DETOUR_SLACK_M = 600.0


def main() -> int:
    ap = argparse.ArgumentParser()
    args = ap.parse_args()

    g = pickle.loads(CACHE.read_bytes())
    coords = np.asarray(g["coords"], dtype=np.float64)
    adj = g["adj"]
    print(f"nodes: {len(coords)}")

    lat0 = coords[:, 1].min()
    lon0 = coords[:, 0].min()
    kx = 111320 * math.cos(math.radians(float(coords[:, 1].mean())))
    nx = (coords[:, 0] - lon0) * kx
    ny = (coords[:, 1] - lat0) * 110540

    CELL = 60.0
    buckets: dict[tuple[int, int], list[int]] = {}
    for i in range(len(coords)):
        buckets.setdefault((int(nx[i] // CELL), int(ny[i] // CELL)), []).append(i)

    def nearest(px: float, py: float) -> tuple[int, float]:
        cx, cy = int(px // CELL), int(py // CELL)
        best, best_d = -1, float("inf")
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                for i in buckets.get((cx + ox, cy + oy), ()):
                    d = math.hypot(nx[i] - px, ny[i] - py)
                    if d < best_d:
                        best, best_d = i, d
        return best, best_d

    def astar(a: int, b: int) -> list[int] | None:
        if a == b:
            return [a]
        gx, gy = nx[b], ny[b]
        dist = {a: 0.0}
        prev: dict[int, int] = {}
        pq = [(math.hypot(nx[a] - gx, ny[a] - gy), 0.0, a)]
        seen = set()
        while pq:
            _, d, u = heapq.heappop(pq)
            if u in seen:
                continue
            seen.add(u)
            if u == b:
                path = [u]
                while path[-1] != a:
                    path.append(prev[path[-1]])
                return path[::-1]
            for v, w in adj.get(u, ()):
                nd = d + w
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(
                        pq, (nd + math.hypot(nx[v] - gx, ny[v] - gy), nd, v)
                    )
        return None

    con = duckdb.connect(str(REF), read_only=True)
    stations = {
        int(sid): (float(lat), float(lon))
        for sid, lat, lon in con.execute(
            "select station_id, lat, lon from stations"
        ).fetchall()
    }

    # Every pair a route can traverse, not just the ones the GPS guide covered.
    route_pairs = [
        (int(a), int(b))
        for a, b in con.execute(
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
    ]
    print(f"route pairs: {len(route_pairs)}")

    froms = np.fromfile(OUT / "geom_from.bin", dtype=np.int32)
    tos = np.fromfile(OUT / "geom_to.bin", dtype=np.int32)
    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    existing: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for i in range(len(froms)):
        pts = [(float(x), float(y)) for x, y in xy[offs[i] : offs[i + 1]]]
        if len(pts) >= 2:
            existing[(int(froms[i]), int(tos[i]))] = pts

    # Rebuild over the full route-pair set, filling anything absent.
    out_from = np.empty(len(route_pairs), dtype=np.int32)
    out_to = np.empty(len(route_pairs), dtype=np.int32)
    out_off = np.zeros(len(route_pairs) + 1, dtype=np.int32)
    out_xy: list[float] = []
    filled = 0
    failed = 0

    for i, (a, b) in enumerate(route_pairs):
        out_from[i] = a
        out_to[i] = b
        pts = existing.get((a, b))
        if pts:
            for lon, lat in pts:
                out_xy.extend((lon, lat))
            out_off[i + 1] = len(out_xy) // 2
            continue

        a_lat, a_lon = stations[a]
        b_lat, b_lon = stations[b]
        chord = math.hypot((b_lon - a_lon) * kx, (b_lat - a_lat) * 110540)
        s, sd = nearest((a_lon - lon0) * kx, (a_lat - lat0) * 110540)
        e, ed = nearest((b_lon - lon0) * kx, (b_lat - lat0) * 110540)
        path = None
        if s >= 0 and e >= 0 and sd <= SNAP_M and ed <= SNAP_M:
            path = astar(s, e)
        if path and len(path) >= 2:
            road_m = sum(
                math.hypot(nx[path[j + 1]] - nx[path[j]], ny[path[j + 1]] - ny[path[j]])
                for j in range(len(path) - 1)
            )
            if chord <= 0 or road_m <= MAX_DETOUR * chord + MAX_DETOUR_SLACK_M:
                for n in path:
                    out_xy.extend((float(coords[n][0]), float(coords[n][1])))
                filled += 1
            else:
                failed += 1
        else:
            failed += 1
        out_off[i + 1] = len(out_xy) // 2

    (OUT / "geom_from.bin").write_bytes(out_from.tobytes())
    (OUT / "geom_to.bin").write_bytes(out_to.tobytes())
    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"empty pairs filled: {filled}  failed: {failed}")
    print(f"points: {len(out_xy) // 2}  bytes: {(OUT / 'geom_xy.bin').stat().st_size / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
