"""Measure the hop distance at which joins fail."""
from __future__ import annotations

import math
import pickle

import numpy as np

from route_on_roads import CACHE, OUT
from shortcut_on_road import build_road_index, on_road

g = pickle.loads(CACHE.read_bytes())
mask = build_road_index(g)

froms = np.fromfile(f"{OUT}/geom_from.bin", dtype=np.int32)
offs = np.fromfile(f"{OUT}/geom_off.bin", dtype=np.int32)
xy = np.fromfile(f"{OUT}/geom_xy.bin", dtype=np.float32).reshape(-1, 2)

# Look at the first pair with a break and print the failing hop.
for i in range(len(froms)):
    pts = xy[offs[i] : offs[i + 1]]
    for j in range(len(pts) - 1):
        a, b = pts[j], pts[j + 1]
        if not (np.isfinite(a[0]) and np.isfinite(a[1])):
            continue
        if not (np.isfinite(b[0]) and np.isfinite(b[1])):
            continue
        kx = 111320 * math.cos(math.radians(float(a[1])))
        hop = math.hypot(
            (float(b[0]) - float(a[0])) * kx,
            (float(b[1]) - float(a[1])) * 110540,
        )
        if hop <= 25:
            continue
        steps = max(2, int(hop // 4))
        bad = 0
        for s in range(steps + 1):
            t = s / steps
            if not on_road(
                mask,
                float(a[0]) + (float(b[0]) - float(a[0])) * t,
                float(a[1]) + (float(b[1]) - float(a[1])) * t,
            ):
                bad += 1
        if bad:
            print(f"pair {i}: hop {hop:.0f} m, {bad}/{steps+1} samples off road")
            print(f"  a={a[0]:.6f},{a[1]:.6f}  b={b[0]:.6f},{b[1]:.6f}")
            break
    else:
        continue
    break
