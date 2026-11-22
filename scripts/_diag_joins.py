"""Measure the straight joins that cut across buildings."""
from __future__ import annotations

import math

import numpy as np

OUT = "web-next/public/data"

offs = np.fromfile(f"{OUT}/geom_off.bin", dtype=np.int32)
xy = np.fromfile(f"{OUT}/geom_xy.bin", dtype=np.float32).reshape(-1, 2)


def metres(a, b):
    kx = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540)


joins = []
for i in range(len(offs) - 1):
    seg = xy[offs[i] : offs[i + 1]]
    for j in range(len(seg) - 1):
        a, b = seg[j], seg[j + 1]
        if not (np.isfinite(a[0]) and np.isfinite(b[0])):
            continue
        d = metres((float(a[0]), float(a[1])), (float(b[0]), float(b[1])))
        if d > 20:
            joins.append(d)

arr = np.array(joins)
print(f"joins longer than 20 m: {len(arr)}")
if len(arr):
    print(f"  median {np.median(arr):.0f} m  p90 {np.percentile(arr,90):.0f}  max {arr.max():.0f}")
    for t in (30, 50, 100, 200):
        print(f"  share > {t} m: {(arr > t).mean()*100:.1f}%")
