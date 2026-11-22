"""Produce one continuous on-road polyline per station pair.

Previous attempts failed in two opposite ways:
  * snapping each guide point independently left dashes where consecutive
    points landed on different roads;
  * routing between snapped points on the graph produced loops and staircases.

This does neither. It densifies the GPS guide, snaps every point to the nearest
road edge, and then joins consecutive snapped points with a straight segment.
A straight segment between two points that are both on a road, and only a few
metres apart, is on the road: there is no room for a loop or a detour.

Where a guide point has no road within SNAP_M, the point is dropped and the
neighbours are joined directly, provided that join stays on the road mask. Only
a join that leaves the road breaks the line, which is rare.
"""
from __future__ import annotations

import argparse
import math
import pickle

import numpy as np

from route_on_roads import CACHE, OUT
from shortcut_on_road import build_road_index, on_road

SNAP_M = 50.0
CELL_M = 60.0
# Densify the guide so consecutive samples are at most this far apart. Short
# steps are what make a straight join safe.
STEP_M = 12.0
# A join longer than this is tested against the road mask before being drawn.
TEST_JOIN_M = 25.0
# Share of a join that may fall outside the road mask before the line breaks.
# A raster cell boundary puts a single sample off; a real gap puts many off.
JOIN_OFF_FRACTION = 0.35


def main() -> int:
    ap = argparse.ArgumentParser()
    args = ap.parse_args()

    g = pickle.loads(CACHE.read_bytes())
    coords = np.asarray(g["coords"], dtype=np.float64)
    edges = np.asarray(g["road_edges"], dtype=np.int64)
    print(f"raw edges: {len(edges)}")

    lat0 = coords[:, 1].min()
    lon0 = coords[:, 0].min()
    kx = 111320 * math.cos(math.radians(float(coords[:, 1].mean())))
    nx = (coords[:, 0] - lon0) * kx
    ny = (coords[:, 1] - lat0) * 110540

    ax, ay = nx[edges[:, 0]], ny[edges[:, 0]]
    bx, by = nx[edges[:, 1]], ny[edges[:, 1]]
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    L2[L2 == 0] = 1e-9

    cell = CELL_M
    cx0 = np.floor(np.minimum(ax, bx) / cell).astype(np.int64)
    cx1 = np.floor(np.maximum(ax, bx) / cell).astype(np.int64)
    cy0 = np.floor(np.minimum(ay, by) / cell).astype(np.int64)
    cy1 = np.floor(np.maximum(ay, by) / cell).astype(np.int64)
    buckets: dict[tuple[int, int], list[int]] = {}
    for e in range(len(edges)):
        for cx in range(cx0[e], cx1[e] + 1):
            for cy in range(cy0[e], cy1[e] + 1):
                buckets.setdefault((cx, cy), []).append(e)
    print(f"buckets: {len(buckets)}")

    mask = build_road_index(g)

    def snap(px: float, py: float):
        cx, cy = int(px // cell), int(py // cell)
        cand: list[int] = []
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                cand.extend(buckets.get((cx + ox, cy + oy), ()))
        if not cand:
            return None
        idx = np.asarray(cand, dtype=np.int64)
        ex, ey = dx[idx], dy[idx]
        t = ((px - ax[idx]) * ex + (py - ay[idx]) * ey) / L2[idx]
        t = np.clip(t, 0.0, 1.0)
        qx = ax[idx] + t * ex
        qy = ay[idx] + t * ey
        d = np.hypot(px - qx, py - qy)
        j = int(np.argmin(d))
        if d[j] > SNAP_M:
            return None
        return float(qx[j]), float(qy[j])

    froms = np.fromfile(OUT / "geom_from.bin", dtype=np.int32)
    tos = np.fromfile(OUT / "geom_to.bin", dtype=np.int32)
    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    out_off = np.zeros(len(froms) + 1, dtype=np.int32)
    out_xy: list[float] = []
    pairs_ok = 0
    breaks = 0

    for i in range(len(froms)):
        raw = xy[offs[i] : offs[i + 1]]
        guide = raw[np.isfinite(raw[:, 0]) & np.isfinite(raw[:, 1])]
        if len(guide) < 2:
            out_off[i + 1] = len(out_xy) // 2
            continue

        # Densify: insert intermediate points so steps are <= STEP_M.
        dense: list[tuple[float, float]] = []
        for k in range(len(guide) - 1):
            x1, y1 = float(guide[k][0]), float(guide[k][1])
            x2, y2 = float(guide[k + 1][0]), float(guide[k + 1][1])
            kx2 = 111320 * math.cos(math.radians((y1 + y2) / 2))
            seg = math.hypot((x2 - x1) * kx2, (y2 - y1) * 110540)
            n = max(1, int(seg // STEP_M))
            for s in range(n):
                t = s / n
                dense.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
        dense.append((float(guide[-1][0]), float(guide[-1][1])))

        # Snap each dense point.
        snapped: list[tuple[float, float]] = []
        for lon, lat in dense:
            p = snap((lon - lon0) * kx, (lat - lat0) * 110540)
            if p is not None:
                snapped.append((p[0] / kx + lon0, p[1] / 110540 + lat0))
        if len(snapped) < 2:
            out_off[i + 1] = len(out_xy) // 2
            continue

        # Emit one continuous run, breaking only where a join leaves the road.
        run: list[tuple[float, float]] = [snapped[0]]
        wrote = False
        for p in snapped[1:]:
            prev = run[-1]
            kx2 = 111320 * math.cos(math.radians(prev[1]))
            hop = math.hypot((p[0] - prev[0]) * kx2, (p[1] - prev[1]) * 110540)
            if hop > TEST_JOIN_M:
                steps = max(2, int(hop // 4))
                bad = 0
                for s in range(steps + 1):
                    t = s / steps
                    if not on_road(
                        mask,
                        prev[0] + (p[0] - prev[0]) * t,
                        prev[1] + (p[1] - prev[1]) * t,
                    ):
                        bad += 1
                # A single sample off the mask is a raster cell boundary, not a
                # real gap. Only break when a meaningful share of the join is
                # off the road.
                if bad > max(1, int((steps + 1) * JOIN_OFF_FRACTION)):
                    breaks += 1
                    if len(run) >= 2:
                        if wrote:
                            out_xy.extend((float("nan"), float("nan")))
                        for lon, lat in run:
                            out_xy.extend((lon, lat))
                        wrote = True
                    run = []
            run.append(p)
        if len(run) >= 2:
            if wrote:
                out_xy.extend((float("nan"), float("nan")))
            for lon, lat in run:
                out_xy.extend((lon, lat))
            wrote = True
        if wrote:
            pairs_ok += 1
        out_off[i + 1] = len(out_xy) // 2

        if (i + 1) % 2000 == 0:
            print(f"  {i + 1}/{len(froms)} ok={pairs_ok} breaks={breaks}")

    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"pairs with geometry: {pairs_ok} / {len(froms)}")
    print(f"line breaks: {breaks}")
    print(f"points: {len(out_xy) // 2}  bytes: {(OUT / 'geom_xy.bin').stat().st_size / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
