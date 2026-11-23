"""Bridge skipped points by routing along the bus network.

snap_to_bus_roads.py skips a guide point when the straight join from the
previous snapped point would leave the bus network. That keeps every drawn
segment on a bus road, but it leaves a long straight join where the points were
skipped: 10.6% of segments are longer than 60 m, and those are the visible
breaks in the line.

This fills them properly. Where a join is long, find the shortest path between
the two snapped points *on the bus network* and draw that instead. Because the
network is already filtered to bus-usable edges, the path is drivable by
construction, and because the endpoints are close the path is short.

If no path exists the join is left as it is: a straight line between two points
that are both on a bus road, which is the best available answer.
"""
from __future__ import annotations

import argparse
import gzip
import heapq
import math
import urllib.request
from pathlib import Path

import mapbox_vector_tile
import numpy as np

OUT = Path("web-next/public/data")
Z = 15
EXTENT = 4096
TILE_URL = (
    "https://tiles.basemaps.cartocdn.com/v/20261026-01/vectortiles/"
    "carto.streets/v2/{z}/{x}/{y}.mvt"
)
BUS_KINDS = {"major_road", "minor_road", "other"}
NO_ACCESS = {"private", "no", "customers", "delivery"}
# Only bridge joins longer than this; shorter ones are already fine.
BRIDGE_OVER_M = 60.0
MAX_BRIDGE_M = 1500.0
NODE_TOL = 1e-5
# Radius for joining vertices that adjacent tiles place a few metres apart.
CONNECT_M = 25.0


def tile_xy(lat: float, lon: float, z: int = Z) -> tuple[int, int]:
    n = 2**z
    x = int((lon + 180) / 360 * n)
    r = math.radians(lat)
    y = int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n)
    return x, y


def tile_to_lonlat(px: float, py: float, tx: int, ty: int, z: int = Z):
    n = 2**z
    lon = (tx + px / EXTENT) / n * 360 - 180
    yy = (ty + 1 - py / EXTENT) / n
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy))))
    return lon, lat


def fetch(z: int, x: int, y: int):
    try:
        raw = urllib.request.urlopen(TILE_URL.format(z=z, x=x, y=y), timeout=45).read()
    except Exception:
        return None
    try:
        raw = gzip.decompress(raw)
    except Exception:
        pass
    try:
        return mapbox_vector_tile.decode(raw)
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    args = ap.parse_args()

    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)
    finite = np.isfinite(xy[:, 0])
    lons, lats = xy[finite, 0], xy[finite, 1]
    x0, y0 = tile_xy(lats.max(), lons.min())
    x1, y1 = tile_xy(lats.min(), lons.max())

    # Build a graph of bus-usable road vertices.
    nodes: dict[tuple[float, float], int] = {}
    coords: list[tuple[float, float]] = []
    adj: dict[int, list[tuple[int, float]]] = {}

    def node_id(lon: float, lat: float) -> int:
        key = (round(lon, 5), round(lat, 5))
        i = nodes.get(key)
        if i is None:
            i = len(coords)
            nodes[key] = i
            coords.append(key)
            adj[i] = []
        return i

    tiles = 0
    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            d = fetch(Z, tx, ty)
            if not d:
                continue
            tiles += 1
            roads = d.get("roads")
            if not roads:
                continue
            for f in roads["features"]:
                p = f["properties"]
                if p.get("kind") not in BUS_KINDS:
                    continue
                if p.get("access") in NO_ACCESS:
                    continue
                geom = f["geometry"]
                lines = (
                    [geom["coordinates"]]
                    if geom["type"] == "LineString"
                    else geom["coordinates"]
                )
                for line in lines:
                    prev = None
                    for px, py in line:
                        lon, lat = tile_to_lonlat(px, py, tx, ty)
                        cur = node_id(lon, lat)
                        if prev is not None and prev != cur:
                            a, b = coords[prev], coords[cur]
                            w = math.hypot(
                                (b[0] - a[0]) * 111320 * math.cos(math.radians(a[1])),
                                (b[1] - a[1]) * 110540,
                            )
                            adj[prev].append((cur, w))
                            adj[cur].append((prev, w))
                        prev = cur
    print(f"tiles: {tiles}  nodes: {len(coords)}  edges: {sum(len(v) for v in adj.values())//2}")

    # Adjacent tiles rarely share exact vertices, so the raw graph arrives
    # shattered: 67,903 components, the largest holding only 11% of nodes. With
    # that fragmentation most joins have no path and are left as straight lines.
    # Bridge vertices that are within CONNECT_M of each other so the network is
    # actually connected.
    lat0 = float(lats.min())
    lon0 = float(lons.min())
    kx = 111320 * math.cos(math.radians(float(lats.mean())))
    nx = [(c[0] - lon0) * kx for c in coords]
    ny = [(c[1] - lat0) * 110540 for c in coords]

    CELL = 60.0
    buckets: dict[tuple[int, int], list[int]] = {}
    for i in range(len(coords)):
        buckets.setdefault((int(nx[i] // CELL), int(ny[i] // CELL)), []).append(i)

    added = 0
    for i in range(len(coords)):
        cx, cy = int(nx[i] // CELL), int(ny[i] // CELL)
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                for j in buckets.get((cx + ox, cy + oy), ()):
                    if j <= i:
                        continue
                    d = math.hypot(nx[j] - nx[i], ny[j] - ny[i])
                    if d <= CONNECT_M:
                        adj[i].append((j, d))
                        adj[j].append((i, d))
                        added += 1
    print(f"connector edges: {added}")

    def nearest(px: float, py: float) -> tuple[int, float]:
        cx, cy = int(px // CELL), int(py // CELL)
        best, best_d = -1, float("inf")
        for ox in range(-2, 3):
            for oy in range(-2, 3):
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
            if d > MAX_BRIDGE_M:
                continue
            for v, w in adj.get(u, ()):
                nd = d + w
                if nd > MAX_BRIDGE_M:
                    continue
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd + math.hypot(nx[v] - gx, ny[v] - gy), nd, v))
        return None

    def metres(a, b) -> float:
        kx2 = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
        return math.hypot((b[0] - a[0]) * kx2, (b[1] - a[1]) * 110540)

    out_off = np.zeros(len(offs), dtype=np.int32)
    out_xy: list[float] = []
    bridged = failed = 0

    for i in range(len(offs) - 1):
        seg = xy[offs[i] : offs[i + 1]]
        pts = [
            (float(lon), float(lat))
            for lon, lat in seg
            if np.isfinite(lon) and np.isfinite(lat)
        ]
        if len(pts) < 2:
            out_off[i + 1] = len(out_xy) // 2
            continue

        out_xy.extend(pts[0])
        for a, b in zip(pts, pts[1:]):
            if metres(a, b) <= BRIDGE_OVER_M:
                out_xy.extend(b)
                continue
            s, sd = nearest((a[0] - lon0) * kx, (a[1] - lat0) * 110540)
            e, ed = nearest((b[0] - lon0) * kx, (b[1] - lat0) * 110540)
            path = None
            if s >= 0 and e >= 0 and sd <= 40 and ed <= 40:
                path = astar(s, e)
            if path and len(path) >= 2:
                for n in path[1:]:
                    out_xy.extend(coords[n])
                bridged += 1
            else:
                out_xy.extend(b)
                failed += 1
        out_off[i + 1] = len(out_xy) // 2

        if (i + 1) % 2000 == 0:
            print(f"  {i+1}/{len(offs)-1} bridged={bridged} failed={failed}")

    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"joins bridged on the bus network: {bridged}  left straight: {failed}")
    print(f"points: {len(out_xy)//2}  bytes: {(OUT/'geom_xy.bin').stat().st_size/1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
