"""Match the GPS guide onto the road graph, point to point.

Snapping each guide point independently leaves gaps: two consecutive points can
land on different roads, and the join between them is not on a road, so the line
breaks into dashes.

The fix is to connect them. For each consecutive pair of snapped nodes, find the
shortest path *on the road graph*. Because consecutive guide points are only
20-50 m apart, that path is a handful of nodes, so it cannot loop or detour -
the failure mode of routing between distant stations.

Result: one continuous polyline per pair, every vertex on a road.
"""
from __future__ import annotations

import argparse
import heapq
import math
import pickle

import numpy as np

from route_on_roads import CACHE, OUT

SNAP_M = 60.0
# Consecutive guide points are close; cap the search so a bad snap cannot
# produce a long detour.
MAX_LINK_M = 400.0


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

    # Grid of nodes for nearest-node lookup.
    CELL = 60.0
    buckets: dict[tuple[int, int], list[int]] = {}
    for i in range(len(coords)):
        buckets.setdefault((int(nx[i] // CELL), int(ny[i] // CELL)), []).append(i)
    print(f"buckets: {len(buckets)}")

    def nearest_node(px: float, py: float) -> tuple[int, float]:
        cx, cy = int(px // CELL), int(py // CELL)
        best, best_d = -1, float("inf")
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                for i in buckets.get((cx + ox, cy + oy), ()):
                    d = math.hypot(nx[i] - px, ny[i] - py)
                    if d < best_d:
                        best, best_d = i, d
        return best, best_d

    def link(a: int, b: int) -> list[int] | None:
        """Shortest road path between two nearby nodes, bounded by MAX_LINK_M."""
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
            if d > MAX_LINK_M:
                continue
            for v, w in adj.get(u, ()):
                nd = d + w
                if nd > MAX_LINK_M:
                    continue
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(
                        pq, (nd + math.hypot(nx[v] - gx, ny[v] - gy), nd, v)
                    )
        return None

    froms = np.fromfile(OUT / "geom_from.bin", dtype=np.int32)
    tos = np.fromfile(OUT / "geom_to.bin", dtype=np.int32)
    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    out_off = np.zeros(len(froms) + 1, dtype=np.int32)
    out_xy: list[float] = []
    pairs_ok = 0
    links_failed = 0
    snapped_pts = 0

    for i in range(len(froms)):
        raw = xy[offs[i] : offs[i + 1]]
        guide = raw[np.isfinite(raw[:, 0]) & np.isfinite(raw[:, 1])]
        if len(guide) < 2:
            out_off[i + 1] = len(out_xy) // 2
            continue

        # Snap each guide point to a road node.
        nodes: list[int] = []
        for lon, lat in guide:
            px = (float(lon) - lon0) * kx
            py = (float(lat) - lat0) * 110540
            n, d = nearest_node(px, py)
            if n >= 0 and d <= SNAP_M:
                nodes.append(n)
                snapped_pts += 1
        if len(nodes) < 2:
            out_off[i + 1] = len(out_xy) // 2
            continue

        # Walk the snapped nodes, connecting each consecutive pair on the graph.
        path: list[int] = [nodes[0]]
        for a, b in zip(nodes, nodes[1:]):
            if a == b:
                continue
            seg = link(a, b)
            if seg is None:
                links_failed += 1
                continue
            path.extend(seg[1:])

        if len(path) < 2:
            out_off[i + 1] = len(out_xy) // 2
            continue

        for n in path:
            out_xy.extend((float(coords[n][0]), float(coords[n][1])))
        pairs_ok += 1
        out_off[i + 1] = len(out_xy) // 2

        if (i + 1) % 2000 == 0:
            print(f"  {i + 1}/{len(froms)} ok={pairs_ok} failed_links={links_failed}")

    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"pairs with geometry: {pairs_ok} / {len(froms)}")
    print(f"guide points snapped: {snapped_pts}  links failed: {links_failed}")
    print(f"points: {len(out_xy) // 2}  bytes: {(OUT / 'geom_xy.bin').stat().st_size / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
