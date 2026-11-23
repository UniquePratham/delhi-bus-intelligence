"""Drop station pairs that are not real consecutive stops.

1,760 pairs have stations 27-37 km apart with only a few hundred metres of road
between them. Those are not geometry failures: the GTFS route pattern itself
lists two stops that are nowhere near each other, so no road path can exist.

A bus does not travel 30 km between consecutive stops. Any pair whose stations
are further apart than MAX_PAIR_M is a data error, and drawing anything for it
is worse than drawing nothing: it produces the long straight lines that cut
across the city.

This removes those pairs from the geometry file. The planner still uses the
route pattern for travel times; only the map geometry is affected.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import duckdb
import numpy as np

REF = Path("data/processed/reference.duckdb")
OUT = Path("web-next/public/data")
# Consecutive stops further apart than this are a data error, not a road. A
# Delhi bus stop spacing is typically 300-800 m; the 99th percentile on this
# network is about 2.5 km. Beyond 3 km the route pattern is wrong.
MAX_PAIR_M = 3000.0


def metres(a, b) -> float:
    kx = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540)


def main() -> int:
    ap = argparse.ArgumentParser()
    args = ap.parse_args()

    con = duckdb.connect(str(REF), read_only=True)
    stations = {
        int(sid): (float(lat), float(lon))
        for sid, lat, lon in con.execute(
            "select station_id, lat, lon from stations"
        ).fetchall()
    }

    froms = np.fromfile(OUT / "geom_from.bin", dtype=np.int32)
    tos = np.fromfile(OUT / "geom_to.bin", dtype=np.int32)
    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    keep_from: list[int] = []
    keep_to: list[int] = []
    out_off = np.zeros(len(froms) + 1, dtype=np.int32)
    out_xy: list[float] = []
    dropped = 0

    for i in range(len(froms)):
        a, b = int(froms[i]), int(tos[i])
        a_lat, a_lon = stations[a]
        b_lat, b_lon = stations[b]
        if metres((a_lon, a_lat), (b_lon, b_lat)) > MAX_PAIR_M:
            dropped += 1
            out_off[i + 1] = len(out_xy) // 2
            continue
        keep_from.append(a)
        keep_to.append(b)
        for lon, lat in xy[offs[i] : offs[i + 1]]:
            if np.isfinite(lon) and np.isfinite(lat):
                out_xy.extend((float(lon), float(lat)))
        out_off[i + 1] = len(out_xy) // 2

    (OUT / "geom_from.bin").write_bytes(np.asarray(keep_from, dtype=np.int32).tobytes())
    (OUT / "geom_to.bin").write_bytes(np.asarray(keep_to, dtype=np.int32).tobytes())
    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"pairs dropped as data errors (>{MAX_PAIR_M:.0f} m apart): {dropped}")
    print(f"pairs kept: {len(keep_from)}")
    print(f"points: {len(out_xy)//2}  bytes: {(OUT/'geom_xy.bin').stat().st_size/1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
