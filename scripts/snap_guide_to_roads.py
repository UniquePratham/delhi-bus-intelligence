"""Snap GPS-derived guide polylines onto the road network.

The GPS median gives the true corridor a bus drove, but individual bins are
noisy: at 64 bins, 43% of points sit off the road mask. The road graph gives
exact road geometry but the router invents loops and staircases.

This combines them: keep the GPS guide's shape, and for each guide point find
the nearest point on a real road edge, then emit that. The result follows the
road the bus actually drove, with no routing decisions to go wrong.

For each guide point:
  1. Find the nearest raw road edge within SNAP_M.
  2. Project the point onto that edge.
  3. Emit the projected point.

Consecutive guide points that snap to the same edge produce a clean run along
that edge. Where the snap jumps to a different edge, the two projected points
are joined only if the join stays on the road mask; otherwise the line breaks.
"""
from __future__ import annotations

import argparse
import math
import pickle
from pathlib import Path

import numpy as np

from route_on_roads import CACHE, OUT
from shortcut_on_road import build_road_index, on_road

GRID = 0.01
SNAP_M = 45.0


def build_edge_index(g: dict):
    """Grid of raw road edges for nearest-edge queries."""
    coords = g["coords"]
    grid: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for u, v in g["road_edges"]:
        (x1, y1), (x2, y2) = coords[u], coords[v]
        lo_x, hi_x = sorted((int(x1 / GRID), int(x2 / GRID)))
        lo_y, hi_y = sorted((int(y1 / GRID), int(y2 / GRID)))
        for gx in range(lo_x - 1, hi_x + 2):
            for gy in range(lo_y - 1, hi_y + 2):
                grid.setdefault((gx, gy), []).append((u, v))
    return grid


def snap_point(coords, grid, lon: float, lat: float):
    """Nearest point on a raw road edge, or None if nothing within SNAP_M."""
    cx, cy = int(lon / GRID), int(lat / GRID)
    kx = 111320 * math.cos(math.radians(lat))
    best = None
    best_d = SNAP_M
    for ring in range(0, 4):
        for gx in range(cx - ring, cx + ring + 1):
            for gy in range(cy - ring, cy + ring + 1):
                if ring and max(abs(gx - cx), abs(gy - cy)) != ring:
                    continue
                for u, v in grid.get((gx, gy), ()):
                    (x1, y1), (x2, y2) = coords[u], coords[v]
                    px = (lon - x1) * kx
                    py = (lat - y1) * 110540
                    dx = (x2 - x1) * kx
                    dy = (y2 - y1) * 110540
                    L2 = dx * dx + dy * dy
                    t = 0.0 if L2 == 0 else max(0.0, min(1.0, (px * dx + py * dy) / L2))
                    d = math.hypot(px - t * dx, py - t * dy)
                    if d < best_d:
                        best_d = d
                        best = (x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
        if best is not None and ring >= 1:
            break
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=float, default=400.0)
    args = ap.parse_args()

    g = pickle.loads(CACHE.read_bytes())
    coords = g["coords"]
    print(f"building edge index from {len(g['road_edges'])} raw edges")
    grid = build_edge_index(g)
    print(f"grid cells: {len(grid)}")
    mask = build_road_index(g)

    froms = np.fromfile(OUT / "geom_from.bin", dtype=np.int32)
    tos = np.fromfile(OUT / "geom_to.bin", dtype=np.int32)
    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    out_off = np.zeros(len(froms) + 1, dtype=np.int32)
    out_xy: list[float] = []
    snapped = 0
    unsnapped = 0
    pairs_ok = 0

    for i in range(len(froms)):
        raw = xy[offs[i] : offs[i + 1]]
        guide = [
            (float(lon), float(lat))
            for lon, lat in raw
            if np.isfinite(lon) and np.isfinite(lat)
        ]
        if len(guide) < 2:
            out_off[i + 1] = len(out_xy) // 2
            continue

        # Snap each guide point to the nearest road edge.
        snapped_pts: list[tuple[float, float] | None] = []
        for lon, lat in guide:
            p = snap_point(coords, grid, lon, lat)
            if p is None:
                unsnapped += 1
                snapped_pts.append(None)
            else:
                snapped += 1
                snapped_pts.append(p)

        # Emit runs of consecutive snapped points, breaking where a snap is
        # missing or where the join between two snapped points leaves the road.
        run: list[tuple[float, float]] = []
        wrote = False
        for k, p in enumerate(snapped_pts):
            if p is None:
                if len(run) >= 2:
                    if wrote:
                        out_xy.extend((float("nan"), float("nan")))
                    for lon, lat in run:
                        out_xy.extend((lon, lat))
                    wrote = True
                run = []
                continue
            if run:
                prev = run[-1]
                # Only join if the short hop stays on the road mask.
                kx = 111320 * math.cos(math.radians(prev[1]))
                hop = math.hypot((p[0] - prev[0]) * kx, (p[1] - prev[1]) * 110540)
                if hop > 1.0:
                    steps = max(2, int(hop // 4))
                    ok = True
                    for s in range(steps + 1):
                        t = s / steps
                        if not on_road(
                            mask,
                            prev[0] + (p[0] - prev[0]) * t,
                            prev[1] + (p[1] - prev[1]) * t,
                        ):
                            ok = False
                            break
                    if not ok:
                        if len(run) >= 2:
                            if wrote:
                                out_xy.extend((float("nan"), float("nan")))
                            for lon, lat in run:
                                out_xy.extend((lon, lat))
                            wrote = True
                        run = []
            run.append(p)
        if len(run) >= 2:
            if wrote:
                out_xy.extend((float("nan"), float("nan")))
            for lon, lat in run:
                out_xy.extend((lon, lat))
            wrote = True
        if wrote:
            pairs_ok += 1

        out_off[i + 1] = len(out_xy) // 2
        if (i + 1) % 2000 == 0:
            print(f"  {i + 1}/{len(froms)} snapped={snapped} unsnapped={unsnapped}")

    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"pairs with geometry: {pairs_ok} / {len(froms)}")
    print(f"guide points snapped: {snapped}, unsnapped: {unsnapped}")
    print(f"points now: {len(out_xy) // 2}  bytes: {(OUT / 'geom_xy.bin').stat().st_size / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
