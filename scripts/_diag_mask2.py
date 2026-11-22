"""Inspect why joins fail the road mask."""
from __future__ import annotations

import math
import pickle

import numpy as np

from route_on_roads import CACHE
from shortcut_on_road import build_road_index, on_road

g = pickle.loads(CACHE.read_bytes())
mask = build_road_index(g)
print(f"mask {mask['ny']}x{mask['nx']} at cell {mask['dlat']*110540:.1f} m")

# Sample a few points that are definitely on a road node and check the mask.
coords = g["coords"]
hits = 0
misses = 0
for i in range(0, len(coords), 5000):
    lon, lat = coords[i]
    if on_road(mask, lon, lat):
        hits += 1
    else:
        misses += 1
print(f"road nodes on mask: {hits} hit, {misses} miss")

# Check the mask's own extent vs the node extent.
lats = [c[1] for c in coords]
lons = [c[0] for c in coords]
print(f"node lat range: {min(lats):.4f}..{max(lats):.4f}")
print(f"mask lat0={mask['lat0']:.4f} dlat={mask['dlat']:.6f} ny={mask['ny']}")
print(f"mask covers lat {mask['lat0']:.4f}..{mask['lat0']+mask['ny']*mask['dlat']:.4f}")
print(f"mask covers lon {mask['lon0']:.4f}..{mask['lon0']+mask['nx']*mask['dlon']:.4f}")
