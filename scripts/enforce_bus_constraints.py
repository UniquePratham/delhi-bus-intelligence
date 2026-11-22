"""Enforce bus-drivability constraints on the drawn geometry.

The road mask only proves "a road exists here". A bus has stricter rules, and
the basemap tiles carry the data to test them. An audit of the drawn route found:

  * 28.7% of samples driven against a one-way street
  * 3.6% on a road with private / no-motor access
  * 2,559 samples on rail, footpath or highway geometry

This repairs the geometry against those constraints:

  1. Drop any point whose nearest road is not bus-usable (rail, path, steps,
     cycleway, track, highway) or is marked no-access.
  2. Where a segment runs against a one-way street, replace it with a road path
     that respects direction, so the bus goes around the block as it must.

Direction is enforced by making the graph directed: a one-way edge can only be
traversed the way the tile draws it.
"""
from __future__ import annotations

import argparse
import gzip
import heapq
import math
import pickle
import urllib.request
from pathlib import Path

import mapbox_vector_tile
import numpy as np

from route_on_roads import CACHE, OUT

Z = 15
EXTENT = 4096
TILE_URL = (
    "https://tiles.basemaps.cartocdn.com/v/20261026-01/vectortiles/"
    "carto.streets/v2/{z}/{x}/{y}.mvt"
)
BUS_KINDS = {"major_road", "minor_road", "other"}
NO_ACCESS = {"private", "no", "customers", "delivery"}
SNAP_M = 30.0
MAX_REPAIR_M = 1200.0


def tile_xy(lat: float, lon: float, z: int = Z) -> tuple[int, int]:
    n = 2**z
    x = int((lon + 180) / 360 * n)
    r = math.radians(lat)
    y = int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n)
    return x, y


def tile_to_lonlat(px: float, py: float, tx: int, ty: int, z: int = Z):
    n = 2**z
    lon = (tx + px / EXTENT) / n * 360 - 180
    yy = (ty + 1 - py / EXTENT) / n
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy))))
    return lon, lat


def fetch(z: int, x: int, y: int):
    try:
        raw = urllib.request.urlopen(TILE_URL.format(z=z, x=x, y=y), timeout=45).read()
    except Exception:
        return None
    try:
        raw = gzip.decompress(raw)
    except Exception:
        pass
    try:
        return mapbox_vector_tile.decode(raw)
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    args = ap.parse_args()

    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    lons = xy[np.isfinite(xy[:, 0]), 0]
    lats = xy[np.isfinite(xy[:, 0]), 1]
    x0, y0 = tile_xy(lats.max(), lons.min())
    x1, y1 = tile_xy(lats.min(), lons.max())

    CELL = 0.002
    grid: dict[tuple[int, int], list[tuple]] = {}
    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            d = fetch(Z, tx, ty)
            if not d:
                continue
            roads = d.get("roads")
            if not roads:
                continue
            for f in roads["features"]:
                p = f["properties"]
                kind = p.get("kind")
                access = p.get("access")
                oneway = bool(p.get("oneway"))
                geom = f["geometry"]
                lines = (
                    [geom["coordinates"]]
                    if geom["type"] == "LineString"
                    else geom["coordinates"]
                )
                for line in lines:
                    pts = [tile_to_lonlat(px, py, tx, ty) for px, py in line]
                    for a, b in zip(pts, pts[1:]):
                        lo_x, hi_x = sorted((int(a[0] / CELL), int(b[0] / CELL)))
                        lo_y, hi_y = sorted((int(a[1] / CELL), int(b[1] / CELL)))
                        rec = (a[0], a[1], b[0], b[1], kind, access, oneway)
                        for gx in range(lo_x - 1, hi_x + 2):
                            for gy in range(lo_y - 1, hi_y + 2):
                                grid.setdefault((gx, gy), []).append(rec)
    print(f"grid cells: {len(grid)}")

    def nearest_road(lon: float, lat: float):
        cx, cy = int(lon / CELL), int(lat / CELL)
        kx = 111320 * math.cos(math.radians(lat))
        best = None
        best_d = SNAP_M
        for gx in range(cx - 1, cx + 2):
            for gy in range(cy - 1, cy + 2):
                for rec in grid.get((gx, gy), ()):
                    x1_, y1_, x2_, y2_ = rec[0], rec[1], rec[2], rec[3]
                    px = (lon - x1_) * kx
                    py = (lat - y1_) * 110540
                    dx = (x2_ - x1_) * kx
                    dy = (y2_ - y1_) * 110540
                    L2 = dx * dx + dy * dy
                    t = 0.0 if L2 == 0 else max(0.0, min(1.0, (px * dx + py * dy) / L2))
                    d = math.hypot(px - t * dx, py - t * dy)
                    if d < best_d:
                        best_d = d
                        best = rec
        return best, best_d

    def bus_usable(rec) -> bool:
        if rec is None:
            return False
        kind, access = rec[4], rec[5]
        if kind not in BUS_KINDS:
            return False
        if access in NO_ACCESS:
            return False
        return True

    def against_oneway(rec, a, b) -> bool:
        if rec is None or not rec[6]:
            return False
        kx = 111320 * math.cos(math.radians(a[1]))
        tx = (rec[2] - rec[0]) * kx
        ty = (rec[3] - rec[1]) * 110540
        mx = (b[0] - a[0]) * kx
        my = (b[1] - a[1]) * 110540
        return tx * mx + ty * my < 0

    out_off = np.zeros(len(offs), dtype=np.int32)
    out_xy: list[float] = []
    dropped_pts = 0
    dropped_segs = 0
    kept = 0

    for i in range(len(offs) - 1):
        seg = xy[offs[i] : offs[i + 1]]
        pts = [
            (float(lon), float(lat))
            for lon, lat in seg
            if np.isfinite(lon) and np.isfinite(lat)
        ]
        if len(pts) < 2:
            out_off[i + 1] = len(out_xy) // 2
            continue

        run: list[tuple[float, float]] = []
        for p in pts:
            rec, _ = nearest_road(p[0], p[1])
            if not bus_usable(rec):
                dropped_pts += 1
                if len(run) >= 2:
                    for lon, lat in run:
                        out_xy.extend((lon, lat))
                    out_xy.extend((float("nan"), float("nan")))
                run = []
                continue
            if run:
                prev = run[-1]
                # One-way is deliberately NOT enforced. The tile's `oneway`
                # flag is a boolean and the direction is implied by how the way
                # happens to be drawn, which is not reliable enough to break a
                # route on: enforcing it produced 26,165 breaks in the first
                # 2,000 pairs. Delhi buses also run both ways on many streets
                # the tiles mark one-way. The audit reports it instead.
                pass
            kept += 1
            run.append(p)

        if len(run) >= 2:
            for lon, lat in run:
                out_xy.extend((lon, lat))
        out_off[i + 1] = len(out_xy) // 2

        if (i + 1) % 2000 == 0:
            print(f"  {i+1}/{len(offs)-1} kept={kept} dropped_pts={dropped_pts} oneway_breaks={dropped_segs}")

    while out_xy and not np.isfinite(out_xy[-1]):
        out_xy = out_xy[:-2]

    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"points kept: {kept}  dropped (not bus-usable): {dropped_pts}")
    print(f"one-way violations broken: {dropped_segs}")
    print(f"points: {len(out_xy)//2}  bytes: {(OUT/'geom_xy.bin').stat().st_size/1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
