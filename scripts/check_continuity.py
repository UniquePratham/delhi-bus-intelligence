"""Verify the demo routes are continuous and on roads."""
from __future__ import annotations

import math

import duckdb
import numpy as np

REF = "data/processed/reference.duckdb"
OUT = "web-next/public/data"

froms = np.fromfile(f"{OUT}/geom_from.bin", dtype=np.int32)
tos = np.fromfile(f"{OUT}/geom_to.bin", dtype=np.int32)
offs = np.fromfile(f"{OUT}/geom_off.bin", dtype=np.int32)
xy = np.fromfile(f"{OUT}/geom_xy.bin", dtype=np.float32).reshape(-1, 2)

geom = {}
for i in range(len(froms)):
    pts = xy[offs[i] : offs[i + 1]]
    pts = pts[np.isfinite(pts[:, 0])]
    if len(pts) >= 2:
        geom[(int(froms[i]), int(tos[i]))] = pts

con = duckdb.connect(REF, read_only=True)


def metres(a, b):
    kx = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540)


for route in ("241DOWN", "274AUP", "930DOWN"):
    rid = con.execute(
        "select route_id from routes where route_name=? limit 1", [route]
    ).fetchone()[0]
    ids = [
        r[0]
        for r in con.execute(
            "select station_id from station_patterns where route_id=? order by stop_sequence",
            [rid],
        ).fetchall()
    ]
    breaks = missing = 0
    worst = 0.0
    prev = None
    for a, b in zip(ids, ids[1:]):
        g = geom.get((a, b))
        if g is None:
            missing += 1
            prev = None
            continue
        if prev is not None:
            gap = metres(
                (float(prev[0]), float(prev[1])), (float(g[0][0]), float(g[0][1]))
            )
            if gap > 1:
                breaks += 1
                worst = max(worst, gap)
        prev = g[-1]
    print(
        f"{route}: {len(ids)-1} pairs, missing={missing}, "
        f"breaks={breaks}, worst={worst:.0f} m"
    )
