"""Why does snapping drop whole stretches of the guide?"""
from __future__ import annotations

import math
import pickle

import numpy as np

from route_on_roads import CACHE, OUT

g = pickle.loads(CACHE.read_bytes())
coords = np.asarray(g["coords"], dtype=np.float64)
edges = np.asarray(g["road_edges"], dtype=np.int64)

lat0 = coords[:, 1].min()
lon0 = coords[:, 0].min()
kx = 111320 * math.cos(math.radians(float(coords[:, 1].mean())))
nx = (coords[:, 0] - lon0) * kx
ny = (coords[:, 1] - lat0) * 110540

ax, ay = nx[edges[:, 0]], ny[edges[:, 0]]
bx, by = nx[edges[:, 1]], ny[edges[:, 1]]
dx, dy = bx - ax, by - ay
L2 = dx * dx + dy * dy
L2[L2 == 0] = 1e-9

CELL = 60.0
cx0 = np.floor(np.minimum(ax, bx) / CELL).astype(np.int64)
cx1 = np.floor(np.maximum(ax, bx) / CELL).astype(np.int64)
cy0 = np.floor(np.minimum(ay, by) / CELL).astype(np.int64)
cy1 = np.floor(np.maximum(ay, by) / CELL).astype(np.int64)
buckets: dict[tuple[int, int], list[int]] = {}
for e in range(len(edges)):
    for cx in range(cx0[e], cx1[e] + 1):
        for cy in range(cy0[e], cy1[e] + 1):
            buckets.setdefault((cx, cy), []).append(e)

# Take a guide point in the middle of a gap and see how far the nearest road is.
offs = np.fromfile(f"{OUT}/geom_off.bin", dtype=np.int32)
xy = np.fromfile(f"{OUT}/geom_xy.bin", dtype=np.float32).reshape(-1, 2)

# Find a pair with a big gap and inspect the guide points around it.
for i in range(len(offs) - 1):
    seg = xy[offs[i] : offs[i + 1]]
    idx = [j for j in range(len(seg)) if not np.isfinite(seg[j][0])]
    if not idx:
        continue
    j = idx[0]
    if j == 0 or j + 1 >= len(seg):
        continue
    a = seg[j - 1]
    b = seg[j + 1]
    if not (np.isfinite(a[0]) and np.isfinite(b[0])):
        continue
    gap = math.hypot(
        (float(b[0]) - float(a[0])) * kx, (float(b[1]) - float(a[1])) * 110540
    )
    if gap < 300:
        continue
    print(f"pair {i}: gap {gap:.0f} m between {a} and {b}")
    # Sample the midpoint and find nearest road distance.
    mid_lon = (float(a[0]) + float(b[0])) / 2
    mid_lat = (float(a[1]) + float(b[1])) / 2
    px = (mid_lon - lon0) * kx
    py = (mid_lat - lat0) * 110540
    cx, cy = int(px // CELL), int(py // CELL)
    cand = []
    for ox in range(-3, 4):
        for oy in range(-3, 4):
            cand.extend(buckets.get((cx + ox, cy + oy), ()))
    if not cand:
        print("  no road edges within 180 m of the midpoint")
        break
    idxs = np.asarray(cand, dtype=np.int64)
    ex, ey = dx[idxs], dy[idxs]
    t = ((px - ax[idxs]) * ex + (py - ay[idxs]) * ey) / L2[idxs]
    t = np.clip(t, 0, 1)
    qx = ax[idxs] + t * ex
    qy = ay[idxs] + t * ey
    d = np.hypot(px - qx, py - qy)
    print(f"  nearest road to midpoint: {d.min():.0f} m")
    break
