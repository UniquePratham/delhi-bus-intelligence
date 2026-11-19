"""Independently measure how far every drawn geometry point sits from a road.

The audit deliberately does not reuse the router's path or its node set. It
rebuilds a reference from the raw vector-tile road centerlines only, cuts every
centerline into <=20 m pieces, and measures each drawn point against the nearest
piece. A chord across a block or a park shows up as tens of metres; a point on a
centerline shows up at a few metres (Float32 storage rounding only).
"""
from __future__ import annotations

import csv
import math
import pickle

import duckdb
import numpy as np
from scipy.spatial import cKDTree

from route_on_roads import ARTIFACTS, CACHE, OUT, REF

PIECE_M = 20.0
OFF_ROAD_M = 25.0


def build_reference(coords: list[tuple[float, float]], road_edges: np.ndarray):
    """Subdivide raw road edges into short pieces and index their midpoints."""
    mean_lat = sum(lat for _, lat in coords) / len(coords)
    kx = 111320 * math.cos(math.radians(mean_lat))
    c = np.asarray(coords, dtype=np.float64)
    a = c[road_edges[:, 0]]
    b = c[road_edges[:, 1]]

    seg_m = np.hypot((b[:, 0] - a[:, 0]) * kx, (b[:, 1] - a[:, 1]) * 110540)
    steps = np.maximum(1, np.ceil(seg_m / PIECE_M)).astype(np.int32)
    total = int(steps.sum())

    p0 = np.empty((total, 2))
    p1 = np.empty((total, 2))
    k = 0
    for i in range(len(a)):
        n = int(steps[i])
        t0 = np.arange(n) / n
        t1 = (np.arange(n) + 1) / n
        p0[k:k + n] = a[i] + (b[i] - a[i]) * t0[:, None]
        p1[k:k + n] = a[i] + (b[i] - a[i]) * t1[:, None]
        k += n

    xy0 = np.column_stack((p0[:, 0] * kx, p0[:, 1] * 110540))
    xy1 = np.column_stack((p1[:, 0] * kx, p1[:, 1] * 110540))
    tree = cKDTree((xy0 + xy1) / 2)
    print(f"reference: {len(road_edges)} road edges -> {total} pieces of <= {PIECE_M:.0f} m")
    return tree, xy0, xy1, kx


def dist_batch(points: np.ndarray, tree, xy0, xy1, kx: float) -> np.ndarray:
    xy = np.column_stack((points[:, 0] * kx, points[:, 1] * 110540))
    _, idx = tree.query(xy, k=8)
    best = np.full(len(xy), np.inf)
    for col in range(idx.shape[1]):
        j = idx[:, col]
        aa, bb = xy0[j], xy1[j]
        d = bb - aa
        den = np.einsum("ij,ij->i", d, d)
        t = np.clip(np.einsum("ij,ij->i", xy - aa, d) / np.maximum(den, 1e-12), 0, 1)
        proj = aa + d * t[:, None]
        off = np.sqrt(np.einsum("ij,ij->i", xy - proj, xy - proj))
        best = np.minimum(best, off)
    return best


def main() -> int:
    g = pickle.loads(CACHE.read_bytes())
    coords = g["coords"]
    if "road_edges" not in g:
        raise RuntimeError("graph lacks raw road-edge provenance; rebuild with --rebuild-graph")
    road_edges = np.asarray(g["road_edges"], dtype=np.int32)
    road_set = {(int(u), int(v)) for u, v in road_edges}
    tree, xy0, xy1, kx = build_reference(coords, road_edges)

    froms = np.fromfile(OUT / "geom_from.bin", dtype=np.int32)
    tos = np.fromfile(OUT / "geom_to.bin", dtype=np.int32)
    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    n = len(froms)
    if len(offs) != n + 1:
        raise RuntimeError(f"offset array has {len(offs)} entries for {n} pairs")

    node_by_xy = {
        (float(np.float32(lon)), float(np.float32(lat))): i
        for i, (lon, lat) in enumerate(coords)
    }

    sampled: list[np.ndarray] = []
    pair_max = np.full(n, np.inf)
    missing = 0
    bad_pairs = 0
    worst: list[tuple[int, int, float]] = []
    link_kind = {"road": 0, "stitch": 0, "foreign": 0}
    chord_like = 0

    # A chord would be a link joining the pair's own two station coordinates.
    # Exact test, 2 m tolerance, in the pair's direction or its reverse.
    con = duckdb.connect(str(REF), read_only=True)
    station_pt = {
        int(sid): (float(lon), float(lat))
        for sid, lat, lon in con.execute("select station_id, lat, lon from stations").fetchall()
    }

    def near(x: float, y: float, pt: tuple[float, float]) -> bool:
        return abs(x - pt[0]) * kx < 2.0 and abs(y - pt[1]) * 110540 < 2.0

    for i in range(n):
        pts = xy[offs[i]: offs[i + 1]]
        if len(pts) < 2:
            missing += 1
            continue
        sa = station_pt.get(int(froms[i]))
        sb = station_pt.get(int(tos[i]))
        seg_pts: list[np.ndarray] = []
        finite_any = False
        for j in range(len(pts) - 1):
            p, q = pts[j], pts[j + 1]
            if not (np.isfinite(p).all() and np.isfinite(q).all()):
                continue
            finite_any = True
            ax, ay = map(float, p)
            bx, by = map(float, q)
            if sa is not None and sb is not None:
                if (near(ax, ay, sa) and near(bx, by, sb)) or (
                    near(ax, ay, sb) and near(bx, by, sa)
                ):
                    chord_like += 1
            u = node_by_xy.get((float(p[0]), float(p[1])))
            v = node_by_xy.get((float(q[0]), float(q[1])))
            if u is not None and v is not None and (min(u, v), max(u, v)) in road_set:
                link_kind["road"] += 1
            elif u is not None and v is not None:
                link_kind["stitch"] += 1
            else:
                # Endpoints must be graph nodes; anything else is not road data.
                link_kind["foreign"] += 1
            seg_m = math.hypot(
                (bx - ax) * 111320 * math.cos(math.radians((ay + by) / 2)),
                (by - ay) * 110540,
            )
            steps = max(1, math.ceil(seg_m / 10.0))
            t = np.arange(steps + 1) / steps
            seg_pts.append(np.column_stack((ax + (bx - ax) * t, ay + (by - ay) * t)))
        if not finite_any or not seg_pts:
            # Only explicit gaps: nothing was drawn for this pair.
            missing += 1
            continue
        arr = np.vstack(seg_pts)
        d = dist_batch(arr, tree, xy0, xy1, kx)
        sampled.append(d)
        mx = float(d.max())
        pair_max[i] = mx
        if mx > OFF_ROAD_M:
            bad_pairs += 1
            worst.append((int(froms[i]), int(tos[i]), mx))

    all_d = np.concatenate(sampled)
    print(f"\npairs in geometry file: {n}")
    print(f"  with drawn geometry: {n - missing}")
    print(f"  with no geometry (omitted from the map): {missing}")
    print(f"\nrendered-line samples at 10 m spacing: {len(all_d)}")
    print(f"  median distance to a road centerline: {np.median(all_d):.2f} m")
    print(f"  p90: {np.percentile(all_d, 90):.2f} m")
    print(f"  p99: {np.percentile(all_d, 99):.2f} m")
    print(f"  max: {all_d.max():.2f} m")
    print(f"  share farther than {OFF_ROAD_M:.0f} m: {100 * (all_d > OFF_ROAD_M).mean():.3f}%")
    print(f"  pairs with any sample > {OFF_ROAD_M:.0f} m: {bad_pairs}")
    print(f"\ndrawn links: road={link_kind['road']} tile-stitch={link_kind['stitch']} "
          f"not-road-data={link_kind['foreign']}")
    print(f"pairs drawn as a bare station-to-station chord: {chord_like}")

    if worst:
        print("\nworst pairs:")
        for a, b, d in sorted(worst, key=lambda r: r[2], reverse=True)[:15]:
            print(f"  {a}->{b}: {d:.1f} m")

    # Coverage of the demo routes.
    have = {(int(froms[i]), int(tos[i])) for i in range(n) if offs[i + 1] > offs[i]}
    print("\ndemo route coverage:")
    for route in ("241DOWN", "274AUP", "930DOWN"):
        row = con.execute("select route_id from routes where route_name = ? limit 1", [route]).fetchone()
        if not row:
            print(f"  {route}: not found")
            continue
        ids = [r[0] for r in con.execute(
            "select station_id from station_patterns where route_id = ? order by stop_sequence",
            [row[0]],
        ).fetchall()]
        pairs = list(zip(ids, ids[1:]))
        miss = [p for p in pairs if p not in have]
        print(f"  {route}: {len(pairs)} pairs, missing={len(miss)}" + (f" {miss}" if miss else ""))

    # Every pair that the map can break must be reported, never hidden.
    stats_path = ARTIFACTS / "route_pair_geometry.csv"
    if stats_path.exists():
        rows = list(csv.DictReader(stats_path.open()))
        ok = [r for r in rows if r["status"] == "ok"]
        ratios = np.asarray([float(r["ratio"]) for r in ok])
        print(f"\nbuilder stats: routed {len(ok)}/{len(rows)}; "
              f"ratio median {np.median(ratios):.2f} p99 {np.percentile(ratios,99):.2f} "
              f"max {ratios.max():.2f}")
        bridge_pairs = sum(1 for r in ok if int(r["bridge_hops"]) > 0)
        print(f"  pairs containing an undrawn bridge hop: {bridge_pairs}")

    # Missing pairs are allowed and reported; a point off the road data is not.
    failed = bad_pairs > 0 or link_kind["foreign"] > 0 or chord_like > 0
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
