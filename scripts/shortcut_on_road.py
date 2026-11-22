"""Remove spurs and zigzags by shortcutting, verified against real roads.

The router produces paths that occasionally step onto a side stub and back, or
wiggle laterally, because a shortest-path search on a dense road graph has many
near-equal options. On the map that reads as a random branch.

This post-processes the geometry files directly:

  For each point i, look ahead to a later point j. If the straight line from i
  to j is (a) shorter than the path between them and (b) stays on a real road
  for its whole length, replace the subpath with that line.

Condition (b) is what makes this safe: a shortcut across a block is rejected,
so the result can only ever be a road. Condition (a) guarantees the path gets
shorter, never longer, so it cannot invent a detour.

Runs on the binary files, so it is fast to iterate without re-routing.
"""
from __future__ import annotations

import argparse
import math
import pickle
from pathlib import Path

import numpy as np

from route_on_roads import CACHE, OUT

GRID = 0.01
# Raster resolution for the road mask. 4 m cells with a 10 m dilation make an
# "on road" test an O(1) array lookup instead of a segment query.
ROAD_CELL_M = 4.0
ROAD_TOL_M = 10.0


def build_road_index(g: dict):
    """Rasterised road mask for fast on-road tests.

    A per-point segment query is far too slow for millions of samples. Instead,
    paint every raw road edge into a boolean grid at ROAD_CELL_M resolution and
    dilate it by the tolerance. A point is "on a road" when its cell is set,
    which is an O(1) lookup.
    """
    coords = g["coords"]
    lats = [c[1] for c in coords]
    lons = [c[0] for c in coords]
    lat0, lat1 = min(lats), max(lats)
    lon0, lon1 = min(lons), max(lons)
    kx = 111320 * math.cos(math.radians((lat0 + lat1) / 2))
    # Cell size in degrees, roughly square on the ground.
    dlat = ROAD_CELL_M / 110540
    dlon = ROAD_CELL_M / kx
    nx = int((lon1 - lon0) / dlon) + 3
    ny = int((lat1 - lat0) / dlat) + 3
    mask = np.zeros((ny, nx), dtype=bool)

    def cell(lon, lat):
        return int((lat - lat0) / dlat) + 1, int((lon - lon0) / dlon) + 1

    # Paint each road edge as a line of cells.
    for u, v in g["road_edges"]:
        (x1, y1), (x2, y2) = coords[u], coords[v]
        r1, c1 = cell(x1, y1)
        r2, c2 = cell(x2, y2)
        steps = max(abs(r2 - r1), abs(c2 - c1), 1)
        for s in range(steps + 1):
            t = s / steps
            r = int(round(r1 + (r2 - r1) * t))
            c = int(round(c1 + (c2 - c1) * t))
            if 0 <= r < ny and 0 <= c < nx:
                mask[r, c] = True

    # Dilate by the tolerance so a point near a road counts as on it.
    rad = max(1, int(round(ROAD_TOL_M / ROAD_CELL_M)))
    if rad > 0:
        padded = np.pad(mask, rad, mode="constant", constant_values=False)
        out = np.zeros_like(mask)
        for dr in range(-rad, rad + 1):
            for dc in range(-rad, rad + 1):
                if dr * dr + dc * dc <= rad * rad:
                    out |= padded[
                        rad + dr : rad + dr + ny, rad + dc : rad + dc + nx
                    ]
        mask = out

    return {"mask": mask, "lat0": lat0, "lon0": lon0, "dlat": dlat, "dlon": dlon,
            "ny": ny, "nx": nx}


def on_road(idx, lon: float, lat: float) -> bool:
    r = int((lat - idx["lat0"]) / idx["dlat"]) + 1
    c = int((lon - idx["lon0"]) / idx["dlon"]) + 1
    if r < 0 or c < 0 or r >= idx["ny"] or c >= idx["nx"]:
        return False
    return bool(idx["mask"][r, c])


def seg_on_road(idx, a, b) -> bool:
    """True when every sample along a->b lies on the road mask."""
    kx = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    length = math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540)
    steps = max(2, int(length // ROAD_CELL_M))
    for k in range(steps + 1):
        t = k / steps
        if not on_road(idx, a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t):
            return False
    return True


def metres(a, b) -> float:
    kx = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540)


def shortcut(pts: list[tuple[float, float]], idx, window_m: float):
    """Greedy on-road shortcutting. Returns (new_pts, removed_count)."""
    if len(pts) < 3:
        return pts, 0
    out: list[tuple[float, float]] = [pts[0]]
    i = 0
    removed = 0
    n = len(pts)
    while i < n - 1:
        best_j = i + 1
        walked = 0.0
        j = i + 1
        while j < n:
            if j > i + 1:
                walked += metres(pts[j - 1], pts[j])
            if walked > window_m:
                break
            direct = metres(pts[i], pts[j])
            # Only accept a genuine shortcut: strictly shorter than the path.
            if direct < walked - 1.0 and seg_on_road(idx, pts[i], pts[j]):
                best_j = j
            j += 1
        if best_j > i + 1:
            removed += best_j - i - 1
        out.append(pts[best_j])
        i = best_j
    return out, removed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=float, default=400.0, help="max shortcut span, metres")
    args = ap.parse_args()

    g = pickle.loads(CACHE.read_bytes())
    print(f"building road mask from {len(g['road_edges'])} raw edges")
    idx = build_road_index(g)
    print(f"mask: {idx['ny']}x{idx['nx']} cells at {ROAD_CELL_M} m")

    froms = np.fromfile(OUT / "geom_from.bin", dtype=np.int32)
    tos = np.fromfile(OUT / "geom_to.bin", dtype=np.int32)
    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    out_off = np.zeros(len(froms) + 1, dtype=np.int32)
    out_xy: list[float] = []
    total_removed = 0
    pairs_changed = 0

    for i in range(len(froms)):
        raw = xy[offs[i] : offs[i + 1]]
        # Split on NaN gap sentinels; each run is simplified independently.
        runs: list[list[tuple[float, float]]] = []
        cur: list[tuple[float, float]] = []
        for lon, lat in raw:
            if np.isfinite(lon) and np.isfinite(lat):
                cur.append((float(lon), float(lat)))
            else:
                if len(cur) >= 2:
                    runs.append(cur)
                cur = []
        if len(cur) >= 2:
            runs.append(cur)

        before = sum(len(r) for r in runs)
        simplified: list[list[tuple[float, float]]] = []
        for r in runs:
            s, rem = shortcut(r, idx, args.window)
            total_removed += rem
            simplified.append(s)
        after = sum(len(r) for r in simplified)
        if after < before:
            pairs_changed += 1

        for k, r in enumerate(simplified):
            if k:
                out_xy.extend((float("nan"), float("nan")))
            for lon, lat in r:
                out_xy.extend((lon, lat))
        out_off[i + 1] = len(out_xy) // 2

        if (i + 1) % 2000 == 0:
            print(f"  {i + 1}/{len(froms)} removed={total_removed}")

    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"pairs changed: {pairs_changed} / {len(froms)}")
    print(f"points removed: {total_removed}")
    print(f"points now: {len(out_xy) // 2}  bytes: {(OUT / 'geom_xy.bin').stat().st_size / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
