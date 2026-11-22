"""Check the road distance for the failing pairs."""
from __future__ import annotations

import heapq
import math
import pickle

import duckdb

from route_on_roads import CACHE, REF

g = pickle.loads(CACHE.read_bytes())
coords = g["coords"]
adj = g["adj"]
con = duckdb.connect(str(REF), read_only=True)
stations = {
    int(sid): (float(lat), float(lon))
    for sid, lat, lon in con.execute(
        "select station_id, lat, lon from stations"
    ).fetchall()
}
names = dict(con.execute("select station_id, name from stations").fetchall())

lat0 = min(c[1] for c in coords)
lon0 = min(c[0] for c in coords)
kx = 111320 * math.cos(math.radians(28.65))
nx = [(c[0] - lon0) * kx for c in coords]
ny = [(c[1] - lat0) * 110540 for c in coords]

CELL = 60.0
buckets: dict[tuple[int, int], list[int]] = {}
for i in range(len(coords)):
    buckets.setdefault((int(nx[i] // CELL), int(ny[i] // CELL)), []).append(i)


def nearest(px, py):
    cx, cy = int(px // CELL), int(py // CELL)
    best, best_d = -1, float("inf")
    for ox in (-1, 0, 1):
        for oy in (-1, 0, 1):
            for i in buckets.get((cx + ox, cy + oy), ()):
                d = math.hypot(nx[i] - px, ny[i] - py)
                if d < best_d:
                    best, best_d = i, d
    return best, best_d


def astar(a, b):
    gx, gy = nx[b], ny[b]
    dist = {a: 0.0}
    prev = {}
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
            return path[::-1], d
        for v, w in adj.get(u, ()):
            nd = d + w
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd + math.hypot(nx[v] - gx, ny[v] - gy), nd, v))
    return None, None


for a, b in [(3332, 781), (50, 750), (957, 959), (353, 136), (136, 830), (353, 351)]:
    a_lat, a_lon = stations[a]
    b_lat, b_lon = stations[b]
    s, _ = nearest((a_lon - lon0) * kx, (a_lat - lat0) * 110540)
    e, _ = nearest((b_lon - lon0) * kx, (b_lat - lat0) * 110540)
    chord = math.hypot((b_lon - a_lon) * kx, (b_lat - a_lat) * 110540)
    path, d = astar(s, e)
    if path is None:
        print(f"{a}->{b}: NO PATH (chord {chord:.0f} m)")
    else:
        print(
            f"{a}->{b}: chord={chord:.0f}m road={d:.0f}m "
            f"ratio={d/chord:.1f} nodes={len(path)}"
        )
