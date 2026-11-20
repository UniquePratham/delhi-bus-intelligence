"""Decisive audit: measure the drawn route against RAW road edges only.

The previous audit measured distance to the routing graph, which contains the
artificial bridge edges used to connect components. That made it circular: a
line drawn along a bridge hop scored 0 m.

This measures against `road_edges` only - the edges that came from actual road
geometry in the tiles. A point on a bridge hop will show its true distance.
"""
from __future__ import annotations

import math
import pickle

import numpy as np

from route_on_roads import CACHE, OUT

GRID = 0.01


def main() -> int:
    g = pickle.loads(CACHE.read_bytes())
    coords = g["coords"]
    road_edges = g["road_edges"]
    print(f"raw road edges: {len(road_edges)}")

    # Bucket raw road segments on a grid.
    seg_grid: dict[tuple[int, int], list[tuple[float, float, float, float]]] = {}
    for u, v in road_edges:
        (x1, y1), (x2, y2) = coords[u], coords[v]
        lo_x, hi_x = sorted((int(x1 / GRID), int(x2 / GRID)))
        lo_y, hi_y = sorted((int(y1 / GRID), int(y2 / GRID)))
        seg = (x1, y1, x2, y2)
        for gx in range(lo_x - 1, hi_x + 2):
            for gy in range(lo_y - 1, hi_y + 2):
                seg_grid.setdefault((gx, gy), []).append(seg)
    print(f"grid cells: {len(seg_grid)}")

    def dist_to_road(lon: float, lat: float) -> float:
        cx, cy = int(lon / GRID), int(lat / GRID)
        kx = 111320 * math.cos(math.radians(lat))
        best = float("inf")
        for ring in range(0, 5):
            for gx in range(cx - ring, cx + ring + 1):
                for gy in range(cy - ring, cy + ring + 1):
                    if ring and max(abs(gx - cx), abs(gy - cy)) != ring:
                        continue
                    for x1, y1, x2, y2 in seg_grid.get((gx, gy), ()):
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

    froms = np.fromfile(OUT / "geom_from.bin", dtype=np.int32)
    tos = np.fromfile(OUT / "geom_to.bin", dtype=np.int32)
    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    dists: list[float] = []
    bad_pairs = 0
    worst: list[tuple[float, int, int]] = []

    for i in range(len(froms)):
        pts = xy[offs[i] : offs[i + 1]]
        if len(pts) == 0:
            continue
        mx = 0.0
        # Walk each drawn segment, not just its vertices: a segment that spans
        # a block would pass a vertex-only check.
        for j in range(len(pts) - 1):
            a, b = pts[j], pts[j + 1]
            if not (np.isfinite(a[0]) and np.isfinite(a[1])):
                continue
            if not (np.isfinite(b[0]) and np.isfinite(b[1])):
                continue
            kx = 111320 * math.cos(math.radians(float(a[1])))
            seg_m = math.hypot(
                (float(b[0]) - float(a[0])) * kx,
                (float(b[1]) - float(a[1])) * 110540,
            )
            steps = max(1, int(seg_m // 10))
            for s in range(steps + 1):
                t = s / steps
                lon = float(a[0]) + (float(b[0]) - float(a[0])) * t
                lat = float(a[1]) + (float(b[1]) - float(a[1])) * t
                d = dist_to_road(lon, lat)
                dists.append(d)
                if d > mx:
                    mx = d
        if mx > 25:
            bad_pairs += 1
            worst.append((mx, int(froms[i]), int(tos[i])))

    arr = np.array(dists)
    print()
    print(f"points measured: {len(arr)}")
    print(f"  median: {np.median(arr):.2f} m")
    print(f"  p90:    {np.percentile(arr, 90):.2f} m")
    print(f"  p99:    {np.percentile(arr, 99):.2f} m")
    print(f"  max:    {arr.max():.2f} m")
    print(f"  share > 25 m: {(arr > 25).mean() * 100:.2f}%")
    print(f"  share > 50 m: {(arr > 50).mean() * 100:.2f}%")
    print(f"pairs with any point > 25 m off a raw road: {bad_pairs} / {len(froms)}")
    worst.sort(reverse=True)
    print("worst pairs:")
    for d, a, b in worst[:10]:
        print(f"    {a}->{b}: {d:.0f} m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
