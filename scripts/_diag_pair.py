"""Dump one pair's full geometry to see the loop shape."""
from __future__ import annotations

import math

import numpy as np

OUT = "web-next/public/data"

froms = np.fromfile(f"{OUT}/geom_from.bin", dtype=np.int32)
tos = np.fromfile(f"{OUT}/geom_to.bin", dtype=np.int32)
offs = np.fromfile(f"{OUT}/geom_off.bin", dtype=np.int32)
xy = np.fromfile(f"{OUT}/geom_xy.bin", dtype=np.float32).reshape(-1, 2)

target = (1756, 1758)
for i in range(len(froms)):
    if (int(froms[i]), int(tos[i])) == target:
        pts = xy[offs[i] : offs[i + 1]]
        print(f"pair {target}: {len(pts)} points")
        for k, (lon, lat) in enumerate(pts):
            print(f"  [{k:3d}] {lon:.6f}, {lat:.6f}")
        break
