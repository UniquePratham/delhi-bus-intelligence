"""Check every drawn segment against real bus-drivability constraints.

The road mask only says "there is a road here". A bus has stricter requirements,
and the basemap tiles carry the data to test them:

  * kind        - a bus cannot use a footpath, steps, cycleway or track
  * access      - private / no-motor access is not drivable
  * oneway      - a one-way street cannot be driven against its direction
  * is_bridge / is_tunnel - allowed, but recorded so the UI can say so

This audits the geometry and reports, per constraint, how many segments fail.
It does not modify the files: the point is to know what is wrong before deciding
what to do about it.
"""
from __future__ import annotations

import argparse
import gzip
import math
import urllib.request
from collections import Counter
from pathlib import Path

import mapbox_vector_tile
import numpy as np

OUT = Path("web-next/public/data")
Z = 15
EXTENT = 4096
TILE_URL = (
    "https://tiles.basemaps.cartocdn.com/v/20261026-01/vectortiles/"
    "carto.streets/v2/{z}/{x}/{y}.mvt"
)

# Road kinds a bus may use. Everything else is excluded.
BUS_KINDS = {"major_road", "minor_road", "other"}
# Access values that forbid a bus.
NO_ACCESS = {"private", "no", "customers", "delivery"}


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
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    # Collect the tiles the geometry actually touches.
    lons = xy[np.isfinite(xy[:, 0]), 0]
    lats = xy[np.isfinite(xy[:, 0]), 1]
    print(f"geometry bbox: lon {lons.min():.4f}..{lons.max():.4f} lat {lats.min():.4f}..{lats.max():.4f}")

    x0, y0 = tile_xy(lats.max(), lons.min())
    x1, y1 = tile_xy(lats.min(), lons.max())
    print(f"tiles: {(x1-x0+1)*(y1-y0+1)}")

    # Build a grid of road segments with their constraint attributes.
    CELL = 0.002
    grid: dict[tuple[int, int], list[tuple]] = {}
    tiles = 0
    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            d = fetch(Z, tx, ty)
            if not d:
                continue
            tiles += 1
            roads = d.get("roads")
            if not roads:
                continue
            for f in roads["features"]:
                p = f["properties"]
                kind = p.get("kind")
                access = p.get("access")
                oneway = p.get("oneway")
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
    print(f"tiles fetched: {tiles}  grid cells: {len(grid)}")

    def nearest_road(lon: float, lat: float):
        cx, cy = int(lon / CELL), int(lat / CELL)
        kx = 111320 * math.cos(math.radians(lat))
        best = None
        best_d = 25.0
        for gx in range(cx - 1, cx + 2):
            for gy in range(cy - 1, cy + 2):
                for rec in grid.get((gx, gy), ()):
                    x1, y1, x2, y2 = rec[0], rec[1], rec[2], rec[3]
                    px = (lon - x1) * kx
                    py = (lat - y1) * 110540
                    dx = (x2 - x1) * kx
                    dy = (y2 - y1) * 110540
                    L2 = dx * dx + dy * dy
                    t = 0.0 if L2 == 0 else max(0.0, min(1.0, (px * dx + py * dy) / L2))
                    d = math.hypot(px - t * dx, py - t * dy)
                    if d < best_d:
                        best_d = d
                        best = rec
        return best, best_d

    kinds = Counter()
    access_bad = 0
    oneway_against = 0
    no_road = 0
    samples = 0

    n_pairs = len(offs) - 1 if not args.limit else min(args.limit, len(offs) - 1)
    for i in range(n_pairs):
        seg = xy[offs[i] : offs[i + 1]]
        pts = [
            (float(lon), float(lat))
            for lon, lat in seg
            if np.isfinite(lon) and np.isfinite(lat)
        ]
        for a, b in zip(pts, pts[1:]):
            kx = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
            L = math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540)
            steps = max(1, int(L // 15))
            for s in range(steps + 1):
                t = s / steps
                lon = a[0] + (b[0] - a[0]) * t
                lat = a[1] + (b[1] - a[1]) * t
                samples += 1
                rec, dist = nearest_road(lon, lat)
                if rec is None:
                    no_road += 1
                    continue
                kind, access, oneway = rec[4], rec[5], rec[6]
                kinds[kind] += 1
                if kind not in BUS_KINDS:
                    pass
                if access in NO_ACCESS:
                    access_bad += 1
                if oneway:
                    # Direction of travel vs the tile's segment direction.
                    kx2 = 111320 * math.cos(math.radians(lat))
                    tx = (rec[2] - rec[0]) * kx2
                    ty = (rec[3] - rec[1]) * 110540
                    mx = (b[0] - a[0]) * kx2
                    my = (b[1] - a[1]) * 110540
                    if tx * mx + ty * my < 0:
                        oneway_against += 1

    print(f"\nsamples: {samples}")
    print(f"  no road within 25 m: {no_road} ({no_road/samples*100:.2f}%)")
    print(f"  non-bus road kind:   {sum(v for k,v in kinds.items() if k not in BUS_KINDS)}")
    print(f"  no-access road:      {access_bad} ({access_bad/samples*100:.2f}%)")
    print(f"  against one-way:     {oneway_against} ({oneway_against/samples*100:.2f}%)")
    print("\nroad kinds on the route:")
    for k, v in kinds.most_common():
        flag = "" if k in BUS_KINDS else "  <-- not bus-usable"
        print(f"  {k}: {v}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
