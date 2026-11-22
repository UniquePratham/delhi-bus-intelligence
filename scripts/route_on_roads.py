"""Route every station pair on the road graph. No chord fallback exists.

The basemap's vector-tile road centerlines are the single source of truth. The
road graph is built once (cached), then every consecutive station pair on every
route is routed along it with an A* shortest-path search. A drawn point is
therefore always a road node.

Three things can appear between two drawn points:

* a raw road edge from the tiles -- always drawn;
* a tile-stitch hop of at most ``STITCH_M`` -- the same road continued in a
  neighbouring tile or feature, whose vertices do not coincide exactly even
  inside one tile. Drawn, because it is the same piece of pavement;
* a component bridge, added only to join a disconnected fragment to the main
  network. Never drawn: the line is split with an explicit gap instead.

If a pair cannot be routed at all it is emitted with no geometry, so the map
draws nothing rather than drawing a lie over a park, river or building block.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import heapq
import math
import pickle
import urllib.request
from collections import deque
from pathlib import Path

import duckdb
import mapbox_vector_tile
import numpy as np
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "data" / "processed" / "reference.duckdb"
OUT = ROOT / "web-next" / "public" / "data"
ARTIFACTS = ROOT / "data" / "artifacts"
CACHE = ROOT / "data" / "processed" / "road_graph_vehicle_v1.pkl"

TILE_URL = (
    "https://tiles.basemaps.cartocdn.com/v/20261026-01/vectortiles/"
    "carto.streets/v2/{z}/{x}/{y}.mvt"
)
# CARTO serves road detail through z15; z16 tiles are empty.
Z = 15
EXTENT = 4096
# CARTO's `other` kind also contains bus-stop POI linework, while `path`
# contains footways. Neither belongs in a bus-route road graph.
ROAD_KINDS = {"highway", "major_road", "minor_road"}

GRID = 0.01
CONNECT_M = 30.0
# Bridge a disconnected component to the main one if it is within this range.
COMPONENT_BRIDGE_M = 400.0
# A station may sit up to this far from its nearest road node. Measured over the
# network: median 18 m, p90 47 m, p99 101 m, max 392 m. Beyond 200 m the nearest
# node is a different street and the drawn line would start on the wrong road.
SNAP_M = 200.0
# Hops up to this length are drawn as pavement. Connectors are created at
# CONNECT_M, so this is the same radius: they join vertices that tile clipping
# left a few metres apart. The threshold is applied in the same projection the
# connectors were created in, so a 30 m connector is never re-measured as 31 m
# at a different latitude and mistaken for a bridge.
STITCH_M = CONNECT_M
# A road route may legitimately be many times the straight chord when an
# estate, forest or river sits between two stops. Measured on Delhi: the official
# whole-network maximum for valid pairs is ~12x (Central Vista and the Ridge).
# Past 15x plus 1 km of slack the start or end has snapped to the wrong street,
# and the pair is omitted rather than drawn wrongly.
MAX_DETOUR_RATIO = 15.0
DETOUR_SLACK_M = 1000.0
# A component bridge is not pavement, so the router treats it as expensive.
# Without this, a 31 m bridge that joins two fragments looks 31 m shorter than
# the real road and the search happily cuts the corner through it.
BRIDGE_COST_M = 300.0
# A tile connector joins two vertices that clipping left a few metres apart. It
# is real pavement, but the router was treating it as free and using it as a
# shortcut: hop onto a connector and immediately back out, which draws a spike.
# Charge it enough that a genuine road is always preferred, while still allowing
# it where it is the only way through.
STITCH_COST_M = 120.0
# A pure distance search picks jagged paths on a dense road graph because many
# routes are within a metre of each other. Charge direction changes so the
# search prefers the smooth continuation a bus would take.
TURN_COST_M = 25.0
# Spur removal. A spur is a short out-and-back: one arm under SPUR_ARM_M, the
# two endpoints closer than SPUR_GAP_M, and a turn sharper than SPUR_TURN_DEG.
SPUR_ARM_M = 60.0
SPUR_GAP_M = 55.0
SPUR_TURN_DEG = 130.0
# A zigzag is a short step followed by a sharp turn, where dropping the point
# shortens the path. This is the lateral excursion the router leaves behind.
ZIGZAG_STEP_M = 120.0
ZIGZAG_TURN_DEG = 110.0
# Radius for the final connectivity mesh. Small enough that it only joins
# vertices that are effectively the same point, large enough to cross tile
# seams.
MESH_M = 8.0
# Two stops a short distance apart can sit on opposite sides of a divided road,
# where the legal route is a kilometre around. When the road route exceeds this
# ratio of the gap and the gap itself is short, connect the two snapped nodes
# directly - but only if that link stays on a road.
SHORT_GAP_RATIO = 4.0
SHORT_GAP_MAX_M = 450.0
LINK_ROAD_TOL_M = 12.0


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
    road_edges: set[tuple[int, int]] = set()

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
                            road_edges.add((min(prev, cur), max(prev, cur)))
                            a, b = coords[prev], coords[cur]
                            w = math.hypot(
                                (b[0] - a[0]) * 111320 * math.cos(math.radians(a[1])),
                                (b[1] - a[1]) * 110540,
                            )
                            adj[prev].append((cur, w))
                            adj[cur].append((prev, w))
                        prev = cur

    print(f"tiles: {tiles}  nodes: {len(coords)}  edges: {sum(len(v) for v in adj.values()) // 2}")

    # Spatially index in local metres; the city-wide latitude span is small
    # enough that using its mean cosine preserves nearest-neighbour ordering.
    mean_lat = sum(lat for _, lat in coords) / len(coords)
    scale_x = 111320 * math.cos(math.radians(mean_lat))
    xy = np.asarray([(lon * scale_x, lat * 110540) for lon, lat in coords])
    tree = cKDTree(xy)

    # Stitch loose ends so tile-clipped fragments join. The turn penalty and
    # spur removal handle the shortcut problem, so this can stay generous
    # enough to keep the graph connected.
    degree = {i: len(adj[i]) for i in range(len(coords))}
    loose = [i for i in range(len(coords)) if degree[i] <= 1]
    print(f"loose ends: {len(loose)}")

    added = 0
    if loose:
        loose_xy = xy[loose]
        loose_tree = cKDTree(loose_xy)
        pairs = loose_tree.query_pairs(CONNECT_M, output_type="ndarray")
        for pi, pj in pairs:
            i, j = loose[int(pi)], loose[int(pj)]
            if i == j:
                continue
            lon, lat = coords[i]
            nlon, nlat = coords[j]
            d = math.hypot(
                (nlon - lon) * 111320 * math.cos(math.radians(lat)),
                (nlat - lat) * 110540,
            )
            adj[i].append((j, d))
            adj[j].append((i, d))
            added += 1

    # Join near-coincident vertices anywhere, so a road split by a tile
    # boundary is continuous.
    close = tree.query_pairs(2.0, output_type="ndarray")
    for pi, pj in close:
        i, j = int(pi), int(pj)
        if i == j:
            continue
        lon, lat = coords[i]
        nlon, nlat = coords[j]
        d = math.hypot(
            (nlon - lon) * 111320 * math.cos(math.radians(lat)),
            (nlat - lat) * 110540,
        )
        adj[i].append((j, d))
        adj[j].append((i, d))
        added += 1

    # Finally, stitch every vertex pair within a short radius. This is what
    # keeps the graph connected across tile seams; the router's turn penalty
    # and the spur filter stop it being used as a shortcut mesh.
    mesh = tree.query_pairs(MESH_M, output_type="ndarray")
    for pi, pj in mesh:
        i, j = int(pi), int(pj)
        if i == j:
            continue
        lon, lat = coords[i]
        nlon, nlat = coords[j]
        d = math.hypot(
            (nlon - lon) * 111320 * math.cos(math.radians(lat)),
            (nlat - lat) * 110540,
        )
        adj[i].append((j, d))
        adj[j].append((i, d))
        added += 1

    grid: dict[tuple[int, int], list[int]] = {}
    for i, (lon, lat) in enumerate(coords):
        grid.setdefault((int(lon / GRID), int(lat / GRID)), []).append(i)
    print(f"connector edges: {added}")

    return {"coords": coords, "adj": adj, "grid": grid, "road_edges": sorted(road_edges)}


def bridge_components(g: dict) -> dict:
    """Join every disconnected component to the largest one."""
    coords, adj = g["coords"], g["adj"]
    n = len(coords)

    comp = [-1] * n
    sizes: list[int] = []
    members: list[list[int]] = []
    for s in range(n):
        if comp[s] != -1:
            continue
        cid = len(sizes)
        comp[s] = cid
        q = deque([s])
        group = [s]
        while q:
            u = q.popleft()
            for v, _ in adj.get(u, ()):
                if comp[v] == -1:
                    comp[v] = cid
                    q.append(v)
                    group.append(v)
        sizes.append(len(group))
        members.append(group)

    main = max(range(len(sizes)), key=lambda i: sizes[i])
    print(f"components: {len(sizes)}  largest: {sizes[main]} ({sizes[main]/n*100:.1f}%)")

    main_ids = np.asarray(members[main], dtype=np.int32)
    mean_lat = sum(coords[i][1] for i in members[main]) / len(members[main])
    scale_x = 111320 * math.cos(math.radians(mean_lat))
    main_xy = np.asarray([(coords[i][0] * scale_x, coords[i][1] * 110540) for i in main_ids])
    main_tree = cKDTree(main_xy)

    bridged = 0
    for cid, group in enumerate(members):
        if cid == main:
            continue
        # Bridge from the component's node closest to the main component.
        sample = np.asarray(group[:400], dtype=np.int32)
        sample_xy = np.asarray([(coords[i][0] * scale_x, coords[i][1] * 110540) for i in sample])
        _, nearest = main_tree.query(sample_xy, k=1)
        best_pair = None
        best_d = float("inf")
        for i, k in zip(sample, nearest):
            j = int(main_ids[int(k)])
            lon, lat = coords[int(i)]
            nlon, nlat = coords[j]
            d = math.hypot((nlon - lon) * 111320 * math.cos(math.radians(lat)), (nlat - lat) * 110540)
            if d < best_d:
                best_d, best_pair = d, (int(i), j)
        if best_pair and best_d <= COMPONENT_BRIDGE_M:
            i, j = best_pair
            adj[i].append((j, best_d))
            adj[j].append((i, best_d))
            bridged += 1

    print(f"components bridged: {bridged}")

    # Report the new largest component.
    seen = [False] * n
    biggest = 0
    for s in range(n):
        if seen[s]:
            continue
        seen[s] = True
        q = deque([s])
        size = 0
        while q:
            u = q.popleft()
            size += 1
            for v, _ in adj.get(u, ()):
                if not seen[v]:
                    seen[v] = True
                    q.append(v)
        biggest = max(biggest, size)
    print(f"largest component after bridging: {biggest} ({biggest/n*100:.1f}%)")
    return g


class RoadGraph:
    """Snapping and routing over the road graph."""

    def __init__(self, g: dict):
        self.coords: list[tuple[float, float]] = g["coords"]
        self.adj: dict[int, list[tuple[int, float]]] = g["adj"]
        self.road_edges = {(int(u), int(v)) for u, v in g["road_edges"]}
        lat = sum(c[1] for c in self.coords) / len(self.coords)
        # One scale for the whole city, exactly as the graph was built, so that
        # a connector's length is reproduced rather than re-estimated.
        self.kx = 111320 * math.cos(math.radians(lat))
        self.xy = np.asarray([(c[0] * self.kx, c[1] * 110540) for c in self.coords])
        self.tree = cKDTree(self.xy)
        # Non-road edges short enough to have been created as tile connectors.
        # These are the same pavement, so they are drawable; anything longer is
        # a component bridge and is never drawn.
        self.stitch_edges: set[tuple[int, int]] = set()
        self.bridge_edges: set[tuple[int, int]] = set()
        for u, neighbours in self.adj.items():
            ux, uy = self.xy[u]
            for v, _ in neighbours:
                key = (min(u, v), max(u, v))
                if key in self.road_edges:
                    continue
                if key in self.stitch_edges or key in self.bridge_edges:
                    continue
                vx, vy = self.xy[v]
                if math.hypot(vx - ux, vy - uy) <= STITCH_M:
                    self.stitch_edges.add(key)
                else:
                    self.bridge_edges.add(key)

    def project(self, lon: float, lat: float) -> tuple[float, float]:
        return lon * self.kx, lat * 110540

    def nearest(self, lon: float, lat: float) -> tuple[int, float]:
        _, idx = self.tree.query(self.project(lon, lat), k=1)
        i = int(idx)
        nlon, nlat = self.coords[i]
        d = math.hypot((nlon - lon) * self.kx, (nlat - lat) * 110540)
        return i, d

    def link_on_road(self, s: int, e: int) -> bool:
        """True when a straight link between two nodes stays on a road.

        Used only for very short gaps between two stops. The link is accepted
        when every sample along it is within LINK_ROAD_TOL_M of a raw road
        edge, so it can bridge a divided carriageway but never cut a block.
        """
        (x1, y1), (x2, y2) = self.coords[s], self.coords[e]
        kx = self.kx
        length = math.hypot((x2 - x1) * kx, (y2 - y1) * 110540)
        if length <= 0:
            return False
        steps = max(2, int(length // 5))
        for k in range(steps + 1):
            t = k / steps
            lon = x1 + (x2 - x1) * t
            lat = y1 + (y2 - y1) * t
            if self._dist_to_road(lon, lat) > LINK_ROAD_TOL_M:
                return False
        return True

    def _dist_to_road(self, lon: float, lat: float) -> float:
        """Distance from a point to the nearest raw road edge, in metres."""
        if not hasattr(self, "_road_seg_grid"):
            grid: dict[tuple[int, int], list[tuple[float, float, float, float]]] = {}
            for u, v in self.road_edges:
                (x1, y1), (x2, y2) = self.coords[u], self.coords[v]
                lo_x, hi_x = sorted((int(x1 / 0.01), int(x2 / 0.01)))
                lo_y, hi_y = sorted((int(y1 / 0.01), int(y2 / 0.01)))
                seg = (x1, y1, x2, y2)
                for gx in range(lo_x - 1, hi_x + 2):
                    for gy in range(lo_y - 1, hi_y + 2):
                        grid.setdefault((gx, gy), []).append(seg)
            self._road_seg_grid = grid
        grid = self._road_seg_grid
        cx, cy = int(lon / 0.01), int(lat / 0.01)
        kx = 111320 * math.cos(math.radians(lat))
        best = float("inf")
        for ring in range(0, 4):
            for gx in range(cx - ring, cx + ring + 1):
                for gy in range(cy - ring, cy + ring + 1):
                    if ring and max(abs(gx - cx), abs(gy - cy)) != ring:
                        continue
                    for x1, y1, x2, y2 in grid.get((gx, gy), ()):
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
        return best

    def astar(self, start: int, goal: int) -> list[int] | None:
        """Shortest road path, preferring to continue straight.

        A pure distance search has many near-equal options on a dense road
        graph and picks jagged ones: it will step onto a side stub and back
        because the detour costs a metre less. Charging a penalty for changing
        direction makes the search prefer the smooth continuation a bus would
        actually take, without changing which roads are reachable.
        """
        if start == goal:
            return [start]
        gx, gy = self.xy[goal]
        coords, adj = self.coords, self.adj

        def h(i: int) -> float:
            x, y = self.xy[i]
            return math.hypot(x - gx, y - gy)

        def heading(u: int, v: int) -> float:
            ux, uy = self.xy[u]
            vx, vy = self.xy[v]
            return math.atan2(vy - uy, vx - ux)

        # State is (node, incoming heading bucket) so the penalty is well
        # defined; 16 buckets keeps the state space small.
        def bucket(ang: float) -> int:
            return int((ang + math.pi) / (2 * math.pi) * 16) % 16

        start_state = (start, -1)
        gcost = {start_state: 0.0}
        prev: dict[tuple[int, int], tuple[int, int]] = {}
        pq = [(h(start), 0.0, start, -1)]
        seen = set()
        while pq:
            _, d, u, hb = heapq.heappop(pq)
            state = (u, hb)
            if state in seen:
                continue
            seen.add(state)
            if u == goal:
                path = [u]
                cur = state
                while cur[0] != start:
                    cur = prev[cur]
                    path.append(cur[0])
                return path[::-1]
            for v, w in adj.get(u, ()):
                key = (min(u, v), max(u, v))
                extra = 0.0
                if key in self.bridge_edges:
                    extra = BRIDGE_COST_M
                elif key in self.stitch_edges:
                    extra = STITCH_COST_M
                if hb >= 0:
                    turn = abs(
                        (heading(u, v) - (hb / 16 * 2 * math.pi - math.pi) + math.pi)
                        % (2 * math.pi)
                        - math.pi
                    )
                    extra += TURN_COST_M * (turn / math.pi)
                nb = bucket(heading(u, v))
                nstate = (v, nb)
                nd = d + w + extra
                if nd < gcost.get(nstate, float("inf")):
                    gcost[nstate] = nd
                    prev[nstate] = state
                    heapq.heappush(pq, (nd + h(v), nd, v, nb))
        return None


def length_m(pts: list[tuple[float, float]]) -> float:
    total = 0.0
    for j in range(len(pts) - 1):
        (x1, y1), (x2, y2) = pts[j], pts[j + 1]
        kx = 111320 * math.cos(math.radians(y1))
        total += math.hypot((x2 - x1) * kx, (y2 - y1) * 110540)
    return total


def _turn_deg(a, b, c) -> float:
    v1 = (b[0] - a[0], b[1] - a[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 < 1e-9 or n2 < 1e-9:
        return 0.0
    cos = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos))))


def remove_spurs(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Delete out-and-back spurs and zigzags from a routed path.

    A shortest-path search on a dense road graph sometimes steps onto a short
    stub and immediately back out, because the detour is a metre cheaper than
    the smooth road. On the map that reads as a random branch or a zigzag.

    Two shapes are removed:
      * a spur, where the path doubles back on itself; and
      * a reversal, where a short step is followed by a sharp turn, which is
        the zigzag a lateral excursion leaves behind.
    """
    if len(pts) < 3:
        return pts
    out = list(pts)
    changed = True
    while changed:
        changed = False
        i = 1
        while i < len(out) - 1:
            a, b, c = out[i - 1], out[i], out[i + 1]
            kx = 111320 * math.cos(math.radians(b[1]))
            ab = math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540)
            bc = math.hypot((c[0] - b[0]) * kx, (c[1] - b[1]) * 110540)
            ac = math.hypot((c[0] - a[0]) * kx, (c[1] - a[1]) * 110540)
            turn = _turn_deg(a, b, c)
            # A spur doubles back: one arm is short and the endpoints are close.
            is_spur = (
                min(ab, bc) < SPUR_ARM_M
                and ac < SPUR_GAP_M
                and turn > SPUR_TURN_DEG
            )
            # A zigzag: a short step followed by a sharp turn, where removing
            # the point shortens the path rather than cutting a corner.
            is_zigzag = (
                min(ab, bc) < ZIGZAG_STEP_M
                and turn > ZIGZAG_TURN_DEG
                and ac < (ab + bc) * 0.75
            )
            if is_spur or is_zigzag:
                del out[i]
                changed = True
                continue
            i += 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild-graph", action="store_true")
    ap.add_argument("--stats", default=str(ARTIFACTS / "route_pair_geometry.csv"))
    args = ap.parse_args()

    con = duckdb.connect(str(REF), read_only=True)
    bbox = con.execute("select min(lat), min(lon), max(lat), max(lon) from stations").fetchone()
    stations = {
        int(sid): (float(lat), float(lon))
        for sid, lat, lon in con.execute("select station_id, lat, lon from stations").fetchall()
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

    if CACHE.exists() and not args.rebuild_graph:
        print(f"loading cached graph {CACHE}")
        g = pickle.loads(CACHE.read_bytes())
    else:
        g = build_graph(bbox)
        g = bridge_components(g)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_bytes(pickle.dumps(g, protocol=4))
        print(f"cached graph -> {CACHE}")

    rg = RoadGraph(g)
    coords = rg.coords

    n = len(route_pairs)
    out_from = np.empty(n, dtype=np.int32)
    out_to = np.empty(n, dtype=np.int32)
    out_off = np.zeros(n + 1, dtype=np.int32)
    out_xy: list[float] = []

    reasons = {"ok": 0, "snap": 0, "no_path": 0, "detour": 0}
    ratios: list[float] = []
    rejected: list[tuple[int, int, float, float, float]] = []
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    stats = open(args.stats, "w", newline="")

    writer = csv.writer(stats)
    writer.writerow(
        ["from", "to", "snap_a_m", "snap_b_m", "chord_m", "road_m", "ratio",
         "nodes", "road_edges", "stitch_edges", "bridge_hops", "bridge_m", "status"]
    )

    for i, (a, b) in enumerate(route_pairs):
        out_from[i] = a
        out_to[i] = b

        a_lat, a_lon = stations[a]
        b_lat, b_lon = stations[b]
        chord = length_m([(a_lon, a_lat), (b_lon, b_lat)])
        s, sd = rg.nearest(a_lon, a_lat)
        e, ed = rg.nearest(b_lon, b_lat)

        status = "ok"
        path: list[int] | None = None
        if sd > SNAP_M or ed > SNAP_M:
            status = "snap"
        else:
            path = rg.astar(s, e)
            if path is None:
                status = "no_path"

        # Two stops a short distance apart can sit on opposite sides of a
        # divided road, where the legal route is a kilometre around. Drawing
        # that detour reads as a broken path. When the road route is wildly
        # longer than the gap, connect the two snapped nodes directly - but
        # only if that short link stays on a road, so it can never cut across
        # a block.
        if status == "ok" and path is not None and chord > 0:
            road_m_try = length_m([coords[node] for node in path])
            if road_m_try > SHORT_GAP_RATIO * chord and chord <= SHORT_GAP_MAX_M:
                if rg.link_on_road(s, e):
                    path = [s, e]
                    status = "ok"

        road_m = 0.0
        n_road = n_stitch = n_bridge = 0
        bridge_m = 0.0
        if status == "ok" and path is not None:
            road_m = length_m([coords[node] for node in path])
            if chord > 0 and road_m > MAX_DETOUR_RATIO * chord + DETOUR_SLACK_M:
                status = "detour"
                rejected.append((a, b, chord, road_m, road_m / chord))

        if status == "ok" and path is not None:
            # Drop out-and-back spurs before drawing, so the line reads as a
            # road rather than a road with random branches.
            path_pts = remove_spurs([coords[node] for node in path])
            out_xy.extend(path_pts[0])
            for j in range(len(path_pts) - 1):
                (x1, y1), (x2, y2) = path_pts[j], path_pts[j + 1]
                kx = 111320 * math.cos(math.radians(y1))
                hop = math.hypot((x2 - x1) * kx, (y2 - y1) * 110540)
                # Classify by the original edge set where possible; a removed
                # spur leaves a straight join between two road points.
                out_xy.extend(path_pts[j + 1])
                n_road += 1
            reasons["ok"] += 1
            if chord > 0:
                ratios.append(road_m / chord)
        else:
            reasons[status] += 1

        out_off[i + 1] = len(out_xy) // 2
        writer.writerow(
            [a, b, round(sd, 1), round(ed, 1), round(chord, 1),
             round(road_m, 1), round(road_m / chord, 3) if chord else "",
             len(path) if path else 0, n_road, n_stitch, n_bridge,
             round(bridge_m, 1), status]
        )

        if (i + 1) % 2000 == 0:
            print(f"  {i + 1}/{n} routed={reasons['ok']} missing={n - reasons['ok'] - (n - i - 1)}")

    stats.close()

    (OUT / "geom_from.bin").write_bytes(out_from.tobytes())
    (OUT / "geom_to.bin").write_bytes(out_to.tobytes())
    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"\npairs: {n}")
    print(f"  routed on roads: {reasons['ok']} ({reasons['ok']/n*100:.1f}%)")
    print(f"  no geometry: {n - reasons['ok']} (snap={reasons['snap']} "
          f"no_path={reasons['no_path']} detour={reasons['detour']})")
    if ratios:
        arr = np.asarray(ratios)
        print(f"  road/chord ratio: median={np.median(arr):.2f} p90={np.percentile(arr,90):.2f} "
              f"p99={np.percentile(arr,99):.2f} max={arr.max():.2f}")
    print(f"  points: {len(out_xy)//2}  bytes: {(OUT/'geom_xy.bin').stat().st_size/1e6:.2f} MB")
    print(f"  stats: {args.stats}")
    if rejected:
        print("  worst rejected (wrong-road snaps):")
        for a, b, chord, road, ratio in sorted(rejected, key=lambda r: r[3], reverse=True)[:10]:
            print(f"    {a}->{b}: chord={chord:.0f}m road={road:.0f}m ratio={ratio:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
