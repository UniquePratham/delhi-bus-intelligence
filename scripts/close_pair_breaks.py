"""Close the remaining breaks between consecutive pairs.

After snapping, bridging and anchoring, 9 breaks remain in the demo route where
a pair's polyline ends far from where the next begins. Each is a station that
sits more than ANCHOR_MAX_M from the nearest bus road, so it was not anchored.

This closes them by routing on the bus network between the two endpoints, which
is the same operation the bridge pass does inside a pair, applied across the
pair boundary.
"""
from __future__ import annotations

import argparse
import gzip
import heapq
import math
import urllib.request
from pathlib import Path

import duckdb
import mapbox_vector_tile
import numpy as np

REF = Path("data/processed/reference.duckdb")
OUT = Path("web-next/public/data")
Z = 15
EXTENT = 4096
TILE_URL = (
    "https://tiles.basemaps.cartocdn.com/v/20261026-01/vectortiles/"
    "carto.streets/v2/{z}/{x}/{y}.mvt"
)
BUS_KINDS = {"major_road", "minor_road", "other"}
NO_ACCESS = {"private", "no", "customers", "delivery"}
CONNECT_M = 25.0
MAX_LINK_M = 2000.0


def tile_xy(lat, lon, z=Z):
    n = 2**z
    x = int((lon + 180) / 360 * n)
    r = math.radians(lat)
    y = int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n)
    return x, y


def to_lonlat(px, py, tx, ty, z=Z):
    n = 2**z
    lon = (tx + px / EXTENT) / n * 360 - 180
    yy = (ty + 1 - py / EXTENT) / n
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy))))
    return lon, lat


def fetch(z, x, y):
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

    froms = np.fromfile(OUT / "geom_from.bin", dtype=np.int32)
    tos = np.fromfile(OUT / "geom_to.bin", dtype=np.int32)
    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    con = duckdb.connect(str(REF), read_only=True)
    stations = {
        int(sid): (float(lon), float(lat))
        for sid, lat, lon in con.execute(
            "select station_id, lat, lon from stations"
        ).fetchall()
    }

    # Build the bus graph once.
    finite = np.isfinite(xy[:, 0])
    lons, lats = xy[finite, 0], xy[finite, 1]
    x0, y0 = tile_xy(lats.max(), lons.min())
    x1, y1 = tile_xy(lats.min(), lons.max())

    nodes: dict[tuple[float, float], int] = {}
    coords: list[tuple[float, float]] = []
    adj: dict[int, list[tuple[int, float]]] = {}

    def nid(lon, lat):
        key = (round(lon, 5), round(lat, 5))
        i = nodes.get(key)
        if i is None:
            i = len(coords)
            nodes[key] = i
            coords.append(key)
            adj[i] = []
        return i

    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            d = fetch(Z, tx, ty)
            if not d:
                continue
            roads = d.get("roads")
            if not roads:
                continue
            for f in roads["features"]:
                p = f["properties"]
                if p.get("kind") not in BUS_KINDS:
                    continue
                if p.get("access") in NO_ACCESS:
                    continue
                g = f["geometry"]
                lines = (
                    [g["coordinates"]]
                    if g["type"] == "LineString"
                    else g["coordinates"]
                )
                for line in lines:
                    prev = None
                    for px, py in line:
                        lon, lat = to_lonlat(px, py, tx, ty)
                        cur = nid(lon, lat)
                        if prev is not None and prev != cur:
                            a, b = coords[prev], coords[cur]
                            w = math.hypot(
                                (b[0] - a[0]) * 111320 * math.cos(math.radians(a[1])),
                                (b[1] - a[1]) * 110540,
                            )
                            adj[prev].append((cur, w))
                            adj[cur].append((prev, w))
                        prev = cur
    print(f"nodes: {len(coords)}")

    lat0 = float(lats.min())
    lon0 = float(lons.min())
    kx = 111320 * math.cos(math.radians(float(lats.mean())))
    nx = [(c[0] - lon0) * kx for c in coords]
    ny = [(c[1] - lat0) * 110540 for c in coords]

    CELL = 60.0
    buckets: dict[tuple[int, int], list[int]] = {}
    for i in range(len(coords)):
        buckets.setdefault((int(nx[i] // CELL), int(ny[i] // CELL)), []).append(i)

    # Bridge vertices that tiles place a few metres apart.
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

    def nearest(px, py):
        cx, cy = int(px // CELL), int(py // CELL)
        best, best_d = -1, float("inf")
        for ox in range(-3, 4):
            for oy in range(-3, 4):
                for i in buckets.get((cx + ox, cy + oy), ()):
                    d = math.hypot(nx[i] - px, ny[i] - py)
                    if d < best_d:
                        best, best_d = i, d
        return best, best_d

    def astar(a, b):
        if a == b:
            return [a]
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
                return path[::-1]
            if d > MAX_LINK_M:
                continue
            for v, w in adj.get(u, ()):
                nd = d + w
                if nd > MAX_LINK_M:
                    continue
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd + math.hypot(nx[v] - gx, ny[v] - gy), nd, v))
        return None

    def metres(a, b):
        kx2 = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
        return math.hypot((b[0] - a[0]) * kx2, (b[1] - a[1]) * 110540)

    # Rebuild, inserting a road path wherever consecutive pairs do not meet.
    out_off = np.zeros(len(froms) + 1, dtype=np.int32)
    out_xy: list[float] = []
    closed = failed = 0
    prev_end = None

    for i in range(len(froms)):
        pts = [
            (float(x), float(y))
            for x, y in xy[offs[i] : offs[i + 1]]
            if np.isfinite(x) and np.isfinite(y)
        ]
        if len(pts) < 2:
            out_off[i + 1] = len(out_xy) // 2
            prev_end = None
            continue

        if prev_end is not None:
            gap = metres(prev_end, pts[0])
            if gap > 60:
                s, sd = nearest((prev_end[0] - lon0) * kx, (prev_end[1] - lat0) * 110540)
                e, ed = nearest((pts[0][0] - lon0) * kx, (pts[0][1] - lat0) * 110540)
                path = None
                if s >= 0 and e >= 0 and sd <= 60 and ed <= 60:
                    path = astar(s, e)
                if path and len(path) >= 2:
                    for n in path:
                        out_xy.extend(coords[n])
                    closed += 1
                else:
                    failed += 1

        for lon, lat in pts:
            out_xy.extend((lon, lat))
        out_off[i + 1] = len(out_xy) // 2
        prev_end = pts[-1]

    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"breaks closed on the bus network: {closed}  left open: {failed}")
    print(f"points: {len(out_xy)//2}  bytes: {(OUT/'geom_xy.bin').stat().st_size/1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
