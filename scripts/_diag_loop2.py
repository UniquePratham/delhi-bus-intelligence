"""Print the exact coordinates around a detected loop."""
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
names = dict(con.execute("select station_id, name from stations").fetchall())


def metres(a, b):
    kx = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540)


# Which pair does index 80..92 belong to?
idx = 0
for a, b in zip(ids, ids[1:]):
    g = geom.get((a, b))
    if not g:
        continue
    n = len(g)
    if idx <= 92 < idx + n:
        print(f"loop is inside pair {a}->{b} ({names.get(a)} -> {names.get(b)})")
        print(f"  pair has {n} points, loop at local index {80-idx}..{92-idx}")
        for k in range(max(0, 80 - idx - 2), min(n, 92 - idx + 3)):
            print(f"    [{k}] {g[k][0]:.6f}, {g[k][1]:.6f}")
        break
    idx += n
