"""Route between consecutive observed pings on the road network.

Raw pings are noisy and sparse, so snapping them individually leaves gaps and
corners cut. Routing station-to-station invents loops, because the tile graph is
fragmented and the stations are far apart.

This does neither. It keeps the observed ping sequence - the real corridor the
bus drove - and routes along the road network between each consecutive pair of
pings. Consecutive pings are 100-300 m apart, so the road path between them is
short and unambiguous: there is no room for a loop or a detour.

Every point is either a real recorded ping or a point on a real road between two
real pings. Nothing is invented.
"""
from __future__ import annotations

import argparse
import gzip
import heapq
import math
import pickle
import urllib.request
from pathlib import Path

import mapbox_vector_tile
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web-next" / "public" / "data"
CACHE = ROOT / "data" / "processed" / "bus_network.pkl"

Z = 15
EXTENT = 4096
TILE_URL = (
    "https://tiles.basemaps.cartocdn.com/v/20261026-01/vectortiles/"
    "carto.streets/v2/{z}/{x}/{y}.mvt"
)
BUS_KINDS = {"major_road", "minor_road", "other"}
NO_ACCESS = {"private", "no", "customers", "delivery"}
CONNECT_M = 25.0
SNAP_MAX_M = 60.0
# A ping-to-ping road path longer than this is a bad snap, not a road.
MAX_LINK_M = 900.0


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
    ap.add_argument("--rebuild-network", action="store_true")
    args = ap.parse_args()

    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)
    finite = np.isfinite(xy[:, 0])
    lons, lats = xy[finite, 0], xy[finite, 1]

    if CACHE.exists() and not args.rebuild_network:
        print(f"loading cached network {CACHE}")
        net = pickle.loads(CACHE.read_bytes())
    else:
        x0, y0 = tile_xy(lats.max(), lons.min())
        x1, y1 = tile_xy(lats.min(), lons.max())
        nodes: dict[tuple[float, float], int] = {}
        coords: list[tuple[float, float]] = []
        adj: dict[int, list[tuple[int, float]]] = {}
        road_edges: set[tuple[int, int]] = set()

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
                                road_edges.add((min(prev, cur), max(prev, cur)))
                            prev = cur
        print(f"nodes: {len(coords):,}  road edges: {len(road_edges):,}")

        lat0 = min(c[1] for c in coords)
        lon0 = min(c[0] for c in coords)
        kx = 111320 * math.cos(math.radians(sum(c[1] for c in coords) / len(coords)))
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
                        dd = math.hypot(nx[j] - nx[i], ny[j] - ny[i])
                        if dd <= CONNECT_M:
                            adj[i].append((j, dd))
                            adj[j].append((i, dd))
                            added += 1
        print(f"connector edges: {added:,}")

        net = {
            "coords": coords, "adj": adj, "road_edges": road_edges,
            "nx": nx, "ny": ny, "lat0": lat0, "lon0": lon0, "kx": kx,
            "buckets": buckets, "cell": CELL,
        }
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_bytes(pickle.dumps(net, protocol=4))
        print(f"cached -> {CACHE}")

    coords = net["coords"]
    adj = net["adj"]
    road_edges = net["road_edges"]
    nx, ny = net["nx"], net["ny"]
    lat0, lon0, kx = net["lat0"], net["lon0"], net["kx"]
    buckets, CELL = net["buckets"], net["cell"]

    def nearest(px, py):
        cx, cy = int(px // CELL), int(py // CELL)
        best, best_d = -1, float("inf")
        for ox in range(-2, 3):
            for oy in range(-2, 3):
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
                key = (min(u, v), max(u, v))
                extra = 0.0 if key in road_edges else 400.0
                nd = d + w + extra
                if nd > MAX_LINK_M:
                    continue
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(
                        pq, (nd + math.hypot(nx[v] - gx, ny[v] - gy), nd, v)
                    )
        return None

    out_off = np.zeros(len(offs), dtype=np.int32)
    out_xy: list[float] = []
    linked = straight = 0

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

        # Snap each ping to a road node.
        nodes_seq: list[int] = []
        for lon, lat in pts:
            n, d = nearest((lon - lon0) * kx, (lat - lat0) * 110540)
            if n >= 0 and d <= SNAP_MAX_M:
                nodes_seq.append(n)
        if len(nodes_seq) < 2:
            out_off[i + 1] = len(out_xy) // 2
            continue

        path: list[int] = [nodes_seq[0]]
        for a, b in zip(nodes_seq, nodes_seq[1:]):
            if a == b:
                continue
            sub = astar(a, b)
            if sub and len(sub) >= 2:
                path.extend(sub[1:])
                linked += 1
            else:
                # No road path between two consecutive pings: keep the real
                # ping positions rather than invent a route.
                path.append(b)
                straight += 1

        for n in path:
            out_xy.extend(coords[n])
        out_off[i + 1] = len(out_xy) // 2

        if (i + 1) % 2000 == 0:
            print(f"  {i+1}/{len(offs)-1} linked={linked} straight={straight}")

    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"ping links routed on roads: {linked:,}  kept straight: {straight:,}")
    print(f"points: {len(out_xy)//2:,}  bytes: {(OUT/'geom_xy.bin').stat().st_size/1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
