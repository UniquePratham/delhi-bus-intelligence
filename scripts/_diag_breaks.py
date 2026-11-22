"""Find exactly where the drawn runs break and why."""
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
    pts = [(float(x), float(y)) for x, y in xy[offs[i] : offs[i + 1]]]
    if len(pts) >= 2:
        geom[(int(froms[i]), int(tos[i]))] = pts

con = duckdb.connect(REF, read_only=True)
names = dict(con.execute("select station_id, name from stations").fetchall())
rid = con.execute(
    "select route_id from routes where route_name='241DOWN' limit 1"
).fetchone()[0]
ids = [
    r[0]
    for r in con.execute(
        "select station_id from station_patterns where route_id=? order by stop_sequence",
        [rid],
    ).fetchall()
]


def metres(a, b):
    kx = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540)


prev_end = None
prev_pair = None
for a, b in zip(ids, ids[1:]):
    g = geom.get((a, b))
    if not g:
        print(f"  MISSING {a}->{b} ({names.get(a)} -> {names.get(b)})")
        prev_end = None
        prev_pair = None
        continue
    if prev_end is not None:
        gap = metres(prev_end, g[0])
        if gap > 1.0:
            print(
                f"  BREAK after {prev_pair} -> {a}->{b}: "
                f"gap {gap:.0f} m ({names.get(a)})"
            )
    prev_end = g[-1]
    prev_pair = (a, b)
