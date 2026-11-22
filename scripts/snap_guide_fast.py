"""Snap GPS guide polylines onto roads, vectorised with numpy.

The GPS median gives the corridor a bus actually drove, but individual bins are
noisy. The road graph gives exact road geometry but the router invents loops.

This keeps the GPS shape and projects each guide point onto the nearest raw road
edge. No routing decisions, so no loops and no staircases.

Vectorised: all raw road edges are held as numpy arrays and each guide point is
projected against the edges in its neighbourhood using array operations.
"""
from __future__ import annotations

import argparse
import math
import pickle

import numpy as np

from route_on_roads import CACHE, OUT
from shortcut_on_road import build_road_index, on_road

SNAP_M = 45.0
CELL_M = 60.0
# Skip over short stretches of guide points that have no road nearby rather
# than breaking the line there. A dash every few hundred metres reads as a
# broken route; a 60 m bridge over GPS noise does not.
MAX_BRIDGE_M = 60.0
# Only test a join against the road mask when the hop is long enough to be a
# real jump between different roads. Short hops are on the road by
# construction, and testing them broke the line into dashes.
BREAK_HOP_M = 80.0


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
    # Local metre coordinates for every node.
    nx = (coords[:, 0] - lon0) * kx
    ny = (coords[:, 1] - lat0) * 110540

    ax, ay = nx[edges[:, 0]], ny[edges[:, 0]]
    bx, by = nx[edges[:, 1]], ny[edges[:, 1]]
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    L2[L2 == 0] = 1e-9

    # Bucket edges by cell so a query only touches nearby edges.
    cell = CELL_M
    ex_min = np.minimum(ax, bx)
    ex_max = np.maximum(ax, bx)
    ey_min = np.minimum(ay, by)
    ey_max = np.maximum(ay, by)
    cx0 = np.floor(ex_min / cell).astype(np.int64)
    cx1 = np.floor(ex_max / cell).astype(np.int64)
    cy0 = np.floor(ey_min / cell).astype(np.int64)
    cy1 = np.floor(ey_max / cell).astype(np.int64)

    buckets: dict[tuple[int, int], list[int]] = {}
    for e in range(len(edges)):
        for cx in range(cx0[e], cx1[e] + 1):
            for cy in range(cy0[e], cy1[e] + 1):
                buckets.setdefault((cx, cy), []).append(e)
    print(f"buckets: {len(buckets)}")

    mask = build_road_index(g)

    froms = np.fromfile(OUT / "geom_from.bin", dtype=np.int32)
    tos = np.fromfile(OUT / "geom_to.bin", dtype=np.int32)
    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    out_off = np.zeros(len(froms) + 1, dtype=np.int32)
    out_xy: list[float] = []
    snapped = unsnapped = pairs_ok = 0

    for i in range(len(froms)):
        raw = xy[offs[i] : offs[i + 1]]
        guide = raw[np.isfinite(raw[:, 0]) & np.isfinite(raw[:, 1])]
        if len(guide) < 2:
            out_off[i + 1] = len(out_xy) // 2
            continue

        gx = (guide[:, 0].astype(np.float64) - lon0) * kx
        gy = (guide[:, 1].astype(np.float64) - lat0) * 110540

        snapped_pts: list[tuple[float, float] | None] = []
        for k in range(len(gx)):
            px, py = gx[k], gy[k]
            cx, cy = int(px // cell), int(py // cell)
            cand: list[int] = []
            for ox in (-1, 0, 1):
                for oy in (-1, 0, 1):
                    cand.extend(buckets.get((cx + ox, cy + oy), ()))
            if not cand:
                snapped_pts.append(None)
                unsnapped += 1
                continue
            idx = np.asarray(cand, dtype=np.int64)
            ex, ey = dx[idx], dy[idx]
            t = ((px - ax[idx]) * ex + (py - ay[idx]) * ey) / L2[idx]
            t = np.clip(t, 0.0, 1.0)
            qx = ax[idx] + t * ex
            qy = ay[idx] + t * ey
            d = np.hypot(px - qx, py - qy)
            j = int(np.argmin(d))
            if d[j] > SNAP_M:
                snapped_pts.append(None)
                unsnapped += 1
            else:
                snapped += 1
                snapped_pts.append((qx[j] / kx + lon0, qy[j] / 110540 + lat0))

        run: list[tuple[float, float]] = []
        wrote = False
        pending_gap = 0.0

        def flush():
            nonlocal run, wrote
            if len(run) >= 2:
                if wrote:
                    out_xy.extend((float("nan"), float("nan")))
                for lon, lat in run:
                    out_xy.extend((lon, lat))
                wrote = True
            run = []

        for p in snapped_pts:
            if p is None:
                # A guide point with no road nearby is usually GPS noise. Skip
                # it and keep the run going; only break once the gap grows past
                # MAX_BRIDGE_M, so the line does not fragment into dashes.
                pending_gap += 1
                if pending_gap * 20 > MAX_BRIDGE_M:
                    flush()
                    pending_gap = 0
                continue
            pending_gap = 0
            if run:
                prev = run[-1]
                kx2 = 111320 * math.cos(math.radians(prev[1]))
                hop = math.hypot((p[0] - prev[0]) * kx2, (p[1] - prev[1]) * 110540)
                # Only break on a long jump that leaves the road. Short hops
                # between two snapped points are always on the road by
                # construction, and testing them fragmented the line.
                if hop > BREAK_HOP_M:
                    steps = max(2, int(hop // 4))
                    ok = True
                    for s in range(steps + 1):
                        t = s / steps
                        if not on_road(
                            mask,
                            prev[0] + (p[0] - prev[0]) * t,
                            prev[1] + (p[1] - prev[1]) * t,
                        ):
                            ok = False
                            break
                    if not ok:
                        flush()
            run.append(p)
        flush()
        if wrote:
            pairs_ok += 1

        out_off[i + 1] = len(out_xy) // 2
        if (i + 1) % 2000 == 0:
            print(f"  {i + 1}/{len(froms)} snapped={snapped} unsnapped={unsnapped}")

    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"pairs with geometry: {pairs_ok} / {len(froms)}")
    print(f"snapped: {snapped}  unsnapped: {unsnapped}")
    print(f"points: {len(out_xy) // 2}  bytes: {(OUT / 'geom_xy.bin').stat().st_size / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
