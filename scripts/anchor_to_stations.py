"""Anchor every pair's geometry to its two stations.

The route breaks because a pair's polyline does not start at its origin station
or end at its destination station: the GPS guide was snapped to the nearest road,
which can be a few hundred metres from the stop itself. Consecutive pairs then
do not meet, and the line breaks.

This appends the station coordinates to each end of the pair's polyline, so
consecutive pairs share an exact endpoint and the line is continuous. The
station-to-road link is a short straight segment, which is honest: a bus stop is
on the roadside, and the gap is the walk from the stop to the carriageway.

Only pairs whose endpoint is further than ANCHOR_OVER_M from the station are
touched, so geometry that already meets its station is left alone.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import duckdb
import numpy as np

REF = Path("data/processed/reference.duckdb")
OUT = Path("web-next/public/data")
ANCHOR_OVER_M = 15.0
# Only anchor when the station is close to the polyline end. A long straight
# anchor cuts across whatever sits between the stop and the road; beyond this
# the gap is bridged on the road network instead.
ANCHOR_MAX_M = 120.0


def metres(a, b) -> float:
    kx = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540)


def main() -> int:
    ap = argparse.ArgumentParser()
    args = ap.parse_args()

    con = duckdb.connect(str(REF), read_only=True)
    stations = {
        int(sid): (float(lon), float(lat))
        for sid, lat, lon in con.execute(
            "select station_id, lat, lon from stations"
        ).fetchall()
    }

    froms = np.fromfile(OUT / "geom_from.bin", dtype=np.int32)
    tos = np.fromfile(OUT / "geom_to.bin", dtype=np.int32)
    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    out_off = np.zeros(len(froms) + 1, dtype=np.int32)
    out_xy: list[float] = []
    anchored_start = anchored_end = 0

    for i in range(len(froms)):
        a, b = int(froms[i]), int(tos[i])
        pts = [
            (float(x), float(y))
            for x, y in xy[offs[i] : offs[i + 1]]
            if np.isfinite(x) and np.isfinite(y)
        ]
        if len(pts) < 2:
            out_off[i + 1] = len(out_xy) // 2
            continue

        sa = stations.get(a)
        sb = stations.get(b)
        if sa:
            d = metres(pts[0], sa)
            if ANCHOR_OVER_M < d <= ANCHOR_MAX_M:
                pts.insert(0, sa)
                anchored_start += 1
        if sb:
            d = metres(pts[-1], sb)
            if ANCHOR_OVER_M < d <= ANCHOR_MAX_M:
                pts.append(sb)
                anchored_end += 1

        for lon, lat in pts:
            out_xy.extend((lon, lat))
        out_off[i + 1] = len(out_xy) // 2

    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"pairs anchored at start: {anchored_start}  at end: {anchored_end}")
    print(f"points: {len(out_xy)//2}  bytes: {(OUT/'geom_xy.bin').stat().st_size/1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
