"""Map-match bus routes onto the real road network.

The GPS-derived geometry follows the corridor but smooths across corners, so
lines still clip building blocks. This script builds a road graph from the
basemap vector tiles (the same tiles the map renders) and snaps each station
pair's path onto it, so a drawn line can only ever run along a road.

Method:
  1. Download the road layer for every tile covering the network, once, and
     cache it to disk.
  2. Build a node graph from road vertices, merging shared points.
  3. For each station pair, take the GPS-derived polyline as a guide and run
     Dijkstra over the road graph, penalising deviation from the guide. The
     result is a road-following path between the two stations.
  4. Emit the same compact binary format the browser already reads.

Falls back to the GPS polyline for pairs the graph cannot connect.
"""
from __future__ import annotations

import argparse
import gzip
import heapq
import math
import pickle
import urllib.request
from pathlib import Path

import duckdb
import mapbox_vector_tile
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "data" / "processed" / "reference.duckdb"
OUT = ROOT / "web-next" / "public" / "data"
CACHE = ROOT / "data" / "processed" / "road_graph.pkl"

TILE_URL = (
    "https://tiles.basemaps.cartocdn.com/v/20261026-01/vectortiles/"
    "carto.streets/v2/{z}/{x}/{y}.mvt"
)
Z = 14
EXTENT = 4096
ROAD_KINDS = {"major_road", "minor_road", "other"}

SNAP_M = 150.0
GUIDE_WEIGHT = 0.30
# Hard corridor: a node further than this from the GPS guide is never
# considered. This bounds Dijkstra to the road corridor the bus actually used,
# which is both correct (a bus does not detour 300 m) and fast.
MAX_DEV_M = 600.0
# Bridge radius for joining road vertices that adjacent tiles place a few
# metres apart. Too small and the graph stays fragmented.
CONNECT_M = 30.0
# A matched road path may be longer than the GPS guide (roads bend), but not
# wildly so. Beyond this ratio the match is a graph artefact, not a road.
MAX_DETOUR = 1.6
GRID = 0.01


def _length_m(pts: list[tuple[float, float]]) -> float:
    total = 0.0
    for j in range(len(pts) - 1):
        (x1, y1), (x2, y2) = pts[j], pts[j + 1]
        kx = 111320 * math.cos(math.radians(y1))
        total += math.hypot((x2 - x1) * kx, (y2 - y1) * 110540)
    return total


def tile_xy(lat: float, lon: float, z: int = Z) -> tuple[int, int]:
    n = 2**z
    x = int((lon + 180) / 360 * n)
    r = math.radians(lat)
    y = int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n)
    return x, y


def tile_to_lonlat(px: float, py: float, tx: int, ty: int, z: int = Z) -> tuple[float, float]:
    n = 2**z
    lon = (tx + px / EXTENT) / n * 360 - 180
    yy = (ty + 1 - py / EXTENT) / n
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy))))
    return lon, lat


def fetch_tile(z: int, x: int, y: int) -> dict | None:
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


def build_graph(bbox: tuple[float, float, float, float]) -> dict:
    lo_lat, lo_lon, hi_lat, hi_lon = bbox
    x0, y0 = tile_xy(hi_lat, lo_lon)
    x1, y1 = tile_xy(lo_lat, hi_lon)

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
            d = fetch_tile(Z, tx, ty)
            if not d:
                continue
            tiles += 1
            roads = d.get("roads")
            if not roads:
                continue
            for f in roads["features"]:
                if f["properties"].get("kind") not in ROAD_KINDS:
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

    print(f"tiles: {tiles}  nodes: {len(coords)}  edges: {sum(len(v) for v in adj.values()) // 2}")

    grid: dict[tuple[int, int], list[int]] = {}
    for i, (lon, lat) in enumerate(coords):
        grid.setdefault((int(lon / GRID), int(lat / GRID)), []).append(i)

    # Road features from adjacent tiles rarely share exact vertices, so the
    # graph arrives fragmented into thousands of small components. Bridge
    # nodes that are within CONNECT_M of each other; without this, most
    # station pairs have no path at all.
    added = 0
    for i, (lon, lat) in enumerate(coords):
        cx, cy = int(lon / GRID), int(lat / GRID)
        kx = 111320 * math.cos(math.radians(lat))
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for j in grid.get((gx, gy), ()):
                    if j <= i:
                        continue
                    nlon, nlat = coords[j]
                    d = math.hypot((nlon - lon) * kx, (nlat - lat) * 110540)
                    if d <= CONNECT_M:
                        adj[i].append((j, d))
                        adj[j].append((i, d))
                        added += 1
    print(f"connector edges: {added}")

    return {"coords": coords, "adj": adj, "grid": grid}


def nearest_node(g: dict, lon: float, lat: float) -> tuple[int, float]:
    coords, grid = g["coords"], g["grid"]
    cx, cy = int(lon / GRID), int(lat / GRID)
    kx = 111320 * math.cos(math.radians(lat))
    best, best_d = -1, float("inf")
    for ring in range(0, 6):
        found = False
        for gx in range(cx - ring, cx + ring + 1):
            for gy in range(cy - ring, cy + ring + 1):
                if ring and max(abs(gx - cx), abs(gy - cy)) != ring:
                    continue
                for i in grid.get((gx, gy), ()):
                    nlon, nlat = coords[i]
                    d = math.hypot((nlon - lon) * kx, (nlat - lat) * 110540)
                    if d < best_d:
                        best, best_d = i, d
                        found = True
        if found and ring >= 1:
            break
    return best, best_d


def route_on_graph(
    g: dict, start: int, goal: int, guide: list[tuple[float, float]]
) -> list[int] | None:
    if start == goal:
        return [start]
    coords, adj = g["coords"], g["adj"]
    kx = 111320 * math.cos(math.radians(coords[start][1]))

    # Bucket guide *segments* into grid cells they pass through. Measuring
    # distance to segments (not vertices) is what makes the corridor correct:
    # a road running between two sparse guide points is still on the guide.
    seg_grid: dict[tuple[int, int], list[tuple[float, float, float, float]]] = {}
    for j in range(len(guide) - 1):
        (x1, y1), (x2, y2) = guide[j], guide[j + 1]
        lo_x, hi_x = sorted((int(x1 / GRID), int(x2 / GRID)))
        lo_y, hi_y = sorted((int(y1 / GRID), int(y2 / GRID)))
        seg = (x1, y1, x2, y2)
        for gx in range(lo_x - 1, hi_x + 2):
            for gy in range(lo_y - 1, hi_y + 2):
                seg_grid.setdefault((gx, gy), []).append(seg)

    cache: dict[int, float] = {}

    def dev(i: int) -> float:
        v = cache.get(i)
        if v is not None:
            return v
        lon, lat = coords[i]
        cx, cy = int(lon / GRID), int(lat / GRID)
        best = float("inf")
        for ring in range(0, 4):
            for gx in range(cx - ring, cx + ring + 1):
                for gy in range(cy - ring, cy + ring + 1):
                    if ring and max(abs(gx - cx), abs(gy - cy)) != ring:
                        continue
                    for x1, y1, x2, y2 in seg_grid.get((gx, gy), ()):
                        # Point-to-segment distance in local metres.
                        px = (lon - x1) * kx
                        py = (lat - y1) * 110540
                        dx = (x2 - x1) * kx
                        dy = (y2 - y1) * 110540
                        L2 = dx * dx + dy * dy
                        t = 0.0 if L2 == 0 else max(0.0, min(1.0, (px * dx + py * dy) / L2))
                        d = math.hypot(px - t * dx, py - t * dy)
                        if d < best:
                            best = d
            if best < float("inf"):
                break
        if best == float("inf"):
            best = 500.0
        cache[i] = best
        return best

    dist = {start: 0.0}
    prev: dict[int, int] = {}
    pq = [(0.0, start)]
    seen = set()
    while pq:
        d, u = heapq.heappop(pq)
        if u in seen:
            continue
        seen.add(u)
        if u == goal:
            path = [u]
            while path[-1] != start:
                path.append(prev[path[-1]])
            return path[::-1]
        for v, w in adj.get(u, ()):
            dv = dev(v)
            if dv > MAX_DEV_M:
                continue
            nd = d + w + GUIDE_WEIGHT * dv
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--rebuild-graph", action="store_true")
    args = ap.parse_args()

    con = duckdb.connect(str(REF), read_only=True)
    bbox = con.execute("select min(lat), min(lon), max(lat), max(lon) from stations").fetchone()
    stations = {
        sid: (lat, lon)
        for sid, lat, lon in con.execute("select station_id, lat, lon from stations").fetchall()
    }

    if CACHE.exists() and not args.rebuild_graph:
        print(f"loading cached graph {CACHE}")
        g = pickle.loads(CACHE.read_bytes())
    else:
        g = build_graph(bbox)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_bytes(pickle.dumps(g, protocol=4))
        print(f"cached graph -> {CACHE}")

    coords = g["coords"]

    froms = np.fromfile(OUT / "geom_from.bin", dtype=np.int32)
    tos = np.fromfile(OUT / "geom_to.bin", dtype=np.int32)
    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    n_pairs = len(froms) if not args.limit else min(args.limit, len(froms))
    out_from = np.empty(n_pairs, dtype=np.int32)
    out_to = np.empty(n_pairs, dtype=np.int32)
    out_off = np.zeros(n_pairs + 1, dtype=np.int32)
    out_xy: list[float] = []

    matched = 0
    for i in range(n_pairs):
        f, t = int(froms[i]), int(tos[i])
        out_from[i] = f
        out_to[i] = t

        guide = [(float(x), float(y)) for x, y in xy[offs[i] : offs[i + 1]]]
        if len(guide) < 2:
            out_off[i + 1] = len(out_xy) // 2
            continue

        a_lat, a_lon = stations[f]
        b_lat, b_lon = stations[t]
        s, sd = nearest_node(g, a_lon, a_lat)
        e, ed = nearest_node(g, b_lon, b_lat)

        path = None
        if s >= 0 and e >= 0 and sd <= SNAP_M and ed <= SNAP_M:
            path = route_on_graph(g, s, e, guide)

        if path and len(path) >= 2:
            # Reject a match that wanders far from the guide's own length.
            # Dijkstra can find a legal but absurd detour through the bridged
            # graph; the GPS guide is the better answer in that case.
            road_pts = [coords[nid] for nid in path]
            road_m = _length_m(road_pts)
            guide_m = _length_m(guide)
            if guide_m > 0 and road_m <= MAX_DETOUR * guide_m:
                for lon, lat in road_pts:
                    out_xy.extend((lon, lat))
                matched += 1
            else:
                for lon, lat in guide:
                    out_xy.extend((lon, lat))
        else:
            for lon, lat in guide:
                out_xy.extend((lon, lat))
        out_off[i + 1] = len(out_xy) // 2

        if (i + 1) % 1000 == 0:
            print(f"  {i + 1}/{n_pairs} matched={matched}")

    (OUT / "geom_from.bin").write_bytes(out_from.tobytes())
    (OUT / "geom_to.bin").write_bytes(out_to.tobytes())
    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"pairs: {n_pairs}  matched: {matched} ({matched / max(1, n_pairs) * 100:.1f}%)")
    print(f"points: {len(out_xy) // 2}  bytes: {(OUT / 'geom_xy.bin').stat().st_size / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
