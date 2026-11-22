"""Dump the demo route exactly as the browser assembles it, and find loops."""
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

geom: dict[tuple[int, int], list[tuple[float, float]]] = {}
for i in range(len(froms)):
    pts = [(float(x), float(y)) for x, y in xy[offs[i] : offs[i + 1]]]
    if len(pts) >= 2:
        geom[(int(froms[i]), int(tos[i]))] = pts

con = duckdb.connect(REF, read_only=True)


def metres(a, b):
    kx = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540)


# Assemble 241DOWN the way the browser does.
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

runs: list[list[tuple[float, float]]] = []
cur: list[tuple[float, float]] = []
for a, b in zip(ids, ids[1:]):
    g = geom.get((a, b))
    if not g:
        if len(cur) >= 2:
            runs.append(cur)
        cur = []
        continue
    start = 0
    if cur:
        if metres(cur[-1], g[0]) <= 1.0:
            start = 1
        else:
            if len(cur) >= 2:
                runs.append(cur)
            cur = []
    for k in range(start, len(g)):
        p = g[k]
        if not (math.isfinite(p[0]) and math.isfinite(p[1])):
            if len(cur) >= 2:
                runs.append(cur)
            cur = []
            continue
        cur.append(p)
if len(cur) >= 2:
    runs.append(cur)

print(f"241DOWN: {len(ids)-1} pairs -> {len(runs)} drawn runs")
total = 0.0
for i, r in enumerate(runs):
    L = sum(metres(r[j], r[j + 1]) for j in range(len(r) - 1))
    total += L
    print(f"  run {i}: {len(r)} pts, {L:.0f} m, start={r[0][0]:.5f},{r[0][1]:.5f} end={r[-1][0]:.5f},{r[-1][1]:.5f}")

# Loop detection: does the path return within 25 m of an earlier point that is
# far away along the path?
print("\nloop candidates (returns within 25 m of a point >200 m earlier):")
loops = 0
for ri, r in enumerate(runs):
    for i in range(len(r)):
        for j in range(i + 1, len(r)):
            d = metres(r[i], r[j])
            if d > 25:
                continue
            # Distance along the path between i and j.
            along = sum(metres(r[k], r[k + 1]) for k in range(i, j))
            if along > 200:
                loops += 1
                if loops <= 10:
                    print(
                        f"  run {ri} idx {i}->{j}: closes to {d:.0f} m "
                        f"after {along:.0f} m of path"
                    )
print(f"total loop candidates: {loops}")
