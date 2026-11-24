"""Remove detour loops from routed paths.

The road graph is built from CARTO rendering tiles, which are not a routing
network: they have fragmented topology, duplicate carriageways and missing
links. A* therefore sometimes finds a path that leaves the direct corridor,
loops around a block and comes back - the Kailash Colony loop in the screenshot.

A loop is a stretch where the path returns close to a point it already visited
after travelling much further than the direct distance. This removes it by
replacing the loop with the direct connection, but only when that connection
stays on the road network.

This is a post-process over the geometry files, so it is fast to iterate.
"""
from __future__ import annotations

import argparse
import math
import pickle
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web-next" / "public" / "data"
CACHE = ROOT / "data" / "processed" / "bus_network.pkl"

# A loop closes when the path returns within this distance of an earlier point.
LOOP_CLOSE_M = 35.0
# ...after travelling at least this much further than the direct distance.
LOOP_MIN_DETOUR_M = 120.0


def metres(a, b) -> float:
    kx = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540)


def main() -> int:
    ap = argparse.ArgumentParser()
    args = ap.parse_args()

    net = pickle.loads(CACHE.read_bytes())
    coords = net["coords"]
    road_edges = net["road_edges"]

    # Grid of road segments for the on-road test.
    GRID = 0.002
    grid: dict[tuple[int, int], list[tuple[float, float, float, float]]] = {}
    for u, v in road_edges:
        (x1, y1), (x2, y2) = coords[u], coords[v]
        lo_x, hi_x = sorted((int(x1 / GRID), int(x2 / GRID)))
        lo_y, hi_y = sorted((int(y1 / GRID), int(y2 / GRID)))
        seg = (x1, y1, x2, y2)
        for gx in range(lo_x - 1, hi_x + 2):
            for gy in range(lo_y - 1, hi_y + 2):
                grid.setdefault((gx, gy), []).append(seg)

    def on_road(lon: float, lat: float, tol: float = 12.0) -> bool:
        cx, cy = int(lon / GRID), int(lat / GRID)
        kx = 111320 * math.cos(math.radians(lat))
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for x1, y1, x2, y2 in grid.get((gx, gy), ()):
                    px = (lon - x1) * kx
                    py = (lat - y1) * 110540
                    dx = (x2 - x1) * kx
                    dy = (y2 - y1) * 110540
                    L2 = dx * dx + dy * dy
                    t = 0.0 if L2 == 0 else max(0.0, min(1.0, (px * dx + py * dy) / L2))
                    if math.hypot(px - t * dx, py - t * dy) <= tol:
                        return True
        return False

    def link_on_road(a, b) -> bool:
        d = metres(a, b)
        steps = max(2, int(d // 8))
        for s in range(steps + 1):
            t = s / steps
            if not on_road(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t):
                return False
        return True

    froms = np.fromfile(OUT / "geom_from.bin", dtype=np.int32)
    tos = np.fromfile(OUT / "geom_to.bin", dtype=np.int32)
    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    out_off = np.zeros(len(froms) + 1, dtype=np.int32)
    out_xy: list[float] = []
    loops_removed = 0
    pairs_changed = 0

    for i in range(len(froms)):
        pts = [
            (float(x), float(y))
            for x, y in xy[offs[i] : offs[i + 1]]
            if np.isfinite(x) and np.isfinite(y)
        ]
        if len(pts) < 3:
            for lon, lat in pts:
                out_xy.extend((lon, lat))
            out_off[i + 1] = len(out_xy) // 2
            continue

        before = len(pts)
        kept: list[tuple[float, float]] = [pts[0]]
        for p in pts[1:]:
            # Look back for a point p closes onto.
            cut = -1
            walked = 0.0
            for j in range(len(kept) - 1, -1, -1):
                if j < len(kept) - 1:
                    walked += metres(kept[j], kept[j + 1])
                if walked > 2000:
                    break
                direct = metres(kept[j], p)
                if direct <= LOOP_CLOSE_M and walked - direct >= LOOP_MIN_DETOUR_M:
                    # Only cut the loop if the direct link is itself on a road.
                    if link_on_road(kept[j], p):
                        cut = j
                        break
            if cut >= 0:
                loops_removed += len(kept) - cut
                kept = kept[: cut + 1]
            kept.append(p)

        if len(kept) < before:
            pairs_changed += 1
        for lon, lat in kept:
            out_xy.extend((lon, lat))
        out_off[i + 1] = len(out_xy) // 2

    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"pairs changed: {pairs_changed} / {len(froms)}")
    print(f"loop points removed: {loops_removed}")
    print(f"points: {len(out_xy)//2}  bytes: {(OUT/'geom_xy.bin').stat().st_size/1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
