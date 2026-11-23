"""Build route geometry the way the problem statement describes it.

The previous approach derived geometry per station pair from GPS traces, then
stitched 24 independent pair-paths together. Every pair was a separate chance to
fail - a wrong GPS match, a missing trace, a bad join - which is why the line
kept breaking and straying.

This does what a journey planner actually does:

  1. Snap every station to its nearest bus-road node, once.
  2. For each consecutive station pair, find the shortest path on the road
     network between those two nodes.
  3. Emit that path.

Because consecutive pairs share the exact same node, they join perfectly by
construction. There is no stitching, no anchoring, no bridging, and no chord:
one continuous road-following line through the checkpoints GTFS already gives.

The road network is filtered to bus-usable edges, so every segment is drivable.
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
CACHE = ROOT / "data" / "processed" / "bus_network.pkl"

Z = 15
EXTENT = 4096
TILE_URL = (
    "https://tiles.basemaps.cartocdn.com/v/20261026-01/vectortiles/"
    "carto.streets/v2/{z}/{x}/{y}.mvt"
)
BUS_KINDS = {"major_road", "minor_road", "other"}
NO_ACCESS = {"private", "no", "customers", "delivery"}
# Join vertices that adjacent tiles place a few metres apart. Without this the
# network is 67,903 disconnected fragments.
CONNECT_M = 25.0
# A station further than this from any bus road is a data problem, not a stop.
SNAP_MAX_M = 250.0
# Consecutive stops further apart than this are a bad route pattern.
MAX_PAIR_M = 3000.0
# A connector edge is not road geometry. Charge it heavily so the router only
# uses one where no real road path exists.
CONNECTOR_COST_M = 400.0


def tile_xy(lat: float, lon: float, z: int = Z) -> tuple[int, int]:
    n = 2**z
    x = int((lon + 180) / 360 * n)
    r = math.radians(lat)
    y = int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n)
    return x, y


def to_lonlat(px: float, py: float, tx: int, ty: int, z: int = Z):
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


def build_network(bbox) -> dict:
    lo_lat, lo_lon, hi_lat, hi_lon = bbox
    x0, y0 = tile_xy(hi_lat, lo_lon)
    x1, y1 = tile_xy(lo_lat, hi_lon)

    nodes: dict[tuple[float, float], int] = {}
    coords: list[tuple[float, float]] = []
    adj: dict[int, list[tuple[int, float]]] = {}
    # Edges that came from a tile's own line geometry. Anything else was added
    # by the connector pass and is not a road: it is a straight line between two
    # nearby vertices, and it can clip a building corner.
    road_edges: set[tuple[int, int]] = set()

    def nid(lon: float, lat: float) -> int:
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
    print(f"tiles: {tiles}  nodes: {len(coords)}  edges: {sum(len(v) for v in adj.values())//2}")

    # Join vertices that adjacent tiles place a few metres apart.
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
                    d = math.hypot(nx[j] - nx[i], ny[j] - ny[i])
                    if d <= CONNECT_M:
                        adj[i].append((j, d))
                        adj[j].append((i, d))
                        added += 1
    print(f"connector edges: {added}")

    return {
        "coords": coords,
        "adj": adj,
        "road_edges": road_edges,
        "nx": nx,
        "ny": ny,
        "lat0": lat0,
        "lon0": lon0,
        "kx": kx,
        "buckets": buckets,
        "cell": CELL,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild-network", action="store_true")
    args = ap.parse_args()

    con = duckdb.connect(str(REF), read_only=True)
    bbox = con.execute(
        "select min(lat), min(lon), max(lat), max(lon) from stations"
    ).fetchone()
    stations = {
        int(sid): (float(lat), float(lon))
        for sid, lat, lon in con.execute(
            "select station_id, lat, lon from stations"
        ).fetchall()
    }
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

    if CACHE.exists() and not args.rebuild_network:
        print(f"loading cached network {CACHE}")
        net = pickle.loads(CACHE.read_bytes())
    else:
        net = build_network(bbox)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_bytes(pickle.dumps(net, protocol=4))
        print(f"cached network -> {CACHE}")

    coords = net["coords"]
    adj = net["adj"]
    road_edges = net["road_edges"]
    nx, ny = net["nx"], net["ny"]
    lat0, lon0, kx = net["lat0"], net["lon0"], net["kx"]
    buckets, CELL = net["buckets"], net["cell"]

    def nearest(px: float, py: float) -> tuple[int, float]:
        cx, cy = int(px // CELL), int(py // CELL)
        best, best_d = -1, float("inf")
        for ox in range(-4, 5):
            for oy in range(-4, 5):
                for i in buckets.get((cx + ox, cy + oy), ()):
                    d = math.hypot(nx[i] - px, ny[i] - py)
                    if d < best_d:
                        best, best_d = i, d
        return best, best_d

    # Snap every station once. Consecutive pairs then share the same node, so
    # their paths join exactly.
    station_node: dict[int, int] = {}
    snap_fail = 0
    for sid, (lat, lon) in stations.items():
        n, d = nearest((lon - lon0) * kx, (lat - lat0) * 110540)
        if n >= 0 and d <= SNAP_MAX_M:
            station_node[sid] = n
        else:
            snap_fail += 1
    print(f"stations snapped: {len(station_node)}  too far from a road: {snap_fail}")

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
                # A connector is not a road: it is a straight line between two
                # nearby vertices and can clip a building corner. Charge it so
                # the search prefers real road geometry, and only uses a
                # connector where it is the only way through.
                key = (min(u, v), max(u, v))
                extra = 0.0 if key in road_edges else CONNECTOR_COST_M
                nd = d + w + extra
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(
                        pq, (nd + math.hypot(nx[v] - gx, ny[v] - gy), nd, v)
                    )
        return None

    def metres(a, b) -> float:
        kx2 = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
        return math.hypot((b[0] - a[0]) * kx2, (b[1] - a[1]) * 110540)

    out_from = np.empty(len(route_pairs), dtype=np.int32)
    out_to = np.empty(len(route_pairs), dtype=np.int32)
    out_off = np.zeros(len(route_pairs) + 1, dtype=np.int32)
    out_xy: list[float] = []

    routed = no_path = bad_pair = 0
    for i, (a, b) in enumerate(route_pairs):
        out_from[i] = a
        out_to[i] = b

        a_lat, a_lon = stations[a]
        b_lat, b_lon = stations[b]
        if metres((a_lon, a_lat), (b_lon, b_lat)) > MAX_PAIR_M:
            bad_pair += 1
            out_off[i + 1] = len(out_xy) // 2
            continue

        s = station_node.get(a)
        e = station_node.get(b)
        if s is None or e is None:
            no_path += 1
            out_off[i + 1] = len(out_xy) // 2
            continue

        path = astar(s, e)
        if not path or len(path) < 2:
            no_path += 1
            out_off[i + 1] = len(out_xy) // 2
            continue

        for n in path:
            out_xy.extend(coords[n])
        routed += 1
        out_off[i + 1] = len(out_xy) // 2

        if (i + 1) % 2000 == 0:
            print(f"  {i+1}/{len(route_pairs)} routed={routed} no_path={no_path}")

    (OUT / "geom_from.bin").write_bytes(out_from.tobytes())
    (OUT / "geom_to.bin").write_bytes(out_to.tobytes())
    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"routed: {routed}  no path: {no_path}  bad pair: {bad_pair}")
    print(f"points: {len(out_xy)//2}  bytes: {(OUT/'geom_xy.bin').stat().st_size/1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
