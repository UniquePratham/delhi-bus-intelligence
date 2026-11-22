"""Build a bus-only road graph, then snap the GPS guide onto it.

The previous approach snapped to every road and then dropped points that landed
on a non-bus road. Dropping points is what created the gaps: 4,183 of them.

The correct order is the other way round. Filter the road network to edges a bus
may actually use, then snap to that filtered network. Every snapped point is
then bus-usable by construction, so nothing has to be dropped and the line stays
continuous.

Bus-usable means:
  * kind is major_road, minor_road or other (not rail, path, steps, cycleway,
    track or highway)
  * access is not private / no / customers / delivery
"""
from __future__ import annotations

import argparse
import gzip
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
SNAP_M = 60.0
CELL_M = 60.0


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
    finite = np.isfinite(xy[:, 0])
    lons, lats = xy[finite, 0], xy[finite, 1]
    x0, y0 = tile_xy(lats.max(), lons.min())
    x1, y1 = tile_xy(lats.min(), lons.max())

    # Collect bus-usable road segments only.
    segs: list[tuple[float, float, float, float]] = []
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
                if p.get("kind") not in BUS_KINDS:
                    continue
                if p.get("access") in NO_ACCESS:
                    continue
                geom = f["geometry"]
                lines = (
                    [geom["coordinates"]]
                    if geom["type"] == "LineString"
                    else geom["coordinates"]
                )
                for line in lines:
                    pts = [tile_to_lonlat(px, py, tx, ty) for px, py in line]
                    for a, b in zip(pts, pts[1:]):
                        segs.append((a[0], a[1], b[0], b[1]))
    print(f"tiles: {tiles}  bus-usable segments: {len(segs)}")

    arr = np.asarray(segs, dtype=np.float64)
    ax, ay, bx, by = arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3]
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    L2[L2 == 0] = 1e-9

    # Bucket the segments for fast nearest lookup.
    lat0 = float(lats.min())
    lon0 = float(lons.min())
    kx = 111320 * math.cos(math.radians(float(lats.mean())))
    nx = (ax - lon0) * kx
    ny = (ay - lat0) * 110540
    mx = (bx - lon0) * kx
    my = (by - lat0) * 110540
    cell = CELL_M
    cx0 = np.floor(np.minimum(nx, mx) / cell).astype(np.int64)
    cx1 = np.floor(np.maximum(nx, mx) / cell).astype(np.int64)
    cy0 = np.floor(np.minimum(ny, my) / cell).astype(np.int64)
    cy1 = np.floor(np.maximum(ny, my) / cell).astype(np.int64)
    buckets: dict[tuple[int, int], list[int]] = {}
    for e in range(len(arr)):
        for cx in range(cx0[e], cx1[e] + 1):
            for cy in range(cy0[e], cy1[e] + 1):
                buckets.setdefault((cx, cy), []).append(e)
    print(f"buckets: {len(buckets)}")

    def snap(px: float, py: float):
        cx, cy = int(px // cell), int(py // cell)
        cand: list[int] = []
        for ox in range(-4, 5):
            for oy in range(-4, 5):
                cand.extend(buckets.get((cx + ox, cy + oy), ()))
        if not cand:
            return None
        idx = np.asarray(cand, dtype=np.int64)
        ex, ey = dx[idx], dy[idx]
        t = ((px - nx[idx]) * ex + (py - ny[idx]) * ey) / L2[idx]
        t = np.clip(t, 0.0, 1.0)
        qx = nx[idx] + t * ex
        qy = ny[idx] + t * ey
        d = np.hypot(px - qx, py - qy)
        j = int(np.argmin(d))
        if d[j] > SNAP_M:
            return None
        return float(qx[j]), float(qy[j])

    def on_bus_road(lon: float, lat: float) -> bool:
        return snap((lon - lon0) * kx, (lat - lat0) * 110540) is not None

    def join_on_road(a, b) -> bool:
        """True when the straight join between two snapped points stays on a
        bus road. Used to decide whether a skipped point can be bridged."""
        kx2 = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
        length = math.hypot((b[0] - a[0]) * kx2, (b[1] - a[1]) * 110540)
        steps = max(2, int(length // 8))
        for s in range(steps + 1):
            t = s / steps
            if not on_bus_road(
                a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            ):
                return False
        return True

    out_off = np.zeros(len(offs), dtype=np.int32)
    out_xy: list[float] = []
    snapped = unsnapped = skipped_join = 0

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
        for lon, lat in pts:
            p = snap((lon - lon0) * kx, (lat - lat0) * 110540)
            if p is None:
                # No bus road nearby: skip the point and keep the run going.
                # Never break here - that is what created the gaps.
                unsnapped += 1
                continue
            snapped += 1
            cand = (p[0] / kx + lon0, p[1] / 110540 + lat0)
            if run and not join_on_road(run[-1], cand):
                # The join leaves the bus network. Rather than draw across a
                # block, keep the previous point and skip this one; the next
                # snapped point will be joined from there.
                skipped_join += 1
                continue
            run.append(cand)

        for lon, lat in run:
            out_xy.extend((lon, lat))
        out_off[i + 1] = len(out_xy) // 2

        if (i + 1) % 2000 == 0:
            print(f"  {i+1}/{len(offs)-1} snapped={snapped} unsnapped={unsnapped}")

    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"snapped: {snapped}  unsnapped: {unsnapped}  joins skipped: {skipped_join}")
    print(f"points: {len(out_xy)//2}  bytes: {(OUT/'geom_xy.bin').stat().st_size/1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
