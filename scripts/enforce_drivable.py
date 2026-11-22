"""Enforce that every drawn segment is drivable by a bus.

The route is built by snapping GPS points to roads, but where the GPS has a gap
the two snapped points are joined with a straight line. Those joins are long -
median 45 m, up to 2.6 km - and a straight line between two roads can pass
through a building.

This adds the missing constraint: a segment is only kept if a bus could drive
it. Each segment is sampled against the road mask; if too much of it is off the
road, it is replaced by the shortest path on the road graph between its two
endpoints. Because the endpoints are close, that path is short and cannot loop.

If no road path exists, the segment is dropped and the line breaks there. A
visible gap is honest; a line through a building is not.
"""
from __future__ import annotations

import argparse
import heapq
import math
import pickle

import numpy as np

from route_on_roads import CACHE, OUT
from shortcut_on_road import build_road_index, on_road

# A segment is drivable when at least this share of its samples is on a road.
DRIVABLE_SHARE = 0.9
# Sample spacing along a segment when testing drivability.
SAMPLE_M = 6.0
# Cap the repair search so a bad snap cannot produce a long detour.
MAX_REPAIR_M = 900.0


def main() -> int:
    ap = argparse.ArgumentParser()
    args = ap.parse_args()

    g = pickle.loads(CACHE.read_bytes())
    coords = np.asarray(g["coords"], dtype=np.float64)
    adj = g["adj"]
    print(f"nodes: {len(coords)}")

    lat0 = coords[:, 1].min()
    lon0 = coords[:, 0].min()
    kx = 111320 * math.cos(math.radians(float(coords[:, 1].mean())))
    nx = (coords[:, 0] - lon0) * kx
    ny = (coords[:, 1] - lat0) * 110540

    CELL = 60.0
    buckets: dict[tuple[int, int], list[int]] = {}
    for i in range(len(coords)):
        buckets.setdefault((int(nx[i] // CELL), int(ny[i] // CELL)), []).append(i)

    def nearest(px: float, py: float) -> tuple[int, float]:
        cx, cy = int(px // CELL), int(py // CELL)
        best, best_d = -1, float("inf")
        for ox in range(-3, 4):
            for oy in range(-3, 4):
                for i in buckets.get((cx + ox, cy + oy), ()):
                    d = math.hypot(nx[i] - px, ny[i] - py)
                    if d < best_d:
                        best, best_d = i, d
        return best, best_d

    def astar(a: int, b: int) -> list[int] | None:
        if a == b:
            return [a]
        gx, gy = nx[b], ny[b]
        dist = {a: 0.0}
        prev: dict[int, int] = {}
        pq = [(math.hypot(nx[a] - gx, ny[a] - gy), 0.0, a)]
        seen = set()
        while pq:
            _, d, u = heapq.heappop(pq)
            if u in seen:
                continue
            seen.add(u)
            if u == b:
                path = [u]
                while path[-1] != a:
                    path.append(prev[path[-1]])
                return path[::-1]
            if d > MAX_REPAIR_M:
                continue
            for v, w in adj.get(u, ()):
                nd = d + w
                if nd > MAX_REPAIR_M:
                    continue
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(
                        pq, (nd + math.hypot(nx[v] - gx, ny[v] - gy), nd, v)
                    )
        return None

    mask = build_road_index(g)

    def drivable(a, b) -> bool:
        kx2 = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
        length = math.hypot((b[0] - a[0]) * kx2, (b[1] - a[1]) * 110540)
        steps = max(2, int(length // SAMPLE_M))
        good = 0
        for s in range(steps + 1):
            t = s / steps
            if on_road(
                mask,
                a[0] + (b[0] - a[0]) * t,
                a[1] + (b[1] - a[1]) * t,
            ):
                good += 1
        return good / (steps + 1) >= DRIVABLE_SHARE

    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    out_off = np.zeros(len(offs), dtype=np.int32)
    out_xy: list[float] = []
    repaired = 0
    dropped = 0
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

        run: list[tuple[float, float]] = [pts[0]]
        for p in pts[1:]:
            prev = run[-1]
            if drivable(prev, p):
                kept += 1
                run.append(p)
                continue

            # Not drivable: replace the straight join with a road path.
            s, sd = nearest((prev[0] - lon0) * kx, (prev[1] - lat0) * 110540)
            e, ed = nearest((p[0] - lon0) * kx, (p[1] - lat0) * 110540)
            path = None
            if s >= 0 and e >= 0 and sd <= 60 and ed <= 60:
                path = astar(s, e)
            if path and len(path) >= 2:
                for n in path[1:]:
                    run.append((float(coords[n][0]), float(coords[n][1])))
                repaired += 1
            else:
                # No road route: break the line rather than draw through a
                # building.
                dropped += 1
                if len(run) >= 2:
                    for lon, lat in run:
                        out_xy.extend((lon, lat))
                    out_xy.extend((float("nan"), float("nan")))
                run = [p]

        if len(run) >= 2:
            for lon, lat in run:
                out_xy.extend((lon, lat))
        out_off[i + 1] = len(out_xy) // 2

        if (i + 1) % 2000 == 0:
            print(f"  {i + 1}/{len(offs)-1} kept={kept} repaired={repaired} dropped={dropped}")

    # Trim a trailing sentinel.
    while out_xy and not np.isfinite(out_xy[-1]):
        out_xy = out_xy[:-2]

    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"segments kept: {kept}  repaired on road: {repaired}  dropped: {dropped}")
    print(f"points: {len(out_xy) // 2}  bytes: {(OUT / 'geom_xy.bin').stat().st_size / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
