"""Fast sampled audit: are the drawn segments on the road mask?

Uses the same rasterised mask the shortcut pass used, so it is a consistency
check on that pass rather than an independent one. For an independent check,
audit_raw_roads.py measures against raw road edges.
"""
from __future__ import annotations

import math
import pickle

import numpy as np

from route_on_roads import CACHE, OUT
from shortcut_on_road import build_road_index, on_road

g = pickle.loads(CACHE.read_bytes())
idx = build_road_index(g)

froms = np.fromfile(OUT / "geom_from.bin", dtype=np.int32)
tos = np.fromfile(OUT / "geom_to.bin", dtype=np.int32)
offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

off = 0
total = 0
for i in range(len(froms)):
    pts = xy[offs[i] : offs[i + 1]]
    for j in range(len(pts) - 1):
        a, b = pts[j], pts[j + 1]
        if not (np.isfinite(a[0]) and np.isfinite(a[1])):
            continue
        if not (np.isfinite(b[0]) and np.isfinite(b[1])):
            continue
        kx = 111320 * math.cos(math.radians(float(a[1])))
        L = math.hypot(
            (float(b[0]) - float(a[0])) * kx,
            (float(b[1]) - float(a[1])) * 110540,
        )
        steps = max(1, int(L // 8))
        for s in range(steps + 1):
            t = s / steps
            lon = float(a[0]) + (float(b[0]) - float(a[0])) * t
            lat = float(a[1]) + (float(b[1]) - float(a[1])) * t
            total += 1
            if not on_road(idx, lon, lat):
                off += 1

print(f"samples: {total}")
print(f"off road mask: {off} ({off / total * 100:.3f}%)")
